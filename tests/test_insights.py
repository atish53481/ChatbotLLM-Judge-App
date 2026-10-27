"""Eval Insights: the judge reads a run's failed and weak test cases and names
the issues (mild / serious) behind them, plus a short overview. Offline."""
import json

from backend import storage
from backend.dashboard import insights
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures
from tests.test_scans import _result, _scan


def _row(golden, passed, score, reply="reply"):
    return {"golden_id": golden, "question": f"question {golden}", "input": f"question {golden}",
            "actual_output": reply, "expected_output": "", "score": score, "passed": passed,
            "reason": f"reason for {golden}"}


def _metric(key, rows, status="fail"):
    return {**_result(key, status, 0.6), "rows": rows}


class Judge:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.payload), 0.0


VERDICT = {
    "overview": ["Answers are relevant but often skip the direct answer.", "  "],
    "issues": [
        {"severity": "serious", "title": "Defers instead of answering", "detail": "Tells users to look online.",
         "metrics": ["answer_relevancy"], "cases": [0]},
        {"severity": "mild", "title": "Long replies", "detail": "Padding.", "metrics": ["answer_relevancy"], "cases": [1, 99]},
        {"severity": "weird", "title": "", "cases": []},
    ],
}


def _run(conn):
    bot = storage.add_target(conn, "bot", "http", {"instructions": ["Be brief."]})
    return _scan(conn, bot, [_metric("answer_relevancy", [_row("g1", False, 0.2), _row("g2", True, 0.72),
                                                         _row("g3", True, 0.95)])])


def test_generate_names_issues_with_their_cases(tmp_path):
    conn = storage.init_db(str(tmp_path / "i.db"))
    job = _run(conn)
    judge = Judge(VERDICT)
    out = insights.generate(conn, job, judge)
    assert out["overview"] == ["Answers are relevant but often skip the direct answer."]
    assert [(i["severity"], i["title"]) for i in out["issues"]] == [
        ("serious", "Defers instead of answering"), ("mild", "Long replies")]
    # Case numbers map to the run's test cases (test_case_0, ...); unknown ones are dropped.
    assert out["issues"][0]["cases"] == ["id:g1"] and out["issues"][1]["cases"] == ["id:g2"]
    assert out["counts"] == {"serious": 1, "mild": 1}
    # The judge sees the failed and the weak (near-threshold) cases, not the clean one.
    assert "question g1" in judge.prompts[0] and "question g2" in judge.prompts[0]
    assert "question g3" not in judge.prompts[0]
    # Saved: reading it back costs no judge call.
    assert insights.saved(conn, job)["issues"] == out["issues"]


def test_a_clean_run_needs_no_judge(tmp_path):
    conn = storage.init_db(str(tmp_path / "i.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    job = _scan(conn, bot, [_metric("answer_relevancy", [_row("g1", True, 0.99)], status="pass")])
    judge = Judge(VERDICT)
    out = insights.generate(conn, job, judge)
    assert out["issues"] == [] and judge.prompts == []
    assert "passed" in out["overview"][0]


def test_hyperparameters_say_what_was_logged(tmp_path):
    conn = storage.init_db(str(tmp_path / "i.db"))
    params = insights.hyperparameters(conn, _run(conn))
    assert params["Judge model"] == "judge-a"
    assert params["Chatbot instructions"] == "1 rule"


def test_insights_endpoints(app_module, client, monkeypatch):
    bot = _create_target(client, name="Insight Bot", config={"url": "http://127.0.0.1:9/insights"})
    job = _scan(app_module._conn, bot["id"], [_metric("answer_relevancy", [_row("g1", False, 0.2)])])
    assert client.get(f"/api/scans/{job}/insights").json()["status"] == "none"
    monkeypatch.setattr(app_module, "build_judge", lambda **_kw: Judge(VERDICT))
    made = client.post(f"/api/scans/{job}/insights").json()
    assert made["status"] == "ready" and made["counts"]["serious"] == 1
    assert client.get(f"/api/scans/{job}/insights").json()["issues"] == made["issues"]
    assert client.post("/api/scans/999999/insights").status_code == 404
