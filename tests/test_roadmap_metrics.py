"""Roadmap batch 1: Role Adherence, Turn Relevancy, Prompt Alignment and the
user's own metrics (custom G-Eval and DAG), all offline."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend import storage
from backend.dashboard.runner import judge_one, run_spec
from backend.datasets import custom_metrics as custom_store
from backend.metrics_catalog import (
    SPECS_BY_KEY,
    all_specs,
    custom_spec,
    get_spec,
    metric_options,
)
from tests.fakes import CannedChatbot
from tests.test_app import app_module, client  # noqa: F401 - fixtures


def _db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    return conn, storage.add_target(conn, "bot", "http", {})


# --- F1 Role Adherence ---------------------------------------------------------
def test_role_adherence_is_a_conversation_metric_on_scenarios_with_a_role():
    spec = SPECS_BY_KEY["role_adherence"]
    assert spec.kind == "conversation" and spec.group == "conversational"
    cases = spec.cases(theme="general_support")
    assert cases and all(c["chatbot_role"] for c in cases)
    case = spec.build_case(cases[0], [{"role": "user", "content": "hi"},
                                      {"role": "assistant", "content": "hello"}])
    assert case.chatbot_role == cases[0]["chatbot_role"]


def test_role_adherence_skips_scenarios_without_a_role():
    from backend.datasets import conversations

    conversations.add_conversation({"theme": "roleless", "name": "n", "user_turns": ["hi"]})
    assert SPECS_BY_KEY["role_adherence"].cases(theme="roleless") == []
    assert SPECS_BY_KEY["knowledge_retention"].cases(theme="roleless")


# --- F2 Turn Relevancy with a window --------------------------------------------
def test_turn_relevancy_window_size_reaches_the_metric(monkeypatch):
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    from backend.judges.judge import build_judge

    spec = SPECS_BY_KEY["turn_relevancy"]
    assert spec.settings["window_size"]["default"] == 10
    options = metric_options(spec, {"window_size": 3}, {})
    assert options == {"window_size": 3}
    metric = spec.build_metric(build_judge(), 0.7, **options)
    assert metric.window_size == 3


def test_metric_options_clamp_and_default():
    spec = SPECS_BY_KEY["turn_relevancy"]
    assert metric_options(spec, None, {}) == {"window_size": 10}
    assert metric_options(spec, {"window_size": 999}, {}) == {"window_size": 50}
    assert metric_options(spec, {"window_size": "x"}, {}) == {"window_size": 10}
    assert metric_options(SPECS_BY_KEY["answer_relevancy"], {"window_size": 3}, {}) == {}


# --- F3 Prompt Alignment --------------------------------------------------------
def test_prompt_alignment_reads_the_targets_instructions(monkeypatch):
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    from backend.judges.judge import build_judge

    spec = SPECS_BY_KEY["prompt_alignment"]
    assert spec.needs_target == ("instructions",)
    options = metric_options(spec, None, {"instructions": [" Reply in English ", "", "Be brief"]})
    assert options == {"instructions": ["Reply in English", "Be brief"]}
    metric = spec.build_metric(build_judge(), 0.7, **options)
    assert metric.prompt_instructions == ["Reply in English", "Be brief"]


def test_run_without_the_targets_instructions_is_an_error(tmp_path):
    conn, target_id = _db(tmp_path)
    result = run_spec(SPECS_BY_KEY["prompt_alignment"], judge=object(), target=CannedChatbot(),
                      target_id=target_id, conn=conn, theme="general_support", metric_kwargs={})
    assert result["status"] == "error"
    assert "instructions" in result["error"]
    assert storage.history(conn, target_id, "prompt_alignment") == []


def test_run_passes_metric_kwargs_to_build_metric(tmp_path):
    conn, target_id = _db(tmp_path)
    seen = {}
    metric = MagicMock(score=0.9, reason="ok")
    metric.is_successful.return_value = True

    def build(judge, threshold, **kw):
        seen.update(kw)
        return metric

    spec = SimpleNamespace(
        key="fake", title="Fake", threshold=0.7, needs=(), needs_target=(),
        cases=lambda **_kw: [{"id": "g1", "question": "What is your refund window?",
                              "expected_answer": "x", "context": []}],
        build_metric=build, build_case=lambda g, reply, retrieval=None: SimpleNamespace(),
    )
    result = run_spec(spec, judge=object(), target=CannedChatbot(), target_id=target_id,
                      conn=conn, metric_kwargs={"window_size": 4})
    assert result["status"] == "pass" and seen == {"window_size": 4}


def test_judge_one_prompt_alignment_needs_instructions():
    result = judge_one(SPECS_BY_KEY["prompt_alignment"], object(), "q", "a")
    assert result["status"] == "error" and "instructions" in result["error"]


# --- F4/F5 custom metrics -------------------------------------------------------
GEVAL_ROW = {
    "name": "Polite tone", "kind": "geval",
    "criteria": "Is the reply polite and free of sarcasm?",
    "params": ["input", "actual_output"], "threshold": 0.6,
}
DAG_ROW = {
    "name": "Mentions refund",
    "kind": "dag",
    "params": ["actual_output"],
    "dag": {"nodes": {
        "root": {"type": "BinaryJudgementNode", "criteria": "Does the output mention a refund?",
                 "evaluation_params": ["actual_output"], "children": ["no", "yes"]},
        "no": {"type": "VerdictNode", "verdict": False, "score": 0},
        "yes": {"type": "VerdictNode", "verdict": True, "score": 10},
    }},
}


def test_custom_store_crud_and_key():
    row = custom_store.add_metric(GEVAL_ROW)
    assert row["key"] == "custom_polite_tone" and row["id"].startswith("cm_")
    assert custom_store.add_metric(GEVAL_ROW)["key"] == "custom_polite_tone_2"
    updated = custom_store.update_metric(row["id"], {**GEVAL_ROW, "threshold": 0.8})
    assert updated["threshold"] == 0.8 and updated["key"] == "custom_polite_tone"
    assert custom_store.delete_metric(row["id"])
    assert not custom_store.delete_metric(row["id"])


@pytest.mark.parametrize("bad, message", [
    ({**GEVAL_ROW, "name": " "}, "name"),
    ({**GEVAL_ROW, "kind": "regex"}, "kind"),
    ({**GEVAL_ROW, "criteria": "", "evaluation_steps": []}, "criteria"),
    ({**GEVAL_ROW, "params": ["nope"]}, "params"),
    ({**GEVAL_ROW, "threshold": 2}, "threshold"),
    ({**DAG_ROW, "dag": {"nodes": {}}}, "DAG"),
])
def test_custom_store_rejects_bad_rows(bad, message):
    with pytest.raises(ValueError, match=message):
        custom_store.add_metric(bad)


@pytest.mark.parametrize("child", [
    {"type": "metric", "metric_class": "AnswerRelevancyMetric", "kwargs": {}},
    {"type": "geval", "name": "x", "criteria": "c", "evaluation_params": ["actual_output"], "model": "gpt-4o"},
])
def test_custom_dag_refuses_children_that_escape_the_judge(child):
    nodes = {**DAG_ROW["dag"]["nodes"], "yes": {"type": "VerdictNode", "verdict": True, "child": child}}
    with pytest.raises(ValueError, match="not allowed|own model"):
        custom_store.add_metric({**DAG_ROW, "dag": {"nodes": nodes}})


def test_custom_geval_spec_builds_and_scores_its_fields(monkeypatch):
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    from backend.judges.judge import build_judge

    row = custom_store.add_metric({**GEVAL_ROW, "strict": True})
    spec = custom_spec(row)
    assert spec.group == "custom" and spec.ui_category == "custom"
    assert spec.scores_on == ("input", "actual_output") and spec.criteria == GEVAL_ROW["criteria"]
    assert spec.improve["chatbot"] and spec.improve["tests"]
    metric = spec.build_metric(build_judge(), 0.6)
    assert metric.strict_mode is True
    case = spec.build_case({"question": "q", "expected_answer": "e", "context": []}, "r")
    assert case.input == "q" and case.actual_output == "r"


def test_custom_dag_spec_builds(monkeypatch):
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    from backend.judges.judge import build_judge

    spec = custom_spec(custom_store.add_metric(DAG_ROW))
    metric = spec.build_metric(build_judge(), 0.5)
    assert type(metric).__name__ == "DAGMetric"


def test_get_spec_and_all_specs_include_custom_metrics():
    row = custom_store.add_metric(GEVAL_ROW)
    assert get_spec(row["key"]).title == "Polite tone (G-Eval)"
    assert get_spec("answer_relevancy") is SPECS_BY_KEY["answer_relevancy"]
    assert get_spec("nope") is None
    assert row["key"] in {s.key for s in all_specs()}


def test_shipped_custom_metrics_are_valid():
    with open(custom_store.DEFAULT_CUSTOM_METRICS_PATH, encoding="utf-8") as f:
        shipped = json.load(f)
    kinds = {row["kind"] for row in shipped}
    assert kinds == {"geval", "dag"}
    for row in shipped:
        custom_spec({**row, **custom_store.clean(row)})


# --- API wiring -------------------------------------------------------------------
def test_api_lists_and_edits_custom_metrics(client):
    shipped = client.get("/api/custom-metrics").json()
    assert {r["kind"] for r in shipped} == {"geval", "dag"}
    r = client.post("/api/custom-metrics", json=GEVAL_ROW)
    assert r.status_code == 200, r.text
    row = r.json()
    metrics = {m["key"]: m for m in client.get("/api/metrics").json()}
    assert metrics[row["key"]]["custom_id"] == row["id"]
    assert metrics[row["key"]]["ui_category"] == "custom"
    assert metrics["answer_relevancy"]["custom_id"] is None
    assert metrics["turn_relevancy"]["settings"]["window_size"]["default"] == 10
    bad = client.post("/api/custom-metrics", json={**GEVAL_ROW, "criteria": ""})
    assert bad.status_code == 400
    assert client.put(f"/api/custom-metrics/{row['id']}", json={**GEVAL_ROW, "threshold": 0.9}).json()["threshold"] == 0.9
    assert client.delete(f"/api/custom-metrics/{row['id']}").status_code == 200
    assert client.delete(f"/api/custom-metrics/{row['id']}").status_code == 404


def test_api_prompt_alignment_unavailable_without_instructions(client):
    from tests.test_app import _create_target

    bare = _create_target(client)
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={bare['id']}").json()}
    assert "instructions" in rows["prompt_alignment"]["unavailable"]
    told = _create_target(client, config={"instructions": ["Reply in English"]})
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={told['id']}").json()}
    assert rows["prompt_alignment"]["unavailable"] is None


def test_api_job_accepts_custom_metric_keys_and_settings(client):
    from tests.test_app import _create_target

    target = _create_target(client)
    row = client.post("/api/custom-metrics", json=GEVAL_ROW).json()
    r = client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": [row["key"], "turn_relevancy"],
                                       "settings": {"turn_relevancy": {"window_size": 4}}})
    assert r.status_code == 200, r.text
    assert client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": ["custom_nope"]}).status_code == 404
