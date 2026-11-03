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
from backend.datasets.build_kit import Budget
from backend.targets.base import ChatReply
from tests.test_app import (  # noqa: F401 - fixtures (jobs inline, no LLM rewrite)
    _create_target, app_module, client, documents_in_the_foreground)

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


class Judge:
    """Writes whatever the build asks for; `fail` names prompts to refuse.
    Its dataset reply meets every minimum (goldens, short-answer goldens, scenarios)."""

    def __init__(self, fail=(), tool_rows=None, persona="A shoe shop assistant."):
        self.tool_rows = tool_rows
        self.persona = persona
        self.calls = []
        self.prompts = []
        self.fail = set(fail)

    def generate(self, prompt, schema=None):
        self.prompts.append(prompt)
        kind = ("tools" if "make the agent use its own tools" in prompt else
                "probes" if "red-team test designer" in prompt else
                "context" if "source passage the answer comes from" in prompt else
                "agent" if "You are the agent for an MCP server" in prompt else
                "dataset" if "realistic questions real users would ask" in prompt else
                "grounded" if "list of test rows" in prompt else
                "repair" if "These conversation scenarios" in prompt else
                "persona" if "describe in ONE sentence" in prompt else
                "tool_probe" if "ask it to DO something concrete" in prompt else "other")
        self.calls.append(kind)
        if kind in self.fail:
            return "not json"
        if kind == "dataset":
            general = [{"question": q, "expected_answer": "Explains it.", "category": "happy_path"}
                       for q in ("Do you sell boots?", "Can I return worn shoes?", "Which sizes do you stock?",
                                 "Do you ship abroad?", "How do I track my parcel?")]
            facts = [{"question": q, "expected_answer": a, "category": c}
                     for q, a, c in (("How long is the return window?", "30 days.", "fact"),
                                     ("What does express shipping cost?", "5 euros.", "Fact"),
                                     ("Which carrier delivers?", "DHL.", "fact"))]
            scenarios = [{"name": n, "scenario": "Buys shoes", "chatbot_role": "shop", "expected_outcome": "Bought",
                          "user_turns": [f"{n}?", "Size 9 please"]} for n in ("Boots", "Sandals", "Trainers")]
            return json.dumps({"goldens": general + facts, "scenarios": scenarios})
        if kind == "grounded":
            n = int(re.search(r"exactly (\d+) verdicts", prompt).group(1))
            return json.dumps({"verdicts": ["supported"] * n})
        if kind == "repair":
            ids = re.findall(r"\[(c_[0-9a-f]+)\]", prompt)
            return json.dumps({"outcomes": {i: "The user gets an answer." for i in ids},
                               "contexts": {i: ["Boots ship free."] for i in ids}})
        if kind == "persona":
            return json.dumps({"persona": self.persona})
        if kind == "tool_probe":
            return json.dumps({"messages": ["Where is order ORD-1001?", "Cancel ORD-1002"]})
        if kind == "tools" and self.tool_rows is not None:
            return json.dumps({"goldens": self.tool_rows})
        if kind == "tools":
            return json.dumps({"goldens": [
                {"question": "Repeat 'hi'", "expected_answer": "hi", "expected_tools": ["echo", "made_up"]},
                {"question": "Sum 2 and 3", "expected_answer": "5", "expected_tools": ["made_up"]},
                {"question": "Repeat 'bye' back to me", "expected_answer": "bye", "expected_tools": ["echo"]}]})
        if kind == "probes":
            metric = re.search(r"METRIC: (\S+)", prompt).group(1)
            return json.dumps({"probes": [{"question": f"Fitted {metric} probe 1"},
                                          {"question": f"Fitted {metric} probe 2"}]})
        if kind == "context":
            passage = " ".join(f"word{i}" for i in range(70))
            rows = [{"question": f"Context question {i}?", "expected_answer": "An answer.", "context": passage}
                    for i in range(prepare_all.CASES_MIN)]
            rows.append({"question": "Too short?", "expected_answer": "Yes.", "context": "only a few words"})
            return json.dumps({"goldens": rows})
        if kind == "agent":
            if "Repeat" in prompt.split("User message:")[-1]:
                return json.dumps({"tool": "echo", "arguments": {"text": "hi"}})
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


