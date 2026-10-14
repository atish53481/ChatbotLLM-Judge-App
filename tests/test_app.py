import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.datasets import goldens as goldens_store
from backend.targets.base import ChatReply

EXTENSION_ORIGIN = "chrome-extension://" + "a" * 32
_REPLY = ChatReply(reply="ok", model="m", mode="http")


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    # The app opens its database at import time, so the env var must be set
    # before the first import; later tests in the session reuse that database.
    monkeypatch.setenv("JUDGE_DB_PATH", str(tmp_path / "app_test.db"))
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    # Tests run queued jobs themselves instead of racing the worker thread.
    monkeypatch.setenv("JUDGE_START_JOB_WORKER", "0")
    # API tests must never rewrite the real goldens.json. It is seeded from
    # goldens.default.json on first read, so a fresh checkout has no file yet —
    # read once to seed it before copying.
    goldens_store.load_goldens()
    goldens_copy = tmp_path / "goldens.json"
    shutil.copy(goldens_store.GOLDENS_PATH, goldens_copy)
    monkeypatch.setattr(goldens_store, "GOLDENS_PATH", str(goldens_copy))
    from backend.dashboard import app as module
    return module


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, base_url="http://127.0.0.1")


BOT_CONFIG = {
    "url": "http://127.0.0.1:9/chat",
    "headers": {"Content-Type": "application/json"},
    "body_template": '{"message": "{{message}}"}',
    "response_path": "reply",
}


