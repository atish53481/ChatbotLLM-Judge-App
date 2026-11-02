"""The MCP connector: an MCP server (Model Context Protocol) as a chatbot target.

An MCP server has no chat: it exposes tools. The app plays the agent for it:
one call to the agent model picks the tool (and its arguments) for the user's
message, the client calls the server, and the tool's output is the reply.
Because the reply always reports what tool ran, every MCP target is an agent
target (the runner's `tools_path` feature is on), and the agent metrics and the
security metrics score the calls it made.

The agent model is the target config's `agent_model` (a model on the judge's
provider) when set, else the judge model itself; app.py passes the factory
and notes on every run when the judge also chose the tools.

Two transports, both JSON-RPC 2.0:

* stdio: config {"command", "args", "env"}: the server runs as a local
  subprocess (args as a list, never a shell; Windows: no console window), with
  only the system's basic environment plus the config's env (never the app's
  own keys), messages are newline-delimited JSON on stdin/stdout.
* streamable HTTP: config {"url", "headers"}: POST JSON-RPC with
  `Accept: application/json, text/event-stream`; the session id the server
  hands back (Mcp-Session-Id) is sent with the next requests and ended on close.

Nothing starts until the client is first used (building one only checks the
config); the handshake (initialize + notifications/initialized) runs once.
Every call has a timeout and checks `cancel`, so Stop ends a run at the next
server call. Errors are McpError with plain messages; env and header values
are never put in them (nor in the traffic log). close() ends the process (its
whole tree) or the HTTP session; a client dropped without close() is cleaned
up by a finalizer.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import weakref
from typing import Any
from urllib.parse import urlsplit

import requests

from backend import cancel, traffic, usage
from backend.judges.ask import ask_json
from backend.targets.base import ChatbotClient, ChatReply

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "llm-judge"
CLIENT_VERSION = "1.0"
TRANSPORTS = ("stdio", "http")
# The runner's "reports tool calls" feature (runner.target_features).
TOOLS_PATH = "tool_calls"
INIT_TIMEOUT_S = 60      # starting a server (npx may download it first) + initialize
TIMEOUT_S = 30           # tools/list and other protocol calls
CALL_TIMEOUT_S = 60      # one tools/call
CLOSE_WAIT_S = 2
# Characters kept of one tool output (a dump of a database is not a reply).
MAX_OUTPUT_CHARS = 10_000
MAX_MESSAGE_BYTES = 5_000_000   # one JSON-RPC message from the server
MAX_TOOLS = 60
MAX_TOOL_PAGES = 5
_STDERR_TAIL = 2000
_SOURCES_FALLBACKS = ("", "cited_links", "knowledge_base")

# Environment a stdio server inherits (as the MCP SDKs do): enough to start
# node / python / uv, never the app's own keys from .env.
_INHERITED_ENV = (
    ("APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT",
     "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES", "SYSTEMDRIVE", "SYSTEMROOT",
     "TEMP", "TMP", "USERNAME", "USERPROFILE", "WINDIR")
    if os.name == "nt" else
    ("HOME", "LANG", "LC_ALL", "LOGNAME", "PATH", "SHELL", "TERM", "TMPDIR", "USER")
)

_SELECT_PROMPT = """You are the agent for an MCP server (Model Context Protocol). A user sent a message.
Choose ONE of the server's tools to answer it, with arguments that follow the tool's input schema,
or answer directly when no tool is needed.

Tools:
{tools}
{history}
User message:
{message}