class ToolBot:
    """Calls `tool` when the message contains `calls_tool_on`."""

    def __init__(self, calls_tool_on, tool="echo"):
        self.trigger = calls_tool_on
        self.tool = tool
        self.asked = []

    def chat(self, message, history=None):
        self.asked.append(message)
        called = [{"name": self.tool}] if self.trigger in message else []
        return ChatReply(reply="ok", model="m", mode="http", tools_called=called)


class DeadBot:
    def __init__(self):
        self.asked = []

    def chat(self, message, history=None):
        self.asked.append(message)
        raise ConnectionError("refused")


def test_tool_goldens_kept_only_when_the_bot_calls_a_tool():
    from backend.datasets.profile import Profile
    bot = ToolBot(calls_tool_on="Repeat")
    judge = Judge(tool_rows=[{"question": "Repeat 'hi'", "expected_answer": "hi", "expected_tools": ["echo"]},
                             {"question": "Tell a joke", "expected_answer": "-", "expected_tools": ["echo"]}])
    added = prepare_all.write_tool_goldens(judge, "t1", [{"name": "echo"}], client=bot, profile=Profile())
    assert added == 1 and "Tell a joke" in bot.asked
    assert judge.calls.count("tools") == prepare_all.TOOL_ATTEMPTS  # second round for the remaining gap


def test_run_cancelled_propagates_from_tool_goldens():
    from backend.cancel import RunCancelled

    class Stopped:
        def chat(self, message, history=None):
            raise RunCancelled("stop")
    judge = Judge(tool_rows=[{"question": "Repeat 'hi'", "expected_answer": "hi", "expected_tools": ["echo"]}])
    with pytest.raises(RunCancelled):
        prepare_all.write_tool_goldens(judge, "t4", [{"name": "echo"}], client=Stopped())


def test_no_gap_no_judge_call():
    existing = [{"expected_tools": ["echo"]}] * prepare_all.TOOL_GOLDENS_PER_TOOL
    judge = Judge()
    assert prepare_all.write_tool_goldens(judge, "t2", [{"name": "echo"}], existing=existing) == 0
    assert judge.calls == []


def test_existing_goldens_naming_a_tool_in_extra_count():
    existing = [{"extra": {"expected_tools": ["echo"]}}] * prepare_all.TOOL_GOLDENS_PER_TOOL
    judge = Judge()
    assert prepare_all.write_tool_goldens(judge, "t3", [{"name": "echo"}], existing=existing) == 0
    assert judge.calls == []


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


def test_uploaded_documents_reach_the_judge():
    judge = Judge()
    _build({"id": 21, "type": "http", "config": {"persona": "p"}}, judge, Bot(), theme="docs_shop",
           documents=[("policy.pdf", "Returns accepted within 45 days.")])
    assert any("Returns accepted within 45 days." in p for p in judge.prompts)


def test_no_material_writes_unverified_context_goldens():
    judge = Judge()
    _out, reports, queued = _build({"id": 22, "type": "http", "config": {"persona": "p"}}, judge, Bot(),
                                   theme="bare", find_pages=lambda c: [])
    rows = [g for g in goldens_store.load_goldens(theme="bare") if g["source"] == "auto_unverified" and g["context"]]
    assert len(rows) == prepare_all.CASES_MIN
    assert all(len(" ".join(g["context"]).split()) >= 60 for g in rows)
    assert _last(reports)["context"][0] == "done" and queued == []
    assert "without a source document (unverified)" in _last(reports)["context"][1]
    # A second build finds enough Context goldens and spends no call on them.
    judge2 = Judge()
    _o, reports2, _q = _build({"id": 22, "type": "http", "config": {"persona": "p"}}, judge2, Bot(),
                              theme="bare", find_pages=lambda c: [])
    assert _last(reports2)["context"][0] == "skipped" and "context" not in judge2.calls


