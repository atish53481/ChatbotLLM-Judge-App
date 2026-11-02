"""MCP servers as targets through the API: saving (secrets masked), listing
their tools, the metrics that apply, the run note when the judge also picks
the tools, and closing the server after use. Offline: tests/fake_mcp_server.py."""
import json
import sys
from pathlib import Path

import pytest
from deepeval.models import DeepEvalBaseLLM

from backend import storage
from backend.dashboard import runner
from backend.targets import mcp_client
from tests.test_app import app_module, client  # noqa: F401 - fixtures

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")
STDIO = {"transport": "stdio", "command": sys.executable, "args": [FAKE_SERVER], "env": {"FAKE_TOKEN": "tok-secret-1"}}


def _create(client, config=None, name="Fake MCP"):
    r = client.post("/api/targets", json={"name": name, "type": "mcp", "config": config or STDIO})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_target_is_saved_masked_and_listed(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("saving must not start the server")

    monkeypatch.setattr(mcp_client.subprocess, "Popen", refuse)
    created = _create(client)
    assert created["type"] == "mcp" and created["config"]["env"] == {"FAKE_TOKEN": "***"}
    listing = client.get("/api/targets")
    assert "tok-secret-1" not in listing.text
    assert any(t["id"] == created["id"] and t["type"] == "mcp" for t in listing.json())


@pytest.mark.parametrize("config", [
    {"transport": "stdio"}, {"transport": "http"}, {"transport": "http", "url": "ftp://x"},
    {"transport": "stdio", "command": "x", "args": "a b"},
])
def test_bad_mcp_configs_are_400(client, config):
    r = client.post("/api/targets", json={"name": "Bad", "type": "mcp", "config": config})
    assert r.status_code == 400


def test_edit_keeps_stored_env_and_header_values(client, app_module):
    created = _create(client, {**STDIO, "env": {"FAKE_TOKEN": "tok-secret-1", "OTHER": "keep-me-2"}})
    shown = created["config"]
    # The form sends back what it was shown: masked values, or no env at all.
    client.put(f"/api/targets/{created['id']}", json={"name": "Fake MCP", "type": "mcp", "config": shown})
    assert storage.get_target(app_module._conn, created["id"])["config"]["env"] == {
        "FAKE_TOKEN": "tok-secret-1", "OTHER": "keep-me-2"}
    client.put(f"/api/targets/{created['id']}", json={
        "name": "Fake MCP", "type": "mcp", "config": {k: v for k, v in shown.items() if k != "env"}})
    assert storage.get_target(app_module._conn, created["id"])["config"]["env"]["OTHER"] == "keep-me-2"


def test_tools_of_an_unsaved_server(client):
    r = client.post("/api/targets/mcp/tools", json={"config": STDIO})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["server"]["name"] == "fake-mcp" and out["server"]["version"] == "1.2.3"
    assert out["tools"][0] == {"name": "echo", "description": "Repeats the given text.",
                               "input_schema": out["tools"][0]["input_schema"]}


def test_tools_errors_are_plain_400s_without_secrets(client):
    r = client.post("/api/targets/mcp/tools", json={"config": {**STDIO, "args": [FAKE_SERVER, "--exit-at-start"]}})
    assert r.status_code == 400 and "exit code 3" in r.json()["detail"]
    assert "tok-secret-1" not in r.text and "Traceback" not in r.text
    r = client.post("/api/targets/mcp/tools", json={"config": {"transport": "stdio", "command": "no-such-cmd-xyz"}})
    assert r.status_code == 400 and "not found" in r.json()["detail"]
    r = client.post("/api/targets/mcp/tools", json={"config": {"transport": "http"}})
    assert r.status_code == 400


def test_tools_of_a_saved_server_use_its_stored_env(client):
    created = _create(client)
    r = client.post("/api/targets/mcp/tools", json={"config": created["config"], "target_id": created["id"]})
    assert r.status_code == 200, r.text


def test_agent_metrics_apply_to_an_mcp_server(client):
    created = _create(client)
    metrics = {m["key"]: m for m in client.get(f"/api/metrics?target_id={created['id']}").json()}
    assert metrics["tool_correctness"]["unavailable"] is None
    assert metrics["jailbreak"]["reads_reported_tools"] is True


class _Judge(DeepEvalBaseLLM):
    """Picks the echo tool as the agent; scores every G-Eval case 0.9."""

    def __init__(self, name="fake-judge"):
        self.name = name
        super().__init__(model=name)

    def generate(self, prompt, schema=None):
        if "You are the agent for an MCP server" in prompt:
            return json.dumps({"tool": "echo", "arguments": {"text": "I refuse."}})
        data = {"score": 9, "reason": "Refused."}
        return json.dumps(data) if schema is None else schema(**{k: v for k, v in data.items()
                                                                  if k in schema.model_fields})

    async def a_generate(self, prompt, schema=None):
        return self.generate(prompt, schema)

    def get_model_name(self):
        return self.name

    def load_model(self, *a, **k):
        return None


def _run_jailbreak(app_module, client, target_id, monkeypatch, models):
    def fake_build_judge(**kw):
        models.append(kw.get("model"))
        return _Judge(kw.get("model") or "fake-judge")

    monkeypatch.setattr(app_module, "build_judge", fake_build_judge)
    monkeypatch.setattr(app_module, "_judge_overrides", lambda: {})
    job = client.post("/api/jobs", json={"target_id": target_id, "metric_keys": ["jailbreak"], "limit": 1}).json()
    while app_module.jobs.process_next():
        pass
    return app_module.jobs.get(job["job_id"])


def test_run_notes_the_judge_also_picked_the_tools_and_closes_the_server(app_module, client, monkeypatch):
    closed = []
    real_close = mcp_client.McpTargetClient.close
    monkeypatch.setattr(mcp_client.McpTargetClient, "close", lambda self: (closed.append(1), real_close(self)))
    created = _create(client)
    models = []
    job = _run_jailbreak(app_module, client, created["id"], monkeypatch, models)
    result = job["results"][0]
    assert result["status"] in ("pass", "fail"), result
    assert "The judge model also chose the MCP server's tools" in result["note"]
    assert result["rows"][0]["tools_called"][0]["name"] == "echo"
    assert closed  # the job's server process ended with the job


def test_an_agent_model_takes_the_judges_place(app_module, client, monkeypatch):
    created = _create(client, {**STDIO, "agent_model": "agent-model-x"})
    models = []
    job = _run_jailbreak(app_module, client, created["id"], monkeypatch, models)
    result = job["results"][0]
    assert "agent-model-x" in models
    assert "also chose the MCP server's tools" not in (result.get("note") or "")


def test_discovery_and_export_refuse_an_mcp_server(client):
    created = _create(client)
    assert client.post(f"/api/targets/{created['id']}/discover", json={"authorized": True}).status_code == 400
    assert client.get(f"/api/export/suite?target_id={created['id']}").status_code == 400


def test_restart_keeps_mcp_targets(tmp_path):
    path = str(tmp_path / "keep.db")
    conn = storage.init_db(path)
    kept = storage.add_target(conn, "Server", "mcp", {"transport": "stdio", "command": "srv"})
    gone = storage.add_target(conn, "Sample", "mock", {})
    conn.close()
    conn = storage.init_db(path)
    assert storage.get_target(conn, kept)["config"] == {"transport": "stdio", "command": "srv"}
    assert storage.get_target(conn, gone) is None
    conn.close()
