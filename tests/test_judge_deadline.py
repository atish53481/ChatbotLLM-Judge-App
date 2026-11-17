"""A hung judge call is abandoned at JUDGE_DEADLINE_S and retried; Stop ends the wait at once."""
import threading
import time

import pytest

from backend import cancel
from backend.judges import judge as judge_module


def test_hung_call_is_abandoned_at_the_deadline(monkeypatch):
    monkeypatch.setattr(judge_module, "JUDGE_DEADLINE_S", 0.3)
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(judge_module.JudgeTimeout):
        judge_module._with_deadline(release.wait, 30)
    assert time.monotonic() - started < 2
    release.set()


def test_answer_and_errors_come_back():
    assert judge_module._with_deadline(lambda x: x * 2, 21) == 42
    with pytest.raises(TimeoutError):  # the call's own TimeoutError is not taken for a hang
        judge_module._with_deadline(lambda: (_ for _ in ()).throw(TimeoutError("own")))


def test_stop_ends_the_wait_at_once(monkeypatch):
    monkeypatch.setattr(judge_module, "JUDGE_DEADLINE_S", 30)
    stopped = threading.Event()
    threading.Timer(0.2, stopped.set).start()
    release = threading.Event()
    started = time.monotonic()
    with cancel.watching(stopped.is_set), pytest.raises(cancel.RunCancelled):
        judge_module._with_deadline(release.wait, 30)
    assert time.monotonic() - started < 2
    release.set()


def test_call_retries_a_timed_out_attempt():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise judge_module.JudgeTimeout("hung")
        return "ok"

    judge = judge_module.GroqJudge.__new__(judge_module.GroqJudge)
    assert judge._call(flaky) == "ok"
    assert len(calls) == 2


def test_call_gives_up_after_the_deadline_retries():
    def hung():
        raise judge_module.JudgeTimeout("hung")

    judge = judge_module.GroqJudge.__new__(judge_module.GroqJudge)
    with pytest.raises(judge_module.JudgeTimeout):
        judge._call(hung)
