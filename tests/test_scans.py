"""Scans: each Run of a chatbot is one numbered scan (v1, v2, ...) with one
overall result, its metric breakdown, and a comparison with another scan."""
from backend import storage
from backend.dashboard import scans
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures


def _result(key, status, score, direction="higher", model="judge-a"):
    return {"key": key, "status": status, "score": score, "threshold": 0.7, "direction": direction,
            "reason": f"{key} reason", "judge": {"model": model, "tokens": 100, "calls": 2}}


def _scan(conn, target_id, results, status="done", kind=None):
    job_id = storage.add_job(conn, target_id, [r["key"] for r in results], {"kind": kind} if kind else {})
    storage.update_job(conn, job_id, status=status, results=results,
                       started_at="2026-10-22T10:00:00Z", finished_at="2026-10-22T10:05:00Z")
    return job_id


def test_scans_are_numbered_per_chatbot_with_one_overall_result(tmp_path):
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    other = storage.add_target(conn, "other", "http", {})
    _scan(conn, bot, [_result("faithfulness", "pass", 0.9), _result("toxicity", "pass", 0.0, "lower")])
    _scan(conn, other, [_result("faithfulness", "fail", 0.2)])
    _scan(conn, bot, [_result("faithfulness", "fail", 0.5), _result("toxicity", "error", None, "lower")])
    _scan(conn, bot, [_result("pairwise", "pass", 1.0)], kind="pairwise")  # not a scan of this bot

    latest, first = scans.list_scans(conn, bot)
    assert (latest["version"], first["version"]) == (2, 1)
    assert first["result"] == "passed" and first["passed"] == 2 and first["failed"] == 0
    assert latest["result"] == "failed" and latest["failed"] == 1 and latest["errors"] == 1
    assert latest["metrics"] == 2 and latest["judge_model"] == "judge-a"
    assert first["average"] == 0.95  # toxicity 0.0 (lower is better) counts as 1.0
    assert scans.list_scans(conn, other)[0]["version"] == 1


def test_scan_detail_and_compare(tmp_path):
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    v1 = _scan(conn, bot, [_result("faithfulness", "pass", 0.9), _result("bias", "fail", 0.6, "lower")])
    v2 = _scan(conn, bot, [_result("faithfulness", "fail", 0.5), _result("bias", "pass", 0.1, "lower"),
                           _result("toxicity", "pass", 0.0, "lower")])
    detail = scans.get_scan(conn, v2)
    assert detail["version"] == 2 and [r["key"] for r in detail["results"]] == ["faithfulness", "bias", "toxicity"]
    rows = {r["key"]: r for r in scans.compare_scans(conn, v2, v1)["metrics"]}
    assert rows["faithfulness"]["change"] == "worse"
    assert rows["bias"]["change"] == "better"  # lower is better
    assert rows["toxicity"]["change"] == "new"


def test_scan_endpoints(app_module, client):
    bot = _create_target(client, name="Scan Bot", config={"url": "http://127.0.0.1:9/scan"})
    assert client.get(f"/api/targets/{bot['id']}/scans").json() == []
    assert client.get("/api/scans/999999").status_code == 404
    first = _scan(app_module._conn, bot["id"], [_result("faithfulness", "pass", 0.9)])
    second = _scan(app_module._conn, bot["id"], [_result("faithfulness", "fail", 0.4)])
    listed = client.get(f"/api/targets/{bot['id']}/scans").json()
    assert [s["version"] for s in listed] == [2, 1]
    assert client.get(f"/api/scans/{second}").json()["results"][0]["score"] == 0.4
    compared = client.get(f"/api/scans/{second}/compare", params={"with": first}).json()
    assert compared["metrics"][0]["change"] == "worse"
    # "Delete all" in History removes the finished scans too, not only the runs.
    client.delete(f"/api/targets/{bot['id']}/runs")
    assert client.get(f"/api/targets/{bot['id']}/scans").json() == []


