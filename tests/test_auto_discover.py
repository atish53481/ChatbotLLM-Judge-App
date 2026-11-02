"""Paths found in chatbot replies during a scan are saved on the chatbot, and the
metrics they make runnable are queued as one follow-up scan."""
import json
from unittest.mock import MagicMock, patch

from tests.test_app import BOT_CONFIG, _create_target, _fake_spec, app_module, client  # noqa: F401 - fixtures

_AGENT_REPLY = json.dumps({
    "reply": "Order 123 ships tomorrow.",
    "tool_calls": [{"name": "lookup_order", "arguments": {"order_id": "123"}}],
})


def _response(text):
    resp = MagicMock()
    resp.status_code, resp.ok, resp.text = 200, True, text
    resp.headers = {"Content-Type": "application/json"}
    resp.json.side_effect = lambda: json.loads(text)
    return resp


def _scan(client, app_module, target, reply_text, keys=("answer_relevancy",)):
    """Runs one queued scan of a fake metric against a chatbot answering reply_text."""
    # The app database is shared by the session: drop jobs earlier tests left queued.
    app_module._conn.execute("UPDATE jobs SET status = 'cancelled' WHERE status = 'queued'")
    app_module._conn.commit()
    with patch.object(app_module, "build_judge", return_value=object()), \
         patch.object(app_module, "SPECS_BY_KEY", {"answer_relevancy": _fake_spec([])}), \
         patch("backend.targets.http_client.requests.request", return_value=_response(reply_text)):
        job_id = client.post("/api/jobs", json={"target_id": target["id"], "metric_keys": list(keys),
                                               "limit": 2, "thresholds": {"answer_relevancy": 0.6}}).json()["job_id"]
        assert app_module.jobs.process_next()
    return job_id


def _queued(client, target):
    return client.get("/api/jobs", params={"target_id": target["id"], "active": 1}).json()


def test_found_tool_calls_are_saved_and_newly_runnable_metrics_queued(client, app_module):
    target = _create_target(client)
    before = {m["key"]: m for m in client.get(f"/api/metrics?target_id={target['id']}").json()}
    assert before["argument_correctness"]["unavailable"]
    job_id = _scan(client, app_module, target, _AGENT_REPLY)

    saved = client.get("/api/targets").json()
    assert next(t for t in saved if t["id"] == target["id"])["config"]["tools_path"] == "tool_calls"
    finished = client.get(f"/api/jobs/{job_id}").json()
    assert finished["status"] == "done"
    assert finished["discovered"] == {"tools_path": "tool_calls"}

    [follow_up] = _queued(client, target)
    after = {m["key"]: m for m in client.get(f"/api/metrics?target_id={target['id']}").json()}
    expected = sorted(k for k, m in before.items()
                      if m["unavailable"] and not after[k]["unavailable"] and after[k]["cases_available"])
    assert "argument_correctness" in expected
    assert sorted(follow_up["metric_keys"]) == expected
    # Runnable before, or still without cases (no golden lists Expected tools): not re-run.
    assert "answer_relevancy" not in follow_up["metric_keys"]
    assert "tool_correctness" not in follow_up["metric_keys"]
    # Same settings as the scan that found the path, plus where it came from.
    assert follow_up["options"]["limit"] == 2
    assert follow_up["options"]["thresholds"] == {"answer_relevancy": 0.6}
    assert follow_up["options"]["auto_discovered"] == {"from_job": job_id, "paths": {"tools_path": "tool_calls"}}


def test_scan_notes_the_follow_up(client, app_module):
    target = _create_target(client)
    job_id = _scan(client, app_module, target, _AGENT_REPLY)
    [follow_up] = _queued(client, target)
    scan = client.get(f"/api/scans/{follow_up['id']}").json()
    assert "tool calls at 'tool_calls'" in scan["note"]
    found = client.get(f"/api/scans/{job_id}").json()
    assert "tool calls at 'tool_calls'" in found["note"]


def test_nothing_found_queues_nothing(client, app_module):
    target = _create_target(client)
    _scan(client, app_module, target, json.dumps({"reply": "hello"}))
    assert _queued(client, target) == []
    config = next(t for t in client.get("/api/targets").json() if t["id"] == target["id"])["config"]
    assert not config.get("tools_path")


def test_a_typed_path_is_never_replaced(client, app_module):
    target = _create_target(client, config={"tools_path": "steps"})
    _scan(client, app_module, target, _AGENT_REPLY)
    config = next(t for t in client.get("/api/targets").json() if t["id"] == target["id"])["config"]
    assert config["tools_path"] == "steps"
    assert _queued(client, target) == []


def test_a_path_is_saved_once_and_not_re_queued(client, app_module):
    target = _create_target(client)
    _scan(client, app_module, target, _AGENT_REPLY)
    [follow_up] = _queued(client, target)
    assert app_module.jobs.cancel(follow_up["id"])
    _scan(client, app_module, target, _AGENT_REPLY)  # the path is saved now: nothing new found
    assert _queued(client, target) == []


def test_saving_the_chatbot_form_keeps_a_found_path(client, app_module):
    target = _create_target(client)
    _scan(client, app_module, target, _AGENT_REPLY)
    [follow_up] = _queued(client, target)
    assert app_module.jobs.cancel(follow_up["id"])
    # The side panel's form, loaded before the scan or refilled by a pasted cURL,
    # sends the path fields empty: saving it keeps the path the scan found.
    blank = {**BOT_CONFIG, "tools_path": "", "context_path": ""}
    r = client.put(f"/api/targets/{target['id']}", json={"name": "Support bot", "type": "http", "config": blank})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["tools_path"] == "tool_calls"
    _scan(client, app_module, target, _AGENT_REPLY)  # still saved: no second follow-up scan
    assert _queued(client, target) == []
    # A path typed in the form still replaces it.
    typed = {**BOT_CONFIG, "tools_path": "steps"}
    r = client.put(f"/api/targets/{target['id']}", json={"name": "Support bot", "type": "http", "config": typed})
    assert r.json()["config"]["tools_path"] == "steps"


def test_follow_up_scan_does_not_inherit_the_first_scans_skips(client, app_module):
    target = _create_target(client)
    # Argument Correctness cannot run before tool calls are found: the scan keeps it as skipped.
    job_id = _scan(client, app_module, target, _AGENT_REPLY, keys=("answer_relevancy", "argument_correctness"))
    first = client.get(f"/api/jobs/{job_id}").json()["options"]
    assert [s["key"] for s in first["skipped"]] == ["argument_correctness"]
    [follow_up] = _queued(client, target)
    assert "argument_correctness" in follow_up["metric_keys"]
    # The follow-up runs those metrics, so it must not list them as not run (nor
    # carry the first scan's Confident AI upload).
    assert "skipped" not in follow_up["options"]
    assert "confident" not in follow_up["options"]
