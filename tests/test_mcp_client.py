"""The MCP connector: an MCP server (Model Context Protocol) judged like a
chatbot. Offline: a tiny stdio server (tests/fake_mcp_server.py) and a local
streamable-HTTP server stand in for real ones; a fake agent picks the tools."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend import cancel, usage
from backend.targets import mcp_client
from backend.targets.mcp_client import McpError, McpTargetClient

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


def stdio_config(*flags, **extra):
    return {"transport": "stdio", "command": sys.executable, "args": [FAKE_SERVER, *flags], **extra}


class Agent:
    """Stands in for the agent model: answers every tool choice with one JSON."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def generate(self, prompt, schema=None):
        self.prompts.append(prompt)
        return json.dumps(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0])

    def get_model_name(self):
        return "fake-agent"


@pytest.fixture
def clients():
    made = []

    def make(config, agent=None):
        client = McpTargetClient(config, judge_factory=(lambda: agent) if agent else None)
        made.append(client)
        return client

    yield make
    for client in made:
        client.close()


# --- Config ---------------------------------------------------------------------

@pytest.mark.parametrize("config, message", [
    ({"transport": "stdio"}, "command"),
    ({"transport": "stdio", "command": "  "}, "command"),
    ({"transport": "stdio", "command": "srv", "args": "--port 1"}, "args"),
    ({"transport": "stdio", "command": "srv", "args": [1, 2]}, "args"),
    ({"transport": "stdio", "command": "srv", "env": ["A=1"]}, "env"),
    ({"transport": "stdio", "command": "srv", "env": {"A": 1}}, "env"),
    ({"transport": "http"}, "url"),
    ({"transport": "http", "url": "file:///etc/passwd"}, "url"),
    ({"transport": "http", "url": "http://localhost:1/mcp", "headers": {"X": 1}}, "headers"),
    ({"transport": "smoke-signals", "command": "srv"}, "transport"),
    ({"transport": "stdio", "command": "srv", "agent_model": 5}, "agent_model"),
])
def test_bad_configs_are_refused_before_anything_starts(config, message):
    with pytest.raises(ValueError, match=message):
        McpTargetClient(config)


