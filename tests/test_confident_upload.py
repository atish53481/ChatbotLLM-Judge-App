"""Send a finished scan to Confident AI as a DeepEval test run: built from the
stored results (nothing judged again), uploaded only when the user asks, and
the API key never leaves the backend. The Confident AI API is faked here."""
from backend import confident_upload, storage
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures


def _row(gid, score, passed, **extra):
    return {"golden_id": gid, "input": f"question {gid}", "actual_output": f"reply {gid}",
            "expected_output": f"expected {gid}", "context": [f"fact {gid}"], "score": score,
            "passed": passed, "reason": f"why {gid}", "latency_s": 1.5, **extra}


def _scan_results():
    return [
        {"key": "faithfulness", "status": "fail", "score": 0.6, "threshold": 0.7, "direction": "higher",
         "judge": {"model": "judge-x (Local Model)"}, "judge_cost_usd": 0.002,
         "rows": [_row("g1", 1.0, True, tools_called=[{"name": "lookup", "input_parameters": {"id": 1}}]),
                  _row("g2", 0.2, False)]},
        {"key": "toxicity", "status": "pass", "score": 1.0, "threshold": 0.5, "direction": "higher",
         "judge": {"model": "judge-x"}, "rows": [_row("g1", 1.0, True), _row("g2", 1.0, True)]},
    ]


def _scan(conn, target_id, status="done"):
    job_id = storage.add_job(conn, target_id, ["faithfulness", "toxicity"], {"limit": 2})
    storage.update_job(conn, job_id, status=status, results=_scan_results(),
                       started_at="2026-10-25T10:00:00Z", finished_at="2026-10-25T10:01:30Z")
    return job_id


class FakeApi:
    calls: list = []

    def __init__(self, api_key=None):
        FakeApi.key = api_key

    def send_request(self, method, endpoint, body=None, **_):
        FakeApi.calls.append((method.value, endpoint.value, body))
        return {"id": "tr_123"}, "https://app.confident-ai.com/project/p/evaluation/test-runs/tr_123"


def test_scan_becomes_a_deepeval_test_run(tmp_path):
    conn = storage.init_db(str(tmp_path / "c.db"))
    bot = storage.add_target(conn, "Shop bot", "http", {"theme": "general_support"})
    from backend.dashboard import scans
    scan = scans.get_scan(conn, _scan(conn, bot))
    body = confident_upload.test_run_body(scan, "Shop bot", "general_support",
                                          titles={"faithfulness": "Faithfulness", "toxicity": "Toxicity"})
    assert body["identifier"] == "Shop bot · v1" and body["datasetAlias"] == "general_support"
    assert (body["testPassed"], body["testFailed"]) == (1, 1)  # g2 fails Faithfulness
    assert body["runDuration"] == 90.0 and body["evaluationCost"] == 0.002
    assert body["hyperparameters"]["judge model"] == "judge-x"
    first, second = body["testCases"]
    assert first["input"] == "question g1" and first["retrievalContext"] == ["fact g1"]
    assert first["toolsCalled"][0]["name"] == "lookup"
    assert [m["name"] for m in first["metricsData"]] == ["Faithfulness", "Toxicity"]
    assert second["success"] is False and second["metricsData"][0]["reason"] == "why g2"
    scores = {m["metric"]: m for m in body["metricsScores"]}
    assert scores["Faithfulness"] == {"metric": "Faithfulness", "scores": [1.0, 0.2], "passes": 1, "fails": 1, "errors": 0}


def test_settings_keep_the_key_on_the_backend(app_module, client, monkeypatch):
    monkeypatch.delenv("CONFIDENT_API_KEY", raising=False)
    client.put("/api/confident/settings", json={"clear_key": True, "auto": False})
    assert client.get("/api/confident/settings").json() == {"has_key": False, "key_source": None, "key_hint": "", "auto": False, "project": None}
    saved = client.put("/api/confident/settings", json={"api_key": " confident_us_secret1234 "}).json()
    # Saving a key turns automatic sending on unless the form says otherwise.
    assert saved == {"has_key": True, "key_source": "panel", "key_hint": "••••1234", "auto": True, "project": None}
    assert "secret" not in str(client.get("/api/confident/settings").json())
    assert client.put("/api/confident/settings", json={"auto": False}).json()["auto"] is False
    monkeypatch.setenv("CONFIDENT_API_KEY", "confident_env_key_9999")
    cleared = client.put("/api/confident/settings", json={"clear_key": True}).json()
    assert cleared["key_source"] == "env" and cleared["key_hint"] == "••••9999"
    client.put("/api/confident/settings", json={"clear_key": True, "auto": False})


def test_upload_endpoint(app_module, client, monkeypatch):
    bot = _create_target(client, name="Upload Bot", config={"url": "http://127.0.0.1:9/up"})
    job = _scan(app_module._conn, bot["id"])
    monkeypatch.delenv("CONFIDENT_API_KEY", raising=False)
    client.put("/api/confident/settings", json={"clear_key": True, "auto": False})
    assert client.post(f"/api/scans/{job}/confident").status_code == 400

    monkeypatch.setattr(confident_upload, "Api", FakeApi)
    FakeApi.calls = []
    client.put("/api/confident/settings", json={"api_key": "confident_us_secret", "auto": False})
    res = client.post(f"/api/scans/{job}/confident").json()
    assert res["link"].endswith("/tr_123") and res["test_cases"] == 2
    assert FakeApi.key == "confident_us_secret"
    assert [c[:2] for c in FakeApi.calls] == [("POST", "/v1/test-run")]
    assert "confident_us_secret" not in str(res)
    # The link is kept with the scan, so the page can offer "View in Confident AI".
    assert client.get(f"/api/scans/{job}").json()["confident"]["link"] == res["link"]
    client.put("/api/confident/settings", json={"clear_key": True, "auto": False})


