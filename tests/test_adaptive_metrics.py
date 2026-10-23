"""Metrics that switch on only when the chatbot gives them something to score:
multi-turn RAG (per-turn retrieved context), agent metrics (tool calls) and
reference text-match scorers (no judge). Offline: no judge tokens."""
import dataclasses
import json
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

from backend import metrics_catalog as catalog
from backend import storage
from backend.dashboard.runner import run_spec
from backend.metric_advice import ADVICE
from backend.targets.base import ChatReply
from backend.targets.curl import find_tools_path, tool_calls
from backend.targets.http_client import HttpTargetClient
from backend.text_match import bleu, exact_match, levenshtein_similarity, rouge_l
from tests.fakes import CannedChatbot

NEW_KEYS = {
    "turn_faithfulness", "turn_contextual_precision", "turn_contextual_recall",
    "turn_contextual_relevancy", "task_completion", "tool_correctness", "argument_correctness",
    "plan_quality", "plan_adherence", "step_efficiency", "bleu", "rouge_l", "levenshtein",
    "exact_match",
}


def _db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    return conn, storage.add_target(conn, "bot", "http", {})


def _recording_metric(score=0.9, reason="ok"):
    """Metric double that keeps every test case it was handed."""
    metric = MagicMock()
    metric.cases = []

    def measure(case):
        metric.cases.append(case)
        metric.score, metric.reason = score, reason

    metric.measure.side_effect = measure
    metric.is_successful.side_effect = lambda: metric.score >= 0.5
    return metric


def _with(spec, metric=None, items=None):
    """A copy of a catalog spec with a fake metric and/or fixed cases."""
    copy = dataclasses.replace(spec)
    if metric is not None:
        copy.build_metric = lambda judge, threshold=0.7, **kw: metric
    if items is not None:
        copy.cases = lambda **_kw: items
    return copy


class ToolBot(CannedChatbot):
    """Canned chatbot that also reports tool calls and retrieved context."""

    def __init__(self, tools=None, retrieval=None):
        super().__init__()
        self.tools, self.retrieval = tools, retrieval
        self.tools_path = "tool_calls" if tools is not None else ""
        self.context_path = "sources" if retrieval is not None else ""

    def chat(self, message, history=None):
        reply = super().chat(message, history)
        return ChatReply(reply=reply.reply, model="canned", mode="http",
                         retrieval_context=self.retrieval, tools_called=self.tools)


# --- reference text match (no judge) ---------------------------------------
def test_exact_match_ignores_case_spacing_and_end_punctuation():
    assert exact_match("Refunds take 7 days.", "  refunds take 7   days ") == 1.0
    assert exact_match("Refunds take 7 days", "Refunds take 8 days") == 0.0


def test_levenshtein_similarity_is_one_minus_edits_over_length():
    assert levenshtein_similarity("kitten", "sitting") == pytest.approx(1 - 3 / 7)
    assert levenshtein_similarity("same", "same") == 1.0
    assert levenshtein_similarity("", "") == 1.0


def test_rouge_l_is_lcs_f1_over_words():
    # LCS "the cat the mat" = 4 words of 6 and 5.
    p, r = 4 / 6, 4 / 5
    assert rouge_l("the cat sat on the mat", "the cat ate the mat") == pytest.approx(2 * p * r / (p + r))
    assert rouge_l("a b c", "x y z") == 0.0


def test_bleu_is_one_for_identical_and_zero_for_disjoint_text():
    text = "orders ship within two business days from our warehouse"
    assert bleu(text, text) == pytest.approx(1.0)
    assert bleu("nothing in common here", text) == 0.0
    assert 0 < bleu("orders ship within two days", text) < 1


def test_text_match_spec_runs_without_a_judge(tmp_path):
    conn, target_id = _db(tmp_path)
    golden = {"id": "g1", "question": "What is your refund window?",
              "expected_answer": "Refunds are processed within 7 business days.", "context": []}
    spec = _with(catalog.get_spec("exact_match"), items=[golden])
    bot = ToolBot()
    bot.chat = lambda message, history=None: ChatReply(
        reply="refunds are processed within 7 business days", model="canned", mode="http")
    result = run_spec(spec, judge=None, target=bot, target_id=target_id, conn=conn)
    assert result["status"] == "pass", result
    assert result["score"] == 1.0
    assert result["judge"]["calls"] == 0


