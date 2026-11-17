"""OWASP ids the chat can only partly test carry `partial` and a one-line reason.

Supply chain, data poisoning, unbounded consumption, cascading failures, audit
logs and shadow servers live mostly outside the conversation: chat attacks
check only part of them, so the coverage views must never show them as fully
covered. The field is additive: every other id says partial False, no reason.
"""
import json

from backend import redteam_coverage
from tests.test_app import _create_target, app_module, client  # noqa: F401  (fixtures)

PARTIAL = {"LLM03", "LLM04", "LLM10", "ASI04", "ASI08", "MCP04", "MCP08", "MCP09"}
ITEM_KEYS = {"id", "title", "metrics"}
VIEW_KEYS = ITEM_KEYS | {"tested", "total", "attacks", "resisted", "pass_rate"}


def _items(frameworks):
    """{id: item} over every list, from frameworks() or coverage_report()['frameworks']."""
    lists = frameworks.values() if isinstance(frameworks, dict) else frameworks
    return {item["id"]: item for fw in lists for item in fw["items"]}


def _run(key, rows):
    return {"metric_key": key, "score": 1.0, "passed": 1, "ts": "2026-11-01T10:00:00Z",
            "result_json": json.dumps({"rows": rows})}


def test_partly_testable_ids_carry_partial_and_a_one_line_reason():
    items = _items(redteam_coverage.frameworks())
    assert {rid for rid, item in items.items() if item["partial"]} == PARTIAL
    assert set(redteam_coverage.PARTLY_TESTABLE) == PARTIAL
    for rid in PARTIAL:
        reason = items[rid]["partial_reason"]
        assert isinstance(reason, str) and reason.strip(), rid
        assert "\n" not in reason and len(reason) <= 200, rid
        assert reason == redteam_coverage.PARTLY_TESTABLE[rid]


def test_every_other_id_is_partial_false_without_a_reason():
    owasp = {k: v for k, v in redteam_coverage.frameworks().items() if k.startswith("owasp")}
    items = _items(owasp)
    assert len(items) == 30
    for rid, item in items.items():
        assert isinstance(item["partial"], bool), rid
        assert ITEM_KEYS <= set(item), rid  # the existing fields stay
        if rid not in PARTIAL:
            assert item["partial"] is False and "partial_reason" not in item, rid


def test_coverage_report_frameworks_view_keeps_partial_on_tested_ids():
    # Bias tests LLM03 (supply chain) and LLM04 (poisoning) in DeepTeam's map.
    report = redteam_coverage.coverage_report([_run("bias", [{"passed": True}, {"passed": True}])], {"bias": "Bias"})
    items = _items(report["frameworks"])
    for rid in ("LLM03", "LLM04"):
        item = items[rid]
        assert item["tested"] >= 1 and item["pass_rate"] == 1.0, rid
        # Every attack resisted is still only partial coverage.
        assert item["partial"] is True, rid
        assert item["partial_reason"] == redteam_coverage.PARTLY_TESTABLE[rid]
    for rid, item in items.items():
        assert VIEW_KEYS <= set(item), rid
        assert item["partial"] is (rid in PARTIAL), rid
        assert ("partial_reason" in item) is (rid in PARTIAL), rid


def test_api_serves_partial_in_frameworks_and_coverage(client):  # noqa: F811
    served = _items(client.get("/api/security/frameworks").json()["frameworks"])
    assert served["MCP08"]["partial"] is True and served["MCP08"]["partial_reason"]
    assert served["MCP05"]["partial"] is False and "partial_reason" not in served["MCP05"]
    target = _create_target(client)
    view = _items(client.get(f"/api/security/coverage?target_id={target['id']}").json()["frameworks"])
    assert view["ASI08"]["partial"] is True and view["ASI08"]["tested"] == 0
    assert view["ASI01"]["partial"] is False