def _last(reports):
    return {step: (status, detail) for step, status, detail in reports}


def test_fitted_metrics_are_the_apps_own_not_deepteams():
    assert set(prepare_all.FITTED_METRICS) <= set(probes.SECURITY_METRICS + probes.SAFETY_PROBE_METRICS)
    assert "prompt_injection" in prepare_all.FITTED_METRICS and len(prepare_all.FITTED_METRICS) == 13


def test_build_fills_every_dataset_once_and_asks_the_bot_once():
    row = {"id": 7, "type": "http", "config": {
        "persona": "shoe shop", "instructions": ["Be brief."],
        "available_tools": [{"name": "echo", "description": "Repeats text"}]}}
    judge, bot = Judge(), ToolBot(calls_tool_on="Repeat")
    out, reports, queued = _build(row, judge, bot)
    steps = _last(reports)
    assert {k: s for k, (s, _d) in steps.items()} == {k: "done" for k, _label in prepare_all.STEPS}
    assert bot.asked[:len(autogen.INTERVIEW)] == autogen.INTERVIEW  # interviewed once, shared by every step
    assert bot.asked.count(autogen.INTERVIEW[0]) == 1
    rows = goldens_store.load_goldens(theme="shoe_shop")
    general = [g for g in rows if g["source"] == "auto_chatbot"]
    assert len(general) >= prepare_all.GOLDENS_MIN
    assert len([g for g in general if "fact" in [c.lower() for c in g["categories"]]]) >= \
        prepare_all.REFERENCE_GOLDENS_MIN
    tool_rows = [g for g in rows if g.get("expected_tools")]
    # Invented tools are dropped; each kept case made the bot call a tool.
    assert [g["expected_tools"] for g in tool_rows] == [["echo"]] * prepare_all.TOOL_GOLDENS_PER_TOOL
    scenarios = conversations_store.load_conversations(theme="shoe_shop")
    assert [c["name"] for c in scenarios] == ["Boots", "Sandals", "Trainers"]
    # An agent's new scenarios were tried once; no sources came back, so they got passages from the material.
    assert all(c["context"] == ["Boots ship free."] for c in scenarios)
    assert [source for source, _t in queued] == ["https://shop.example/faq", "chatbot instructions"]
    own = probes.load_probes(probe_set="target_7")
    assert len(own) == 2 * len(prepare_all.FITTED_METRICS) and out["probes"] == len(own)
    assert out["help_pages"] == ["https://shop.example/faq"]
    assert out["profile"]["kind"] == "agent" and out["learned"] == {"persona": "", "available_tools": []}

    # A second build writes nothing twice and spends no judge call; the bot is asked only the interview.
    judge2, bot2 = Judge(), ToolBot(calls_tool_on="Repeat")
    _out, reports2, queued2 = _build(row, judge2, bot2, known_documents={"https://shop.example/faq",
                                                                         "chatbot instructions"})
    assert {k: s for k, (s, _d) in _last(reports2).items()} == {
        "profile": "done", "goldens": "skipped", "scenarios": "skipped", "context": "skipped",
        "tools": "skipped", "probes": "skipped"}
    assert judge2.calls == [] and bot2.asked == autogen.INTERVIEW and queued2 == []
    assert len(goldens_store.load_goldens(theme="shoe_shop")) == len(rows)


def test_unreachable_bot_reports_and_finishes():
    out, reports, _ = _build({"id": 31, "type": "http", "config": {"persona": "p"}}, Judge(), DeadBot(), theme="dead")
    steps = _last(reports)
    assert set(steps) == {k for k, _ in prepare_all.STEPS}
    assert all(s in ("done", "skipped", "error") for s, _d in steps.values())


