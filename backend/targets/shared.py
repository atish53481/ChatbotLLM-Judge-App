"""One chatbot reply per question for a whole job, as DeepEval works: a golden
becomes one test case (one actual_output) that every metric scores, instead of
each metric asking the chatbot again and judging a different reply.

SharedReplies wraps the job's chatbot client. The first metric to send a
message (with the same conversation so far) gets the chatbot's reply; every
other metric of the job gets that same reply, waiting for it if it is still on
its way. Failures are not kept, so the next metric tries again. Metrics that
need fresh replies (Self-Check's several samples, a jailbreak's escalation)
use .fresh, the client itself.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time


class SharedReplies:
    def __init__(self, client):
        self._client = client
        self._lock = threading.Lock()
        self._replies: dict[tuple, object] = {}
        self._flights: dict[tuple, threading.Lock] = {}

    @property
    def fresh(self):
        """The chatbot client itself: every message is sent."""
        return self._client

    def __getattr__(self, name):
        # Everything else (max_message_length, send_delay, discovered, ...) is the client's.
        return getattr(self._client, name)

    def chat(self, message: str, history: list[dict] | None = None):
        key = (message, json.dumps(history or [], sort_keys=True))
        with self._lock:
            if key in self._replies:
                return self._replies[key]
            flight = self._flights.setdefault(key, threading.Lock())
        with flight:  # one send per message; the others wait for its reply
            with self._lock:
                if key in self._replies:
                    return self._replies[key]
            started = time.monotonic()
            answer = self._client.chat(message, history=history)
            if dataclasses.is_dataclass(answer) and getattr(answer, "latency_s", None) is None:
                answer = dataclasses.replace(answer, latency_s=round(time.monotonic() - started, 3))
            with self._lock:
                self._replies[key] = answer
        return answer
