"""Budget wrappers and duplicate check for the automatic test-data build. Offline."""
import pytest

from backend.datasets.build_kit import (
    Budget, BudgetedClient, BudgetedJudge, BudgetSpent, caps_from_env, is_duplicate,
)


class FakeJudge:
    def generate(self, prompt, schema=None):
        return "ok"

    def get_model_name(self):
        return "fake"


class FakeBot:
    def __init__(self, discovered=None):
        self.discovered = discovered

    def chat(self, message, history=None):
        return "reply"


def test_budgeted_judge_stops_at_cap():
    b = Budget(max_messages=1, max_judge_calls=2)
    j = BudgetedJudge(FakeJudge(), b)
    j.generate("a"); j.generate("b")
    with pytest.raises(BudgetSpent):
        j.generate("c")
    assert b.judge_calls == 2 and j.get_model_name() == "fake"


def test_budgeted_client_stops_at_cap_and_delegates():
    b = Budget(1, 1); c = BudgetedClient(FakeBot(discovered={"tools_path": "t"}), b)
    c.chat("hi")
    with pytest.raises(BudgetSpent):
        c.chat("again")
    assert c.discovered == {"tools_path": "t"}


def test_caps_from_env(monkeypatch):
    monkeypatch.delenv("JUDGE_BUILD_MAX_MESSAGES", raising=False)
    monkeypatch.delenv("JUDGE_BUILD_MAX_JUDGE_CALLS", raising=False)
    assert caps_from_env() == (30, 40)
    monkeypatch.setenv("JUDGE_BUILD_MAX_MESSAGES", "5"); monkeypatch.setenv("JUDGE_BUILD_MAX_JUDGE_CALLS", "x")
    assert caps_from_env() == (5, 40)


@pytest.mark.parametrize("q,dup", [("Do you ship to Canada?", True), ("do you ship to canada", True),
                                   ("How do refunds work?", False)])
def test_is_duplicate(q, dup):
    assert is_duplicate(q, ["Do you ship to Canada?!"]) is dup