def test_budget_reached_skips_the_rest_and_keeps_data():
    budget = Budget(max_messages=len(autogen.INTERVIEW), max_judge_calls=2)
    _out, reports, _ = _build({"id": 32, "type": "http", "config": {"persona": "p"}}, Judge(), Bot(),
                              theme="tight", budget=budget)
    status, detail = _last(reports)["probes"]
    assert status == "skipped" and "Build budget reached" in detail
    assert goldens_store.load_goldens(theme="tight")


def test_budget_reached_by_the_judge_mid_build():
    budget = Budget(max_messages=30, max_judge_calls=2)  # the dataset and its grounding check, then nothing
    judge = Judge()
    _out, reports, _ = _build({"id": 35, "type": "http", "config": {"persona": "p"}}, judge, Bot(),
                              theme="tight_judge", budget=budget)
    steps = _last(reports)
    assert steps["goldens"][0] == "done" and len(judge.calls) == 2
    # Written by the same call and kept; their source passages needed a third call, over the cap.
    assert steps["scenarios"][0] == "skipped" and "Build budget reached" in steps["scenarios"][1]
    assert len(conversations_store.load_conversations(theme="tight_judge")) == prepare_all.SCENARIOS_MIN
    assert steps["probes"] == ("skipped", "Build budget reached (3 messages / 2 judge calls); "
                                          "press Build all test data to continue.")
    assert len(goldens_store.load_goldens(theme="tight_judge")) >= prepare_all.GOLDENS_MIN


def test_scenarios_kept_when_the_budget_ends_at_the_grounding_check():
    budget = Budget(max_messages=30, max_judge_calls=1)  # the dataset call only
    _out, reports, _ = _build({"id": 37, "type": "http", "config": {"persona": "p"}}, Judge(), Bot(),
                              theme="tight_grounding", budget=budget)
    steps = _last(reports)
    assert steps["goldens"][0] == "skipped" and "Build budget reached" in steps["goldens"][1]
    assert [c["name"] for c in conversations_store.load_conversations(theme="tight_grounding")] == \
        ["Boots", "Sandals", "Trainers"]
    assert not [g for g in goldens_store.load_goldens(theme="tight_grounding") if g["source"] == "auto_chatbot"]


def test_context_goldens_do_not_count_as_general_goldens():
    for i in range(prepare_all.GOLDENS_MIN):
        goldens_store.add_golden("ctx_only", f"Context question {i}?", "A.", ["a passage"], ["fact"],
                                 source="synthesized")
    judge = Judge()
    _out, reports, _ = _build({"id": 38, "type": "http", "config": {"persona": "p"}}, judge, Bot(), theme="ctx_only")
    assert _last(reports)["goldens"][0] == "done" and "dataset" in judge.calls


def test_stop_stops_the_build():
    from backend.cancel import RunCancelled

    class Stopping(Judge):
        def generate(self, prompt, schema=None):
            raise RunCancelled("stop")
    with pytest.raises(RunCancelled):
        _build({"id": 36, "type": "http", "config": {"persona": "p"}}, Stopping(), Bot(), theme="stopped")


def test_outcome_reports_learned_tools_and_profile():
    out, _r, _q = _build({"id": 33, "type": "http", "config": {"tools_path": "tools_called"}},
                         Judge(persona="Shop bot.", tool_rows=[
                             {"question": "Where is order ORD-7?", "expected_answer": "Its status.",
                              "expected_tools": ["lookup_order"]},
                             {"question": "Has ORD-8 shipped yet?", "expected_answer": "Its shipping date.",
                              "expected_tools": ["lookup_order"]}]),
                         ToolBot(calls_tool_on="ORD", tool="lookup_order"),
                         theme="agent_shop")
    assert out["profile"]["kind"] == "agent" and out["learned"]["persona"] == "Shop bot."
    assert [t["name"] for t in out["learned"]["available_tools"]] == ["lookup_order"]
    assert out["profile"]["tools_seen"] is True
    # The app saves what was learned; a second build then only asks the interview and spends no judge call.
    learned_config = {"tools_path": "tools_called", "persona": out["learned"]["persona"],
                      "available_tools": out["learned"]["available_tools"]}
    judge2, bot2 = Judge(), ToolBot(calls_tool_on="ORD", tool="lookup_order")
    out2, reports2, _q = _build({"id": 33, "type": "http", "config": learned_config}, judge2, bot2,
                                theme="agent_shop")
    assert judge2.calls == [] and bot2.asked == autogen.INTERVIEW, (judge2.calls, _last(reports2))
    assert out2["learned"] == {"persona": "", "available_tools": []}


