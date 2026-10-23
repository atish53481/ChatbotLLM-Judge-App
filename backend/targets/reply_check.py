"""Tells a chatbot's real answer apart from a service message.

Many chatbots answer HTTP 200 even when they did not answer the question: a
daily limit, a login wall, an expired session or an internal error comes back
as ordinary reply text. Scored as an answer, that text drags every metric down
and pollutes the trend, so the connector treats it as an error instead.

Only short replies are checked: a service message is a sentence or two, while
a real answer that happens to mention "rate limit" (an API docs bot, say) is
usually longer.
"""
from __future__ import annotations

import re

MAX_SERVICE_MESSAGE_CHARS = 300

_PATTERNS = [
    r"\b(question|message|chat|usage|request|daily|monthly|rate|free)\s+limit\b",
    r"\blimit\s+(reached|exceeded)\b",
    r"\bquota\b.{0,40}\b(exceeded|reached|used up)\b",
    r"\b(exceeded|reached)\b.{0,40}\bquota\b",
    r"\btoo many requests\b",
    # A login wall ("Please log in.", "...sign in to continue", "...sign in first"),
    # not advice inside an answer ("please sign in and check My Orders").
    r"\b(please|you need to|you must|you have to)\s+(log|sign)\s*in\b(?=\s*(to\b|first\b|[.!]|$))",
    r"\b(log|sign)\s*in\s+to\s+continue\b",
    r"\bsession\s+(has\s+)?(expired|timed out)\b",
    r"\bsomething went wrong\b",
    r"\binternal server error\b",
    r"\bservice\s+(is\s+)?(temporarily\s+)?unavailable\b",
]
_SERVICE = re.compile("|".join(_PATTERNS), re.IGNORECASE)


class ChatbotUnavailable(ValueError):
    """The chatbot answered with a service message, not an answer to the question."""


def service_message(reply: str) -> str | None:
    """Why this reply is not a real answer, or None when it looks like one."""
    text = (reply or "").strip()
    if not text:
        return "the chatbot sent an empty reply"
    if len(text) <= MAX_SERVICE_MESSAGE_CHARS and _SERVICE.search(text):
        return "the chatbot replied with a service message"
    return None