def test_building_a_client_starts_no_process(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the MCP server must start only when it is first used")

    monkeypatch.setattr(mcp_client.subprocess, "Popen", refuse)
    client = McpTargetClient(stdio_config())
    assert client.tools_path and client.mode == "mcp"
    client.close()


# --- stdio ------------------------------------------------------------------------

def test_lists_tools_after_the_handshake(clients):
    client = clients(stdio_config())  # the fake refuses requests sent before initialize
    tools = client.list_tools()
    assert [t["name"] for t in tools] == ["echo", "add", "env", "fail", "big", "pongs"]
    assert tools[0]["description"] == "Repeats the given text."
    assert tools[0]["input_schema"]["required"] == ["text"]
    assert client.server_info == {"name": "fake-mcp", "version": "1.2.3"}
    assert "echo" in client.server_instructions


def test_reads_every_page_of_tools(clients):
    assert len(clients(stdio_config("--page")).list_tools()) == 6


def test_ignores_log_lines_and_answers_the_servers_ping(clients):
    client = clients(stdio_config("--chatty", "--ping"), Agent({"tool": "pongs", "arguments": {}}))
    assert len(client.list_tools()) == 6
    assert client.chat("How many pongs?").reply == "1"


def test_chat_runs_the_tool_the_agent_picks(clients):
    agent = Agent({"tool": "add", "arguments": {"a": 2, "b": 3}})
    client = clients(stdio_config(), agent)
    with usage.meter() as counts:
        reply = client.chat("What is 2 + 3?")
    assert reply.reply == "5" and reply.mode == "mcp"
    assert reply.tools_called == [{"name": "add", "input_parameters": {"a": 2, "b": 3}, "output": "5"}]
    assert counts["target_calls"] == 1  # one tool call to the server under test
    assert "add: Adds two numbers a and b." in agent.prompts[0] and "What is 2 + 3?" in agent.prompts[0]


def test_conversation_so_far_reaches_the_agent(clients):
    agent = Agent({"answer": "You asked about 2 + 3."})
    client = clients(stdio_config(), agent)
    history = [{"role": "user", "content": "What is 2 + 3?"}, {"role": "assistant", "content": "5"}]
    reply = client.chat("What did I ask?", history=history)
    assert reply.reply == "You asked about 2 + 3." and not reply.tools_called
    assert "What is 2 + 3?" in agent.prompts[0]


def test_a_tool_the_server_lacks_is_never_called(clients):
    client = clients(stdio_config(), Agent({"tool": "rm_rf", "arguments": {}}))
    with usage.meter() as counts:
        reply = client.chat("Delete everything")
    assert "rm_rf" in reply.reply and counts["target_calls"] == 0
    assert reply.tools_called[0]["name"] == "rm_rf"


def test_tool_errors_and_long_outputs(clients, monkeypatch):
    monkeypatch.setattr(mcp_client, "MAX_OUTPUT_CHARS", 1000)
    client = clients(stdio_config(), Agent({"tool": "fail", "arguments": {}}))
    assert "boom: the tool broke" in client.chat("Break it").reply
    client = clients(stdio_config(), Agent({"tool": "big", "arguments": {}}))
    assert len(client.chat("Say a lot").reply) == 1000


def test_server_gets_its_env_but_not_the_apps_secrets(clients, monkeypatch):
    monkeypatch.setenv("JUDGE_API_KEY", "sk-judge-secret")
    client = clients(stdio_config(env={"FAKE_TOKEN": "tok-123"}),
                     Agent({"tool": "env", "arguments": {"name": "FAKE_TOKEN"}},
                           {"tool": "env", "arguments": {"name": "JUDGE_API_KEY"}}))
    assert client.chat("Is the token set?").reply == "set"
    assert client.chat("Is the judge key set?").reply == "unset"


def test_unknown_command_is_a_plain_error(clients):
    client = clients({"transport": "stdio", "command": "no-such-mcp-server-xyz"})
    with pytest.raises(McpError, match="not found"):
        client.list_tools()


def test_a_server_that_dies_at_start_says_why_without_leaking_env(clients):
    client = clients(stdio_config("--exit-at-start", env={"FAKE_TOKEN": "tok-secret-9"}))
    with pytest.raises(McpError) as caught:
        client.list_tools()
    text = str(caught.value)
    assert "exit code 3" in text and "cannot start" in text
    assert "tok-secret-9" not in text and "***" in text


def test_a_silent_server_times_out(clients, monkeypatch):
    monkeypatch.setattr(mcp_client, "INIT_TIMEOUT_S", 1)
    client = clients(stdio_config("--silent"))
    with pytest.raises(McpError, match="did not answer"):
        client.list_tools()


def test_close_stops_the_server_process(clients):
    client = clients(stdio_config())
    client.list_tools()
    proc = client._transport._proc
    assert proc.poll() is None
    client.close()
    assert proc.poll() is not None


def test_stop_reaches_the_server_calls(clients):
    client = clients(stdio_config(), Agent({"tool": "echo", "arguments": {"text": "hi"}}))
    client.list_tools()
    with cancel.watching(lambda: True), pytest.raises(cancel.RunCancelled):
        client.chat("hi")


def test_no_agent_model_is_an_error_not_a_reply(clients):
    def no_judge():
        raise RuntimeError("JUDGE_API_KEY is not set.")

    client = McpTargetClient(stdio_config(), judge_factory=no_judge)
    try:
        with pytest.raises(McpError, match="JUDGE_API_KEY"):
            client.chat("hi")
    finally:
        client.close()


def test_describe_tells_the_server_and_its_tools(clients):
    answers = clients(stdio_config()).describe()
    text = "\n".join(a for _q, a in answers)
    assert "fake-mcp" in text and "echo: Repeats the given text." in text and "add to sum" in text


# --- streamable HTTP ----------------------------------------------------------------

class _Mcp(BaseHTTPRequestHandler):
    seen: list = []
    deleted: list = []

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        type(self).deleted.append(self.headers.get("Mcp-Session-Id"))
        self._send(200)

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((message.get("method"), dict(self.headers)))
        if self.headers.get("Authorization") != "Bearer good-token":
            return self._send(401, b'{"error": "bad token Bearer wrong-token"}')
        if "id" not in message:
            return self._send(202)
        rid, method = message["id"], message["method"]
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-mcp", "version": "0.9"}}}
            return self._send(200, json.dumps(body).encode(), headers={"Mcp-Session-Id": "sess-1"})
        if self.headers.get("Mcp-Session-Id") != "sess-1":
            return self._send(400, b"missing session")
        if method == "tools/list":
            body = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "lookup", "description": "Looks an order up.", "inputSchema": {"type": "object"}}]}}
            return self._send(200, json.dumps(body).encode())
        if method == "tools/call":  # answered as a server-sent-events stream
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
            body = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "Order 42 shipped."}]}}
            stream = f"event: message\ndata: {json.dumps(note)}\n\nevent: message\ndata: {json.dumps(body)}\n\n"
            return self._send(200, stream.encode(), content_type="text/event-stream")
        return self._send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"message": "nope"}}).encode())


@pytest.fixture
def http_mcp():
    _Mcp.seen, _Mcp.deleted = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Mcp)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()
    server.server_close()


def test_http_server_session_headers_and_sse_answers(clients, http_mcp):
    config = {"transport": "http", "url": http_mcp, "headers": {"Authorization": "Bearer good-token"}}
    client = clients(config, Agent({"tool": "lookup", "arguments": {"order": 42}}))
    assert [t["name"] for t in client.list_tools()] == ["lookup"]
    assert client.server_info == {"name": "http-mcp", "version": "0.9"}
    reply = client.chat("Where is order 42?")
    assert reply.reply == "Order 42 shipped." and reply.tools_called[0]["name"] == "lookup"
    methods = [m for m, _h in _Mcp.seen]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    later = dict(_Mcp.seen[-1][1])
    assert later["Mcp-Session-Id"] == "sess-1" and later["MCP-Protocol-Version"] == "2025-06-18"
    client.close()
    assert _Mcp.deleted == ["sess-1"]  # the session is ended on close


def test_http_refusal_is_plain_and_never_echoes_header_values(clients, http_mcp):
    config = {"transport": "http", "url": http_mcp, "headers": {"Authorization": "Bearer wrong-token"}}
    with pytest.raises(McpError) as caught:
        clients(config).list_tools()
    text = str(caught.value)
    assert "401" in text and "wrong-token" not in text


def test_http_server_that_is_down(clients):
    client = clients({"transport": "http", "url": "http://127.0.0.1:9/mcp"})
    with pytest.raises(McpError, match="Could not reach"):
        client.list_tools()