# --- catalog ----------------------------------------------------------------
def test_new_specs_are_complete_catalog_entries():
    for key in NEW_KEYS:
        spec = catalog.get_spec(key)
        assert spec is not None, key
        assert set(spec.presets()) == set(catalog.ENVIRONMENTS), key
        assert spec.ui_category in catalog.UI_CATEGORIES, key
        assert spec.scale_hint and spec.question and spec.description, key
        assert key in ADVICE, key


# --- tool calls from the chatbot's response ----------------------------------
def test_tool_calls_reads_openai_and_plain_shapes():
    openai = [{"id": "c1", "type": "function",
               "function": {"name": "lookup_order", "arguments": '{"order_id": "48213"}'}}]
    assert tool_calls(openai) == [{"name": "lookup_order", "input_parameters": {"order_id": "48213"}}]
    plain = [{"name": "search", "args": {"q": "refund"}, "output": "30 days", "reasoning": "need policy"},
             "send_email"]
    assert tool_calls(plain) == [
        {"name": "search", "input_parameters": {"q": "refund"}, "output": "30 days", "reasoning": "need policy"},
        {"name": "send_email"},
    ]


def test_find_tools_path_finds_tool_calls_in_a_response():
    data = {"choices": [{"message": {"content": "hi", "tool_calls": [{"function": {"name": "x"}}]}}]}
    assert find_tools_path(data) == "choices.0.message.tool_calls"
    assert find_tools_path({"reply": "hi"}) == ""


def _response(payload):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps(payload).encode()
    r.headers["Content-Type"] = "application/json"
    return r


def test_http_client_reads_tool_calls_at_tools_path():
    client = HttpTargetClient({"url": "https://bot.test/chat", "body_template": '{"q": "{{message}}"}',
                               "response_path": "reply", "tools_path": "tools"})
    reply = client.read_reply(_response({"reply": "Done", "tools": [{"name": "lookup", "args": {"id": 1}}]}))
    assert reply.tools_called == [{"name": "lookup", "input_parameters": {"id": 1}}]
    # An answer without tool calls is not an error: the agent metrics skip it.
    assert client.read_reply(_response({"reply": "Hi"})).tools_called is None


# --- run-time applicability ---------------------------------------------------
@pytest.mark.parametrize("key", ["argument_correctness", "plan_quality", "plan_adherence",
                                 "step_efficiency", "tool_correctness"])
def test_tool_metrics_do_not_apply_to_a_chatbot_without_tools_path(tmp_path, key):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    golden = {"id": "g1", "question": "Where is my order?", "expected_answer": "x", "context": [],
              "expected_tools": ["lookup_order"]}
    spec = _with(catalog.get_spec(key), metric=metric, items=[golden])
    result = run_spec(spec, judge=object(), target=ToolBot(), target_id=target_id, conn=conn)
    assert result["status"] == "error"
    assert "Not applicable" in result["error"]
    assert metric.cases == []  # no judge call