def test_typed_scenario_fields_survive_repair():
    conversations_store.add_conversation({"theme": "keep", "name": "Mine", "scenario": "", "chatbot_role": "Typed role",
                                          "expected_outcome": "", "user_turns": ["a", "b"]})
    _build({"id": 34, "type": "http", "config": {"persona": "Shop bot."}}, Judge(), Bot(), theme="keep")
    mine = next(c for c in conversations_store.load_conversations(theme="keep") if c["name"] == "Mine")
    assert mine["chatbot_role"] == "Typed role" and mine["expected_outcome"]


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
    # Described by the server itself: the agent is never asked the interview questions.
    assert not any(q in p for q in autogen.INTERVIEW for p in judge.prompts)
    steps = _last(reports)
    assert steps["profile"][0] == "done" and out["profile"]["kind"] == "mcp"
    assert steps["goldens"][0] == "done" and steps["scenarios"][0] == "done" and steps["tools"][0] == "done"
    own = goldens_store.load_goldens(theme="mcp_theme")
    assert len([g for g in own if g["source"] == "auto_unverified" and not g["context"]]) >= prepare_all.GOLDENS_MIN
    assert len(conversations_store.load_conversations(theme="mcp_theme")) >= prepare_all.SCENARIOS_MIN
    assert [g["expected_tools"] for g in goldens_store.load_goldens(theme="mcp_theme") if g.get("expected_tools")] \
        == [["echo"]] * prepare_all.TOOL_GOLDENS_PER_TOOL  # only the cases the agent really ran a tool for
    # No help pages, instructions or documents: unverified Context goldens stand in.
    assert queued == [] and steps["context"][0] == "done"


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
    assert [s["key"] for s in view["steps"]] == [k for k, _label in prepare_all.STEPS]
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
    # No help page, instructions or documents: the judge wrote unverified Context goldens for its theme.
    assert before > 0 and view["metrics"]["ready"] > 0
    assert status["context"] == "done"
    own = goldens_store.load_goldens(theme="shoe_builder")
    assert len([g for g in own if g["source"] == "auto_unverified" and g["context"]]) == prepare_all.CASES_MIN
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


# --- what the build saves, leftovers, auto-build setting --------------------------

class AgentBot(ToolBot):
    """An agent that always calls lookup_order, and whose replies showed a context path."""

    def __init__(self):
        super().__init__("", tool="lookup_order")
        self.discovered = {"context_path": "sources"}


def _run_fake_build(app_module, client, monkeypatch, config, return_id=False, bot=None):
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: bot or AgentBot())
    target = _create_target(client, name="Order Agent", config=config)
    r = client.post(f"/api/targets/{target['id']}/prepare-all", json={"generate": True})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/targets/{target['id']}/prepare-all").json()["status"] == "done"
    if return_id:
        return target["id"]
    return {t["id"]: t for t in client.get("/api/targets").json()}[target["id"]]["config"]


def test_build_saves_found_paths_tools_and_persona_but_not_over_typed(app_module, client, offline_judge, monkeypatch):
    cfg = _run_fake_build(app_module, client, monkeypatch, config={"persona": "Typed", "tools_path": "tools_called"})
    assert cfg["persona"] == "Typed" and cfg["context_path"] == "sources"
    assert [t["name"] for t in cfg["available_tools"]] == ["lookup_order"] and cfg["auto_built"] is True
    assert cfg["profile"]["kind"] == "agent" and cfg["tools_path"] == "tools_called"


