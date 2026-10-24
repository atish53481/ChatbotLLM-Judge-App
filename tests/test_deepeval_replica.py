"""The app works as DeepEval does: one reply per golden scored by every metric
of a job, and DeepEval's own score scale for every shipped metric."""
import json
import threading
import time

from backend import storage
from backend.targets.base import ChatReply
from backend.targets.shared import SharedReplies


class CountingBot:
    max_message_length = 123

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def chat(self, message, history=None):
        with self.lock:
            self.calls.append((message, tuple(json.dumps(h) for h in history or [])))
            n = len(self.calls)
        time.sleep(self.delay)
        return ChatReply(reply=f"reply {n}", model="m", mode="http")


def test_each_question_is_sent_once_and_every_metric_gets_that_reply():
    bot = CountingBot(delay=0.05)
    shared = SharedReplies(bot)
    replies = []
    threads = [threading.Thread(target=lambda: replies.append(shared.chat("Q1").reply)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(bot.calls) == 1 and set(replies) == {"reply 1"}
    assert shared.chat("Q1").latency_s >= 0.05  # the chatbot's own time, kept for latency
    # Another conversation so far is another test case.
    shared.chat("Q1", history=[{"role": "user", "content": "hi"}])
    assert len(bot.calls) == 2
    assert shared.max_message_length == 123  # the client's settings show through


def test_fresh_replies_bypass_the_shared_ones():
    bot = CountingBot()
    shared = SharedReplies(bot)
    shared.chat("Q")
    shared.fresh.chat("Q")
    assert len(bot.calls) == 2


def test_a_failed_send_is_not_kept():
    class Flaky:
        tries = 0

        def chat(self, message, history=None):
            Flaky.tries += 1
            if Flaky.tries == 1:
                raise TimeoutError("slow")
            return ChatReply(reply="ok", model="m", mode="http")

    shared = SharedReplies(Flaky())
    try:
        shared.chat("Q")
    except TimeoutError:
        pass
    assert shared.chat("Q").reply == "ok"


def test_stored_runs_move_to_deepevals_scale_once(tmp_path):
    path = str(tmp_path / "old.db")
    conn = storage.init_db(path)
    conn.execute("DELETE FROM meta WHERE key = 'deepeval_scale'")  # as before the change
    target = storage.add_target(conn, "bot", "http", {})
    old = {"key": "bias", "direction": "lower", "score": 0.2, "threshold": 0.5, "rows": [{"score": 0.2}]}
    conn.execute("INSERT INTO runs (target_id, metric_key, score, passed, ts, cases_run, result_json) "
                 "VALUES (?, 'bias', 0.2, 1, '2026-10-01T00:00:00Z', 1, ?)", (target, json.dumps(old)))
    job = storage.add_job(conn, target, ["bias"], {})
    storage.update_job(conn, job, status="done", results=[old])
    conn.commit()
    conn.close()

    for _ in range(2):  # the second open changes nothing
        conn = storage.init_db(path)
        row = conn.execute("SELECT score, result_json FROM runs").fetchone()
        result = json.loads(row["result_json"])
        assert row["score"] == 0.8
        assert (result["direction"], result["score"], result["threshold"]) == ("higher", 0.8, 0.5)
        assert result["rows"][0]["score"] == 0.8
        assert storage.get_job(conn, job)["results"][0]["score"] == 0.8
        conn.close()


def test_deepeval_options_are_set_as_deepevals_constructor_would():
    from deepeval.metrics import AnswerRelevancyMetric
    from deepeval.models import DeepEvalBaseLLM

    from backend.metrics_catalog import apply_deepeval_options

    class Offline(DeepEvalBaseLLM):  # never called: only the metric's settings are checked
        def __init__(self):
            super().__init__("offline")

        def load_model(self):
            return None

        def generate(self, prompt, schema=None):
            raise AssertionError("no judge call expected")

        async def a_generate(self, prompt, schema=None):
            raise AssertionError("no judge call expected")

        def get_model_name(self):
            return "offline"

    metric = AnswerRelevancyMetric(threshold=0.7, model=Offline(), async_mode=False)
    apply_deepeval_options(metric, {"strict_mode": True, "include_reason": False, "async_mode": True})
    assert (metric.strict_mode, metric.threshold, metric.include_reason, metric.async_mode) == (True, 1, False, True)
    plain = AnswerRelevancyMetric(threshold=0.7, model=Offline(), async_mode=False)
    assert apply_deepeval_options(plain, None).threshold == 0.7


def test_strict_mode_run_reports_threshold_one(tmp_path):
    from tests.fakes import CannedChatbot
    from tests.test_runner import _case, _db, _fake_metric, _fake_spec
    from backend.dashboard.runner import run_spec

    conn, target_id = _db(tmp_path)
    metric = _fake_metric([1.0], [True])
    metric.strict_mode = False
    spec = _fake_spec([_case("q1")], metric)
    result = run_spec(spec, judge=object(), target=CannedChatbot(), target_id=target_id, conn=conn,
                      deepeval_options={"strict_mode": True})
    assert result["threshold"] == 1.0 and metric.strict_mode is True
    assert result["deepeval_options"] == {"strict_mode": True}
