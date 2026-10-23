"""Prompt Alignment without hand-written instructions: the chatbot's own rules
come from its captured system prompt, else from standard production rules
fitted to what the chatbot is (RAG, agent, a role)."""
from backend import storage
from backend.dashboard.runner import run_spec
from backend.metrics_catalog import get_spec, metric_options
from backend.targets import instructions
from tests.fakes import CannedChatbot
from tests.test_adaptive_metrics import _recording_metric, _with

SPEC = get_spec("prompt_alignment")


def test_users_own_instructions_win():
    rules = metric_options(SPEC, None, {"instructions": ["Always greet by name"],
                                        "body_template": '{"system": "Be terse"}'})["instructions"]
    assert list(rules) == ["Always greet by name"] and rules.source == "chatbot"


def test_system_prompt_in_the_captured_request_is_used_next():
    body = '{"messages": [{"role": "system", "content": "Only talk about coffee.\\nBe brief."}, {"role": "user", "content": "{{message}}"}]}'
    rules = metric_options(SPEC, None, {"body_template": body})["instructions"]
    assert list(rules) == ["Only talk about coffee.", "Be brief."] and rules.source == "request"


def test_standard_rules_fit_the_kind_of_chatbot():
    plain = metric_options(SPEC, None, {})["instructions"]
    assert plain.source == "default" and list(plain) == instructions.BASE_RULES
    full = metric_options(SPEC, None, {"context_path": "sources", "tools_path": "tool_calls",
                                       "persona": "ShopEasy support bot"})["instructions"]
    assert set(instructions.RAG_RULES) <= set(full) and set(instructions.AGENT_RULES) <= set(full)
    assert any("ShopEasy support bot" in rule for rule in full)


def test_run_without_instructions_uses_standard_rules_and_says_so(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    target_id = storage.add_target(conn, "bot", "http", {})
    metric = _recording_metric()
    seen = {}
    spec = _with(SPEC, items=[{"id": "g1", "question": "What is your refund window?", "expected_answer": "x", "context": []}])
    spec.build_metric = lambda judge, threshold=0.7, **kw: seen.update(kw) or metric
    result = run_spec(spec, judge=object(), target=CannedChatbot(), target_id=target_id, conn=conn,
                      metric_kwargs=metric_options(SPEC, None, {}))
    assert result["status"] == "pass", result
    assert list(seen["instructions"]) == instructions.BASE_RULES
    assert "standard production rules" in result["note"]