def test_build_saves_learned_persona_when_none_typed(app_module, client, offline_judge, monkeypatch):
    cfg = _run_fake_build(app_module, client, monkeypatch, config={"tools_path": "tools_called"})
    assert cfg["persona"]


def test_build_does_not_save_tools_for_an_mcp_server(app_module, monkeypatch):
    row = {"id": 1, "name": "m", "type": "mcp", "config": {}}
    outcome = {"learned": {"persona": "P", "available_tools": [{"name": "echo"}]},
               "profile": {"kind": "mcp"}, "help_pages": []}
    config = app_module._merge_learned(row, outcome)
    assert config["persona"] == "P" and "available_tools" not in config and config["auto_built"] is True


def test_leftovers_name_plan_metrics_without_reasoning(app_module, client, offline_judge, monkeypatch):
    tid = _run_fake_build(app_module, client, monkeypatch, config={"tools_path": "tools_called"}, return_id=True)
    view = client.get(f"/api/targets/{tid}/prepare-all").json()
    plan = {l["key"]: l for l in view["leftovers"]}["plan_quality"]
    assert "reasoning" in plan["hint"] and view["kind"] == "agent"


def test_leftovers_mark_metrics_that_do_not_apply(app_module):
    row = {"id": 1, "type": "http", "config": {"profile": {"kind": "chat"}}}
    left = {l["key"]: l for l in app_module._leftovers(row)}
    assert left["tool_correctness"] == {"key": "tool_correctness", "title": "Tool Correctness",
                                        "reason": "Does not apply to this target", "hint": ""}
    assert "contextual_precision" in left and "turn_contextual_recall" in left
    assert "task_completion" not in left or left["task_completion"]["reason"] != "Does not apply to this target"


def test_a_missing_tool_path_gets_its_hint(app_module):
    row = {"id": 1, "type": "http", "config": {"profile": {"kind": "agent"}}}
    left = {l["key"]: l for l in app_module._leftovers(row)}
    assert left["tool_correctness"]["hint"] == "Set 'Tool calls path', or run Find missing metric data."