def _create_target(client, **overrides):
    config = {**BOT_CONFIG, **overrides.pop("config", {})}
    body = {"name": "Support bot", "type": "http", "config": config}
    body.update(overrides)
    r = client.post("/api/targets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _fake_spec(seen_themes):
    metric = MagicMock()
    metric.score = 0.85
    metric.is_successful.return_value = True
    metric.reason = "fine"

    def cases(theme="general_support", **_kw):
        seen_themes.append(theme)
        return [{"id": "g1", "theme": theme, "question": "hi",
                 "expected_answer": "hello", "context": []}]

    return SimpleNamespace(
        key="answer_relevancy",
        title="Answer Relevancy",
        threshold=0.7,
        needs=(),
        cases=cases,
        build_metric=lambda judge, threshold=0.7: metric,
        build_case=lambda g, reply, retrieval=None: SimpleNamespace(input=g["question"], actual_output=reply),
    )


def test_status_reports_judge_configured(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["judge"]["up"] is True


def test_status_reports_judge_missing(client, monkeypatch):
    monkeypatch.delenv("JUDGE_API_KEY")
    assert client.get("/api/status").json()["judge"]["up"] is False


def test_create_list_delete_target(client):
    target = _create_target(client)
    assert target["name"] == "Support bot"
    assert any(t["id"] == target["id"] for t in client.get("/api/targets").json())

    assert client.delete(f"/api/targets/{target['id']}").status_code == 200
    assert not any(t["id"] == target["id"] for t in client.get("/api/targets").json())


def test_delete_unknown_target_returns_404(client):
    assert client.delete("/api/targets/999999").status_code == 404


def test_create_target_rejects_unknown_type_and_bad_http_config(client):
    before = len(client.get("/api/targets").json())
    r = client.post("/api/targets", json={"name": "x", "type": "carrier-pigeon", "config": {}})
    assert r.status_code == 400
    r = client.post("/api/targets", json={"name": "x", "type": "http", "config": {}})
    assert r.status_code == 400  # url missing
    assert len(client.get("/api/targets").json()) == before


def test_target_responses_mask_header_values(client):
    created = _create_target(
        client,
        name="OpenAI bot",
        type="http",
        config={"headers": {"Authorization": "Bearer sk-secret"}},
    )
    assert created["config"]["headers"] == {"Authorization": "***"}
    listing = client.get("/api/targets")
    assert "sk-secret" not in listing.text
    assert any(t["id"] == created["id"] for t in listing.json())


def test_update_golden_route(client):
    created = client.post("/api/goldens", json={
        "theme": "general_support",
        "question": "Does it ship internationally?",
        "expected_answer": "Yes, 10-14 business days.",
    }).json()
    r = client.put(f"/api/goldens/{created['id']}", json={
        "theme": "general_support",
        "question": "Does it ship worldwide?",
        "expected_answer": "Yes, 10-14 business days.",
        "context": ["Ships worldwide."],
        "categories": ["shipping"],
    })
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]
    assert r.json()["question"] == "Does it ship worldwide?"
    listed = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert [g["question"] for g in listed if g["id"] == created["id"]] == ["Does it ship worldwide?"]


def test_update_unknown_golden_is_404(client):
    r = client.put("/api/goldens/g_missing", json={
        "theme": "general_support", "question": "q", "expected_answer": "a"})
    assert r.status_code == 404


def test_goldens_crud(client):
    r = client.post("/api/goldens", json={
        "theme": "general_support",
        "question": "Does it ship internationally?",
        "expected_answer": "Yes, 10-14 business days.",
        "context": ["Yes, 10-14 business days."],
        "categories": ["shipping"],
    })
    assert r.status_code == 200
    golden_id = r.json()["id"]
    assert any(g["id"] == golden_id for g in client.get("/api/goldens").json())
    themed = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert themed and all(g["theme"] == "general_support" for g in themed)

    assert client.delete(f"/api/goldens/{golden_id}").status_code == 200
    assert client.delete(f"/api/goldens/{golden_id}").status_code == 404


def test_create_golden_rejects_empty_question(client):
    r = client.post("/api/goldens",
                    json={"theme": "general_support", "question": "", "expected_answer": "a"})
    assert r.status_code == 422


def test_metrics_endpoint_lists_catalog(client):
    keys = {m["key"] for m in client.get("/api/metrics").json()}
    assert {"answer_relevancy", "faithfulness", "hallucination", "bias",
            "toxicity", "pii_leakage", "correctness"} <= keys


def test_run_uses_target_theme_and_records_one_history_row(client, app_module):
    target = _create_target(client, name="Billing bot", config={"theme": "billing"})
    seen = []
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec(seen)}), \
         patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        r = client.post("/api/run",
                        json={"target_id": target["id"], "metric_key": "answer_relevancy"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pass"
    assert body["theme"] == "billing"
    assert seen == ["billing"]

    latest = client.get("/api/runs/latest", params={"target_id": target["id"]}).json()
    assert [row["metric_key"] for row in latest] == ["answer_relevancy"]
    history = client.get("/api/history", params={
        "target_id": target["id"], "metric_key": "answer_relevancy"}).json()
    assert len(history) == 1
    assert history[0]["score"] == 0.85


def test_run_returns_404_for_unknown_target_or_metric(client):
    target = _create_target(client)
    r = client.post("/api/run", json={"target_id": 999999, "metric_key": "answer_relevancy"})
    assert r.status_code == 404
    r = client.post("/api/run", json={"target_id": target["id"], "metric_key": "no_such_metric"})
    assert r.status_code == 404


def test_run_returns_503_when_judge_not_configured(client, monkeypatch):
    target = _create_target(client)
    monkeypatch.delenv("JUDGE_API_KEY")
    r = client.post("/api/run", json={"target_id": target["id"], "metric_key": "answer_relevancy"})
    assert r.status_code == 503
    assert "JUDGE_API_KEY" in r.json()["detail"]


def test_judge_scores_one_answer_and_records_nothing(client, app_module):
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([])}):
        r = client.post("/api/judge", json={
            "metric_key": "answer_relevancy",
            "question": "What is your refund window?",
            "actual_output": "Refunds within 7 business days.",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pass"
    assert body["score"] == 0.85
    assert body["cases_run"] == 1
    assert body["rows"][0]["actual_output"] == "Refunds within 7 business days."
    # Ad-hoc judging must not pollute the golden-set trend history.
    history = client.get("/api/history", params={
        "target_id": 1, "metric_key": "answer_relevancy"}).json()
    assert history == []


def test_judge_takes_reference_data_from_the_golden_set(client, app_module):
    seen = {}
    spec = _fake_spec([])
    spec.needs = ("expected_answer",)
    spec.build_case = lambda g, reply, retrieval=None: seen.update(g) or SimpleNamespace()
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": spec}):
        r = client.post("/api/judge", json={
            "metric_key": "answer_relevancy",
            "question": "What is your refund window?",
            "actual_output": "Refunds within 7 business days.",
            "theme": "general_support",
        })
    assert r.status_code == 200, r.text
    assert seen["expected_answer"]  # supplied by the golden row, not the request


def test_judge_reports_a_missing_reference_instead_of_scoring(client, app_module):
    spec = _fake_spec([])
    spec.needs = ("expected_answer",)
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": spec}):
        r = client.post("/api/judge", json={
            "metric_key": "answer_relevancy",
            "question": "A question that is not in the golden set",
            "actual_output": "some answer",
            "theme": "general_support",
        })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error"
    assert "expected_answer" in r.json()["error"]


def test_judge_404s_on_unknown_metric_and_503s_without_a_judge(client, monkeypatch):
    r = client.post("/api/judge", json={
        "metric_key": "no_such_metric", "question": "q", "actual_output": "a"})
    assert r.status_code == 404
    monkeypatch.delenv("JUDGE_API_KEY")
    r = client.post("/api/judge", json={
        "metric_key": "answer_relevancy", "question": "q", "actual_output": "a"})
    assert r.status_code == 503


def test_cors_allows_only_the_extension_origin(client):
    preflight = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/api/targets", headers={"Origin": EXTENSION_ORIGIN, **preflight})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == EXTENSION_ORIGIN

    # Editing a golden sends PUT from the extension, so the preflight must allow it.
    put_ok = client.options("/api/goldens/g_0001", headers={
        "Origin": EXTENSION_ORIGIN, "Access-Control-Request-Method": "PUT"})
    assert put_ok.status_code == 200
    assert "PUT" in put_ok.headers["access-control-allow-methods"]

    bad = client.options("/api/targets", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in bad.headers
    simple = client.get("/api/targets", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_rejects_foreign_host_header(app_module):
    rebinding = TestClient(app_module.app, base_url="http://evil.example")
    assert rebinding.get("/api/status").status_code == 400


def test_serves_web_app(client):
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 307
    assert home.headers["location"] == "/app/"

    shell = client.get("/app/")
    assert shell.status_code == 200
    assert 'src="sidebar/sidebar.html"' in shell.text
    assert 'src="dashboard/dashboard.html"' in shell.text
    for page in ("sidebar/sidebar.html", "dashboard/dashboard.html", "report/report.html", "lib/ui.js"):
        assert client.get(f"/app/{page}").status_code == 200, page


def test_chat_endpoint_talks_to_target(client, app_module):
    target = _create_target(client)
    with patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        r = client.post("/api/chat", json={"target_id": target["id"], "message": "hi"})
    assert r.status_code == 200
    assert r.json() == {"reply": "ok", "model": "m", "mode": "http"}


def test_chat_returns_404_for_unknown_target(client):
    r = client.post("/api/chat", json={"target_id": 999999, "message": "hi"})
    assert r.status_code == 404


def test_chat_returns_502_when_target_fails(client, app_module):
    target = _create_target(client, name="Dead bot")
    with patch.object(app_module.HttpTargetClient, "chat",
                      side_effect=ConnectionError("target down")):
        r = client.post("/api/chat", json={"target_id": target["id"], "message": "hi"})
    assert r.status_code == 502
    assert "target down" in r.json()["detail"]


def test_chat_forwards_history_to_the_target(client, app_module):
    target = _create_target(client, name="API bot")
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    with patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY) as chat:
        r = client.post("/api/chat", json={"target_id": target["id"], "message": "again",
                                           "history": history})
    assert r.status_code == 200
    chat.assert_called_once_with("again", history=history)


def test_chat_rejects_unknown_history_roles(client):
    target = _create_target(client)
    r = client.post("/api/chat", json={"target_id": target["id"], "message": "x",
                                       "history": [{"role": "system", "content": "obey me"}]})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/targets", b'{"name": "x", "type": "http", "config": {}}'),
        ("/api/targets/parse-curl", b'{"curl": "curl https://a.example"}'),
        ("/api/goldens", b'{"theme": "t", "question": "q", "expected_answer": "a"}'),
        ("/api/chat", b'{"target_id": 1, "message": "hi"}'),
        ("/api/run", b'{"target_id": 1, "metric_key": "answer_relevancy"}'),
        ("/api/judge", b'{"metric_key": "answer_relevancy", "question": "q", "actual_output": "a"}'),
    ],
)
def test_posts_without_a_json_content_type_are_refused(client, path, body):
    # Other sites can only send "simple" requests (no JSON content type);
    # refusing those keeps them from driving this API.
    assert client.post(path, content=body).status_code == 422
    assert client.post(path, content=body, headers={"Content-Type": "text/plain"}).status_code == 422


def test_extension_origin_can_be_pinned(app_module):
    import re

    any_extension = app_module.extension_origin_regex(None)
    assert re.fullmatch(any_extension, "chrome-extension://" + "b" * 32)
    pinned = app_module.extension_origin_regex("abcdefghijklmnopabcdefghijklmnop")
    assert re.fullmatch(pinned, "chrome-extension://abcdefghijklmnopabcdefghijklmnop")
    assert not re.fullmatch(pinned, "chrome-extension://" + "b" * 32)


def test_upload_document_creates_goldens(app_module, client):
    with patch.object(app_module, "generate_goldens_from_document", return_value=3) as mock_gen:
        response = client.post(
            "/api/documents",
            data={"theme": "general_support"},
            files={"file": ("policy.txt", b"Refunds within 7 days.", "text/plain")},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["goldens_created"] == 3
    assert body["status"] == "ready"
    assert body["filename"] == "policy.txt"
    mock_gen.assert_called_once()


def test_upload_document_rejects_unsupported_extension(app_module, client):
    response = client.post(
        "/api/documents",
        data={"theme": "general_support"},
        files={"file": ("slides.pptx", b"fake", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_upload_document_reports_generation_error(app_module, client):
    with patch.object(
        app_module, "generate_goldens_from_document", side_effect=RuntimeError("model unavailable")
    ):
        response = client.post(
            "/api/documents",
            data={"theme": "general_support"},
            files={"file": ("policy.txt", b"text", "text/plain")},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "model unavailable" in body["error"]


def test_list_and_delete_documents(app_module, client):
    with patch.object(app_module, "generate_goldens_from_document", return_value=1):
        created = client.post(
            "/api/documents",
            data={"theme": "general_support"},
            files={"file": ("policy.txt", b"text", "text/plain")},
        ).json()
    listing = client.get("/api/documents?theme=general_support").json()
    assert any(d["id"] == created["id"] for d in listing)
    response = client.delete(f"/api/documents/{created['id']}")
    assert response.status_code == 200
    listing_after = client.get("/api/documents?theme=general_support").json()
    assert not any(d["id"] == created["id"] for d in listing_after)


SHOP_CURL = ("curl 'https://shop.example/api/chat' -H 'content-type: application/json' "
             "-b 'sid=1' --data-raw '{\"message\":\"Where is my order?\",\"conv\":\"c1\","
             "\"history\":[{\"role\":\"user\",\"content\":\"Where is my order?\"}]}'")


def _http_response(payload):
    import json as json_module
    resp = MagicMock(ok=True, status_code=200, headers={"Content-Type": "application/json"})
    resp.text = json_module.dumps(payload)
    resp.json.return_value = payload
    return resp


def test_parse_curl_detects_message_history_and_reply_path(client):
    answer = _http_response({"reply": "It ships tomorrow.", "mode": "live"})
    with patch("backend.targets.http_client.requests.request", return_value=answer) as sent:
        r = client.post("/api/targets/parse-curl", json={"curl": SHOP_CURL})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == "https://shop.example/api/chat"
    assert body["method"] == "POST"
    assert body["headers"]["Cookie"] == "sid=1"
    assert body["sample_message"] == "Where is my order?"
    assert body["message_marked"] is True
    import json as json_module
    assert json_module.loads(body["body_template"]) == {
        "message": "{{message}}", "conv": "c1", "history": []}
    assert body["response_path"] == "reply"
    assert body["reply_preview"] == "It ships tomorrow."
    assert body["probe_error"] is None
    # The probe asks the captured question, so the site sees a normal request.
    assert b"Where is my order?" in sent.call_args.kwargs["data"]


def test_parse_curl_still_fills_fields_when_probe_fails(client):
    with patch("backend.targets.http_client.requests.request", side_effect=ConnectionError("down")):
        r = client.post("/api/targets/parse-curl", json={"curl": SHOP_CURL})
    body = r.json()
    assert body["message_marked"] is True
    assert body["response_path"] == ""
    assert "down" in body["probe_error"]


def test_parse_curl_rejects_garbage(client):
    r = client.post("/api/targets/parse-curl", json={"curl": "wget https://a.example"})
    assert r.status_code == 400


def test_test_endpoint_reports_reply_or_error(client, app_module):
    with patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        r = client.post("/api/targets/test", json={"config": BOT_CONFIG, "message": "hi"})
    assert r.json() == {"ok": True, "reply": "ok"}
    with patch.object(app_module.HttpTargetClient, "chat", side_effect=ValueError("HTTP 403")):
        r = client.post("/api/targets/test", json={"config": BOT_CONFIG})
    assert r.json()["ok"] is False and "HTTP 403" in r.json()["error"]
    r = client.post("/api/targets/test", json={"config": {"url": "http://a.example"}})
    assert r.status_code == 400  # no {{message}} placeholder


def test_edit_without_headers_keeps_the_stored_ones(client, app_module):
    target = _create_target(client, config={"headers": {"Cookie": "secret=1"}})
    config = {k: v for k, v in BOT_CONFIG.items() if k != "headers"}
    r = client.put(f"/api/targets/{target['id']}",
                   json={"name": "Renamed", "type": "http", "config": config})
    assert r.status_code == 200, r.text
    stored = app_module.storage.get_target(app_module._conn, target["id"])
    assert stored["config"]["headers"] == {"Cookie": "secret=1"}


def test_run_uses_the_requested_threshold(client, app_module):
    target = _create_target(client)
    spec = _fake_spec([])
    seen_thresholds = []
    spec.build_metric = lambda judge, threshold=0.7: seen_thresholds.append(threshold) or MagicMock(
        score=0.8, reason="", is_successful=lambda: 0.8 >= threshold)
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": spec}), \
         patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        r = client.post("/api/run", json={"target_id": target["id"],
                                          "metric_key": "answer_relevancy", "threshold": 0.9})
    assert seen_thresholds == [0.9]
    assert r.json()["threshold"] == 0.9
    assert r.json()["status"] == "fail"
    r = client.post("/api/run", json={"target_id": target["id"],
                                      "metric_key": "answer_relevancy", "threshold": 1.5})
    assert r.status_code == 422


def test_run_reports_progress_while_it_works(client, app_module):
    target = _create_target(client)
    seen = []

    def chat(message, history=None):
        seen.append(dict(app_module._progress["run-1"]))
        return _REPLY

    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([])}), \
         patch.object(app_module.HttpTargetClient, "chat", side_effect=chat):
        r = client.post("/api/run", json={"target_id": target["id"],
                                          "metric_key": "answer_relevancy", "run_id": "run-1"})
    assert r.status_code == 200, r.text
    assert seen == [{"done": 0, "total": 1, "phase": "chat", "question": "hi"}]
    assert client.get("/api/run/progress", params={"run_id": "run-1"}).json() == {"active": False}


def test_cancel_stops_a_run_and_records_nothing(client, app_module):
    target = _create_target(client)
    spec = _fake_spec([])
    spec.cases = lambda theme="general_support", **_kw: [
        {"id": f"g{i}", "theme": theme, "question": f"q{i}", "expected_answer": "a", "context": []}
        for i in range(3)
    ]
    asked = []

    def chat(message, history=None):
        asked.append(message)
        # The user presses Stop while the first answer is on its way.
        assert client.post("/api/run/cancel", json={"run_id": "run-2"}).json() == {"cancelling": True}
        return _REPLY

    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": spec}), \
         patch.object(app_module.HttpTargetClient, "chat", side_effect=chat):
        r = client.post("/api/run", json={"target_id": target["id"],
                                          "metric_key": "answer_relevancy", "run_id": "run-2"})
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["error"] == "stopped after 0 of 3 cases"
    assert asked == ["q0"]
    assert client.get("/api/history", params={
        "target_id": target["id"], "metric_key": "answer_relevancy"}).json() == []
    assert client.post("/api/run/cancel", json={"run_id": "run-2"}).json() == {"cancelling": False}
    app_module._cancelled.discard("run-2")


def test_stop_pressed_before_the_run_reports_still_stops_it(client, app_module):
    target = _create_target(client)
    assert client.post("/api/run/cancel", json={"run_id": "run-3"}).json() == {"cancelling": False}
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([])}), \
         patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY) as chat:
        r = client.post("/api/run", json={"target_id": target["id"],
                                          "metric_key": "answer_relevancy", "run_id": "run-3"})
    assert r.json()["status"] == "cancelled"
    chat.assert_not_called()
    assert "run-3" not in app_module._cancelled


