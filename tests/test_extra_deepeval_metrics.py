"""DeepEval metrics added for full framework coverage: Misuse, Non-Advice,
Role Violation, Topic Adherence, Goal Accuracy, Tool Use, Tool Permission,
Agent Loop Detection, Pattern Match, JSON Correctness. Offline, no judge tokens."""
import pytest

from backend import metrics_catalog as mc

GOLDEN = {"id": "g1", "question": "Where is ORD-1001?", "expected_answer": "", "context": []}


def test_text_settings_reach_the_deepeval_metric(monkeypatch):
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    from backend.judges import judge
    j = judge.build_judge()
    misuse = mc.get_spec("misuse")
    metric = misuse.build_metric(j, **mc.metric_options(misuse, {"domain": "food delivery"}, {}))
    assert metric.domain == "food delivery"
    advice = mc.get_spec("non_advice_deepeval")
    metric = advice.build_metric(j, **mc.metric_options(advice, {"advice_types": "medical\nlegal"}, {}))
    assert metric.advice_types == ["medical", "legal"]
    topics = mc.get_spec("topic_adherence")
    metric = topics.build_metric(j, **mc.metric_options(topics, {"relevant_topics": "orders, returns"}, {}))
    assert metric.relevant_topics == ["orders", "returns"]
    tool_use = mc.get_spec("tool_use")
    metric = tool_use.build_metric(j, **mc.metric_options(tool_use, None, {"available_tools": ["lookup_order"]}))
    assert [t.name for t in metric.available_tools] == ["lookup_order"]


def test_pattern_match_is_a_full_match_without_a_judge():
    spec = mc.get_spec("pattern_match")
    metric = spec.build_metric(None, pattern=r"[\s\S]*ORD-\d+[\s\S]*")
    metric.measure(spec.build_case(GOLDEN, "Order ORD-1001 shipped."))
    assert metric.score == 1.0
    metric.measure(spec.build_case(GOLDEN, "It shipped."))
    assert metric.score == 0.0


def test_tool_permission_flags_a_denied_tool_and_needs_a_list():
    spec = mc.get_spec("tool_permission")
    with pytest.raises(ValueError, match="allow list or a deny list"):
        spec.build_metric(None)
    metric = spec.build_metric(None, denied_tools="delete_account")
    metric.measure(spec.build_case({**GOLDEN, "_tools": [{"name": "delete_account"}]}, "done"))
    assert metric.score == 0.0
    metric.measure(spec.build_case({**GOLDEN, "_tools": [{"name": "lookup_order"}]}, "done"))
    assert metric.score == 1.0
    assert spec.opt_in is True


def test_agent_loop_detection_reads_the_reported_tool_calls():
    spec = mc.get_spec("agent_loop_detection")
    metric = spec.build_metric(None)
    call = {"name": "lookup_order", "input_parameters": {"id": "ORD-1001"}}
    metric.measure(spec.build_case({**GOLDEN, "_tools": [call] * 5}, "ok"))
    assert metric.score < 1.0 and "5 times" in metric.reason
    metric.measure(spec.build_case({**GOLDEN, "_tools": [call]}, "ok"))
    assert metric.score == 1.0


def test_json_schema_model_and_bad_json():
    model = mc._schema_model('{"properties": {"status": {"type": "string"}}, "required": ["status"]}')
    assert model(status="shipped").status == "shipped"
    with pytest.raises(ValueError, match="not valid JSON"):
        mc._schema_model("{nope")


def test_conversation_case_carries_each_replys_tool_calls():
    turns = [{"role": "user", "content": "Track ORD-1001"},
             {"role": "assistant", "content": "Shipped.", "tools_called": [{"name": "lookup_order"}]}]
    case = mc.get_spec("tool_use").build_case({"name": "s"}, turns)
    assert case.turns[0].tools_called is None
    assert [t.name for t in case.turns[1].tools_called] == ["lookup_order"]


def test_new_metrics_are_where_the_ui_expects_them():
    expect = {"misuse": "safety", "non_advice_deepeval": "safety", "role_violation_deepeval": "safety",
              "topic_adherence": "conversational", "goal_accuracy": "conversational", "tool_use": "agent",
              "tool_permission": "agent", "agent_loop_detection": "agent", "pattern_match": "reference",
              "json_correctness": "reference"}
    for key, category in expect.items():
        spec = mc.get_spec(key)
        assert spec.ui_category == category and spec.improve and spec.question.endswith("?"), key
    assert mc.get_spec("tool_use").applies({"tools_path": False}, []) is not None


def test_mcp_metrics_read_the_server_and_its_tool_calls():
    server = {"server_name": "shop", "transport": "stdio",
              "available_tools": [{"name": "lookup_order", "description": "Find an order", "inputSchema": {}}]}
    item = {**GOLDEN, "_mcp_server": server,
            "_tools": [{"name": "lookup_order", "input_parameters": {"id": "ORD-1001"}, "output": "shipped"}]}
    case = mc.get_spec("mcp_use").build_case(item, "Shipped.")
    assert case.mcp_servers[0].server_name == "shop"
    assert case.mcp_tools_called[0].name == "lookup_order" and case.mcp_tools_called[0].args == {"id": "ORD-1001"}
    turns = [{"role": "user", "content": "Track ORD-1001"},
             {"role": "assistant", "content": "Shipped.", "tools_called": item["_tools"]}]
    conv = mc.get_spec("mcp_task_completion").build_case({"name": "s", "_mcp_server": server}, turns)
    assert conv.mcp_servers[0].available_tools[0]["name"] == "lookup_order"
    assert conv.turns[1].mcp_tools_called[0].result.content[0].text == "shipped"
    for key in ("mcp_use", "multi_turn_mcp_use", "mcp_task_completion"):
        assert mc.get_spec(key).applies({"mcp": False}, []) == mc.NEEDS_MCP
        assert mc.get_spec(key).applies({"mcp": True}, []) is None


def test_prompt_alignment_rules_and_their_source_are_described():
    from backend.targets import instructions
    rules = instructions.resolve({"persona": "Support bot", "context_path": "sources"})
    info = instructions.describe(rules)
    assert info["source"] == "default" and "Build all test data" in info["source_label"]
    assert info["rules"] == list(rules) and len(info["rules"]) > 9
    typed = instructions.describe(instructions.resolve({"instructions": "Answer in English.\nBe brief."}))
    assert typed == {"source": "chatbot", "source_label": instructions.SOURCE_LABELS["chatbot"],
                     "rules": ["Answer in English.", "Be brief."]}
    assert instructions.describe(None) is None
