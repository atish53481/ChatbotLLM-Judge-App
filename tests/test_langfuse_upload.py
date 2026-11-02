"""Send a finished scan to Langfuse as one trace: a root span for the scan, a
span per metric run and per test case (OTLP/HTTP JSON), and the judge's scores
(score-create events). Nothing is judged again, nothing is sent without both
keys, and no key or chatbot header ever lands in a payload. Langfuse's HTTP API
is faked here (no network)."""
import json
import re

import pytest
import requests

from backend import langfuse_upload, storage
from backend.dashboard import scans
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures

PUBLIC, SECRET = "pk-lf-public-key-1234", "sk-lf-secret-key-abcd"
TITLES = {"faithfulness": "Faithfulness", "prompt_injection": "Prompt Injection",
          "multi_turn_jailbreak": "Multi-turn Jailbreak", "toxicity": "Toxicity"}
OPTIONS = {"limit": 2, "metric_concurrency": 2, "enhancements": ["base64"],
           "settings": {"multi_turn_jailbreak": {"strategy": "crescendo"}}}


def _row(gid, score, passed, **extra):
    return {"golden_id": gid, "question": f"question {gid}", "input": f"question {gid}",
            "actual_output": f"reply {gid}", "expected_output": f"expected {gid}", "context": [f"fact {gid}"],
            "context_source": "golden", "score": score, "passed": passed, "reason": f"why {gid}",
            "latency_s": 1.5, **extra}


def _results():
    return [
        {"key": "faithfulness", "status": "fail", "score": 0.6, "threshold": 0.7, "direction": "higher",
         "judge": {"model": "judge-x (Local Model)", "tokens": 1200, "calls": 4}, "duration_s": 10.0,
         "cases_run": 2, "cases_passed": 1, "note": "1 of 2 cases failed, so the run fails.",
         "rows": [_row("g1", 1.0, True, context_source="chatbot"), _row("g2", 0.2, False)]},
        {"key": "prompt_injection", "status": "pass", "score": 1.0, "threshold": 0.5, "direction": "higher",
         "judge": {"model": "judge-x", "tokens": 300, "calls": 2}, "duration_s": 4.0,
         "cases_run": 2, "cases_passed": 2,
         "rows": [_row("sp_1", 1.0, True, probe=True, enhancement=None, expected_output="", context=[]),
                  _row("sp_1~base64", 1.0, True, probe=True, enhancement="base64", base_probe_id="sp_1",
                       expected_output="", context=[], vulnerability_type="direct_injection")]},
        {"key": "multi_turn_jailbreak", "status": "pass", "score": 0.9, "threshold": 0.5, "direction": "higher",
         "judge": {"model": "judge-x", "tokens": 500, "calls": 5}, "duration_s": 6.0,
         "cases_run": 1, "cases_passed": 1,
         "rows": [_row("sp_2", 0.9, True, latency_s=None, expected_output="")]},
        {"key": "toxicity", "status": "error", "score": None, "threshold": 0.5, "direction": "higher",
         "error": "judge unreachable", "reason": "judge unreachable", "rows": []},
    ]


def _scan(conn, target_id, status="done", options=None):
    job_id = storage.add_job(conn, target_id, [r["key"] for r in _results()], dict(options or OPTIONS))
    storage.update_job(conn, job_id, status=status, results=_results(),
                       started_at="2026-10-25T10:00:00Z", finished_at="2026-10-25T10:00:20Z")
    return job_id


def _value(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    return [_value(v) for v in value["arrayValue"].get("values", [])]


def _attrs(span) -> dict:
    return {a["key"]: _value(a["value"]) for a in span["attributes"]}


def _meta(span, key):
    return _attrs(span).get(f"langfuse.observation.metadata.{key}")


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body = status, body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeLangfuse:
    """Langfuse's OTLP traces endpoint and its ingestion API; records every post."""

    def __init__(self, otel=None, ingestion=None):
        self.calls, self.otel, self.ingestion = [], otel, ingestion

    def post(self, url, json=None, auth=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "auth": auth, "headers": dict(headers or {})})
        if url.endswith("/api/public/otel/v1/traces"):
            return self.otel or FakeResponse(200, {})
        if self.ingestion is not None:
            return self.ingestion
        return FakeResponse(207, {"successes": [{"id": e["id"], "status": 201} for e in json["batch"]],
                                  "errors": []})


