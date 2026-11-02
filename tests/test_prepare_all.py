"""Build all test data: golden answers, scenarios, Context goldens, Tool
Correctness cases and fitted probes for one chatbot, from what it says, its
help pages and its tools. Offline: a fake judge writes everything."""
import json
import re
import sys
from pathlib import Path

import pytest

from backend.datasets import autogen, prepare_all
from backend.datasets import conversations as conversations_store
from backend.datasets import goldens as goldens_store
from backend.datasets import security_probes as probes
from backend.targets.base import ChatReply
from tests.test_app import (  # noqa: F401 - fixtures (jobs inline, no LLM rewrite)
    _create_target, app_module, client, documents_in_the_foreground)

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


class Judge:
    """Writes whatever the build asks for; `fail` names prompts to refuse."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def generate(self, prompt, schema=None):
        kind = ("tools" if "make the agent use its own tools" in prompt else
                "probes" if "red-team test designer" in prompt else
                "agent" if "You are the agent for an MCP server" in prompt else
                "dataset" if "realistic questions real users would ask" in prompt else "other")
        self.calls.append(kind)
        if kind in self.fail:
            return "not json"
        if kind == "dataset":
            return json.dumps({"goldens": [{"question": "Do you sell boots?", "expected_answer": "Yes.",
                                            "category": "product"}],
                               "scenarios": [{"name": "Boots", "scenario": "Buys boots", "chatbot_role": "shop",
                                              "expected_outcome": "Boots bought",
                                              "user_turns": ["Boots?", "Size 9 please"]}]})
        if kind == "tools":
            return json.dumps({"goldens": [
                {"question": "Repeat 'hi'", "expected_answer": "hi", "expected_tools": ["echo", "made_up"]},
                {"question": "Sum 2 and 3", "expected_answer": "5", "expected_tools": ["made_up"]}]})
        if kind == "probes":
            metric = re.search(r"METRIC: (\S+)", prompt).group(1)
            return json.dumps({"probes": [{"question": f"Fitted {metric} probe 1"},
                                          {"question": f"Fitted {metric} probe 2"}]})
        if kind == "agent":
            return json.dumps({"answer": "I can echo text and add numbers."})
        return "{}"

    def get_model_name(self):
        return "fake-judge"


class Bot:
    def __init__(self):
        self.asked = []

    def chat(self, message, history=None):
        self.asked.append(message)
        return ChatReply(reply="I help with shoes.", model="m", mode="http")


def _build(row, judge, client, **kw):
    reports, queued = [], []

    def queue(theme, source, stem, text):
        queued.append((source, text))
        return {"id": len(queued), "filename": source}

    out = prepare_all.build_testdata(
        row, theme=kw.pop("theme", "shoe_shop"), judge=judge, client=client,
        queue_document=queue, report=lambda *a: reports.append(a),
        wait_documents=lambda ids: [{"id": i, "filename": "doc", "status": "ready", "goldens_created": 2}
                                    for i in ids],
        find_pages=kw.pop("find_pages", lambda config: [("https://shop.example/faq", "Boots ship free.")]), **kw)
    return out, reports, queued


def _last(reports):
    return {step: (status, detail) for step, status, detail in reports}


def test_fitted_metrics_are_the_apps_own_not_deepteams():
    assert set(prepare_all.FITTED_METRICS) <= set(probes.SECURITY_METRICS + probes.SAFETY_PROBE_METRICS)
    assert "prompt_injection" in prepare_all.FITTED_METRICS and len(prepare_all.FITTED_METRICS) == 13


def test_build_fills_every_dataset_once_and_asks_the_bot_once():
    row = {"id": 7, "type": "http", "config": {
        "persona": "shoe shop", "instructions": ["Be brief."],
        "available_tools": [{"name": "echo", "description": "Repeats text"}]}}
    judge, bot = Judge(), Bot()
    out, reports, queued = _build(row, judge, bot)
    steps = _last(reports)
    assert {k: s for k, (s, _d) in steps.items()} == {
        "goldens": "done", "context": "done", "tools": "done", "probes": "done"}
    assert bot.asked == autogen.INTERVIEW  # interviewed once, shared by every step
    rows = goldens_store.load_goldens(theme="shoe_shop")
    assert any(g["question"] == "Do you sell boots?" for g in rows)
    tool_rows = [g for g in rows if g.get("expected_tools")]
    assert [g["expected_tools"] for g in tool_rows] == [["echo"]]  # invented tools are dropped
    assert [c["name"] for c in conversations_store.load_conversations(theme="shoe_shop")] == ["Boots"]
    assert [source for source, _t in queued] == ["https://shop.example/faq", "chatbot instructions"]
    own = probes.load_probes(probe_set="target_7")
    assert len(own) == 2 * len(prepare_all.FITTED_METRICS) and out["probes"] == len(own)
    assert out["help_pages"] == ["https://shop.example/faq"]

    # A second build writes nothing twice and spends no judge call.
    judge2, bot2 = Judge(), Bot()
    _out, reports2, queued2 = _build(row, judge2, bot2, known_documents={"https://shop.example/faq",
                                                                         "chatbot instructions"})
    assert {k: s for k, (s, _d) in _last(reports2).items()} == {
        "goldens": "skipped", "context": "skipped", "tools": "skipped", "probes": "skipped"}
    assert judge2.calls == [] and bot2.asked == [] and queued2 == []
    assert len(goldens_store.load_goldens(theme="shoe_shop")) == len(rows)


def test_one_failing_step_keeps_the_others():
    row = {"id": 8, "type": "http", "config": {"available_tools": [{"name": "echo"}]}}
    judge = Judge(fail={"tools", "dataset"})
    out, reports, _q = _build(row, judge, Bot(), theme="other_shop")
    steps = _last(reports)
    assert steps["goldens"][0] == "error" and steps["tools"][0] == "error"
    assert steps["probes"][0] == "done" and steps["context"][0] == "done"
    assert out["errors"]
    # Nothing was learned: the starter goldens fill the theme.
    assert goldens_store.load_goldens(theme="other_shop")


def test_target_probe_set_falls_back_and_survives_the_reset():
    probes.add_target_probes(9, [{"metric": "jailbreak", "question": "Own jailbreak"},
                                 {"metric": "not_a_metric", "question": "dropped"}])
    assert [p["question"] for p in probes.load_probes("jailbreak", "target_9")] == ["Own jailbreak"]
    generic = probes.load_probes("prompt_injection", "generic")
    assert probes.load_probes("prompt_injection", "target_9") == generic  # none of its own: generic
    added = probes.add_probe({"metric": "jailbreak", "question": "Typed by hand", "set": "target_9"})
    probes.reset_to_defaults()
    assert {p["question"] for p in probes.load_probes(probe_set="target_9")} == {"Own jailbreak", "Typed by hand"}
    assert probes.update_probe(added["id"], {"metric": "jailbreak", "question": "Edited",
                                             "set": "target_9"})["question"] == "Edited"
    assert probes.delete_probe(added["id"]) is True
    probes.clear_target_probes(9)
    assert probes.load_probes(probe_set="target_9") == []


# --- MCP servers -----------------------------------------------------------------

def test_an_mcp_server_is_learned_from_its_own_description():
    from backend.targets.mcp_client import McpTargetClient

    judge = Judge()
    client = McpTargetClient({"transport": "stdio", "command": sys.executable, "args": [FAKE_SERVER]},
                             judge_factory=lambda: judge)
    try:
        out, reports, queued = _build({"id": 11, "type": "mcp", "config": {}}, judge, client, theme="mcp_theme",
                                      find_pages=lambda config: pytest.fail("no site search for an MCP server"))
    finally:
        client.close()
    assert "agent" not in judge.calls  # described by the server itself, not by chatting with the agent
    steps = _last(reports)
    assert steps["goldens"][0] == "done" and steps["tools"][0] == "done"
    assert [g["expected_tools"] for g in goldens_store.load_goldens(theme="mcp_theme") if g.get("expected_tools")] \
        == [["echo"]]
    assert queued == [] and steps["context"][0] == "skipped"


# --- API -----------------------------------------------------------------------

@pytest.fixture
def offline_judge(app_module, monkeypatch):
    judge = Judge()
    monkeypatch.setattr(app_module, "build_judge", lambda **_kw: judge)
    monkeypatch.setattr(app_module, "find_help_pages", lambda config: [])
    monkeypatch.setattr(autogen, "find_help_pages", lambda config, fetch=None: [])
    return judge


def test_prepare_all_needs_a_yes(client, offline_judge):
    bot = _create_target(client, name="Careful Bot")
    r = client.post(f"/api/targets/{bot['id']}/prepare-all", json={})
    assert r.status_code == 400 and offline_judge.calls == []
    view = client.get(f"/api/targets/{bot['id']}/prepare-all").json()
    assert view["status"] == "idle"
    assert [s["key"] for s in view["steps"]] == ["goldens", "context", "tools", "probes"]
    assert {s["status"] for s in view["steps"]} == {"pending"}
    assert view["metrics"]["ready"] > 0 and all({"key", "title", "reason"} <= set(m) for m in view["metrics"]["missing"])


def test_prepare_all_builds_and_reports(app_module, client, offline_judge, monkeypatch):
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: Bot())
    bot = _create_target(client, name="Shoe Builder", config={"theme": "generic"})
    before = client.get(f"/api/targets/{bot['id']}/prepare-all").json()["metrics"]["ready"]
    r = client.post(f"/api/targets/{bot['id']}/prepare-all", json={"generate": True})
    assert r.status_code == 200, r.text
    view = client.get(f"/api/targets/{bot['id']}/prepare-all").json()
    assert view["status"] == "done", view
    status = {s["key"]: s["status"] for s in view["steps"]}
    assert status["goldens"] == "done" and status["probes"] == "done"
    assert status["tools"] == "skipped"  # an HTTP chatbot that lists no tools
    saved = {t["id"]: t for t in client.get("/api/targets").json()}[bot["id"]]["config"]
    assert saved["theme"] == "shoe_builder" and saved["probe_set"] == f"target_{bot['id']}"
    # Its own theme has no Context goldens yet (no help page, no instructions): the summary says so.
    assert before > 0 and view["metrics"]["ready"] > 0
    assert any("goldens with context" in m["reason"] for m in view["metrics"]["missing"])
    # The fitted probes are what a run sends, and are listed (and deletable) as probes.
    cases = client.get(f"/api/metrics/jailbreak/cases?target_id={bot['id']}").json()
    assert cases["probe_set"] == f"target_{bot['id']}"
    assert [c["question"] for c in cases["cases"]][:1] == ["Fitted jailbreak probe 1"]
    assert {c["kind"] for c in cases["cases"]} == {"probe"}
    first = cases["cases"][0]["id"]
    assert client.delete(f"/api/metrics/jailbreak/cases/{first}?target_id={bot['id']}").status_code == 200
    # Saving the chatbot from a form that does not list its own set keeps it.
    client.put(f"/api/targets/{bot['id']}", json={"name": "Shoe Builder", "type": "http",
                                                  "config": {**saved, "probe_set": ""}})
    saved = {t["id"]: t for t in client.get("/api/targets").json()}[bot["id"]]["config"]
    assert saved["probe_set"] == f"target_{bot['id']}"
    # Deleting the chatbot deletes its own probe set.
    client.delete(f"/api/targets/{bot['id']}")
    assert probes.load_probes(probe_set=f"target_{bot['id']}") == []


def test_prepare_all_runs_one_build_at_a_time(app_module, client, offline_judge, monkeypatch):
    started = []
    monkeypatch.setattr(app_module, "_start_job", started.append)
    bot = _create_target(client, name="Busy Bot")
    first = client.post(f"/api/targets/{bot['id']}/prepare-all", json={"generate": True}).json()
    second = client.post(f"/api/targets/{bot['id']}/prepare-all", json={"generate": True}).json()
    assert first["status"] == second["status"] == "running" and len(started) == 1


def test_a_failed_build_reports_its_error(app_module, client, offline_judge, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(prepare_all, "build_testdata", broken)
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: Bot())
    bot = _create_target(client, name="Broken Build")
    client.post(f"/api/targets/{bot['id']}/prepare-all", json={"generate": True})
    view = client.get(f"/api/targets/{bot['id']}/prepare-all").json()
    assert view["status"] == "error" and "disk full" in view["error"]