def test_reset_restores_shipped_goldens_and_keeps_other_themes(client):
    shipped = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert len(shipped) == 10
    client.delete(f"/api/goldens/{shipped[0]['id']}")
    client.put(f"/api/goldens/{shipped[1]['id']}", json={
        "theme": "general_support", "question": "edited?", "expected_answer": "edited"})
    client.post("/api/goldens", json={"theme": "general_support", "question": "extra?",
                                      "expected_answer": "extra"})
    client.post("/api/goldens", json={"theme": "my_bot", "question": "mine?",
                                      "expected_answer": "kept"})

    r = client.post("/api/goldens/reset", json={})
    assert r.status_code == 200
    restored = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert restored == shipped
    assert [g["question"] for g in client.get("/api/goldens", params={"theme": "my_bot"}).json()] == ["mine?"]
    # Like every POST here, it needs a JSON body, so other sites cannot trigger it.
    assert client.post("/api/goldens/reset").status_code == 422


def test_judge_settings_saved_from_the_panel_override_env(client, app_module, monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", "env-model")
    before = client.get("/api/judge/settings").json()
    assert before["key_source"] == "env" and before["model"] == "env-model"

    r = client.put("/api/judge/settings", json={
        "api_key": "sk-panel-secret-1234", "model": "panel-model",
        "base_url": "https://api.example.com/v1"})
    assert r.status_code == 200
    saved = r.json()
    assert saved == {"model": "panel-model", "base_url": "https://api.example.com/v1",
                     "has_key": True, "key_hint": "••••1234", "key_source": "panel"}
    assert "sk-panel-secret" not in client.get("/api/judge/settings").text
    assert client.get("/api/status").json()["judge"] == {"model": "panel-model", "up": True}

    # Runs use the saved values.
    with patch.object(app_module, "build_judge", return_value=object()) as build, \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([])}), \
         patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        target = _create_target(client)
        client.post("/api/run", json={"target_id": target["id"], "metric_key": "answer_relevancy"})
    build.assert_called_once_with(api_key="sk-panel-secret-1234", model="panel-model",
                                  base_url="https://api.example.com/v1")

    # An empty key keeps the saved one; empty model / URL fall back to .env.
    kept = client.put("/api/judge/settings", json={"api_key": "", "model": "", "base_url": ""}).json()
    assert kept["key_source"] == "panel" and kept["model"] == "env-model"
    cleared = client.put("/api/judge/settings", json={"clear_key": True}).json()
    assert cleared["key_source"] == "env"
    assert client.put("/api/judge/settings", json={"base_url": "ftp://x"}).status_code == 400


