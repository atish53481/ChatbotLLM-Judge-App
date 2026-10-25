"""Stop: how a job's Stop reaches every chatbot and judge call it makes.

A job runs its metrics under watching(is_stopped). Runs and their worker
threads carry that check along (contextvars, like usage.meter), and check() is
called before each chatbot message, each judge call and each retry wait, so a
stopped job ends at its next call instead of after the metric's whole case.
Code outside a job (no check set) is never stopped.
"""
from __future__ import annotations

import contextlib
import contextvars
import time


class RunCancelled(Exception):
    """Raised to stop a run: from on_progress between steps, or from check() before a call."""


_CHECK: contextvars.ContextVar = contextvars.ContextVar("cancel_check", default=None)


@contextlib.contextmanager
def watching(is_stopped):
    """Inside the block (and contexts copied from it) check() raises once is_stopped() is true."""
    token = _CHECK.set(is_stopped)
    try:
        yield
    finally:
        _CHECK.reset(token)


def check() -> None:
    is_stopped = _CHECK.get()
    if is_stopped is not None and is_stopped():
        raise RunCancelled()


def sleep(seconds: float, step: float = 0.5) -> None:
    """time.sleep that ends early (raising RunCancelled) when the run is stopped."""
    end = time.monotonic() + seconds
    while True:
        check()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(step, left))
