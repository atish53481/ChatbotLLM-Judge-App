"""Helpers for the automatic test-data build: caps on how much one build may
spend, and a duplicate check for new questions."""
import os
import re

from backend import cancel

BUILD_MAX_TARGET_MESSAGES = 30
BUILD_MAX_JUDGE_CALLS = 40


class BudgetSpent(RuntimeError):
    """A build used up its message or judge-call allowance."""


class Budget:
    def __init__(self, max_messages: int, max_judge_calls: int):
        self.max_messages = max_messages
        self.max_judge_calls = max_judge_calls
        self.messages = 0
        self.judge_calls = 0


class BudgetedJudge:
    """Wraps a judge and refuses calls once the judge-call cap is reached."""

    def __init__(self, judge, budget: Budget):
        self._judge = judge
        self._budget = budget

    def generate(self, prompt, schema=None):
        cancel.check()  # Stop ends the build before its next judge call
        if self._budget.judge_calls >= self._budget.max_judge_calls:
            raise BudgetSpent(f"judge call limit reached ({self._budget.max_judge_calls})")
        self._budget.judge_calls += 1
        return self._judge.generate(prompt, schema)

    def __getattr__(self, name):
        return getattr(self._judge, name)


class BudgetedClient:
    """Wraps a target client and refuses messages once the message cap is reached."""

    def __init__(self, client, budget: Budget):
        self._client = client
        self._budget = budget

    def chat(self, message, history=None):
        cancel.check()  # Stop ends the build before its next message
        if self._budget.messages >= self._budget.max_messages:
            raise BudgetSpent(f"message limit reached ({self._budget.max_messages})")
        self._budget.messages += 1
        return self._client.chat(message, history)

    @property
    def wrapped(self):
        """The client itself (paths found in replies are set on it)."""
        return self._client

    def __getattr__(self, name):
        return getattr(self._client, name)


def _cap(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def caps_from_env() -> tuple[int, int]:
    return (_cap("JUDGE_BUILD_MAX_MESSAGES", BUILD_MAX_TARGET_MESSAGES),
            _cap("JUDGE_BUILD_MAX_JUDGE_CALLS", BUILD_MAX_JUDGE_CALLS))


def normalise(text: str) -> str:
    text = re.sub(r"[^\w\s]", "", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def is_duplicate(question: str, existing: list[str], threshold: float = 0.9) -> bool:
    """True when the question matches an existing one after normalising, or
    shares at least `threshold` of its words (Jaccard)."""
    q = normalise(question)
    tokens = set(q.split())
    for other in existing:
        o = normalise(other)
        if q == o:
            return True
        o_tokens = set(o.split())
        union = tokens | o_tokens
        if union and len(tokens & o_tokens) / len(union) >= threshold:
            return True
    return False