def test_judge_settings_test_reports_the_reply_or_the_error(client, app_module):
    judge = MagicMock()
    judge.generate.return_value = ('{"ok": true}', 0.0)
    with patch.object(app_module, "build_judge", return_value=judge) as build:
        r = client.post("/api/judge/settings/test", json={"api_key": "sk-try", "model": "m"})
    assert r.json() == {"ok": True, "model": "m", "reply": '{"ok": true}'}
    assert build.call_args.kwargs["api_key"] == "sk-try"
    judge.generate.side_effect = RuntimeError("401 invalid key")
    with patch.object(app_module, "build_judge", return_value=judge):
        r = client.post("/api/judge/settings/test", json={})
    assert r.json()["ok"] is False and "401 invalid key" in r.json()["error"]


def test_conversation_scenarios_crud_and_reset(client):
    shipped = client.get("/api/conversations", params={"theme": "general_support"}).json()
    assert len(shipped) == 3

    created = client.post("/api/conversations", json={
        "theme": "general_support", "name": "Gift card", "expected_outcome": "Explains balance",
        "user_turns": ["I have a gift card.", " ", "What's the balance on code ABC?"]}).json()
    assert created["user_turns"] == ["I have a gift card.", "What's the balance on code ABC?"]
    edited = client.put(f"/api/conversations/{created['id']}", json={
        "theme": "general_support", "name": "Gift card v2", "user_turns": ["Hi"]}).json()
    assert edited["name"] == "Gift card v2"
    assert client.delete(f"/api/conversations/{shipped[0]['id']}").status_code == 200
    assert client.delete(f"/api/conversations/{shipped[0]['id']}").status_code == 404
    assert client.post("/api/conversations", json={
        "theme": "t", "name": "x", "user_turns": [" "]}).status_code == 400
    client.post("/api/conversations", json={"theme": "my_bot", "name": "mine", "user_turns": ["hey"]})

    assert client.post("/api/conversations/reset", json={}).json() == {"restored": 5}
    assert client.get("/api/conversations", params={"theme": "general_support"}).json() == shipped
    assert [c["name"] for c in client.get("/api/conversations", params={"theme": "my_bot"}).json()] == ["mine"]
    # The panel-load reset restores scenarios along with the goldens.
    client.delete(f"/api/conversations/{shipped[1]['id']}")
    assert client.post("/api/goldens/reset", json={}).json()["conversations_restored"] == 5
    assert len(client.get("/api/conversations", params={"theme": "general_support"}).json()) == 3


