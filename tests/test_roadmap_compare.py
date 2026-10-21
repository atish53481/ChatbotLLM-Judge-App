"""Roadmap batch 2: summary stats, run-vs-run diff, pairwise A/B judging."""
import json
from unittest.mock import patch

import pytest

from backend import storage
from backend.dashboard.compare import compare_runs
from backend.dashboard.pairwise import judge_pair, run_pairwise
from backend.dashboard.stats import percentile, summary_stats
from backend.metrics_catalog import ask
from backend.targets.base import ChatbotClient, ChatReply
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures


# --- F8 stats ---------------------------------------------------------------------
def test_summary_stats():
    stats = summary_stats([0.0, 0.5, 1.0, None], [False, True, True])
    assert stats["n"] == 3 and stats["mean"] == 0.5 and stats["median"] == 0.5
    assert stats["min"] == 0.0 and stats["max"] == 1.0
    assert stats["histogram"] == [1, 0, 0, 0, 0, 1, 0, 0, 0, 1]
    assert stats["pass_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert summary_stats([None]) is None


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile([], 50) is None


def test_run_result_carries_stats_and_golden_ids(tmp_path):
    from tests.test_runner import _case, _fake_metric, _fake_spec
    from backend.dashboard.runner import run_spec
    from tests.fakes import CannedChatbot

    conn = storage.init_db(str(tmp_path / "t.db"))
    target_id = storage.add_target(conn, "bot", "http", {})
    spec = _fake_spec([_case("q1"), _case("q2")], _fake_metric([0.9, 0.4], [True, False]))
    result = run_spec(spec, judge=object(), target=CannedChatbot(), target_id=target_id, conn=conn)
    assert result["stats"]["n"] == 2 and result["stats"]["pass_rate"] == 0.5
    assert [r["golden_id"] for r in result["rows"]] == ["q1", "q2"]


# --- F6 compare -------------------------------------------------------------------
def _run(run_id, rows, score, key="answer_relevancy"):
    return {"id": run_id, "ts": "t", "metric_key": key, "score": score, "passed": score >= 0.7,
            "result_json": json.dumps({"rows": rows})}


def test_compare_flags_regressions_fixes_and_changed_case_sets():
    a = _run(1, [{"golden_id": "g1", "question": "q1", "score": 0.9, "passed": True},
                 {"golden_id": "g2", "question": "q2", "score": 0.2, "passed": False},
                 {"question": "old only", "score": 1.0, "passed": True}], 0.7)
    b = _run(2, [{"golden_id": "g1", "question": "q1", "score": 0.3, "passed": False, "reason": "worse"},
                 {"golden_id": "g2", "question": "q2", "score": 0.8, "passed": True},
                 {"question": "new only", "score": 1.0, "passed": True}], 0.7)
    diff = compare_runs(a, b)
    assert diff["counts"] == {"regressed": 1, "fixed": 1, "same": 0, "added": 1, "removed": 1}
    assert diff["cases"][0]["change"] == "regressed" and diff["cases"][0]["delta"] == -0.6
    assert diff["cases"][0]["reason_b"] == "worse"
    assert diff["delta"] == 0.0 and diff["a"]["stats"]["n"] == 3


def test_compare_refuses_different_metrics_and_runs_without_cases():
    with pytest.raises(ValueError, match="same metric"):
        compare_runs(_run(1, [], 1), _run(2, [], 1, key="bias"))
    with pytest.raises(ValueError, match="kept no cases"):
        compare_runs({**_run(1, [], 1), "result_json": None}, _run(2, [], 1))


def test_api_compare(client, app_module):
    target = _create_target(client)
    conn = app_module._conn
    ids = [storage.record_run(conn, target["id"], "answer_relevancy", s, s >= 0.7, "t",
                              result_json=json.dumps({"rows": [{"question": "q", "score": s, "passed": s >= 0.7}]}))
           for s in (0.9, 0.5)]
    diff = client.get(f"/api/runs/compare?a={ids[0]}&b={ids[1]}").json()
    assert diff["counts"]["regressed"] == 1
    assert client.get(f"/api/runs/compare?a={ids[0]}&b=999999").status_code == 404


# --- F7 pairwise ------------------------------------------------------------------
class ScriptedJudge:
    """Answers the pairwise prompt from a function of (first reply, second reply)."""

    def __init__(self, pick):
        self.pick = pick

    def generate(self, prompt):
        first = prompt.split("Reply 1:\n", 1)[1].split("\n\nReply 2:", 1)[0]
        second = prompt.split("Reply 2:\n", 1)[1].split("\n\nReturn only JSON", 1)[0]
        return json.dumps({"winner": self.pick(first, second), "reason": "r"}), 0.0

    def get_model_name(self):
        return "scripted"


class Bot(ChatbotClient):
    def __init__(self, reply):
        self.reply = reply

    def chat(self, message, history=None):
        return ChatReply(reply=self.reply, model="m", mode="http")


def test_consistent_preference_wins():
    judge = ScriptedJudge(lambda first, second: "1" if first == "good" else "2")
    verdict = judge_pair(judge, "q", "good", "bad")
    assert verdict["winner"] == "a" and verdict["orderings"] == ["1", "2"]


def test_position_bias_is_a_tie():
    judge = ScriptedJudge(lambda first, second: "1")  # always the first shown
    verdict = judge_pair(judge, "q", "x", "y")
    assert verdict["winner"] == "tie" and "position bias" in verdict["note"]


def test_long_winner_gets_a_verbosity_note():
    judge = ScriptedJudge(lambda first, second: "1" if len(first) > len(second) else "2")
    verdict = judge_pair(judge, "q", "long " * 20, "short")
    assert verdict["winner"] == "a" and "longer" in verdict["note"]


def test_run_pairwise_counts_wins():
    judge = ScriptedJudge(lambda first, second: "1" if first == "good" else "2")
    items = [{"id": "g1", "question": "q1"}, {"id": "g2", "question": "q2"}]
    result = run_pairwise(judge, Bot("good"), Bot("bad"), items, prompt=ask, names=("A", "B"))
    assert (result["wins_a"], result["wins_b"], result["ties"]) == (2, 0, 0)
    assert result["score"] == 1.0 and result["winner"] == "a" and result["judge"]["model"] == "scripted"


def test_api_pairwise_job_records_a_comparison(client, app_module):
    a = _create_target(client, name="Bot A")
    b = _create_target(client, name="Bot B")
    assert client.post("/api/pairwise", json={"target_a": a["id"], "target_b": a["id"]}).status_code == 400
    r = client.post("/api/pairwise", json={"target_a": a["id"], "target_b": b["id"], "limit": 2})
    assert r.status_code == 200, r.text
    judge = ScriptedJudge(lambda first, second: "tie")
    with patch.object(app_module, "build_judge", return_value=judge), \
         patch.object(app_module, "_client_or_400", return_value=Bot("same")):
        while app_module.jobs.process_next():
            pass
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    result = job["results"][0]
    assert result["ties"] == 2 and result["names"] == {"a": "Bot A", "b": "Bot B"}
    listed = client.get(f"/api/pairwise?target_id={b['id']}").json()
    assert listed and listed[0]["result"]["ties"] == 2
