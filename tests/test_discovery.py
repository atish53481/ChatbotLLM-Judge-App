"""Discovery assistant: when the cURL alone does not show what a metric needs
(tool calls, retrieved sources, the chatbot's rules), the app looks for it in
the chatbot's API docs, tries request variants, reads its help pages and asks
it, then shows a report the user approves item by item. Offline."""
import json
from types import SimpleNamespace

import pytest

from backend.targets import discovery
from backend.targets import instructions as target_instructions
from backend.targets.base import ChatReply
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures

CHAT_URL = "https://api.acme.com/v1/chat"
BASE_CONFIG = {"url": CHAT_URL, "method": "POST", "body_template": '{"message": "{{message}}"}',
               "response_path": "reply", "headers": {"Cookie": "secret"}}

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.acme.com/v1"}],
    "paths": {
        "/chat": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ChatIn"}}}},
            "responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/ChatOut"}}}}},
        }},
        "/tools": {"get": {"responses": {"200": {"description": "tool list"}}}},
    },
    "components": {"schemas": {
        "ChatIn": {"type": "object", "properties": {
            "message": {"type": "string"},
            "include_sources": {"type": "boolean"},
            "debug": {"type": "boolean"}}},
        "ChatOut": {"type": "object", "properties": {
            "reply": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "object"}},
            "debug": {"type": "object", "properties": {
                "tool_calls": {"type": "array", "items": {"type": "object"}}}}}},
    }},
}


def _response(data):
    return SimpleNamespace(json=lambda: data, text=json.dumps(data), headers={"Content-Type": "application/json"})


class Bot:
    """A chatbot that only reports tool calls and sources when asked with debug: true."""

    def __init__(self, config, log):
        self.config, self.log = config, log

    def send(self, message, history=None):
        self.log.append(("send", self.config["body_template"], message))
        body = json.loads(self.config["body_template"].replace("{{message}}", "x"))
        data = {"reply": "Your order ships tomorrow."}
        if body.get("debug"):
            data["debug"] = {"tool_calls": [{"name": "lookup_order", "args": {"order_id": "ORD-1"}}]}
            data["sources"] = [{"text": "Orders ship in 1-2 days."}]
        return _response(data)

    def read_reply(self, response):
        return ChatReply(reply=response.json()["reply"], model="m", mode="http")

    def chat(self, message, history=None):
        self.log.append(("chat", message))
        return ChatReply(reply="I can track orders and handle returns. I won't give legal advice.",
                         model="m", mode="http")


class Judge:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.payload), 0.0


INFERRED = {"rules": [{"rule": "Only help with Acme orders and returns.", "evidence": "the chatbot's answer"},
                      {"rule": "  ", "evidence": "dropped: empty"}],
            "tools": [{"name": "lookup_order", "description": "Find an order"}, {"name": ""}]}


def _fetch_from(pages):
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        if url in pages:
            return pages[url]
        raise ValueError("404")
    fetch.calls = calls
    return fetch


def _no_pages(config, fetch=None):
    return []


def _discover(config=None, *, pages=None, judge=None, help_pages=_no_pages, **kw):
    log = []
    fetch = _fetch_from(pages or {})
    report = discovery.discover(
        dict(config or BASE_CONFIG), client=Bot(dict(config or BASE_CONFIG), log),
        make_client=lambda cfg: Bot(cfg, log), judge=judge, fetch=fetch,
        find_help_pages=help_pages, **kw)
    return report, log, fetch


# --- API docs -----------------------------------------------------------------

def test_analyze_openapi_reads_the_chat_endpoint_through_refs_and_base_path():
    found = discovery.analyze_openapi(SPEC, CHAT_URL, "POST")
    assert found["tools_path"] == "debug.tool_calls"
    assert found["context_path"] == "sources"
    assert [f["name"] for f in found["flags"]] == ["include_sources", "debug"]
    assert all(f["in"] == "body" for f in found["flags"])
    assert found["tool_endpoints"] == ["https://api.acme.com/v1/tools"]


def test_analyze_openapi_ignores_a_spec_without_the_chat_endpoint():
    found = discovery.analyze_openapi({"openapi": "3.0.0", "paths": {"/other": {"post": {}}}}, CHAT_URL, "POST")
    assert found == {"tools_path": "", "context_path": "", "flags": [], "tool_endpoints": []}


def test_find_openapi_tries_the_usual_paths_on_the_chatbot_origin_only():
    fetch = _fetch_from({"https://api.acme.com/v1/openapi.json": SPEC})
    url, spec = discovery.find_openapi(BASE_CONFIG, fetch=fetch, budget=discovery.Budget())
    assert url == "https://api.acme.com/v1/openapi.json" and spec is SPEC
    assert all(u.startswith("https://api.acme.com/") for u, _ in fetch.calls)
    # The chatbot's cookies / keys go to its docs only when the user allows it.
    assert all(h is None for _, h in fetch.calls)


