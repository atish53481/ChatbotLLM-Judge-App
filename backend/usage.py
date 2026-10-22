"""Token and call counts for the dashboard's "Tokens used" tile.

In memory, per backend process: a restart starts from zero. The judge reports
token usage on every completion; chatbots under test are arbitrary websites,
so only their calls are counted.

meter() also counts what one piece of work spends, apart from anything running
beside it (metrics of a job run in parallel): calls made in its context, which
runs and their worker threads carry along (contextvars), add to it as well.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading

_LOCK = threading.Lock()
_state = {"judge_calls": 0, "judge_prompt_tokens": 0, "judge_completion_tokens": 0, "target_calls": 0}
_METERS: contextvars.ContextVar[tuple[dict, ...]] = contextvars.ContextVar("usage_meters", default=())


def _empty() -> dict:
    return {"judge_calls": 0, "judge_prompt_tokens": 0, "judge_completion_tokens": 0, "target_calls": 0}


def _counters() -> tuple[dict, ...]:
    return (_state, *_METERS.get())


@contextlib.contextmanager
def meter():
    """Counts the calls made inside the block (and in contexts copied from it);
    read it with view(), during or after the block."""
    counts = _empty()
    token = _METERS.set((*_METERS.get(), counts))
    try:
        yield counts
    finally:
        _METERS.reset(token)


def _field(u, name: str) -> int:
    value = u.get(name) if isinstance(u, dict) else getattr(u, name, None)
    return int(value or 0)


def record_judge(u) -> None:
    """One judge completion; `u` is the response's usage (object, dict or None)."""
    prompt, completion = (_field(u, "prompt_tokens"), _field(u, "completion_tokens")) if u is not None else (0, 0)
    with _LOCK:
        for counts in _counters():
            counts["judge_calls"] += 1
            counts["judge_prompt_tokens"] += prompt
            counts["judge_completion_tokens"] += completion


def record_target() -> None:
    with _LOCK:
        for counts in _counters():
            counts["target_calls"] += 1


def view(counts: dict) -> dict:
    with _LOCK:
        judge_tokens = counts["judge_prompt_tokens"] + counts["judge_completion_tokens"]
        return {
            "total_tokens": judge_tokens,
            "calls": counts["judge_calls"] + counts["target_calls"],
            "target_calls": counts["target_calls"],
            "judge_calls": counts["judge_calls"],
            "judge_tokens": judge_tokens,
        }


def snapshot() -> dict:
    return view(_state)


def reset() -> None:
    with _LOCK:
        for key in _state:
            _state[key] = 0