def test_metrics_include_card_copy_and_cases_available(client):
    target = _create_target(client, config={"theme": "no_such_theme", "persona": "Shop bot"})
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={target['id']}").json()}
    assert len(rows) == 25
    assert rows["answer_relevancy"]["cases_available"] == 0
    # Bias / Toxicity / No-Prompt-Leak read the probe store too, so they report the probe set.
    assert rows["bias"]["probe_set"] == rows["no_prompt_leak"]["probe_set"] == "generic"
    assert rows["answer_relevancy"]["probe_set"] is None
    assert rows["prompt_injection"]["cases_available"] == 1
    assert rows["prompt_injection"]["dataset"] == "security_probes"
    assert rows["jailbreak"]["ui_category"] == "security"
    assert rows["bias"]["scale_hint"].startswith("0.00")
    assert rows["answer_relevancy"]["question"].endswith("?")


def test_metrics_without_target_still_list_cases(client):
    rows = {m["key"]: m for m in client.get("/api/metrics").json()}
    assert rows["answer_relevancy"]["cases_available"] > 0


def test_metrics_for_unknown_target_is_404(client):
    assert client.get("/api/metrics?target_id=999999").status_code == 404


def test_target_persona_round_trips(client):
    target = _create_target(client, config={"persona": "Shop bot"})
    listed = next(t for t in client.get("/api/targets").json() if t["id"] == target["id"])
    assert listed["config"]["persona"] == "Shop bot"


def test_run_rejects_zero_limit(client):
    target = _create_target(client)
    r = client.post("/api/run", json={"target_id": target["id"], "metric_key": "answer_relevancy", "limit": 0})
    assert r.status_code == 422


def test_run_forwards_limit_and_persona(client, app_module, monkeypatch):
    target = _create_target(client, config={"persona": "Shop bot", "probe_set": "generic"})
    seen = {}

    def fake_run_spec(spec, judge, target_client, target_id, conn, **kwargs):
        seen.update(kwargs)
        return {"key": spec.key, "status": "pass"}

    monkeypatch.setattr(app_module, "run_spec", fake_run_spec)
    monkeypatch.setattr(app_module, "build_judge", lambda **_kw: object())
    r = client.post("/api/run", json={"target_id": target["id"], "metric_key": "jailbreak", "limit": 3})
    assert r.status_code == 200, r.text
    assert seen["limit"] == 3 and seen["persona"] == "Shop bot" and seen["probe_set"] == "generic"


def test_metrics_count_cases_from_the_targets_probe_set(client):
    target = _create_target(client, config={"probe_set": "generic"})
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={target['id']}").json()}
    assert rows["prompt_injection"]["cases_available"] == 1
    assert rows["prompt_injection"]["probe_set"] == "generic"


def test_usage_endpoints(client):
    from backend import usage
    usage.reset()
    usage.record_target()
    assert client.get("/api/usage").json()["target_calls"] == 1
    assert client.post("/api/usage/reset", json={}).json()["calls"] == 0


