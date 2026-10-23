"""Case-by-case regressions between two scans: every test case matched across
the scans by its golden (else its input), with each metric's verdict on both
sides, flagged improved / regressed / unchanged."""
from backend import storage
from backend.dashboard import scans
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures
from tests.test_scans import _result, _scan


def _row(golden, passed, score, reply="reply", tools=None):
    row = {"golden_id": golden, "question": f"question {golden}", "input": f"question {golden}",
           "actual_output": reply, "expected_output": f"expected {golden}", "score": score,
           "passed": passed, "reason": "because"}
    if tools:
        row["tools_called"] = tools
    return row


def _metric(key, rows, status="pass"):
    return {**_result(key, status, 0.8), "rows": rows}


def test_cases_are_matched_across_scans_with_every_metric(tmp_path):
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    before = _scan(conn, bot, [
        _metric("answer_relevancy", [_row("g1", True, 0.9), _row("g2", True, 0.9), _row("g3", False, 0.2)]),
        _metric("correctness", [_row("g1", True, 0.8), _row("g2", True, 0.9)]),
    ])
    now = _scan(conn, bot, [
        _metric("answer_relevancy", [_row("g1", True, 0.9, reply="new reply"), _row("g2", True, 0.9),
                                     _row("g3", True, 0.8), _row("g4", True, 1.0)]),
        _metric("correctness", [_row("g1", False, 0.2, tools=[{"name": "lookup_order"}]), _row("g2", True, 0.9)]),
    ])
    diff = scans.compare_scan_cases(conn, now, before)
    cases = {c["key"]: c for c in diff["cases"]}
    assert cases["id:g1"]["change"] == "regressed"  # correctness pass -> fail
    assert cases["id:g2"]["change"] == "same"
    assert cases["id:g3"]["change"] == "improved"
    assert cases["id:g4"]["change"] == "new"
    g1 = cases["id:g1"]
    assert g1["input"] == "question g1"
    assert [(m["key"], m["passed"]) for m in g1["now"]["metrics"]] == [("answer_relevancy", True), ("correctness", False)]
    # The reply shown is the one the metric whose verdict changed (correctness) scored.
    assert g1["now"]["actual_output"] == "reply"
    assert [m["actual_output"] for m in g1["now"]["metrics"]] == ["new reply", "reply"]
    assert g1["now"]["tools_called"] == [{"name": "lookup_order"}]
    assert g1["before"]["expected_output"] == "expected g1"
    assert diff["counts"] == {"regressed": 1, "improved": 1, "same": 1, "new": 1, "removed": 0}
    assert diff["scan"]["version"] == 2 and diff["other"]["version"] == 1


def test_cases_endpoint(app_module, client):
    bot = _create_target(client, name="Case Bot", config={"url": "http://127.0.0.1:9/cases"})
    first = _scan(app_module._conn, bot["id"], [_metric("faithfulness", [_row("g1", True, 0.9)])])
    second = _scan(app_module._conn, bot["id"], [_metric("faithfulness", [_row("g1", False, 0.3)])])
    body = client.get(f"/api/scans/{second}/cases?with={first}").json()
    assert body["cases"][0]["change"] == "regressed"
    assert client.get(f"/api/scans/{second}/cases?with=999999").status_code == 404