def test_finished_runs_are_sent_only_when_auto_is_on(app_module, client, monkeypatch):
    monkeypatch.delenv("CONFIDENT_API_KEY", raising=False)
    monkeypatch.setattr(confident_upload, "Api", FakeApi)
    bot = _create_target(client, name="Auto Bot", config={"url": "http://127.0.0.1:9/auto"})
    FakeApi.calls = []
    client.put("/api/confident/settings", json={"api_key": "confident_us_auto", "auto": False})
    quiet = _scan(app_module._conn, bot["id"])
    app_module._after_job(quiet)
    assert FakeApi.calls == [] and storage.get_job(app_module._conn, quiet)["options"].get("confident") is None

    client.put("/api/confident/settings", json={"auto": True})
    sent = _scan(app_module._conn, bot["id"])
    app_module._after_job(sent)
    assert storage.get_job(app_module._conn, sent)["options"]["confident"]["link"].endswith("/tr_123")
    # A stopped run is not sent.
    stopped = _scan(app_module._conn, bot["id"], status="cancelled")
    FakeApi.calls = []
    app_module._after_job(stopped)
    assert FakeApi.calls == []
    client.put("/api/confident/settings", json={"clear_key": True, "auto": False})


def test_upload_refuses_unfinished_or_missing_scans(app_module, client, monkeypatch):
    monkeypatch.setenv("CONFIDENT_API_KEY", "confident_env_key")
    monkeypatch.setattr(confident_upload, "Api", FakeApi)
    bot = _create_target(client, name="Busy Bot", config={"url": "http://127.0.0.1:9/busy"})
    running = storage.add_job(app_module._conn, bot["id"], ["faithfulness"], {})
    assert client.post(f"/api/scans/{running}/confident").status_code == 409
    assert client.post("/api/scans/999999/confident").status_code == 404


def test_large_scans_go_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(confident_upload, "Api", FakeApi)
    FakeApi.calls = []
    rows = [_row(f"g{i}", 1.0, True) for i in range(45)]
    scan = {"version": 3, "results": [{"key": "toxicity", "status": "pass", "score": 1.0, "threshold": 0.5, "rows": rows}],
            "started_at": "2026-10-25T10:00:00Z", "finished_at": "2026-10-25T10:00:10Z", "run_options": {}}
    out = confident_upload.upload(scan, "bot", "generic", {}, api_key="k")
    assert out["test_cases"] == 45
    assert [(m, len((b or {}).get("testCases", []))) for m, _, b in FakeApi.calls] == [("POST", 40), ("PUT", 5)]
    assert FakeApi.calls[1][2]["testRunId"] == "tr_123"


class FakeHttp:
    """Confident AI's v2 API: list projects, create a project key."""
    calls: list = []

    def __init__(self, projects):
        self.projects = projects

    def get(self, url, headers=None, timeout=None):
        FakeHttp.calls.append(("GET", url, headers))
        return FakeResponse({"success": True, "data": {"projects": self.projects}})

    def post(self, url, headers=None, json=None, timeout=None):
        FakeHttp.calls.append(("POST", url, headers, json))
        return FakeResponse({"success": True, "data": {"id": 7, "value": "confident_us_proj_made_by_app_5678"}})


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def test_an_org_key_gets_a_project_key_for_the_named_project(app_module, client, monkeypatch):
    monkeypatch.delenv("CONFIDENT_API_KEY", raising=False)
    monkeypatch.setattr(confident_upload, "_http", FakeHttp([{"id": "p1", "name": "Other"}, {"id": "p2", "name": "Test"}]))
    FakeHttp.calls = []
    saved = client.put("/api/confident/settings",
                       json={"api_key": "confident_us_org_secretorgkey", "project": "test"}).json()
    assert saved["has_key"] and saved["key_hint"] == "••••5678" and saved["project"] == "Test"
    get, post = FakeHttp.calls
    assert get[1] == "https://api.confident-ai.com/v2/projects" and get[2]["CONFIDENT_API_KEY"] == "confident_us_org_secretorgkey"
    assert post[1] == "https://api.confident-ai.com/v2/projects/p2/api-keys" and post[3] == {"name": "LLM Judge app"}
    # Only the project key is kept; the org key is never stored.
    assert storage.get_meta(app_module._conn, "confident_api_key") == "confident_us_proj_made_by_app_5678"
    assert "secretorgkey" not in str(saved)
    client.put("/api/confident/settings", json={"clear_key": True, "auto": False})


def test_org_key_errors_say_what_to_do(app_module, client, monkeypatch):
    monkeypatch.setattr(confident_upload, "_http", FakeHttp([{"id": "p1", "name": "Other"}]))
    missing = client.put("/api/confident/settings", json={"api_key": "confident_us_org_x", "project": "Test"})
    assert missing.status_code == 400 and "Other" in missing.json()["detail"]
    no_name = client.put("/api/confident/settings", json={"api_key": "confident_us_org_x"})
    assert no_name.status_code == 400 and "project" in no_name.json()["detail"]
    assert storage.get_meta(app_module._conn, "confident_api_key") is None