def _spans(calls):
    return [s for c in calls if c["url"].endswith("/otel/v1/traces")
            for rs in c["json"]["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


def _score_events(calls):
    return [e for c in calls if c["url"].endswith("/api/public/ingestion") for e in c["json"]["batch"]]


def _stored_scan(tmp_path, name="Shop bot"):
    conn = storage.init_db(str(tmp_path / "lf.db"))
    bot = storage.add_target(conn, name, "http", {"theme": "general_support"})
    return scans.get_scan(conn, _scan(conn, bot))


def _trace(scan):
    return langfuse_upload.build_trace(
        scan, "Shop bot", "general_support", TITLES, settings=OPTIONS["settings"],
        attacks={"multi_turn_jailbreak": "Crescendo jailbreaking"}, enhancements={"base64": "Base64"}, parallel=2)


def test_a_scan_becomes_one_trace_with_a_span_per_metric_and_per_case(tmp_path):
    trace = _trace(_stored_scan(tmp_path))
    spans, trace_id = trace["spans"], trace["trace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
    assert {s["traceId"] for s in spans} == {trace_id}
    assert all(re.fullmatch(r"[0-9a-f]{16}", s["spanId"]) for s in spans)
    roots = [s for s in spans if not s.get("parentSpanId")]
    assert len(roots) == 1
    root = roots[0]
    attrs = _attrs(root)
    assert root["name"] == attrs["langfuse.trace.name"] == "LLM Judge scan 1 · Shop bot"
    # Input: the metrics and how the scan was run. Output: its overall result.
    trace_input = json.loads(attrs["langfuse.trace.input"])
    assert [m["key"] for m in trace_input["metrics"]] == list(TITLES)
    assert trace_input["chatbot"] == "Shop bot" and trace_input["golden_set"] == "general_support"
    assert trace_input["options"]["limit"] == 2 and trace_input["options"]["enhancements"] == ["base64"]
    assert trace_input["settings"] == {"multi_turn_jailbreak": {"strategy": "crescendo"}}
    assert json.loads(attrs["langfuse.trace.output"])["result"] == "failed"
    assert attrs["langfuse.observation.input"] == attrs["langfuse.trace.input"]  # Langfuse v4 reads the root's
    assert attrs["langfuse.trace.metadata.judge_model"] == "judge-x"
    assert attrs["langfuse.trace.metadata.judge_tokens"] == 2000
    assert attrs["langfuse.trace.metadata.duration_s"] == 20.0
    assert "llm-judge" in attrs["langfuse.trace.tags"]

    metric_spans = [s for s in spans if s.get("parentSpanId") == root["spanId"]]
    assert [s["name"] for s in metric_spans] == list(TITLES.values())
    faith = metric_spans[0]
    assert _meta(faith, "score") == 0.6 and _meta(faith, "passed") is False and _meta(faith, "threshold") == 0.7
    assert _meta(faith, "judge_model") == "judge-x" and _meta(faith, "judge_tokens") == 1200
    assert _meta(faith, "duration_s") == 10.0 and _meta(faith, "note") == "1 of 2 cases failed, so the run fails."
    assert _attrs(faith)["langfuse.observation.level"] == "WARNING"
    toxicity = _attrs(metric_spans[3])
    assert toxicity["langfuse.observation.level"] == "ERROR"
    assert toxicity["langfuse.observation.status_message"] == "judge unreachable"
    # Two metrics at a time, each next one starting when one finished (as the job ran them).
    t0 = int(root["startTimeUnixNano"])
    assert [(int(s["startTimeUnixNano"]) - t0) / 1e9 for s in metric_spans] == [0, 0, 4, 10]
    assert (int(faith["endTimeUnixNano"]) - t0) / 1e9 == 10

    cases = {m["name"]: [s for s in spans if s.get("parentSpanId") == m["spanId"]] for m in metric_spans}
    assert [len(c) for c in cases.values()] == [2, 2, 1, 0]
    g1, g2 = cases["Faithfulness"]
    assert _attrs(g2)["langfuse.observation.input"] == "question g2"  # what was sent to the chatbot
    assert _attrs(g2)["langfuse.observation.output"] == "reply g2"
    assert _meta(g2, "expected_output") == "expected g2" and _meta(g2, "context") == ["fact g2"]
    assert _meta(g2, "context_source") == "golden" and _meta(g1, "context_source") == "chatbot"
    assert _meta(g2, "latency_s") == 1.5 and _meta(g2, "golden_id") == "g2"
    assert _attrs(g2)["langfuse.observation.level"] == "WARNING"
    assert _attrs(g1)["langfuse.observation.level"] == "DEFAULT"
    assert (int(g2["endTimeUnixNano"]) - int(g2["startTimeUnixNano"])) / 1e9 == 1.5  # the reply's latency
    baseline, base64 = cases["Prompt Injection"]
    assert _meta(baseline, "attack") == "Baseline"
    assert _meta(baseline, "expected_output") is None and _meta(baseline, "context") is None  # empty: left out
    assert _meta(base64, "attack") == "Base64" and _meta(base64, "enhancement") == "base64"
    assert _meta(base64, "base_probe_id") == "sp_1" and _meta(base64, "vulnerability_type") == "direct_injection"
    jailbreak = cases["Multi-turn Jailbreak"][0]
    assert _meta(jailbreak, "attack") == "Crescendo jailbreaking" and _meta(jailbreak, "latency_s") is None


def test_scores_one_per_metric_run_on_the_trace_and_one_per_case_on_its_span(tmp_path):
    scan = _stored_scan(tmp_path)
    trace = _trace(scan)
    spans = {s["spanId"]: s for s in trace["spans"]}
    run_scores = [s for s in trace["scores"] if "observationId" not in s]
    # The errored metric has no score to send.
    assert [(s["name"], s["value"]) for s in run_scores] == [
        ("Faithfulness", 0.6), ("Prompt Injection", 1.0), ("Multi-turn Jailbreak", 0.9)]
    assert run_scores[0]["comment"].startswith("failed") and "1 of 2 cases failed" in run_scores[0]["comment"]
    assert run_scores[1]["comment"].startswith("passed")
    case_scores = [s for s in trace["scores"] if "observationId" in s]
    assert len(case_scores) == 5
    g2 = next(s for s in case_scores if _meta(spans[s["observationId"]], "golden_id") == "g2")
    assert (g2["name"], g2["value"], g2["comment"], g2["dataType"]) == ("Faithfulness", 0.2, "why g2", "NUMERIC")
    assert spans[g2["observationId"]]["parentSpanId"] in spans
    assert {s["traceId"] for s in trace["scores"]} == {trace["trace_id"]}
    # Ids come from the scan, so sending it again updates its trace and scores
    # in Langfuse instead of adding a second copy; another scan gets its own trace.
    same = _trace(json.loads(json.dumps(scan)))
    assert same["trace_id"] == trace["trace_id"]
    assert [s["spanId"] for s in same["spans"]] == [s["spanId"] for s in trace["spans"]]
    assert [s["id"] for s in same["scores"]] == [s["id"] for s in trace["scores"]]
    assert _trace(_stored_scan(tmp_path))["trace_id"] != trace["trace_id"]


def test_upload_sends_spans_over_otlp_then_scores_as_score_create_events(tmp_path, monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse_upload, "_http", fake)
    sent = langfuse_upload.upload(_stored_scan(tmp_path), "Shop bot", "general_support", TITLES,
                                  public_key=PUBLIC, secret_key=SECRET, host="https://langfuse.example.com/")
    otel, ingestion = fake.calls
    assert otel["url"] == "https://langfuse.example.com/api/public/otel/v1/traces"
    assert otel["auth"] == (PUBLIC, SECRET) and otel["headers"]["x-langfuse-ingestion-version"] == "4"
    assert len(_spans(fake.calls)) == 1 + 4 + 5
    assert ingestion["url"] == "https://langfuse.example.com/api/public/ingestion"
    assert ingestion["auth"] == (PUBLIC, SECRET)
    events = _score_events(fake.calls)
    assert {e["type"] for e in events} == {"score-create"} and len(events) == 3 + 5
    assert all(e["id"] and e["timestamp"].endswith("Z") and e["body"]["id"] for e in events)
    assert len({e["id"] for e in events}) == len(events)
    assert sent["url"] == f"https://langfuse.example.com/trace/{sent['trace_id']}"
    assert (sent["spans"], sent["scores"]) == (10, 8)
    # The keys travel only in the Authorization header, never in a payload.
    payload = json.dumps([c["json"] for c in fake.calls])
    assert PUBLIC not in payload and SECRET not in payload


def test_langfuse_cloud_is_the_default_and_any_host_works():
    assert langfuse_upload.normalize_host(None) == "https://cloud.langfuse.com"
    assert langfuse_upload.normalize_host("  ") == "https://cloud.langfuse.com"
    assert langfuse_upload.normalize_host("us.cloud.langfuse.com") == "https://us.cloud.langfuse.com"
    assert langfuse_upload.normalize_host("http://localhost:3000/") == "http://localhost:3000"
    assert langfuse_upload.normalize_host("https://lf.example.com/api/public/") == "https://lf.example.com"
    assert langfuse_upload.normalize_host("https://example.com/langfuse") == "https://example.com/langfuse"
    for bad in ("ftp://lf.example.com", "https://", "http:// spaces .com"):
        with pytest.raises(ValueError):
            langfuse_upload.normalize_host(bad)


def test_large_scans_go_in_several_requests(tmp_path, monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse_upload, "_http", fake)
    monkeypatch.setattr(langfuse_upload, "MAX_REQUEST_BYTES", 4000)
    rows = [_row(f"g{i}", 1.0, True) for i in range(30)]
    scan = {"version": 3, "job_id": 9, "target_id": 1, "result": "passed", "run_options": {},
            "started_at": "2026-10-25T10:00:00Z", "finished_at": "2026-10-25T10:00:10Z",
            "results": [{"key": "toxicity", "status": "pass", "score": 1.0, "threshold": 0.5, "rows": rows}]}
    sent = langfuse_upload.upload(scan, "bot", "generic", {}, public_key=PUBLIC, secret_key=SECRET)
    otel = [c for c in fake.calls if c["url"].endswith("/otel/v1/traces")]
    ingestion = [c for c in fake.calls if c["url"].endswith("/api/public/ingestion")]
    assert len(otel) > 1 and len(ingestion) > 1
    assert all(c["url"].startswith("https://cloud.langfuse.com/") for c in fake.calls)
    spans = _spans(fake.calls)
    assert len(spans) == len({s["spanId"] for s in spans}) == 1 + 1 + 30
    assert not _spans(otel[:1])[0].get("parentSpanId")  # the root goes first
    assert len(_score_events(fake.calls)) == sent["scores"] == 1 + 30


def test_refused_keys_and_wrong_hosts_raise_a_plain_error_without_the_keys(tmp_path, monkeypatch):
    scan = _stored_scan(tmp_path)
    monkeypatch.setattr(langfuse_upload, "_http", FakeLangfuse(
        otel=FakeResponse(401, {"message": "Invalid credentials. Confirm that you've configured the correct host."})))
    with pytest.raises(langfuse_upload.LangfuseError) as refused:
        langfuse_upload.upload(scan, "bot", "generic", TITLES, public_key=PUBLIC, secret_key=SECRET)
    assert "401" in str(refused.value) and "key" in str(refused.value)
    monkeypatch.setattr(langfuse_upload, "_http", FakeLangfuse(otel=FakeResponse(200, None, text="<html>hi</html>")))
    with pytest.raises(langfuse_upload.LangfuseError) as not_langfuse:
        langfuse_upload.upload(scan, "bot", "generic", TITLES, public_key=PUBLIC, secret_key=SECRET)
    assert "Langfuse" in str(not_langfuse.value)
    # Scores Langfuse rejects one by one (HTTP 207) leave the trace in place, with a warning.
    rejected = FakeResponse(207, {"successes": [], "errors": [{"id": "x", "status": 400, "message": "Invalid score"}]})
    monkeypatch.setattr(langfuse_upload, "_http", FakeLangfuse(ingestion=rejected))
    partial = langfuse_upload.upload(scan, "bot", "generic", TITLES, public_key=PUBLIC, secret_key=SECRET)
    assert partial["rejected_scores"] == 1 and "Invalid score" in partial["warning"]
    assert langfuse_upload.safe_message(f"failed for {SECRET} / {PUBLIC}", PUBLIC, SECRET) == "failed for *** / ***"


# --- The app: settings, the send endpoint and automatic sending -------------------

@pytest.fixture
def langfuse_off(app_module, client, monkeypatch):
    """Each test starts and ends without Langfuse keys (the test database is shared,
    so no other test may ever send anything), and sends run inline."""
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app_module, "_in_background", lambda work: work())
    client.put("/api/langfuse/settings", json={"clear_keys": True, "auto": False})
    yield
    client.put("/api/langfuse/settings", json={"clear_keys": True, "auto": False})


def test_settings_keep_the_keys_on_the_backend(client, langfuse_off, monkeypatch):
    assert client.get("/api/langfuse/settings").json() == {
        "has_keys": False, "key_source": None, "public_key_hint": "", "secret_key_hint": "",
        "host": "https://cloud.langfuse.com", "auto": False}
    saved = client.put("/api/langfuse/settings", json={
        "public_key": f" {PUBLIC} ", "secret_key": SECRET, "host": "https://lf.example.com/"}).json()
    # Saving keys turns automatic sending on unless the form says otherwise.
    assert saved == {"has_keys": True, "key_source": "panel", "public_key_hint": "••••1234",
                     "secret_key_hint": "••••abcd", "host": "https://lf.example.com", "auto": True}
    shown = str(client.get("/api/langfuse/settings").json())
    assert "public-key" not in shown and "secret-key" not in shown
    assert client.put("/api/langfuse/settings", json={"auto": False}).json()["auto"] is False
    # An empty host goes back to Langfuse Cloud; empty keys keep the saved ones.
    back = client.put("/api/langfuse/settings", json={"host": "", "public_key": "", "secret_key": ""}).json()
    assert back["host"] == "https://cloud.langfuse.com" and back["has_keys"] is True
    # Swapped keys, a host that is not a web address, or one key alone are refused.
    assert client.put("/api/langfuse/settings", json={"public_key": SECRET, "secret_key": PUBLIC}).status_code == 400
    assert client.put("/api/langfuse/settings", json={"host": "ftp://lf.example.com"}).status_code == 400
    client.put("/api/langfuse/settings", json={"clear_keys": True, "auto": False})
    alone = client.put("/api/langfuse/settings", json={"public_key": PUBLIC})
    assert alone.status_code == 400 and "secret" in alone.json()["detail"]
    # Keys in .env work too (not sent until automatic sending is turned on here).
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-from-env-5678")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-from-env-9999")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    env = client.get("/api/langfuse/settings").json()
    assert (env["key_source"], env["secret_key_hint"], env["host"], env["auto"]) == (
        "env", "••••9999", "http://localhost:3000", False)


def test_send_endpoint_sends_one_scan_and_keeps_its_link(app_module, client, langfuse_off, monkeypatch):
    bot = _create_target(client, name="Langfuse Bot", config={
        "url": "http://127.0.0.1:9/lf", "headers": {"Authorization": "Bearer chatbot-token-777",
                                                     "Cookie": "sid=cookie-888"}})
    job = _scan(app_module._conn, bot["id"])
    assert client.post(f"/api/scans/{job}/langfuse", json={}).status_code == 400  # no keys
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse_upload, "_http", fake)
    client.put("/api/langfuse/settings", json={"public_key": PUBLIC, "secret_key": SECRET, "auto": False})
    # Like every POST, it takes a JSON body, so another site's page cannot send one.
    assert client.post(f"/api/scans/{job}/langfuse").status_code == 422 and fake.calls == []
    res = client.post(f"/api/scans/{job}/langfuse", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["url"] == f"https://cloud.langfuse.com/trace/{body['trace_id']}"
    # Kept with the scan, so a page can offer "View in Langfuse".
    assert client.get(f"/api/scans/{job}").json()["langfuse"]["url"] == body["url"]
    root = next(s for s in _spans(fake.calls) if not s.get("parentSpanId"))
    assert root["name"].endswith("· Langfuse Bot")
    sent = json.dumps([c["json"] for c in fake.calls]) + json.dumps(body)
    for secret in (PUBLIC, SECRET, "chatbot-token-777", "cookie-888"):
        assert secret not in sent
    running = storage.add_job(app_module._conn, bot["id"], ["faithfulness"], {})
    assert client.post(f"/api/scans/{running}/langfuse", json={}).status_code == 409
    assert client.post("/api/scans/999999/langfuse", json={}).status_code == 404
    monkeypatch.setattr(langfuse_upload, "_http", FakeLangfuse(otel=FakeResponse(401, {"message": "Invalid credentials"})))
    refused = client.post(f"/api/scans/{job}/langfuse", json={})
    assert refused.status_code == 502 and SECRET not in refused.text


def test_finished_runs_are_sent_only_with_keys_and_auto_on(app_module, client, langfuse_off, monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse_upload, "_http", fake)
    conn = app_module._conn
    bot = _create_target(client, name="Auto Langfuse Bot", config={"url": "http://127.0.0.1:9/alf"})
    no_keys = _scan(conn, bot["id"])
    app_module._after_job(no_keys)
    assert fake.calls == [] and "langfuse" not in storage.get_job(conn, no_keys)["options"]
    client.put("/api/langfuse/settings", json={"public_key": PUBLIC, "secret_key": SECRET, "auto": False})
    quiet = _scan(conn, bot["id"])
    app_module._after_job(quiet)
    assert fake.calls == [] and "langfuse" not in storage.get_job(conn, quiet)["options"]

    client.put("/api/langfuse/settings", json={"auto": True})
    sent = _scan(conn, bot["id"])
    app_module._after_job(sent)
    kept = storage.get_job(conn, sent)["options"]["langfuse"]
    assert kept["url"] == f"https://cloud.langfuse.com/trace/{kept['trace_id']}" and kept["sent_at"]
    assert _spans(fake.calls) and _score_events(fake.calls)
    # Stopped runs and A/B comparisons are not sent.
    fake.calls.clear()
    app_module._after_job(_scan(conn, bot["id"], status="cancelled"))
    pairwise = storage.add_job(conn, bot["id"], ["pairwise"], {"kind": "pairwise", "target_b": bot["id"]})
    storage.update_job(conn, pairwise, status="done", results=[])
    app_module._after_job(pairwise)
    assert fake.calls == []


def test_the_job_worker_does_not_wait_for_langfuse(app_module, client, langfuse_off, monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse_upload, "_http", fake)
    queued = []
    monkeypatch.setattr(app_module, "_in_background", queued.append)
    bot = _create_target(client, name="Background Langfuse Bot", config={"url": "http://127.0.0.1:9/blf"})
    client.put("/api/langfuse/settings", json={"public_key": PUBLIC, "secret_key": SECRET})
    job = _scan(app_module._conn, bot["id"])
    app_module._after_job(job)
    assert fake.calls == [] and len(queued) == 1  # handed off, not sent on the job's thread
    queued[0]()
    assert storage.get_job(app_module._conn, job)["options"]["langfuse"]["url"]


def test_failed_sends_are_kept_on_the_run_instead_of_raised(app_module, client, langfuse_off, monkeypatch):
    conn = app_module._conn
    bot = _create_target(client, name="Failing Langfuse Bot", config={"url": "http://127.0.0.1:9/flf"})
    client.put("/api/langfuse/settings", json={"public_key": PUBLIC, "secret_key": SECRET})
    monkeypatch.setattr(langfuse_upload, "_http", FakeLangfuse(otel=FakeResponse(401, {"message": "Invalid credentials"})))
    refused = _scan(conn, bot["id"])
    app_module._after_job(refused)
    error = storage.get_job(conn, refused)["options"]["langfuse"]["error"]
    assert "401" in error and SECRET not in error

    class Down:
        def post(self, *args, **kwargs):
            raise requests.ConnectionError(f"could not connect (auth {PUBLIC}:{SECRET})")

    monkeypatch.setattr(langfuse_upload, "_http", Down())
    unreachable = _scan(conn, bot["id"])
    app_module._after_job(unreachable)
    error = storage.get_job(conn, unreachable)["options"]["langfuse"]["error"]
    assert "reach" in error and SECRET not in error and PUBLIC not in error


def test_a_follow_up_scan_does_not_inherit_the_langfuse_link(app_module, client, langfuse_off, monkeypatch):
    monkeypatch.setattr(langfuse_upload, "_http", FakeLangfuse())
    conn = app_module._conn
    bot = _create_target(client, name="Follow-up Langfuse Bot", config={"url": "http://127.0.0.1:9/ulf"})
    client.put("/api/langfuse/settings", json={"public_key": PUBLIC, "secret_key": SECRET})
    job = _scan(conn, bot["id"])
    app_module._found[job] = {"paths": {"tools_path": "tools"}, "keys": ["tool_correctness"]}
    app_module._after_job(job)
    assert storage.get_job(conn, job)["options"]["langfuse"]["url"]
    follow_up = storage.target_jobs(conn, bot["id"])[-1]
    assert follow_up["options"]["auto_discovered"]["from_job"] == job
    assert "langfuse" not in follow_up["options"]
