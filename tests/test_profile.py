"""Profile step: target kind, tools learned from replies, persona. Offline."""
import json

import pytest

from backend.datasets import autogen
from backend.datasets.build_kit import BudgetSpent
from backend.datasets.profile import PROBE_TOOL_MESSAGES, Profile, observe, profile_target
from backend.targets.base import ChatbotClient, ChatReply


class PlainBot(ChatbotClient):
    def __init__(self):
        self.asked = []

    def chat(self, message, history=None):
        self.asked.append(message)
        return ChatReply("hello", "m", "http")


class ToolBot(PlainBot):
    def __init__(self, calls_tool_on):
        super().__init__()
        self.trigger = calls_tool_on

    def chat(self, message, history=None):
        self.asked.append(message)
        calls = [{"name": "lookup_order", "args": {"order_id": "1"}, "reasoning": "needs data"}] \
            if self.trigger in message else None
        return ChatReply("ok", "m", "http", tools_called=calls)


class FakeMcp(PlainBot):
    def __init__(self, tools):
        super().__init__()
        self.tools = tools

    def list_tools(self):
        return self.tools


class FakeJudge:
    def __init__(self, tool_probe=None, persona="P."):
        self.tool_probe, self.persona, self.calls = tool_probe, persona, []

    def generate(self, prompt):
        self.calls.append(prompt)
        if '"messages"' in prompt:
            return json.dumps({"messages": self.tool_probe}), 0.0
        return json.dumps({"persona": self.persona}), 0.0


def test_tools_learned_from_replies():
    p = Profile()
    observe(p, ChatReply("ok", "m", "http", tools_called=[{"name": "lookup_order", "input_parameters": {"order_id": "1"}}]))
    assert p.tools == [{"name": "lookup_order", "description": "", "parameters": ["order_id"]}]
    assert p.tools_seen and not p.tool_reasoning_seen


def test_observe_falls_back_to_args_and_reads_reasoning_and_sources():
    p = Profile()
    observe(p, ChatReply("ok", "m", "http", retrieval_context=["doc"],
                         tools_called=[{"name": "t", "args": {"a": 1}, "reasoning": "why"}]))
    observe(p, ChatReply("ok", "m", "http", tools_called=[{"name": "t"}]))
    assert p.tools == [{"name": "t", "description": "", "parameters": ["a"]}]
    assert p.tool_reasoning_seen and p.sources_seen


def test_probe_messages_sent_only_when_tools_path_and_none_seen():
    bot = ToolBot(calls_tool_on="ORD-1001")
    judge = FakeJudge(tool_probe=["Where is ORD-1001?", "Hi", "Price of SKU-1?"], persona="Shop support bot.")
    p = profile_target(bot, judge, {"tools_path": "tools_called"}, False, "material", Profile())
    assert PROBE_TOOL_MESSAGES == 3
    assert bot.asked == ["Where is ORD-1001?", "Hi", "Price of SKU-1?"]
    assert p.kind == "agent" and p.persona == "Shop support bot."
    assert p.tool_reasoning_seen


def test_no_probe_without_tools_path():
    bot = PlainBot()
    profile_target(bot, FakeJudge(tool_probe=["a"]), {}, False, "m", Profile())
    assert bot.asked == []


def test_typed_persona_kept_without_judge_call():
    judge = FakeJudge()
    p = profile_target(PlainBot(), judge, {"persona": "Typed."}, False, "", Profile())
    assert p.persona == "Typed." and judge.calls == [] and p.kind == "chat"


def test_rag_kind():
    assert profile_target(PlainBot(), FakeJudge(persona="X."), {"context_path": "sources"}, False, "", Profile()).kind == "rag"
    assert profile_target(PlainBot(), FakeJudge(persona="X."), {}, False, "", Profile(sources_seen=True)).kind == "rag"


def test_mcp_kind_and_tools_from_list_tools():
    p = profile_target(FakeMcp(tools=[{"name": "echo"}]), FakeJudge(persona="X."), {}, True, "", Profile())
    assert p.kind == "mcp" and [t["name"] for t in p.tools] == ["echo"]


def test_known_tools_deduped_by_name():
    cfg = {"available_tools": [{"name": "a", "description": "d"}, "b"]}
    p = Profile()
    observe(p, ChatReply("ok", "m", "http", tools_called=[{"name": "a"}]))
    p = profile_target(PlainBot(), FakeJudge(persona="X."), cfg, False, "", p)
    assert sorted(t["name"] for t in p.tools) == ["a", "b"] and p.kind == "agent"


def test_budget_spent_propagates_from_probe_and_bad_reply_is_skipped():
    class Spent(PlainBot):
        def chat(self, message, history=None):
            raise BudgetSpent("out")
    with pytest.raises(BudgetSpent):
        profile_target(Spent(), FakeJudge(tool_probe=["a"]), {"tools_path": "t"}, False, "m", Profile())

    class Broken(PlainBot):
        def chat(self, message, history=None):
            raise ValueError("boom")
    p = profile_target(Broken(), FakeJudge(tool_probe=["a", "b"]), {"tools_path": "t"}, False, "m", Profile())
    assert p.persona == "P."


def test_run_cancelled_propagates_from_probe():
    from backend.cancel import RunCancelled

    class Stopped(PlainBot):
        def chat(self, message, history=None):
            raise RunCancelled("stop")
    with pytest.raises(RunCancelled):
        profile_target(Stopped(), FakeJudge(tool_probe=["a"]), {"tools_path": "t"}, False, "m", Profile())


def test_interview_calls_on_reply():
    got = []
    answers = autogen.interview(PlainBot(), on_reply=got.append)
    assert len(got) == len(answers) == len(autogen.INTERVIEW)


def test_no_probe_when_the_config_already_lists_tools():
    bot = ToolBot(calls_tool_on="ORD-1001")
    judge = FakeJudge(tool_probe=["Where is ORD-1001?"], persona="Shop support bot.")
    config = {"tools_path": "tools_called", "persona": "Shop bot.",
              "available_tools": [{"name": "lookup_order", "parameters": []}]}
    p = profile_target(bot, judge, config, False, "material", Profile())
    assert bot.asked == [] and judge.calls == [] and p.kind == "agent"
