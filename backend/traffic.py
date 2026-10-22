"""Traffic log for the backend window: what goes to the chatbot and the judge,
and what comes back, as it happens.

On by default; JUDGE_LOG_TRAFFIC=0 turns it off. Only questions, replies and
judge outputs are logged, cut to MAX_CHARS: header values (cookies, API keys)
and request bodies never are.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("llm_judge.traffic")
MAX_CHARS = 300


def enabled() -> bool:
    return os.getenv("JUDGE_LOG_TRAFFIC", "1").strip().lower() not in ("0", "false", "no", "off")


def clip(text) -> str:
    """One line, at most MAX_CHARS, with how much was cut."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= MAX_CHARS:
        return flat
    return f"{flat[:MAX_CHARS]}... (+{len(flat) - MAX_CHARS} chars)"


def log(message: str, *args) -> None:
    if enabled():
        logger.info(message, *args)
