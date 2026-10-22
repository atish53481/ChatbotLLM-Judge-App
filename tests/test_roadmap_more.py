"""Roadmap batches 4-7: conversation simulator, synthetic data quality, golden
review, latency and cost, robustness, self-check hallucination, CI export and
judge calibration. All offline."""
import json
from unittest.mock import MagicMock, patch

import pytest

from backend.datasets import conversations as conversations_store
from backend.datasets import simulator
from backend.metrics_catalog import SPECS_BY_KEY
from backend.targets.base import ChatbotClient, ChatReply
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures


class EchoBot(ChatbotClient):
    def __init__(self):
        self.calls = []

    def chat(self, message, history=None):
        self.calls.append((message, list(history or [])))
        return ChatReply(reply=f"reply to {message}", model="m", mode="http")


class UserJudge:
    """Plays the simulated user: a scripted list of (message, done) replies."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        message, done = self.script.pop(0)
        return json.dumps({"message": message, "done": done}), 0.0


# --- F13 simulator ------------------------------------------------------------------
def test_simulator_follows_the_chatbot_and_stops_at_the_outcome():
    judge = UserJudge([("hi, my order is late", False), ("it is #42", False), ("", True)])
    bot = EchoBot()
    result = simulator.simulate(judge, bot, {"scenario": "late order", "expected_outcome": "tracking link"},
                                max_turns=5)
    assert result["user_turns"] == ["hi, my order is late", "it is #42"]
    assert result["stop_reason"] == "outcome reached"
    assert bot.calls[1][1][1]["content"] == "reply to hi, my order is late"
    assert "reply to hi, my order is late" in judge.prompts[1]


def test_simulator_respects_the_turn_cap():
    judge = UserJudge([("a", False), ("b", False), ("c", False)])
    result = simulator.simulate(judge, EchoBot(), {"scenario": "s"}, max_turns=2)
    assert result["user_turns"] == ["a", "b"] and result["stop_reason"] == "turn limit"


def test_score_conversation_reports_errors_without_raising():
    spec = SPECS_BY_KEY["knowledge_retention"]
    broken = MagicMock(side_effect=RuntimeError("judge down"))
    with patch.object(spec, "build_metric", broken):
        out = simulator.score_conversation(spec, object(), {"name": "x"}, [{"role": "user", "content": "hi"}])
    assert out["status"] == "error" and "judge down" in out["error"]


def test_api_simulate_scores_and_saves(client, app_module):
    target = _create_target(client)
    judge = UserJudge([("where is my parcel", False), ("thanks", True)])
    metric = MagicMock(score=0.9, reason="kept facts")
    metric.is_successful.return_value = True
    with patch.object(app_module, "build_judge", return_value=judge), \
         patch.object(app_module, "_client_or_400", return_value=EchoBot()), \
         patch.object(SPECS_BY_KEY["knowledge_retention"], "build_metric", return_value=metric):
        r = client.post("/api/simulate", json={
            "target_id": target["id"], "scenario": "late parcel", "expected_outcome": "tracking info",
            "metric_keys": ["knowledge_retention"], "save_as": "Simulated late parcel"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_turns"] == ["where is my parcel"]
    assert body["scores"][0]["score"] == 0.9 and body["scores"][0]["status"] == "pass"
    saved = next(c for c in conversations_store.load_conversations() if c["id"] == body["saved"]["id"])
    assert saved["user_turns"] == ["where is my parcel"]


def test_api_simulate_validates(client):
    target = _create_target(client)
    assert client.post("/api/simulate", json={"target_id": target["id"]}).status_code == 400
    assert client.post("/api/simulate", json={"target_id": target["id"], "scenario": "s",
                                              "metric_keys": ["answer_relevancy"]}).status_code == 400
    assert client.post("/api/simulate", json={"target_id": target["id"], "scenario": "s",
                                              "conversation_id": "nope"}).status_code == 404


# --- F14 synthesizer settings ----------------------------------------------------
def test_no_generation_options_keeps_deepeval_defaults():
    from backend.rag.generate import synthesizer_configs

    assert synthesizer_configs({}, judge=None) == {}
    assert synthesizer_configs(None, judge=None) == {}


def test_generation_options_build_deepeval_configs(monkeypatch):
    from deepeval.synthesizer import Evolution

    from backend.judges.judge import build_judge
    from backend.rag.generate import synthesizer_configs

    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    judge = build_judge()
    configs = synthesizer_configs({"evolutions": ["reasoning", "hypothetical"], "num_evolutions": 2,
                                   "quality_threshold": 0.8, "max_retries": 1,
                                   "task": "answer shop questions", "input_format": "a customer email"}, judge)
    evo = configs["evolution_config"]
    assert evo.num_evolutions == 2
    assert evo.evolutions == {Evolution.REASONING: 0.5, Evolution.HYPOTHETICAL: 0.5}
    assert configs["filtration_config"].synthetic_input_quality_threshold == 0.8
    assert configs["filtration_config"].critic_model is judge
    assert configs["styling_config"].task == "answer shop questions"
    with pytest.raises(ValueError, match="unknown evolution"):
        synthesizer_configs({"evolutions": ["nope"]}, judge)


def test_generated_goldens_record_evolutions_and_drafts(tmp_path, monkeypatch):
    from backend.datasets import goldens as goldens_store
    from backend.rag import generate

    (tmp_path / "g.json").write_text("[]")
    monkeypatch.setattr(goldens_store, "GOLDENS_PATH", str(tmp_path / "g.json"))
    golden = MagicMock(input="q?", expected_output="a", context=["c"],
                       additional_metadata={"evolutions": ["Reasoning"]})
    with patch("backend.rag.generate.Synthesizer") as synth, \
         patch("backend.rag.generate.LocalSentenceEmbedder"), \
         patch("backend.rag.generate.ContextConstructionConfig"):
        synth.return_value.generate_goldens_from_docs.return_value = [golden]
        generate.generate_goldens_from_document("d.pdf", "t", "d.pdf", MagicMock(),
                                                options={"review": True, "evolutions": ["reasoning"]})
    assert "evolution_config" in synth.call_args.kwargs
    row = goldens_store.load_goldens(theme="t")[0]
    assert row["evolutions"] == ["Reasoning"] and row["status"] == "draft"


def test_upload_passes_generation_options_and_rejects_bad_ones(client, app_module):
    gen = json.dumps({"evolutions": ["reasoning"], "review": True})
    with patch.object(app_module, "_start_job", side_effect=lambda job: job()), \
         patch.object(app_module, "enhance_document", side_effect=lambda path, judge: path), \
         patch.object(app_module, "generate_goldens_from_document", return_value=1) as mock_gen:
        r = client.post("/api/documents", data={"theme": "t", "generation": gen},
                        files={"file": ("faq.txt", b"Refunds within 7 days.", "text/plain")})
    assert r.status_code == 200, r.text
    assert mock_gen.call_args.kwargs["options"] == {"evolutions": ["reasoning"], "review": True}
    bad = client.post("/api/documents", data={"theme": "t", "generation": json.dumps({"evolutions": ["x"]})},
                      files={"file": ("faq.txt", b"x", "text/plain")})
    assert bad.status_code == 400


# --- F15 review queue -------------------------------------------------------------
def test_drafts_are_listed_but_never_run_until_accepted(client):
    from backend.datasets import goldens as goldens_store

    draft = goldens_store.add_golden("rev", "Draft q?", "a", ["ctx"], extra={"status": "draft"})
    kept = goldens_store.add_golden("rev", "Real q?", "a", ["ctx"])
    assert [g["id"] for g in SPECS_BY_KEY["answer_relevancy"].cases(theme="rev")] == [kept["id"]]
    assert [g["id"] for g in SPECS_BY_KEY["faithfulness"].cases(theme="rev")] == [kept["id"]]
    assert {g["id"] for g in client.get("/api/goldens?theme=rev").json()} == {draft["id"], kept["id"]}
    r = client.post("/api/goldens/review", json={"golden_ids": [draft["id"], kept["id"]], "action": "accept"})
    assert r.json() == {"changed": 1}
    assert len(SPECS_BY_KEY["answer_relevancy"].cases(theme="rev")) == 2


def test_rejecting_drafts_deletes_only_drafts():
    from backend.datasets import goldens as goldens_store

    draft = goldens_store.add_golden("rev2", "d?", "a", extra={"status": "draft"})
    kept = goldens_store.add_golden("rev2", "k?", "a")
    assert goldens_store.review_goldens([draft["id"], kept["id"]], "reject") == 1
    assert [g["id"] for g in goldens_store.load_goldens(theme="rev2")] == [kept["id"]]
    with pytest.raises(ValueError):
        goldens_store.review_goldens([kept["id"]], "maybe")


# --- F16 latency and cost -----------------------------------------------------------
class SlowBot(ChatbotClient):
    def __init__(self, delays):
        self.delays = list(delays)

    def chat(self, message, history=None):
        import time as _time
        _time.sleep(self.delays.pop(0))
        return ChatReply(reply="ok", model="m", mode="http")


def _fake_spec(n):
    from types import SimpleNamespace

    metric = MagicMock(score=1.0, reason="r")
    metric.is_successful.return_value = True
    return SimpleNamespace(
        key="fake_metric", title="Fake", threshold=0.7, needs=(),
        cases=lambda **_kw: [{"id": f"g{i}", "question": f"q{i}", "expected_answer": "", "context": []}
                             for i in range(n)],
        build_metric=lambda judge, threshold=0.7: metric,
        build_case=lambda g, reply, retrieval=None: object(),
    )


def _db(tmp_path):
    from backend import storage

    conn = storage.init_db(str(tmp_path / "t.db"))
    return conn, storage.add_target(conn, "bot", "http", {})


def test_run_reports_latency_and_records_it(tmp_path):
    from backend import storage
    from backend.dashboard.runner import run_spec

    conn, target_id = _db(tmp_path)
    result = run_spec(_fake_spec(2), judge=object(), target=SlowBot([0.0, 0.05]), target_id=target_id, conn=conn)
    assert result["status"] == "pass"
    assert result["latency"]["n"] == 2 and result["latency"]["p95"] >= 0.05
    assert all(r["latency_s"] is not None for r in result["rows"])
    assert storage.history(conn, target_id, "fake_metric")[0]["latency_p95"] >= 0.05
    assert result["judge_cost_usd"] is None


def test_slow_p95_fails_the_run_only_when_a_limit_is_set(tmp_path):
    from backend.dashboard.runner import run_spec

    conn, target_id = _db(tmp_path)
    slow = run_spec(_fake_spec(1), judge=object(), target=SlowBot([0.05]), target_id=target_id, conn=conn,
                    max_latency_s=0.01)
    assert slow["status"] == "fail" and "Too slow" in slow["note"]
    fine = run_spec(_fake_spec(1), judge=object(), target=SlowBot([0.05]), target_id=target_id, conn=conn)
    assert fine["status"] == "pass"


def test_judge_cost_uses_the_price(tmp_path, monkeypatch):
    from backend import usage
    from backend.dashboard.runner import run_spec

    conn, target_id = _db(tmp_path)
    # What the run's own usage meter counted.
    monkeypatch.setattr(usage, "view", lambda _counts: {"judge_tokens": 2_000_000, "judge_calls": 4,
                                                        "target_calls": 1})
    result = run_spec(_fake_spec(1), judge=object(), target=SlowBot([0.0]), target_id=target_id, conn=conn,
                      price_per_mtok=0.5)
    assert result["judge_cost_usd"] == 1.0


def test_judge_price_round_trips(client):
    assert client.put("/api/judge/price", json={"price_per_mtok": 0.6}).json()["price_per_mtok"] == 0.6
    client.put("/api/judge/settings", json={"model": "m"})
    assert client.get("/api/judge/price").json()["price_per_mtok"] == 0.6
    assert client.put("/api/judge/price", json={"price_per_mtok": None}).json()["price_per_mtok"] is None


# --- F17 / F18 several replies per golden ----------------------------------------------
def test_perturbations_keep_the_question_recognisable():
    from backend.metrics_catalog import perturbations

    original, typo, casual = perturbations({"question": "What is your refund window?", "context": []})
    assert original == "What is your refund window?"
    assert typo != original and len(typo) == len(original)
    assert casual == "hey quick question - what is your refund window"


def test_robustness_sends_every_variant_and_scores_them_together(tmp_path):
    from backend.dashboard.runner import judge_one, run_spec

    conn, target_id = _db(tmp_path)
    spec = SPECS_BY_KEY["robustness"]
    assert spec.kind == "multi_sample" and spec.ui_category == "quality"
    seen = []
    metric = MagicMock(score=0.9, reason="consistent")
    metric.is_successful.return_value = True
    metric.measure.side_effect = seen.append
    bot = EchoBot()
    with patch.object(spec, "build_metric", return_value=metric):
        result = run_spec(spec, judge=object(), target=bot, target_id=target_id, conn=conn,
                          theme="general_support", limit=1)
    assert result["status"] == "pass" and len(bot.calls) == 3
    case = seen[0]
    assert case.input.startswith("Message 1: ") and "Message 3: hey quick question" in case.input
    assert case.actual_output.count("Reply ") == 3
    assert judge_one(spec, object(), "q", "a")["status"] == "error"


def test_self_check_samples_setting_and_dataset(tmp_path):
    from backend.dashboard.runner import run_spec
    from backend.datasets import goldens as goldens_store
    from backend.metrics_catalog import metric_options

    conn, target_id = _db(tmp_path)
    spec = SPECS_BY_KEY["self_check"]
    goldens_store.add_golden("sc", "No context?", "a")
    goldens_store.add_golden("sc", "With context?", "a", ["fact"])
    assert [g["question"] for g in spec.cases(theme="sc")] == ["No context?"]
    metric = MagicMock(score=1.0, reason="r")
    metric.is_successful.return_value = True
    bot = EchoBot()
    with patch.object(spec, "build_metric", return_value=metric):
        run_spec(spec, judge=object(), target=bot, target_id=target_id, conn=conn, theme="sc",
                 metric_kwargs=metric_options(spec, {"samples": 4}, {}))
    assert [m for m, _ in bot.calls] == ["No context?"] * 4


# --- F19 export ---------------------------------------------------------------------
SECRET = "sk-very-secret-value"


def _export(client, **params):
    import io
    import zipfile

    target = _create_target(client, config={"headers": {"Content-Type": "application/json",
                                                        "Authorization": f"Bearer {SECRET}"},
                                            "theme": "general_support"})
    query = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/export/suite?target_id={target['id']}&{query}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


def test_export_never_contains_header_values(client):
    files = _export(client, metrics="answer_relevancy,correctness", env="production")
    assert all(SECRET not in content for content in files.values())
    chatbot = json.loads(files["tests/evals/chatbot.json"])
    assert chatbot["headers"] == {"Content-Type": "application/json"}
    assert "`Authorization`" in files["tests/evals/README.md"]
    assert "secrets.LLM_EVAL_HEADERS" in files[".github/workflows/llm-evals.yml"]


def test_export_metrics_use_the_environment_thresholds(client):
    files = _export(client, metrics="answer_relevancy,correctness,hallucination,knowledge_retention",
                    env="production")
    metrics = {m["key"]: m for m in json.loads(files["tests/evals/metrics.json"])}
    assert metrics["answer_relevancy"] == {**metrics["answer_relevancy"], "kind": "standard",
                                           "cls": "AnswerRelevancyMetric", "threshold": 0.85}
    assert metrics["correctness"]["kind"] == "geval" and metrics["correctness"]["criteria"]
    # Lower-is-better metrics are handed to DeepEval as the matching minimum.
    assert metrics["hallucination"]["threshold"] == 0.8
    assert "knowledge_retention" not in metrics
    assert "Knowledge Retention" in files["tests/evals/README.md"]
    goldens = json.loads(files["tests/evals/goldens.json"])
    assert goldens and all(g["message"] for g in goldens)


def test_exported_client_sends_like_the_app(client, tmp_path, monkeypatch):
    import importlib.util
    import py_compile

    files = _export(client, metrics="answer_relevancy")
    for path, content in files.items():
        out = tmp_path / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
    py_compile.compile(str(tmp_path / "tests/evals/test_chatbot_evals.py"), doraise=True)
    spec = importlib.util.spec_from_file_location("exported_chatbot", tmp_path / "tests/evals/chatbot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sent = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        sent.update(method=method, url=url, data=data, headers=headers)
        return MagicMock(text='{"reply": "7 days"}', json=lambda: {"reply": "7 days"},
                         raise_for_status=lambda: None)

    monkeypatch.setattr(module.requests, "request", fake_request)
    monkeypatch.setenv("LLM_EVAL_HEADERS", json.dumps({"Authorization": "Bearer from-env"}))
    assert module.ask('Say "hi"') == "7 days"
    assert json.loads(sent["data"]) == {"message": 'Say "hi"'}
    assert sent["headers"]["Authorization"] == "Bearer from-env"


def test_export_validates(client):
    target = _create_target(client)
    assert client.get(f"/api/export/suite?target_id={target['id']}&env=moon").status_code == 400
    assert client.get(f"/api/export/suite?target_id={target['id']}&metrics=nope").status_code == 404


# --- F20 calibration ------------------------------------------------------------------
def _run_with_cases(app_module, target_id, cases, threshold=0.7, key="correctness"):
    from backend import storage

    return storage.record_run(app_module._conn, target_id, key, 0.5, False, "t", result_json=json.dumps(
        {"threshold": threshold, "direction": "higher", "rows": cases}))


def test_labels_measure_agreement_and_replace_on_relabel(client, app_module):
    target = _create_target(client)
    run_id = _run_with_cases(app_module, target["id"], [
        {"question": "q1", "actual_output": "a1", "score": 0.9, "passed": True},
        {"question": "q2", "actual_output": "a2", "score": 0.8, "passed": True},
    ])
    assert client.post("/api/calibration", json={"run_id": run_id, "case_index": 0, "human_score": 1.0}).status_code == 200
    client.post("/api/calibration", json={"run_id": run_id, "case_index": 1, "human_score": 0.9})
    relabel = client.post("/api/calibration", json={"run_id": run_id, "case_index": 1, "human_score": 0.2,
                                                    "note": "wrong refund window"})
    data = client.get(f"/api/calibration?target_id={target['id']}").json()
    assert len(data["labels"]) == 2
    agreement = data["agreement"]
    assert agreement["n"] == 2 and agreement["mean_abs_gap"] == pytest.approx(0.35)
    assert agreement["verdict_agreement"] == 0.5 and agreement["within_0_2"] == 0.5
    assert data["by_metric"]["correctness"]["n"] == 2
    assert client.delete(f"/api/calibration/{relabel.json()['id']}").status_code == 200
    assert client.post("/api/calibration", json={"run_id": run_id, "case_index": 9, "human_score": 1}).status_code == 404
    assert client.post("/api/calibration", json={"run_id": 999999, "case_index": 0, "human_score": 1}).status_code == 404


def test_calibration_examples_extend_geval_criteria_only():
    from backend.dashboard.runner import _calibrate

    geval = MagicMock(criteria="Is it correct?")
    examples = [{"question": f"q{i}", "actual_output": "a", "human_score": 0.2, "note": "too vague"}
                for i in range(7)]
    assert _calibrate(geval, examples) == 5
    assert geval.criteria.startswith("Is it correct?") and "Human score: 0.20 (too vague)" in geval.criteria
    assert geval.criteria.count("- Input:") == 5
    plain = MagicMock(spec=["measure"])  # DeepEval's own metrics have no criteria
    assert _calibrate(plain, examples) == 0
    assert _calibrate(MagicMock(criteria="c"), []) == 0


# --- Production hardening ------------------------------------------------------------------
def test_health_reports_database_judge_and_version(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"] is True
    assert body["version"] and body["deepeval"].startswith("4.")
    assert body["judge_configured"] is True  # the fixture sets JUDGE_API_KEY


def test_health_is_503_when_the_database_fails():
    from backend.dashboard.hardening import health

    broken = MagicMock()
    broken.execute.side_effect = RuntimeError("disk gone")
    status, body = health(broken, judge_configured=False, worker_alive=None)
    assert status == 503 and body["status"] == "error"


def test_api_responses_carry_security_headers_and_no_cache(client):
    r = client.get("/api/metrics/environments")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


def test_oversized_bodies_are_refused(client):
    from backend.dashboard import hardening

    too_big = hardening.max_body_bytes() + 1
    r = client.post("/api/goldens", content=b"{}", headers={"Content-Type": "application/json",
                                                             "Content-Length": str(too_big)})
    assert r.status_code == 413


def test_unexpected_errors_become_json_without_a_traceback(app_module):
    from fastapi.testclient import TestClient

    def boom():
        raise RuntimeError("secret internals")

    if not any(getattr(route, "path", "") == "/api/__test_boom" for route in app_module.app.routes):
        app_module.app.add_api_route("/api/__test_boom", boom)
    r = TestClient(app_module.app, base_url="http://127.0.0.1", raise_server_exceptions=False).get("/api/__test_boom")
    assert r.status_code == 500
    assert r.json()["detail"].startswith("internal error (RuntimeError)")
    assert "secret internals" not in r.text


def test_untrusted_host_is_still_refused(app_module):
    from fastapi.testclient import TestClient

    r = TestClient(app_module.app, base_url="http://evil.example").get("/api/status")
    assert r.status_code == 400