def test_build_reads_ready_documents_text(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    (tmp_path / "t").mkdir()
    doc_id = app_module.storage.add_document(app_module._conn, "t", "faq.txt")
    app_module.storage.set_document_status(app_module._conn, doc_id, "ready")
    (tmp_path / "t" / f"{doc_id}_faq.txt").write_text("Returns take 30 days.", encoding="utf-8")
    other = app_module.storage.add_document(app_module._conn, "t", "gone.txt")
    app_module.storage.set_document_status(app_module._conn, other, "ready")  # no file: skipped
    assert app_module._ready_documents("t") == [("faq.txt", "Returns take 30 days.")]


def test_build_settings_flag(client, monkeypatch):
    monkeypatch.delenv("JUDGE_AUTO_BUILD", raising=False)
    assert client.get("/api/build/settings").json() == {"auto_build": True}
    monkeypatch.setenv("JUDGE_AUTO_BUILD", "0")
    assert client.get("/api/build/settings").json() == {"auto_build": False}


def test_editing_a_scenario_keeps_its_context(client):
    row = client.post("/api/conversations", json={
        "theme": "t", "name": "n", "user_turns": ["hi"], "context": ["Boots ship free."]}).json()
    assert row["context"] == ["Boots ship free."]
    r = client.put(f"/api/conversations/{row['id']}", json={"theme": "t", "name": "n2", "user_turns": ["hi"]})
    assert r.json()["context"] == ["Boots ship free."] and r.json()["name"] == "n2"
    r = client.put(f"/api/conversations/{row['id']}", json={"theme": "t", "name": "n2", "user_turns": ["hi"],
                                                            "context": ["Other."]})
    assert r.json()["context"] == ["Other."]


# --- final review fixes ------------------------------------------------------------

class FindsPathsBot:
    """An HTTP-like bot whose first reply shows where its tool calls are
    (`discovered`); its replies carry the calls only once that path is set."""

    def __init__(self):
        self.tools_path = ""
        self.context_path = ""
        self.discovered = {}
        self.asked = []

    def chat(self, message, history=None):
        self.asked.append(message)
        if not self.tools_path:
            self.discovered.setdefault("tools_path", "tool_calls")
            return ChatReply(reply="I look up orders.", model="m", mode="http")
        called = [{"name": "lookup_order"}] if "ORD" in message else []
        return ChatReply(reply="ok", model="m", mode="http", tools_called=called)


def test_paths_found_in_replies_are_used_in_the_same_build():
    bot = FindsPathsBot()
    judge = Judge(tool_rows=[
        {"question": "Where is order ORD-7?", "expected_answer": "Its status.", "expected_tools": ["lookup_order"]},
        {"question": "Has ORD-8 shipped yet?", "expected_answer": "Its date.", "expected_tools": ["lookup_order"]}])
    out, _reports, _q = _build({"id": 41, "type": "http", "config": {"persona": "Order bot."}}, judge, bot,
                               theme="found_paths")
    assert bot.tools_path == "tool_calls"  # set on the client during the build
    assert out["profile"]["kind"] == "agent" and out["profile"]["tools_seen"] is True
    assert [t["name"] for t in out["learned"]["available_tools"]] == ["lookup_order"]
    rows = goldens_store.load_goldens(theme="found_paths")
    assert [g["expected_tools"] for g in rows if g.get("expected_tools")] == \
        [["lookup_order"]] * prepare_all.TOOL_GOLDENS_PER_TOOL


def test_a_found_path_never_replaces_one_already_set():
    bot = FindsPathsBot()
    bot.context_path = "typed.sources"
    bot.discovered["context_path"] = "found.sources"
    _build({"id": 42, "type": "http", "config": {"persona": "p", "context_path": "typed.sources"}}, Judge(), bot,
           theme="typed_paths")
    assert bot.context_path == "typed.sources"


def test_a_chat_bot_with_material_gets_scenario_context():
    conversations_store.add_conversation({"theme": "chat_ctx", "name": "Typed", "scenario": "s",
                                          "chatbot_role": "Typed role", "expected_outcome": "Typed outcome",
                                          "user_turns": ["Hello?"]})
    conversations_store.add_conversation({"theme": "chat_ctx", "name": "Has one", "scenario": "s",
                                          "chatbot_role": "r", "expected_outcome": "o",
                                          "user_turns": ["Hi?"], "context": ["Own passage."]})
    judge, bot = Judge(), Bot()
    out, _reports, _q = _build({"id": 43, "type": "http", "config": {"persona": "p"}}, judge, bot, theme="chat_ctx")
    assert out["profile"]["kind"] == "chat"
    scenarios = {c["name"]: c for c in conversations_store.load_conversations(theme="chat_ctx")}
    assert len(scenarios) == prepare_all.SCENARIOS_MIN
    assert scenarios["Has one"]["context"] == ["Own passage."]  # never overwritten
    typed = scenarios["Typed"]
    assert typed["context"] == ["Boots ship free."]
    assert typed["chatbot_role"] == "Typed role" and typed["expected_outcome"] == "Typed outcome"
    assert all(c["context"] for c in scenarios.values())
    # A chat bot's scenarios are not sent: the context is asked for directly.
    assert not any(c["user_turns"][0] in bot.asked for c in scenarios.values())
    # A second build: every scenario has its context, so no judge call for them.
    judge2 = Judge()
    _build({"id": 43, "type": "http", "config": {"persona": "p"}}, judge2, Bot(), theme="chat_ctx",
           known_documents={"https://shop.example/faq"})
    assert "repair" not in judge2.calls


def test_a_rag_bot_returning_sources_gets_no_scenario_context():
    class SourceBot(Bot):
        def chat(self, message, history=None):
            self.asked.append(message)
            return ChatReply(reply="ok", model="m", mode="http", retrieval_context=["From the index."])
    bot = SourceBot()
    out, _r, _q = _build({"id": 44, "type": "http", "config": {"persona": "p"}}, Judge(), bot, theme="rag_ctx")
    assert out["profile"]["kind"] == "rag"
    scenarios = conversations_store.load_conversations(theme="rag_ctx")
    assert all(c["user_turns"][0] in bot.asked for c in scenarios)  # tried once each
    assert not any(c.get("context") for c in scenarios)


def test_stop_ends_a_running_build(app_module, client, offline_judge, monkeypatch):
    started = []
    monkeypatch.setattr(app_module, "_start_job", started.append)
    target = _create_target(client, name="Stoppable")
    tid = target["id"]

    class StopsBot(Bot):
        def chat(self, message, history=None):
            if not self.asked:  # the user presses Stop while the first message is answered
                assert client.post(f"/api/targets/{tid}/prepare-all/cancel").status_code == 200
                assert app_module._builds[tid]["stop"] is True
            return super().chat(message, history)

    bot = StopsBot()
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: bot)
    assert client.post(f"/api/targets/{tid}/prepare-all", json={"generate": True}).json()["status"] == "running"
    assert app_module._builds[tid]["stop"] is False
    started[0]()
    view = client.get(f"/api/targets/{tid}/prepare-all").json()
    assert view["status"] == "stopped" and "error" not in view
    assert bot.asked == [autogen.INTERVIEW[0]] and offline_judge.calls == []
    assert not any(s["status"] == "running" for s in view["steps"])
    # A new build starts afresh.
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: Bot())
    client.post(f"/api/targets/{tid}/prepare-all", json={"generate": True})
    assert app_module._builds[tid]["stop"] is False