Return only JSON: {{"tool": "<tool name>", "arguments": {{...}}}} when a tool answers the message,
or {{"answer": "<your reply to the user>"}} when no tool is needed. Never invent a tool name."""


class McpError(RuntimeError):
    """The MCP server could not be reached, started or refused a call."""


class _SessionExpired(McpError):
    """An HTTP server forgot the session (404): start a new one."""


_EOF = object()
_OVERSIZE = object()


# --- Config -------------------------------------------------------------------

def _str_map(value, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"{name} must map names to string values")
    return value


def check_config(config: dict) -> dict:
    """The transport settings of an MCP target config; ValueError when unusable."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    transport = config.get("transport") or ("http" if config.get("url") else "stdio")
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of: {', '.join(TRANSPORTS)}")
    agent_model = config.get("agent_model", "")
    if agent_model is not None and not isinstance(agent_model, str):
        raise ValueError("agent_model must be a model name")
    if config.get("sources_fallback", "") not in _SOURCES_FALLBACKS:
        raise ValueError("sources_fallback must be one of: cited_links, knowledge_base (or empty)")
    if transport == "http":
        url = config.get("url")
        if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
            raise ValueError("an HTTP MCP server needs a url starting with http:// or https://")
        return {"transport": "http", "url": url.strip(), "headers": _str_map(config.get("headers"), "headers")}
    command = config.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("a stdio MCP server needs the command that starts it")
    args = config.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("args must be a list of strings (one per argument)")
    env = _str_map(config.get("env"), "env")
    if any("=" in k for k in env):
        raise ValueError("env names cannot contain '='")
    return {"transport": "stdio", "command": command.strip(), "args": list(args), "env": env}