def test_find_openapi_sends_headers_only_when_allowed_and_stops_at_the_cap():
    fetch = _fetch_from({})
    budget = discovery.Budget(fetches=3)
    assert discovery.find_openapi(BASE_CONFIG, fetch=fetch, budget=budget, send_headers=True) == (None, None)
    assert len(fetch.calls) == 3
    assert fetch.calls[0][1] == {"Cookie": "secret"}


# --- Request variants ---------------------------------------------------------

def test_variant_configs_add_a_flag_to_a_json_body_or_the_url():
    body, query = discovery.variant_configs(BASE_CONFIG, [{"name": "debug", "in": "body"},
                                                          {"name": "trace", "in": "query"}])
    assert json.loads(body[1]["body_template"]) == {"message": "{{message}}", "debug": True}
    assert query[1]["url"] == CHAT_URL + "?trace=true"
    form = {**BASE_CONFIG, "body_template": "q={{message}}"}
    assert discovery.variant_configs(form, [{"name": "debug", "in": "body"}]) == []


# --- The whole discovery -------------------------------------------------------

def test_discover_finds_tool_calls_behind_a_debug_flag_from_the_api_docs():
    report, log, _ = _discover(pages={"https://api.acme.com/v1/openapi.json": SPEC})
    items = {i["kind"]: i for i in report["items"]}
    request = items["request"]
    assert request["status"] == "found"
    assert json.loads(request["value"]["body_template"])["debug"] is True
    assert request["value"]["tools_path"] == "debug.tool_calls"
    assert request["value"]["context_path"] == "sources"
    agent = next(m for m in report["metrics"] if m["group"] == "agent")
    assert agent["status"] == "found"


def test_discover_without_docs_still_tries_the_usual_flags():
    report, log, _ = _discover()
    assert any(i["kind"] == "request" for i in report["items"])
    sends = [entry for entry in log if entry[0] == "send"]
    assert len(sends) <= discovery.MAX_MESSAGES


def test_discover_infers_rules_and_tools_with_the_judge():
    judge = Judge(INFERRED)
    page = ("https://acme.com/faq", "Returns within 30 days. " * 20)
    report, log, _ = _discover(judge=judge, help_pages=lambda config, fetch=None: [page])
    items = {i["kind"]: i for i in report["items"]}
    assert items["instructions"]["status"] == "inferred"
    assert items["instructions"]["value"] == ["Only help with Acme orders and returns."]
    assert items["available_tools"]["value"] == [{"name": "lookup_order", "description": "Find an order"}]
    assert "Returns within 30 days." in judge.prompts[0] and "I can track orders" in judge.prompts[0]
    alignment = next(m for m in report["metrics"] if m["group"] == "prompt_alignment")
    assert alignment["status"] == "inferred"


def test_discover_falls_back_to_black_box_when_nothing_is_found():
    class PlainBot(Bot):
        def send(self, message, history=None):
            self.log.append(("send", self.config["body_template"], message))
            return _response({"reply": "Hello"})

    log = []
    report = discovery.discover(dict(BASE_CONFIG), client=PlainBot(BASE_CONFIG, log),
                                make_client=lambda cfg: PlainBot(cfg, log), judge=None,
                                fetch=_fetch_from({}), find_help_pages=_no_pages)
    assert report["items"] == []
    by_group = {m["group"]: m for m in report["metrics"]}
    assert by_group["agent"]["status"] == "black_box" and "tool" in by_group["agent"]["ask"]
    assert by_group["contextual"]["status"] == "black_box"
    assert by_group["prompt_alignment"]["status"] == "black_box"
    assert by_group["reply"]["status"] == "ready"


def test_discover_asks_the_chatbots_own_questions_so_tools_show():
    class OrderBot(Bot):
        """Calls a tool only for an order question, like ShopSphere."""

        def send(self, message, history=None):
            self.log.append(("send", self.config["body_template"], message))
            data = {"reply": "Sure.", "tools_called": [], "sources": []}
            if "ORD-" in message:
                data["tools_called"] = [{"name": "lookup_order"}]
                data["sources"] = [{"text": "Order ORD-1 ships today."}]
            return _response(data)

    log = []
    report = discovery.discover(dict(BASE_CONFIG), client=OrderBot(BASE_CONFIG, log),
                                make_client=lambda cfg: OrderBot(cfg, log), judge=None,
                                fetch=_fetch_from({}), find_help_pages=_no_pages,
                                questions=["Where is my order ORD-1?", "Hi"])
    items = {i["kind"]: i["value"] for i in report["items"]}
    assert items == {"tools_path": "tools_called", "context_path": "sources"}
    assert ("send", BASE_CONFIG["body_template"], "Where is my order ORD-1?") in log