def test_cases_without_tool_calls_are_skipped_not_failed(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    goldens = [{"id": "g1", "question": "Where is my order?", "expected_answer": "x", "context": []}]
    spec = _with(catalog.get_spec("argument_correctness"), metric=metric, items=goldens)
    # tools_path set, but the chatbot called no tool for this question.
    result = run_spec(spec, judge=object(), target=ToolBot(tools=[]), target_id=target_id, conn=conn)
    assert result["status"] == "error"
    assert "called no tools" in result["error"]
    assert metric.cases == []


def test_argument_correctness_scores_the_tool_calls(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    goldens = [{"id": "g1", "question": "Where is my order 48213?", "expected_answer": "x", "context": []}]
    tools = [{"name": "lookup_order", "input_parameters": {"order_id": "48213"}}]
    spec = _with(catalog.get_spec("argument_correctness"), metric=metric, items=goldens)
    result = run_spec(spec, judge=object(), target=ToolBot(tools=tools), target_id=target_id, conn=conn)
    assert result["status"] == "pass", result
    case = metric.cases[0]
    assert [t.name for t in case.tools_called] == ["lookup_order"]
    assert case.tools_called[0].input_parameters == {"order_id": "48213"}
    assert result["rows"][0]["tools_called"] == tools


def test_tool_correctness_compares_with_the_goldens_expected_tools(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    goldens = [{"id": "g1", "question": "Where is my order?", "expected_answer": "x", "context": [],
                "expected_tools": ["lookup_order"]}]
    spec = _with(catalog.get_spec("tool_correctness"), metric=metric, items=goldens)
    run_spec(spec, judge=object(), target=ToolBot(tools=[{"name": "search"}]), target_id=target_id, conn=conn)
    case = metric.cases[0]
    assert [t.name for t in case.expected_tools] == ["lookup_order"]
    assert [t.name for t in case.tools_called] == ["search"]


def test_tool_correctness_dataset_holds_only_goldens_with_expected_tools(monkeypatch):
    rows = [{"id": "a", "theme": "t", "question": "q", "expected_answer": "x", "context": [],
             "expected_tools": ["lookup"]},
            {"id": "b", "theme": "t", "question": "q", "expected_answer": "x", "context": []}]
    monkeypatch.setattr(catalog, "load_goldens", lambda theme=None: rows)
    assert [g["id"] for g in catalog.get_spec("tool_correctness").cases(theme="t")] == ["a"]


def test_plan_metrics_get_a_trace_built_from_the_tool_calls(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    goldens = [{"id": "g1", "question": "Book the cheapest flight", "expected_answer": "x", "context": []}]
    tools = [{"name": "search_flights", "input_parameters": {"to": "LA"}, "output": "FL1 $380"},
             {"name": "book_flight", "input_parameters": {"id": "FL1"}}]
    spec = _with(catalog.get_spec("step_efficiency"), metric=metric, items=goldens)
    run_spec(spec, judge=object(), target=ToolBot(tools=tools), target_id=target_id, conn=conn)
    trace = metric.cases[0]._trace_dict
    assert trace["input"] == "Book the cheapest flight"
    assert [c["name"] for c in trace["children"]] == ["search_flights", "book_flight"]


def test_plan_metric_without_a_plan_is_not_applicable_rather_than_a_free_pass(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric(
        score=1.0, reason="There were no plans to evaluate within the trace of your agent's execution.")
    goldens = [{"id": "g1", "question": "q", "expected_answer": "x", "context": []}]
    spec = _with(catalog.get_spec("plan_quality"), metric=metric, items=goldens)
    result = run_spec(spec, judge=object(), target=ToolBot(tools=[{"name": "t"}]), target_id=target_id, conn=conn)
    assert result["status"] == "error"
    assert "no plan" in result["error"].lower()


def test_task_completion_always_applies_and_passes_tools_when_present(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    goldens = [{"id": "g1", "question": "What is your refund window?", "expected_answer": "x", "context": []}]
    spec = _with(catalog.get_spec("task_completion"), metric=metric, items=goldens)
    result = run_spec(spec, judge=object(), target=ToolBot(), target_id=target_id, conn=conn)
    assert result["status"] == "pass"
    assert not metric.cases[0].tools_called
    run_spec(spec, judge=object(), target=ToolBot(tools=[{"name": "lookup"}]), target_id=target_id, conn=conn)
    assert [t.name for t in metric.cases[1].tools_called] == ["lookup"]


# --- multi-turn RAG -----------------------------------------------------------
SCENARIO = {"id": "c1", "theme": "t", "name": "Refund chat", "scenario": "asks about refunds",
            "expected_outcome": "explains the refund window", "chatbot_role": "support",
            "user_turns": ["What is your refund window?", "And for sale items?"]}


def test_turn_rag_scores_the_chatbots_context_on_each_turn(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("turn_faithfulness"), metric=metric, items=[SCENARIO])
    result = run_spec(spec, judge=object(), target=ToolBot(retrieval=["Refunds: 30 days."]),
                      target_id=target_id, conn=conn)
    assert result["status"] == "pass", result
    assistant = [t for t in metric.cases[0].turns if t.role == "assistant"]
    assert [t.retrieval_context for t in assistant] == [["Refunds: 30 days."]] * 2
    assert result["rows"][0]["context_source"] == "chatbot"


def test_turn_rag_falls_back_to_the_scenarios_context(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    scenario = {**SCENARIO, "context": ["Refunds are accepted within 30 days."]}
    spec = _with(catalog.get_spec("turn_contextual_recall"), metric=metric, items=[scenario, SCENARIO])
    result = run_spec(spec, judge=object(), target=ToolBot(), target_id=target_id, conn=conn)
    assert result["status"] == "pass", result
    assert len(metric.cases) == 1  # the scenario without context was not applicable
    assistant = [t for t in metric.cases[0].turns if t.role == "assistant"]
    assert assistant[0].retrieval_context == ["Refunds are accepted within 30 days."]
    assert result["rows"][0]["context_source"] == "golden"
    assert "not applicable" in result["note"].lower()


def test_turn_rag_does_not_apply_without_any_context(tmp_path):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("turn_contextual_relevancy"), metric=metric, items=[SCENARIO])
    result = run_spec(spec, judge=object(), target=ToolBot(), target_id=target_id, conn=conn)
    assert result["status"] == "error"
    assert "Not applicable" in result["error"]
    assert metric.cases == []


# --- what the UI is told --------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_DB_PATH", str(tmp_path / "app_test.db"))
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    monkeypatch.setenv("JUDGE_START_JOB_WORKER", "0")
    from backend.dashboard import app as app_module

    monkeypatch.setattr(app_module, "_conn", storage.init_db(str(tmp_path / "api.db")))
    return TestClient(app_module.app, base_url="http://127.0.0.1"), app_module


def _target(app_module, **config):
    base = {"url": "https://bot.test/chat", "body_template": '{"q": "{{message}}"}',
            "response_path": "reply", "theme": "generic"}
    return storage.add_target(app_module._conn, "bot", "http", {**base, **config})


def test_metrics_endpoint_marks_tool_metrics_unavailable_until_tools_path_is_set(api):
    client, app_module = api
    plain = {m["key"]: m for m in client.get(f"/api/metrics?target_id={_target(app_module)}").json()}
    assert plain["argument_correctness"]["unavailable"]
    assert plain["argument_correctness"]["cases_available"] == 0
    assert plain["task_completion"]["unavailable"] is None
    assert plain["exact_match"]["unavailable"] is None
    tooled = {m["key"]: m for m in client.get(
        f"/api/metrics?target_id={_target(app_module, tools_path='tool_calls')}").json()}
    assert tooled["argument_correctness"]["unavailable"] is None
    assert tooled["argument_correctness"]["cases_available"] > 0


def test_metrics_endpoint_marks_turn_rag_unavailable_without_context(api):
    client, app_module = api
    plain = {m["key"]: m for m in client.get(f"/api/metrics?target_id={_target(app_module)}").json()}
    assert plain["turn_faithfulness"]["unavailable"]
    rag = {m["key"]: m for m in client.get(
        f"/api/metrics?target_id={_target(app_module, context_path='sources')}").json()}
    assert rag["turn_faithfulness"]["unavailable"] is None


def test_golden_expected_tools_round_trip(api):
    client, _ = api
    created = client.post("/api/goldens", json={
        "theme": "tools_test", "question": "Where is my order?", "expected_answer": "Shipped.",
        "expected_tools": ["lookup_order", " "]}).json()
    assert created["expected_tools"] == ["lookup_order"]
    # An edit that does not mention expected_tools keeps them.
    updated = client.put(f"/api/goldens/{created['id']}", json={
        "theme": "tools_test", "question": "Where is my order now?", "expected_answer": "Shipped."}).json()
    assert updated["expected_tools"] == ["lookup_order"]
    cleared = client.put(f"/api/goldens/{created['id']}", json={
        "theme": "tools_test", "question": "q", "expected_answer": "a", "expected_tools": []}).json()
    assert "expected_tools" not in cleared