def test_cancel_without_a_build_changes_nothing(client, offline_judge):
    bot = _create_target(client, name="Idle Bot")
    r = client.post(f"/api/targets/{bot['id']}/prepare-all/cancel")
    assert r.status_code == 200 and r.json()["status"] == "idle"
    assert client.post("/api/targets/99999/prepare-all/cancel").status_code == 404


def test_editing_a_chatbot_keeps_what_the_build_learned(app_module, client):
    target = _create_target(client, name="Edited Bot")
    row = app_module.storage.get_target(app_module._conn, target["id"])
    form = dict(row["config"])
    learned = {"auto_built": True, "profile": {"kind": "rag", "sources_seen": True}, "docs_searched": True}
    app_module.storage.update_target(app_module._conn, row["id"], row["name"], row["type"],
                                     {**row["config"], **learned})
    r = client.put(f"/api/targets/{target['id']}", json={"name": "Edited Bot 2", "type": "http", "config": form})
    assert r.status_code == 200, r.text
    saved = app_module.storage.get_target(app_module._conn, target["id"])["config"]
    assert {k: saved.get(k) for k in learned} == learned


def test_the_build_saves_under_the_found_lock(app_module, client, offline_judge, monkeypatch):
    held = []
    real = app_module.storage.update_target

    def spy(*args, **kwargs):
        held.append(app_module._found_lock.locked())
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module.storage, "update_target", spy)
    _run_fake_build(app_module, client, monkeypatch, config={"tools_path": "tools_called"})
    assert held and held[-1] is True


def test_tool_hint_when_the_tools_path_is_already_set(app_module):
    row = {"id": 1, "type": "http", "config": {"tools_path": "tool_calls", "profile": {"kind": "agent"}}}
    left = {l["key"]: l for l in app_module._leftovers(row)}
    assert left["tool_correctness"]["hint"] == "Press Build all test data again."


def test_build_view_counts_metrics_once(app_module, client, monkeypatch):
    bot = _create_target(client, name="Viewed Bot")
    calls = []
    real = app_module._metrics_summary
    monkeypatch.setattr(app_module, "_metrics_summary", lambda row: calls.append(1) or real(row))
    view = app_module._build_view(bot["id"])
    assert len(calls) == 1 and "leftovers" in view