def secret_values(config: dict) -> list[str]:
    """Env and header values: never shown in an error or a log."""
    values = list((config.get("env") or {}).values()) + list((config.get("headers") or {}).values())
    values = [str(v) for v in values if isinstance(v, str) and len(v) >= 4]
    for value in list(values):  # "Bearer abc...": the token alone too
        values += [part for part in value.split() if len(part) >= 4]
    return sorted(set(values), key=len, reverse=True)


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _request(method: str, params: dict | None, request_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _matching(message: Any, request_id: int) -> dict | None:
    """The response to request_id in a message (or a batch of them)."""
    for item in message if isinstance(message, list) else [message]:
        if isinstance(item, dict) and item.get("id") == request_id and "method" not in item:
            return item
    return None


def _error_message(message: Any) -> str | None:
    error = message.get("error") if isinstance(message, dict) else None
    if error is None:
        return None
    if isinstance(error, dict):
        return str(error.get("message") or json.dumps(error, ensure_ascii=False, default=str))
    return str(error)


# --- stdio --------------------------------------------------------------------

def _pump_stdout(stream, out: queue.Queue) -> None:
    """Reads the server's JSON lines into `out` (no reference to the client, so
    a dropped client can be collected and its finalizer kill the process)."""
    try:
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_BYTES and not line.endswith(b"\n"):
                while True:  # drop the rest of an oversized message
                    more = stream.readline(MAX_MESSAGE_BYTES + 1)
                    if not more or more.endswith(b"\n"):
                        break
                out.put(_OVERSIZE)
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                out.put(json.loads(text))
            except ValueError:
                continue  # a log line some servers print on stdout
    except (OSError, ValueError):
        pass
    finally:
        out.put(_EOF)


def _pump_stderr(stream, tail: collections.deque) -> None:
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            tail.append(chunk)
    except (OSError, ValueError):
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """Ends a server and anything it started (npx -> node)."""
    if proc.poll() is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()  # well-behaved servers exit on end of input
        proc.wait(timeout=CLOSE_WAIT_S)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if os.name == "nt":
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _child_env(env: dict) -> dict:
    base = {}
    for name in _INHERITED_ENV:
        value = os.environ.get(name)
        if value is not None and not value.startswith("()"):
            base[name] = value
    return {**base, **env}


class _StdioTransport:
    """One MCP server process, spoken to over newline-delimited JSON lines."""

    def __init__(self, command: str, args: list[str], env: dict, secrets: list[str]):
        self._command, self._args, self._env, self._secrets = command, args, env, secrets
        self._proc: subprocess.Popen | None = None
        self._finalizer = None
        self._queue: queue.Queue = queue.Queue()
        self._stderr: collections.deque = collections.deque(maxlen=16)
        self._ended = False
        self._next_id = 0
        self._lock = threading.Lock()

    def _resolve(self) -> str:
        if os.path.dirname(self._command):
            return self._command  # a path: used as given
        found = shutil.which(self._command)  # honours PATHEXT (npx -> npx.cmd) on Windows
        if not found:
            raise McpError(f"Could not start the MCP server: the command {self._command!r} was not found. "
                           "Check its name, or give its full path.")
        return found

    def start(self) -> None:
        if self._proc is not None:
            return
        windows = os.name == "nt"
        try:
            proc = subprocess.Popen(
                [self._resolve(), *self._args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_child_env(self._env), bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
                start_new_session=not windows)
        except FileNotFoundError as e:
            raise McpError(f"Could not start the MCP server: {self._command!r} was not found.") from e
        except PermissionError as e:
            raise McpError(f"Could not start the MCP server: not allowed to run {self._command!r}.") from e
        except OSError as e:
            raise McpError(f"Could not start the MCP server ({type(e).__name__}: "
                           f"{_scrub(str(e), self._secrets)}).") from e
        self._proc, self._queue, self._ended = proc, queue.Queue(), False
        self._stderr = collections.deque(maxlen=16)
        self._finalizer = weakref.finalize(self, _kill_tree, proc)
        threading.Thread(target=_pump_stdout, args=(proc.stdout, self._queue), daemon=True,
                         name="mcp-stdout").start()
        self._stderr_thread = threading.Thread(target=_pump_stderr, args=(proc.stderr, self._stderr),
                                               daemon=True, name="mcp-stderr")
        self._stderr_thread.start()

    def _stopped(self) -> McpError:
        proc = self._proc
        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                code = proc.poll()
            if code is not None:
                self._stderr_thread.join(timeout=1)  # its last words, when it has exited
        tail = b"".join(self._stderr)[-_STDERR_TAIL:].decode("utf-8", errors="replace")
        tail = _clip(_scrub(tail, self._secrets))
        return McpError(f"The MCP server stopped{f' (exit code {code})' if code is not None else ''}"
                        f"{f': {tail}' if tail else ''}.")

    def _write(self, message: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise self._stopped() from e

    def call(self, method: str, params: dict | None, timeout: float) -> dict:
        with self._lock:  # one request at a time: answers are read in order
            self.start()
            if self._ended or self._proc.poll() is not None:
                raise self._stopped()
            self._next_id += 1
            request_id = self._next_id
            self._write(_request(method, params, request_id))
            deadline = time.monotonic() + timeout
            while True:
                cancel.check()
                left = deadline - time.monotonic()
                if left <= 0:
                    raise McpError(f"The MCP server did not answer {method} within {timeout:g} s.")
                try:
                    message = self._queue.get(timeout=min(0.25, left))
                except queue.Empty:
                    continue
                if message is _EOF:
                    self._ended = True
                    raise self._stopped()
                if message is _OVERSIZE:
                    raise McpError(f"The MCP server's answer to {method} was over "
                                   f"{MAX_MESSAGE_BYTES // 1_000_000} MB, so it was not read.")
                found = _matching(message, request_id)
                if found is not None:
                    return found
                if isinstance(message, dict) and "method" in message and "id" in message:
                    # The server asks something (ping): answer, as the protocol expects.
                    reply = ({"result": {}} if message["method"] == "ping"
                             else {"error": {"code": -32601, "message": "Method not found"}})
                    self._write({"jsonrpc": "2.0", "id": message["id"], **reply})
                # notifications and late answers to earlier requests are dropped

    def notify(self, method: str) -> None:
        with self._lock:
            try:
                self._write({"jsonrpc": "2.0", "method": method})
            except McpError:
                pass  # best effort; the next call reports a stopped server

    def close(self) -> None:
        # No lock: a call waiting on the server ends as soon as the process does.
        finalizer, self._finalizer = self._finalizer, None
        if finalizer is not None:
            finalizer()  # runs _kill_tree once
        self._proc = None  # a later call starts a new process


# --- streamable HTTP ----------------------------------------------------------

def _read_capped(response: requests.Response) -> bytes:
    body = b""
    for chunk in response.iter_content(65536):
        body += chunk
        if len(body) > MAX_MESSAGE_BYTES:
            raise McpError(f"The MCP server's answer was over {MAX_MESSAGE_BYTES // 1_000_000} MB, "
                           "so it was not read.")
    return body


class _HttpTransport:
    """A streamable-HTTP MCP server: POST JSON-RPC, JSON or SSE answer."""

    def __init__(self, url: str, headers: dict, secrets: list[str]):
        self._url = url
        self._host = urlsplit(url).netloc
        self._headers = dict(headers)
        self._secrets = secrets
        self._session: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        pass

    def reset_session(self) -> None:
        self._session, self.protocol_version = None, None

    def _request_headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", **self._headers}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, payload: dict, timeout: float, what: str) -> requests.Response:
        try:
            return requests.post(self._url, json=payload, headers=self._request_headers(),
                                 timeout=(min(10, timeout), timeout), stream=True)
        except requests.Timeout as e:
            raise McpError(f"The MCP server at {self._host} did not answer {what} within {timeout:g} s.") from e
        except requests.ConnectionError as e:
            raise McpError(f"Could not reach the MCP server at {self._host}: check the URL and that the "
                           "server is running.") from e
        except requests.RequestException as e:
            raise McpError(f"Could not reach the MCP server at {self._host} ({type(e).__name__}).") from e

    def _refused(self, response: requests.Response, method: str) -> McpError:
        status = response.status_code
        if status in (401, 403):
            return McpError(f"The MCP server at {self._host} refused the request (HTTP {status}): check "
                            "its headers (API key or token).")
        if status in (404, 405) and method == "initialize":
            return McpError(f"The MCP server did not answer at this URL (HTTP {status}): this app speaks "
                            "streamable HTTP, which servers serve on one endpoint (e.g. "
                            "http://localhost:3000/mcp); the older HTTP+SSE transport is not supported.")
        try:
            text = _read_capped(response).decode("utf-8", errors="replace")
        except McpError:
            text = ""
        text = _clip(_scrub(text, self._secrets), 200)
        return McpError(f"The MCP server refused {method} (HTTP {status}{f': {text}' if text else ''}).")

    def _read_sse(self, response: requests.Response, request_id: int, deadline: float, method: str) -> dict:
        data: list[str] = []
        size = 0
        for raw in response.iter_lines(decode_unicode=False):
            cancel.check()
            if time.monotonic() > deadline:
                raise McpError(f"The MCP server did not answer {method} in time.")
            size += len(raw or b"")
            if size > MAX_MESSAGE_BYTES:
                raise McpError(f"The MCP server's answer was over {MAX_MESSAGE_BYTES // 1_000_000} MB, "
                               "so it was not read.")
            line = (raw or b"").decode("utf-8", errors="replace")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line or not data:
                continue
            payload, data = "\n".join(data), []
            try:
                found = _matching(json.loads(payload), request_id)
            except ValueError:
                continue
            if found is not None:
                return found
        if data:
            try:
                found = _matching(json.loads("\n".join(data)), request_id)
            except ValueError:
                found = None
            if found is not None:
                return found
        raise McpError(f"The MCP server's stream for {method} ended without an answer.")

    def call(self, method: str, params: dict | None, timeout: float) -> dict:
        with self._lock:
            cancel.check()
            self._next_id += 1
            request_id = self._next_id
            deadline = time.monotonic() + timeout
            response = self._post(_request(method, params, request_id), timeout, method)
            try:
                if response.status_code == 404 and self._session and method != "initialize":
                    raise _SessionExpired("The MCP server ended the session.")
                if not 200 <= response.status_code < 300:
                    raise self._refused(response, method)
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session = session
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    return self._read_sse(response, request_id, deadline, method)
                body = _read_capped(response)
                try:
                    found = _matching(json.loads(body.decode("utf-8", errors="replace")), request_id)
                except ValueError as e:
                    raise McpError(f"The MCP server's answer to {method} was not JSON.") from e
                if found is None:
                    raise McpError(f"The MCP server's answer to {method} was not a JSON-RPC response.")
                return found
            finally:
                response.close()

    def notify(self, method: str) -> None:
        with self._lock:
            try:
                self._post({"jsonrpc": "2.0", "method": method}, TIMEOUT_S, method).close()
            except McpError:
                pass  # best effort; some servers accept the handshake without it

    def close(self) -> None:
        if not self._session:
            return
        try:  # end the session, as the protocol asks of a client that is done
            requests.delete(self._url, headers=self._request_headers(), timeout=5).close()
        except requests.RequestException:
            pass
        self._session = None


# --- Results ------------------------------------------------------------------

def _content_text(result: Any) -> str:
    """The readable text of a tools/call result: its text parts, a note for
    other parts, else its structured content as JSON."""
    content = result.get("content") if isinstance(result, dict) else None
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str) and part["text"]:
            parts.append(part["text"])
        elif part.get("type") in ("image", "audio"):
            parts.append(f"[{part['type']}: {part.get('mimeType', 'unknown type')}]")
        elif part.get("type") in ("resource", "resource_link"):
            resource = part.get("resource") if isinstance(part.get("resource"), dict) else part
            text = resource.get("text") if isinstance(resource.get("text"), str) else ""
            parts.append(text or f"[resource: {resource.get('uri', '')}]")
    text = "\n".join(parts).strip()
    if text:
        return text
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if structured not in (None, "", {}, []):
        return json.dumps(structured, ensure_ascii=False, default=str)
    return ""


class McpTargetClient(ChatbotClient):
    """An MCP server scored like a chatbot: the agent model picks the tool, the
    server runs it, the tool output is the reply (with the call reported for
    the agent and security metrics)."""

    mode = "mcp"

    def __init__(self, config: dict, judge_factory=None):
        settings = check_config(config)
        self.config = dict(config)
        self.transport = settings["transport"]
        self.tools_path = TOOLS_PATH  # an MCP target always reports its tool calls
        self.context_path = ""
        self.sources_fallback = config.get("sources_fallback") or ""
        self.max_message_length = None
        self.send_delay = 0
        self.discovered = None
        self.agent_model = (config.get("agent_model") or "").strip()
        # Set by app.py when the judge model also plays the agent (shown on every run).
        self.run_note: str | None = None
        self.server_info: dict = {}
        self.server_instructions = ""
        self._judge_factory = judge_factory
        self._agent = None
        self._tools: list[dict] | None = None
        self._ready = False
        self._lock = threading.RLock()
        self._secrets = secret_values(config)
        if self.transport == "http":
            self._transport = _HttpTransport(settings["url"], settings["headers"], self._secrets)
            self._label = urlsplit(settings["url"]).netloc
        else:
            self._transport = _StdioTransport(settings["command"], settings["args"], settings["env"],
                                              self._secrets)
            self._label = os.path.basename(settings["command"])

    # -- protocol --

    def _raw(self, method: str, params: dict | None, timeout: float) -> Any:
        cancel.check()
        try:
            message = self._transport.call(method, params, timeout)
        except McpError as e:
            raise type(e)(_scrub(str(e), self._secrets)) from e
        error = _error_message(message)
        if error:
            raise McpError(f"The MCP server refused {method}: {_clip(_scrub(error, self._secrets))}")
        return message.get("result") if isinstance(message, dict) else None

    def connect(self) -> None:
        """Starts the server (stdio) and runs the handshake, once."""
        with self._lock:
            if self._ready:
                return
            try:
                result = self._raw("initialize", {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}}, INIT_TIMEOUT_S)
            except cancel.RunCancelled:
                raise
            except Exception:
                self._transport.close()  # a server that cannot start is not left running
                raise
            result = result if isinstance(result, dict) else {}
            info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
            self.server_info = {"name": str(info.get("name") or ""), "version": str(info.get("version") or "")}
            self.server_instructions = str(result.get("instructions") or "").strip()
            if isinstance(self._transport, _HttpTransport):
                self._transport.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            self._transport.notify("notifications/initialized")
            self._ready = True

    def _rpc(self, method: str, params: dict | None = None, timeout: float = TIMEOUT_S) -> Any:
        self.connect()
        try:
            return self._raw(method, params, timeout)
        except _SessionExpired:
            with self._lock:  # the HTTP server forgot us: a new session, once
                self._ready = False
                self._transport.reset_session()
            self.connect()
            return self._raw(method, params, timeout)

    def list_tools(self, refresh: bool = False) -> list[dict]:
        """[{"name", "description", "input_schema"}], cached."""
        with self._lock:
            if self._tools is not None and not refresh:
                return self._tools
            tools: list[dict] = []
            cursor = None
            for _page in range(MAX_TOOL_PAGES):
                result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
                result = result if isinstance(result, dict) else {}
                for tool in result.get("tools") if isinstance(result.get("tools"), list) else []:
                    if isinstance(tool, dict) and str(tool.get("name", "")).strip():
                        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
                        tools.append({"name": str(tool["name"]).strip(),
                                      "description": str(tool.get("description") or "").strip(),
                                      "input_schema": schema if isinstance(schema, dict) else {}})
                cursor = result.get("nextCursor")
                if not cursor or len(tools) >= MAX_TOOLS:
                    break
            self._tools = tools[:MAX_TOOLS]
            return self._tools

    def describe(self) -> list[tuple[str, str]]:
        """What the server says about itself, as interview answers
        (autogen.write_dataset reads them like a chatbot's own)."""
        tools = self.list_tools()
        name = self.server_info.get("name") or "an MCP server"
        version = self.server_info.get("version")
        about = f"I am the MCP server '{name}'{f' (version {version})' if version else ''}."
        if self.server_instructions:
            about += f" {self.server_instructions}"
        answers = [("What are you, and what can you help me with?", about)]
        if tools:
            answers.append(("Which tools can you use?", "\n".join(
                f"- {t['name']}: {t['description'] or '(no description)'}" for t in tools)))
        return answers

    # -- the agent --

    def _agent_model(self):
        with self._lock:
            if self._agent is None:
                try:
                    if self._judge_factory is None:
                        from backend.judges.judge import build_judge  # lazy: no key needed for tools/list

                        self._agent = build_judge(**({"model": self.agent_model} if self.agent_model else {}))
                    else:
                        self._agent = self._judge_factory()
                except Exception as e:  # noqa: BLE001 - reported as the reason nothing was called
                    raise McpError(f"No model to choose the MCP server's tools: {e}") from e
            return self._agent

    def _pick_tool(self, message: str, history: list[dict] | None):
        """(tool name or "", arguments, direct answer) from the agent model."""
        agent = self._agent_model()
        tools = "\n".join(
            f"- {t['name']}: {_clip(t['description'] or '(no description)', 300)}"
            f"  input schema: {json.dumps(t['input_schema'], ensure_ascii=False)[:400]}"
            for t in self.list_tools()) or "(the server lists no tools)"
        earlier = [t for t in history or [] if isinstance(t, dict) and t.get("content")][-6:]
        history_text = ("\nConversation so far:\n" + "\n".join(
            f"{t.get('role', 'user')}: {_clip(t['content'], 500)}" for t in earlier) + "\n") if earlier else ""
        data = ask_json(agent, _SELECT_PROMPT.format(tools=tools, history=history_text, message=message[:4000]))
        tool = str(data.get("tool") or "").strip()
        if tool:
            args = data.get("arguments")
            return tool, args if isinstance(args, dict) else {}, ""
        return "", {}, str(data.get("answer") or "").strip()

    def chat(self, message: str, history: list[dict] | None = None) -> ChatReply:
        traffic.log("-> mcp %s%s: %s", self._label, f" (+{len(history)} earlier turns)" if history else "",
                    traffic.clip(message))
        started = time.monotonic()
        tool, arguments, answer = self._pick_tool(message, history)
        model = f"mcp:{self.server_info.get('name') or self._label}"
        if not tool:
            traffic.log("<- mcp agent answered without a tool: %s", traffic.clip(answer))
            return ChatReply(reply=answer or "(no answer)", model=model, mode=self.mode)
        if tool not in {t["name"] for t in self.list_tools()}:
            reply = f"The agent chose a tool the MCP server does not have: {tool}."
            traffic.log("x  mcp agent chose unknown tool %s", tool)
            return ChatReply(reply=reply, model=model, mode=self.mode, tools_called=[
                {"name": tool, "input_parameters": arguments, "output": "error: no such tool"}])
        try:
            result = self._rpc("tools/call", {"name": tool, "arguments": arguments}, CALL_TIMEOUT_S)
        except McpError as e:
            traffic.log("x  mcp %s tool %s failed after %.1fs: %s", self._label, tool,
                        time.monotonic() - started, e)
            raise
        usage.record_target()
        output = _content_text(result)
        failed = bool(isinstance(result, dict) and result.get("isError"))
        reply = (output or ("The tool failed and said nothing." if failed else "The tool ran and returned nothing."))
        reply = reply[:MAX_OUTPUT_CHARS]
        traffic.log("<- mcp %s tool %s in %.1fs: %s", self._label, tool, time.monotonic() - started,
                    traffic.clip(reply))
        return ChatReply(reply=reply, model=model, mode=self.mode, tools_called=[
            {"name": tool, "input_parameters": arguments, "output": output[:2000]}])

    def close(self) -> None:
        self._ready = False
        self._transport.close()
