import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from deepeval.models import LocalModel

from backend import usage
from backend.judges.judge import GroqJudge


@pytest.fixture(autouse=True)
def clean_counter():
    usage.reset()
    yield
    usage.reset()


def test_snapshot_starts_at_zero():
    assert usage.snapshot() == {
        "total_tokens": 0, "calls": 0, "target_calls": 0, "judge_calls": 0, "judge_tokens": 0,
    }


def test_records_judge_usage_object_and_target_calls():
    usage.record_judge(SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    usage.record_judge({"prompt_tokens": 1, "completion_tokens": 2})
    usage.record_target()
    snap = usage.snapshot()
    assert snap["judge_calls"] == 2
    assert snap["judge_tokens"] == 18
    assert snap["target_calls"] == 1
    assert snap["calls"] == 3
    assert snap["total_tokens"] == 18


def test_missing_usage_still_counts_the_call():
    usage.record_judge(None)
    snap = usage.snapshot()
    assert snap["judge_calls"] == 1 and snap["judge_tokens"] == 0


def test_reset_clears_everything():
    usage.record_target()
    usage.reset()
    assert usage.snapshot()["calls"] == 0


def _fake_client(usage_obj):
    def create(**_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
            usage=usage_obj,
        )
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _judge():
    return GroqJudge(model="m", api_key="k", base_url="http://127.0.0.1:9/v1", temperature=0.0, format="json")


def test_judge_generate_records_usage(monkeypatch):
    client = _fake_client(SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10))
    monkeypatch.setattr(LocalModel, "load_model", lambda self, async_mode=False: client)
    _judge().generate("hi")
    snap = usage.snapshot()
    assert snap["judge_calls"] == 1 and snap["judge_tokens"] == 10


def test_judge_generate_without_usage_still_counts(monkeypatch):
    client = _fake_client(None)
    monkeypatch.setattr(LocalModel, "load_model", lambda self, async_mode=False: client)
    _judge().generate("hi")
    snap = usage.snapshot()
    assert snap["judge_calls"] == 1 and snap["judge_tokens"] == 0


def test_a_meter_counts_only_its_own_work():
    usage.reset()
    seen = {}

    def work(name, calls):
        with usage.meter() as counts:
            for _ in range(calls):
                usage.record_target()
            seen[name] = usage.view(counts)["target_calls"]

    threads = [threading.Thread(target=work, args=("a", 2)), threading.Thread(target=work, args=("b", 3))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": 2, "b": 3}
    assert usage.snapshot()["target_calls"] == 5


def test_parallel_runs_report_their_own_chatbot_calls(tmp_path, monkeypatch):
    from backend import storage
    from backend.dashboard import runner
    from backend.dashboard.runner import run_spec

    monkeypatch.setattr(runner, "JUDGE_CONCURRENCY", 4)
    conn = storage.init_db(str(tmp_path / "t.db"))
    target_id = storage.add_target(conn, "bot", "http", {})

    class Bot:
        def chat(self, message, history=None):
            usage.record_target()
            return SimpleNamespace(reply="ok")

    def spec(n):
        metric = MagicMock(score=1.0, reason="r")
        metric.is_successful.return_value = True
        return SimpleNamespace(
            key=f"m{n}", title="M", threshold=0.5, needs=(),
            cases=lambda theme="", **_kw: [{"id": i, "question": f"q{i}"} for i in range(n)],
            build_metric=lambda judge, threshold=0.5: metric,
            build_case=lambda g, reply, retrieval=None: SimpleNamespace(input=g["question"], actual_output=reply),
        )

    results = {}

    def run(n):
        results[n] = run_spec(spec(n), judge=object(), target=Bot(), target_id=target_id, conn=conn)

    threads = [threading.Thread(target=run, args=(n,)) for n in (2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results[2]["chatbot_calls"] == 2 and results[3]["chatbot_calls"] == 3