def test_scan_counts_test_cases_the_deepeval_way(tmp_path):
    """A test case passes only when every metric that scored it passes."""
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    row = lambda gid, passed: {"golden_id": gid, "input": gid, "score": 0.9 if passed else 0.1, "passed": passed}  # noqa: E731
    _scan(conn, bot, [
        {**_result("faithfulness", "fail", 0.5), "rows": [row("g1", True), row("g2", False)]},
        {**_result("toxicity", "pass", 0.0, "lower"), "rows": [row("g1", True), row("g2", True)]},
    ])
    scan = scans.list_scans(conn, bot)[0]
    assert (scan["test_cases"], scan["test_cases_passed"]) == (2, 1)
    assert scan["skipped"] == []


def test_scan_lists_the_metrics_it_did_not_run(tmp_path):
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    skipped = [{"key": "tool_correctness", "title": "Tool Correctness", "reason": "Not applicable: no tools"}]
    job_id = storage.add_job(conn, bot, ["faithfulness"], {"skipped": skipped})
    storage.update_job(conn, job_id, status="done", results=[_result("faithfulness", "pass", 0.9)])
    assert scans.get_scan(conn, job_id)["skipped"] == skipped


def test_scan_lists_each_metrics_score_and_spread(tmp_path):
    """The runs table shows each metric's score; the chart its spread over the cases."""
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    rows = [{"golden_id": "g1", "score": 0.6, "passed": False}, {"golden_id": "g2", "score": 1.0, "passed": True}]
    _scan(conn, bot, [{**_result("faithfulness", "fail", 0.8), "rows": rows},
                      _result("toxicity", "error", None, "lower")])
    faith, tox = scans.list_scans(conn, bot)[0]["metric_scores"]
    assert faith == {"key": "faithfulness", "score": 0.8, "status": "fail", "direction": "higher",
                     "std": 0.2, "cases": 2}
    assert tox["score"] is None and tox["std"] is None and tox["cases"] == 0


def test_scan_detail_carries_its_run_options(tmp_path):
    conn = storage.init_db(str(tmp_path / "s.db"))
    bot = storage.add_target(conn, "bot", "http", {})
    job_id = storage.add_job(conn, bot, ["faithfulness"], {"limit": 1, "metric_concurrency": 3,
                                                          "deepeval": {"strict_mode": True}, "skipped": []})
    storage.update_job(conn, job_id, status="done", results=[_result("faithfulness", "pass", 0.9)])
    assert scans.get_scan(conn, job_id)["run_options"] == {"limit": 1, "metric_concurrency": 3,
                                                            "deepeval": {"strict_mode": True}}


def test_delete_one_scan_removes_its_job_and_runs(app_module, client):
    bot = _create_target(client, name="Delete Bot", config={"url": "http://127.0.0.1:9/del"})
    conn = app_module._conn
    keep = _scan(conn, bot["id"], [_result("faithfulness", "pass", 0.9)])
    gone = _scan(conn, bot["id"], [_result("faithfulness", "fail", 0.4)])
    storage.update_job(conn, gone, started_at="2026-10-23T10:00:00Z", finished_at="2026-10-23T10:05:00Z")
    storage.record_run(conn, bot["id"], "faithfulness", 0.4, False, "2026-10-23T10:03:00Z")  # in the scan
    storage.record_run(conn, bot["id"], "faithfulness", 0.9, True, "2026-10-22T10:03:00Z")   # another scan's
    assert client.delete(f"/api/scans/{gone}").json() == {"deleted": gone, "runs_deleted": 1}
    assert [s["job_id"] for s in client.get(f"/api/targets/{bot['id']}/scans").json()] == [keep]
    assert [r[0] for r in conn.execute("SELECT score FROM runs WHERE target_id = ?", (bot["id"],))] == [0.9]
    assert client.delete(f"/api/scans/{gone}").status_code == 404
    running = storage.add_job(conn, bot["id"], ["faithfulness"], {})
    assert client.delete(f"/api/scans/{running}").status_code == 409