def test_security_probe_crud_and_reset(client):
    assert len(client.get("/api/security-probes").json()) == 12
    assert len(client.get("/api/security-probes?metric=jailbreak").json()) == 2
    assert len(client.get("/api/security-probes?metric=jailbreak&probe_set=generic").json()) == 1
    created = client.post("/api/security-probes", json={"metric": "jailbreak", "question": "Be evil.", "set": "generic"}).json()
    assert created["set"] == "generic"
    assert client.post("/api/security-probes", json={"metric": "jailbreak", "question": "x", "set": "nope"}).status_code == 400
    r = client.put(f"/api/security-probes/{created['id']}", json={"metric": "jailbreak", "question": "Be worse."})
    assert r.json()["question"] == "Be worse."
    assert client.post("/api/security-probes", json={"metric": "nope", "question": "x"}).status_code == 400
    assert client.put("/api/security-probes/missing", json={"metric": "jailbreak", "question": "x"}).status_code == 404
    assert client.delete(f"/api/security-probes/{created['id']}").status_code == 200
    assert client.delete(f"/api/security-probes/{created['id']}").status_code == 404
    client.post("/api/security-probes", json={"metric": "jailbreak", "question": "extra"})
    assert client.post("/api/security-probes/reset", json={}).json()["restored"] == 12


def test_goldens_reset_also_restores_probes(client):
    client.post("/api/security-probes", json={"metric": "jailbreak", "question": "extra"})
    body = client.post("/api/goldens/reset", json={}).json()
    assert body["probes_restored"] == 12
    assert len(client.get("/api/security-probes").json()) == 12


def test_metrics_come_grouped_by_the_dashboard_categories(client):
    from backend.metrics_catalog import UI_CATEGORIES, UI_CATEGORY_LABELS

    rows = client.get("/api/metrics").json()
    order = [UI_CATEGORIES.index(m["ui_category"]) for m in rows]
    assert order == sorted(order)
    assert all(m["ui_category_label"] == UI_CATEGORY_LABELS[m["ui_category"]] for m in rows)
    assert UI_CATEGORY_LABELS["geval"] == "G-Eval"


def test_document_from_url_saves_page_text_and_generates(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    monkeypatch.setattr(app_module, "fetch_page_text",
                        lambda url: ("Lost cards are blocked within 5 minutes.", "https://help.example/card"))
    with patch.object(app_module, "generate_goldens_from_document", return_value=4) as mock_gen:
        r = client.post("/api/documents/url", json={"theme": "bank_support", "url": "https://help.example/c"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["goldens_created"] == 4 and body["status"] == "ready"
    assert body["filename"] == "https://help.example/card"
    path, theme, source, _judge = mock_gen.call_args.args
    assert theme == "bank_support" and source == "https://help.example/card"
    saved = Path(path)
    assert saved.parent == tmp_path / "bank_support" and saved.name.endswith("_help.example.txt")
    assert saved.read_text(encoding="utf-8") == "Lost cards are blocked within 5 minutes."


def test_document_from_url_reports_unusable_pages(app_module, client, monkeypatch):
    def refuse(url):
        raise ValueError("the page has almost no readable text")

    monkeypatch.setattr(app_module, "fetch_page_text", refuse)
    r = client.post("/api/documents/url", json={"theme": "bank_support", "url": "https://help.example/spa"})
    assert r.status_code == 400
    assert "readable text" in r.json()["detail"]


def test_document_from_url_rejects_unsafe_theme(client):
    r = client.post("/api/documents/url", json={"theme": "../evil", "url": "https://help.example"})
    assert r.status_code == 422


def test_upload_saves_names_the_synthesizer_can_use(app_module, client, monkeypatch, tmp_path):
    # DeepEval names a Chroma collection after the file; spaces or brackets made it fail silently.
    import re
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    with patch.object(app_module, "generate_goldens_from_document", return_value=2) as mock_gen:
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("new 21 (PRD) v2.txt", b"Refunds within 7 days.", "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == "new 21 (PRD) v2.txt"
    saved = Path(mock_gen.call_args.args[0])
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]", saved.stem), saved.name
    assert saved.suffix == ".txt" and saved.read_bytes() == b"Refunds within 7 days."


def test_upload_that_yields_no_goldens_is_reported_as_an_error(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    with patch.object(app_module, "generate_goldens_from_document", return_value=0):
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("notes.txt", b"hi", "text/plain")})
    body = r.json()
    assert body["status"] == "error" and body["goldens_created"] == 0
    assert "no golden answers" in body["error"]


@pytest.fixture(autouse=True)
def documents_in_the_foreground(app_module, monkeypatch):
    """Generation normally runs on a background thread and enhances the text with
    the judge first; tests run the job inline and skip the LLM rewrite unless
    they say otherwise."""
    monkeypatch.setattr(app_module, "_start_job", lambda job: job())
    monkeypatch.setattr(app_module, "enhance_document", lambda path, judge: path)


def test_upload_returns_at_once_and_the_job_reports_progress(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    jobs = []
    monkeypatch.setattr(app_module, "_start_job", jobs.append)
    with patch.object(app_module, "generate_goldens_from_document", return_value=5):
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("faq.txt", b"Refunds within 7 days.", "text/plain")})
        assert r.status_code == 200 and r.json()["status"] == "queued"
        doc_id = r.json()["id"]
        jobs[0]()
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert (doc["status"], doc["goldens_created"]) == ("ready", 5)


def test_upload_enhances_the_text_before_generating(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    seen = {}

    def fake_enhance(path, judge):
        seen["status"] = app_module.storage.list_documents(app_module._conn)[0]["status"]
        out = path.with_name(f"{path.stem}_enhanced.txt")
        out.write_text("# Refunds\nRefunds are paid within 7 days.", encoding="utf-8")
        return out

    monkeypatch.setattr(app_module, "enhance_document", fake_enhance)
    with patch.object(app_module, "generate_goldens_from_document", return_value=3) as mock_gen:
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("faq.json", b'[{"q": 1}]', "text/plain")})
    assert r.status_code == 400  # .json is not a supported upload type
    with patch.object(app_module, "generate_goldens_from_document", return_value=3) as mock_gen:
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("faq.txt", b'[{"q": 1}]', "text/plain")})
    doc = r.json()
    assert seen["status"] == "enhancing"
    assert Path(mock_gen.call_args.args[0]).name.endswith("_enhanced.txt")
    assert doc["status"] == "ready" and doc["enhanced_path"].endswith("_enhanced.txt")
    text = client.get(f"/api/documents/{doc['id']}/enhanced").json()["text"]
    assert text.startswith("# Refunds")