def test_discover_says_so_when_the_chatbot_does_not_answer():
    class DownBot(Bot):
        def send(self, message, history=None):
            raise ValueError("HTTP 502 from https://api.acme.com/v1/chat: LLM error 401")

    report = discovery.discover(dict(BASE_CONFIG), client=DownBot(BASE_CONFIG, []),
                                make_client=lambda cfg: DownBot(cfg, []), judge=None,
                                fetch=_fetch_from({}), find_help_pages=_no_pages)
    assert any("did not answer" in n and "HTTP 502" in n for n in report["notes"])
    assert report["used"]["messages"] == 1  # no variants are tried on a chatbot that is down


def test_discover_reports_what_the_chatbot_already_returns():
    config = {**BASE_CONFIG, "tools_path": "debug.tool_calls", "instructions": ["Be brief."]}
    report, _, _ = _discover(config)
    by_group = {m["group"]: m for m in report["metrics"]}
    assert by_group["agent"]["status"] == "ready"
    assert by_group["prompt_alignment"]["status"] == "ready"


# --- Approving items -----------------------------------------------------------

def _report_with_everything():
    return _discover(pages={"https://api.acme.com/v1/openapi.json": SPEC}, judge=Judge(INFERRED),
                     help_pages=lambda config, fetch=None: [])[0]


def test_apply_uses_only_the_approved_items():
    report = _report_with_everything()
    config = discovery.apply({**BASE_CONFIG, "discovery": report}, ["request", "instructions"])
    assert json.loads(config["body_template"])["debug"] is True
    assert config["tools_path"] == "debug.tool_calls" and config["context_path"] == "sources"
    assert config["inferred_instructions"] == ["Only help with Acme orders and returns."]
    assert "available_tools" not in config
    assert config["discovery"]["approved"] == ["request", "instructions"]


def test_apply_never_replaces_a_path_the_user_typed():
    report = _report_with_everything()
    config = discovery.apply({**BASE_CONFIG, "tools_path": "my.tools", "discovery": report}, ["request"])
    assert config["tools_path"] == "my.tools"


def test_resolve_uses_approved_inferred_rules_before_standard_ones():
    rules = target_instructions.resolve({**BASE_CONFIG, "inferred_instructions": ["Only help with Acme."]})
    assert list(rules) == ["Only help with Acme."] and rules.source == "inferred"
    assert "inferred" in target_instructions.source_note(rules)
    typed = target_instructions.resolve({**BASE_CONFIG, "instructions": ["Be brief."],
                                         "inferred_instructions": ["Only help with Acme."]})
    assert list(typed) == ["Be brief."]


# --- API -------------------------------------------------------------------------

@pytest.fixture
def offline(app_module, monkeypatch):
    log = []
    monkeypatch.setattr(app_module, "build_judge", lambda **_kw: Judge(INFERRED))
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: Bot(row["config"], log))
    monkeypatch.setattr(discovery, "fetch_json", _fetch_from({"https://api.acme.com/v1/openapi.json": SPEC}))
    monkeypatch.setattr(discovery, "_find_help_pages", _no_pages)
    return log


def test_discover_endpoint_needs_the_user_to_confirm_they_may_test_the_chatbot(client, offline):
    bot = _create_target(client, name="Acme", config=dict(BASE_CONFIG))
    r = client.post(f"/api/targets/{bot['id']}/discover", json={})
    assert r.status_code == 400 and "allowed to test" in r.json()["detail"]
    assert offline == []  # nothing was sent


def test_discover_then_apply_through_the_api(client, offline):
    bot = _create_target(client, name="Acme", config=dict(BASE_CONFIG))
    report = client.post(f"/api/targets/{bot['id']}/discover", json={"authorized": True}).json()
    assert {i["kind"] for i in report["items"]} >= {"request", "instructions"}
    saved = client.post(f"/api/targets/{bot['id']}/discovery/apply", json={"items": ["request"]}).json()
    assert saved["config"]["tools_path"] == "debug.tool_calls"
    assert saved["config"]["headers"] == {"Cookie": "***"}
    # Saving the chatbot form (which knows nothing of discovery) keeps the report.
    form = {k: v for k, v in BASE_CONFIG.items() if k != "headers"}
    client.put(f"/api/targets/{bot['id']}", json={"name": "Acme", "type": "http", "config": form})
    kept = {t["id"]: t for t in client.get("/api/targets").json()}[bot["id"]]["config"]
    assert kept["discovery"]["approved"] == ["request"]