def test_upload_can_skip_the_ai_enhancement(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    called = []
    monkeypatch.setattr(app_module, "enhance_document", lambda path, judge: called.append(path) or path)
    with patch.object(app_module, "generate_goldens_from_document", return_value=1) as mock_gen:
        r = client.post("/api/documents", data={"theme": "general_support", "enhance": "false"},
                        files={"file": ("faq.txt", b"Refunds within 7 days.", "text/plain")})
    assert called == [] and not Path(mock_gen.call_args.args[0]).name.endswith("_enhanced.txt")
    assert r.json()["enhanced_path"] is None
    assert client.get(f"/api/documents/{r.json()['id']}/enhanced").status_code == 404


def test_a_failed_enhancement_is_reported(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)

    def broken(path, judge):
        raise ValueError("faq.txt has no text to work with")

    monkeypatch.setattr(app_module, "enhance_document", broken)
    with patch.object(app_module, "generate_goldens_from_document", return_value=1) as mock_gen:
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("faq.txt", b" ", "text/plain")})
    doc = r.json()
    assert doc["status"] == "error" and "enhancing failed" in doc["error"] and "no text" in doc["error"]
    mock_gen.assert_not_called()


def test_url_documents_are_enhanced_too(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    monkeypatch.setattr(app_module, "fetch_page_text", lambda url: ("Card help text.", url))
    enhanced = []
    monkeypatch.setattr(app_module, "enhance_document", lambda path, judge: enhanced.append(path) or path)
    with patch.object(app_module, "generate_goldens_from_document", return_value=2):
        r = client.post("/api/documents/url", json={"theme": "bank", "url": "https://help.example/card"})
    assert r.json()["status"] == "ready" and len(enhanced) == 1


def test_unknown_document_is_404(client):
    assert client.get("/api/documents/999999").status_code == 404
    assert client.get("/api/documents/999999/enhanced").status_code == 404
    assert client.post("/api/documents/999999/regenerate", json={}).status_code == 404


def _fake_generator(counts):
    """Stands in for DeepEval's Synthesizer: writes counts.pop(0) goldens per call."""
    def generate(path, theme, filename, judge, document_id=None):
        count = counts.pop(0)
        for n in range(count):
            goldens_store.add_golden(
                theme=theme, question=f"Q{n}", expected_answer=f"A{n}", context=[], categories=[],
                source="synthesized", source_document=filename, source_document_id=document_id,
            )
        return count
    return generate


def test_goldens_from_an_upload_survive_a_panel_reload(app_module, client, monkeypatch, tmp_path):
    # The panel resets goldens on every load. The document row outlives the load,
    # so the goldens it generated must too, or the panel would list a document
    # claiming "4 golden answers added" next to a golden set without them.
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    Path(goldens_store.GOLDENS_PATH).write_text("[]", encoding="utf-8")
    defaults = tmp_path / "goldens.default.json"
    defaults.write_text(json.dumps([
        {"id": "g_0001", "theme": "general_support", "question": "Shipped?",
         "expected_answer": "Yes.", "context": [], "categories": []},
    ]), encoding="utf-8")
    monkeypatch.setattr(goldens_store, "DEFAULT_GOLDENS_PATH", str(defaults))
    monkeypatch.setattr(app_module, "generate_goldens_from_document", _fake_generator([4]))

    client.post("/api/documents", data={"theme": "general_support"},
                files={"file": ("new 21.txt", b"Refunds within 7 days.", "text/plain")})
    assert len(client.get("/api/goldens", params={"theme": "general_support"}).json()) == 4

    client.post("/api/goldens/reset", json={})

    after = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert [g["question"] for g in after] == ["Shipped?", "Q0", "Q1", "Q2", "Q3"]


def test_regenerating_a_document_replaces_its_goldens(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    Path(goldens_store.GOLDENS_PATH).write_text("[]", encoding="utf-8")
    monkeypatch.setattr(app_module, "generate_goldens_from_document", _fake_generator([4, 2]))

    upload = client.post("/api/documents", data={"theme": "general_support"},
                         files={"file": ("faq.txt", b"Refunds within 7 days.", "text/plain")})
    document_id = upload.json()["id"]
    assert upload.json()["goldens_created"] == 4

    r = client.post(f"/api/documents/{document_id}/regenerate", json={})
    assert r.json()["status"] == "ready" and r.json()["goldens_created"] == 2

    goldens = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert len(goldens) == 2, goldens  # replaced, not four plus two
    assert all(g["source_document_id"] == document_id for g in goldens)


def test_reuploading_the_same_file_replaces_its_goldens(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    Path(goldens_store.GOLDENS_PATH).write_text("[]", encoding="utf-8")
    monkeypatch.setattr(app_module, "generate_goldens_from_document", _fake_generator([3, 3]))

    for _ in range(2):
        r = client.post("/api/documents", data={"theme": "general_support"},
                        files={"file": ("faq.txt", b"Refunds within 7 days.", "text/plain")})
        assert r.json()["status"] == "ready"

    goldens = client.get("/api/goldens", params={"theme": "general_support"}).json()
    assert len(goldens) == 3, goldens  # the second upload replaced the first


def test_target_keeps_its_limits_and_rejects_bad_ones(client):
    created = _create_target(client, config={
        "max_message_length": 2000, "send_delay": 1.5, "check_replies": False})
    stored = client.get("/api/targets").json()
    config = next(t for t in stored if t["id"] == created["id"])["config"]
    assert (config["max_message_length"], config["send_delay"], config["check_replies"]) == (2000, 1.5, False)
    r = client.post("/api/targets", json={"name": "x", "type": "http",
                                          "config": {**BOT_CONFIG, "max_message_length": -5}})
    assert r.status_code == 400 and "max_message_length" in r.text


def test_run_can_check_judge_consistency_and_reports_how_it_was_judged(client, app_module):
    target = _create_target(client)
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([])}), \
         patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        r = client.post("/api/run", json={"target_id": target["id"], "metric_key": "answer_relevancy",
                                          "check_consistency": True})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["judge_spread"] == 0 and body["judge_unstable"] is False
    assert set(body["judge"]) == {"model", "tokens", "calls"}
    history = client.get("/api/history", params={
        "target_id": target["id"], "metric_key": "answer_relevancy"}).json()
    assert history[0]["judge_spread"] == 0 and "duration_s" in history[0]


def test_goldens_health_reports_missing_shipped_rows(client):
    shipped = client.get("/api/goldens", params={"theme": "general_support"}).json()
    client.delete(f"/api/goldens/{shipped[0]['id']}")
    health = {h["theme"]: h for h in client.get("/api/goldens/health").json()}
    assert health["general_support"]["missing"] == 1
    assert health["generic"]["missing"] == 0


def test_metrics_carry_improvement_advice(client):
    rows = {m["key"]: m for m in client.get("/api/metrics").json()}
    assert rows["faithfulness"]["improve"]["chatbot"]
    assert rows["prompt_injection"]["improve"]["tests"]


def test_citation_quality_is_not_applicable_without_the_chatbots_sources(client):
    plain = _create_target(client)
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={plain['id']}").json()}
    assert rows["citation_quality"]["cases_available"] == 0
    assert "Retrieved context path" in rows["citation_quality"]["unavailable"]
    assert rows["faithfulness"]["unavailable"] is None
    with_sources = _create_target(client, config={"context_path": "sources", "theme": "general_support"})
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={with_sources['id']}").json()}
    assert rows["citation_quality"]["cases_available"] > 0 and rows["citation_quality"]["unavailable"] is None


def test_parse_llm_fills_an_openai_compatible_config(client):
    filled = client.post(
        "/api/targets/parse-llm",
        json={
            "base_url": "https://api.groq.com/openai/v1",
            "model": "openai/gpt-oss-120b",
            "api_key": "sk-test",
            "system_prompt": "Be brief.",
        },
    ).json()
    assert filled["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert filled["method"] == "POST"
    assert filled["headers"]["Authorization"] == "Bearer sk-test"
    assert filled["response_path"] == "choices.0.message.content"
    assert filled["history_path"] == "messages"
    assert "{{message}}" in filled["body_template"]


def test_parse_llm_rejects_a_missing_model_or_bad_url(client):
    assert client.post("/api/targets/parse-llm", json={"model": ""}).status_code == 400
    bad_url = client.post("/api/targets/parse-llm", json={"base_url": "ftp://x", "model": "m"})
    assert bad_url.status_code == 400


def test_llm_config_saves_as_a_normal_target_with_masked_key(client):
    filled = client.post(
        "/api/targets/parse-llm", json={"model": "gpt-4o-mini", "api_key": "sk-secret"}
    ).json()
    created = client.post("/api/targets", json={"name": "My LLM", "type": "http", "config": filled})
    assert created.status_code == 200
    assert created.json()["config"]["headers"]["Authorization"] == "***"


def test_metrics_default_to_the_generic_probe_set(client):
    target = _create_target(client)
    rows = {m["key"]: m for m in client.get(f"/api/metrics?target_id={target['id']}").json()}
    assert rows["prompt_injection"]["probe_set"] == "generic"


def _run_jobs(app_module):
    """Runs every queued job now (tests do not rely on the worker thread's timing)."""
    while app_module.jobs.process_next():
        pass


def test_job_runs_every_metric_and_records_them(client, app_module):
    target = _create_target(client)
    with patch.object(app_module, "build_judge", return_value=object()),          patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([]), "bias": _fake_spec([])}),          patch.object(app_module.HttpTargetClient, "chat", return_value=_REPLY):
        created = client.post("/api/jobs", json={"target_id": target["id"],
                                                 "metric_keys": ["answer_relevancy", "bias"],
                                                 "thresholds": {"bias": 0.5}}).json()
        assert created["status"] == "queued"
        active = client.get("/api/jobs", params={"target_id": target["id"], "active": 1}).json()
        assert [j["id"] for j in active] == [created["job_id"]]
        _run_jobs(app_module)
    job = client.get(f"/api/jobs/{created['job_id']}").json()
    assert job["status"] == "done" and len(job["results"]) == 2
    assert client.get("/api/jobs", params={"target_id": target["id"], "active": 1}).json() == []
    latest = client.get("/api/runs/latest", params={"target_id": target["id"]}).json()
    assert latest and latest[0]["result"]["rows"] and "result_json" not in latest[0]


def test_job_requests_are_validated(client, app_module):
    target = _create_target(client)
    assert client.post("/api/jobs", json={"target_id": 999999, "metric_keys": ["bias"]}).status_code == 404
    assert client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": ["nope"]}).status_code == 404
    assert client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": []}).status_code == 400
    assert client.get("/api/jobs/999999").status_code == 404
    with patch.object(app_module, "build_judge", side_effect=RuntimeError("no judge key")):
        r = client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": ["bias"]})
    assert r.status_code == 503


def test_job_for_a_deleted_chatbot_fails_clearly(client, app_module):
    target = _create_target(client)
    with patch.object(app_module, "build_judge", return_value=object()):
        job_id = client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": ["bias"]}).json()["job_id"]
        client.delete(f"/api/targets/{target['id']}")
        _run_jobs(app_module)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "error" and job["error"] == "chatbot deleted"


def test_queued_job_can_be_cancelled(client, app_module):
    target = _create_target(client)
    with patch.object(app_module, "build_judge", return_value=object()):
        job_id = client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": ["bias"]}).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/cancel", json={}).json() == {"cancelling": True}
    _run_jobs(app_module)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"
