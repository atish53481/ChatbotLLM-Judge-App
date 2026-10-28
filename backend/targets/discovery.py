"""Finds what a chatbot's metrics need when its cURL alone does not show it.

A pasted cURL gives the reply, which every reply-only metric scores. Prompt
Alignment also needs the chatbot's rules, the agent metrics its tool calls and
the Contextual metrics its retrieved sources. When those are missing, the app
looks for them the way a tester would, then shows a report the user approves
item by item (`apply`); whatever stays missing is scored black box, as before.

1. what the reply already carries (tool calls / sources at some path);
2. the chatbot's API docs (OpenAPI / Swagger) on its own origin: response
   fields for tool calls and sources, request flags that may switch them on,
   endpoints that list its tools;
3. request variants: the same question with one such flag added, kept only
   when the response gains tool calls or sources;
4. its rules and tools, inferred by the judge from its help pages and its own
   answers to plain questions (never from attempts to make it leak its prompt).

Only for chatbots the user may test (the API refuses without that tick):
read-only GETs on the chatbot's origin and ordinary chat messages, capped by
`Budget`. The chatbot's headers go to its docs only when the user allows it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from backend.datasets.autogen import find_help_pages as _find_help_pages
from backend.judges.ask import ask_json
from backend.targets import instructions as target_instructions
from backend.targets.curl import (
    _CONTEXT_KEYS, _TOOLS_KEYS, _norm, find_context_path, find_tools_path)

# Where APIs usually publish their OpenAPI / Swagger document.
SPEC_PATHS = ("/openapi.json", "/swagger.json", "/v3/api-docs", "/swagger/v1/swagger.json",
              "/api-docs", "/.well-known/ai-plugin.json")
# Request flags that often make a chatbot return its tool calls or sources.
FLAG_NAMES = ("debug", "include_sources", "return_sources", "include_context", "return_context",
              "include_tool_calls", "return_tool_calls", "return_intermediate_steps", "verbose", "trace")
_FLAG_NORMS = {_norm(n) for n in FLAG_NAMES}
_FLAG_WORDS = ("debug", "source", "context", "tool", "trace", "step", "verbose", "citation", "retriev")
# Tried when the docs name no flag (a JSON body only).
DEFAULT_FLAGS = [{"name": n, "in": "body"} for n in
                 ("debug", "include_sources", "return_tool_calls", "return_intermediate_steps", "verbose")]
_TOOL_LIST_SEGMENTS = ("tools", "functions", "actions", "plugins", "skills")

MAX_FETCHES = 20      # GETs on the chatbot's site (API docs, tool lists)
MAX_MESSAGES = 10     # chat messages sent
MAX_QUESTIONS = 2     # of the chatbot's own golden questions, asked to see its tools and sources
MAX_JSON_BYTES = 2 * 1024 * 1024
TIMEOUT = 15
PAGE_TEXT_CHARS = 4000

QUESTION = "Hi! What can you help me with?"
ASK = [
    QUESTION,
    "Is there anything you won't do or can't help with?",
    "Which actions can you take for me, like looking something up or changing something?",
]

INFER_PROMPT = """You are preparing to evaluate a chatbot. Its owner did not give its instructions or its tools,
so infer them from the material below. Only write what the material supports.

What the chatbot said when asked about itself:
{answers}

Its help / FAQ pages:
{pages}

Tools its API lists:
{tools}

Return only JSON:
{{"rules": [{{"rule": "one instruction the chatbot evidently follows, checkable on a single reply",
             "evidence": "where it shows (quote or page)"}}],
  "tools": [{{"name": "tool_name", "description": "what it does"}}]}}
At most 12 rules and 15 tools; empty lists when the material shows none."""

GROUPS = {
    "reply": ("Answer quality, safety and Task Completion",
              "Answer Relevancy, Correctness, Helpfulness, Toxicity, Bias, PII Leakage, security probes, "
              "Task Completion"),
    "prompt_alignment": ("Prompt Alignment", "Prompt Alignment"),
    "agent": ("Agent metrics", "Tool Correctness, Argument Correctness, Plan Quality, Plan Adherence, "
                               "Step Efficiency"),
    "contextual": ("Retrieval metrics", "Contextual Precision, Contextual Recall, Contextual Relevancy"),
}
ASK_TEAM = {
    "prompt_alignment": "Ask the chatbot's team for the rules in its system prompt and type them under "
                        "Chatbot instructions.",
    "agent": "Ask the chatbot's team to return the tools it called (e.g. a tools_called list) for test accounts.",
    "contextual": "Ask the chatbot's team to return the retrieved sources (e.g. a sources list) for test "
                  "accounts.",
}


class Budget:
    """Caps on what one discovery may send, so a slow or large site cannot stall it."""

    def __init__(self, fetches: int = MAX_FETCHES, messages: int = MAX_MESSAGES):
        self.fetches_left, self.messages_left = fetches, messages
        self.fetches = self.messages = 0

    def take_fetch(self) -> bool:
        if self.fetches_left <= 0:
            return False
        self.fetches_left -= 1
        self.fetches += 1
        return True

    def take_message(self) -> bool:
        if self.messages_left <= 0:
            return False
        self.messages_left -= 1
        self.messages += 1
        return True


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fetch_json(url: str, headers: dict | None = None) -> Any:
    """A JSON document from the chatbot's own origin; ValueError when it is not one."""
    try:
        r = requests.get(url, headers=headers or {}, timeout=TIMEOUT, stream=True)
    except requests.RequestException as e:
        raise ValueError(f"{type(e).__name__}") from e
    with r:
        if _origin(r.url) != _origin(url):
            raise ValueError("redirected to another site")
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        body = r.raw.read(MAX_JSON_BYTES + 1, decode_content=True)
    if len(body) > MAX_JSON_BYTES:
        raise ValueError("too large")
    try:
        return json.loads(body)
    except ValueError as e:
        raise ValueError("not JSON") from e


# --- 2. API docs -------------------------------------------------------------

def _is_spec(data: Any) -> bool:
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data) and isinstance(data.get("paths"), dict)


def _spec_urls(config: dict) -> list[str]:
    """Spec addresses to try, deepest folder of the chat URL first, then the root."""
    url = config.get("url", "")
    origin = _origin(url)
    folders = [p for p in urlsplit(url).path.split("/") if p][:-1]
    prefixes = ["/" + "/".join(folders[:i]) for i in range(len(folders), 0, -1)] + [""]
    return [origin + prefix + path for prefix in prefixes for path in SPEC_PATHS]


def find_openapi(config: dict, *, fetch=None, budget: Budget, send_headers: bool = False):
    """(spec URL, spec) of the chatbot's OpenAPI / Swagger document, else (None, None)."""
    fetch = fetch or fetch_json
    headers = dict(config.get("headers") or {}) if send_headers else None
    origin = _origin(config.get("url", ""))
    for url in _spec_urls(config):
        if not budget.take_fetch():
            break
        try:
            data = fetch(url, headers)
        except ValueError:
            continue
        if _is_spec(data):
            return url, data
        api = data.get("api") if isinstance(data, dict) else None  # ai-plugin.json points at the spec
        spec_url = api.get("url") if isinstance(api, dict) else None
        if isinstance(spec_url, str) and _origin(spec_url) == origin and budget.take_fetch():
            try:
                data = fetch(spec_url, headers)
            except ValueError:
                continue
            if _is_spec(data):
                return spec_url, data
    return None, None


def _resolve(spec: dict, node: Any, depth: int = 0) -> Any:
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 20:
        target: Any = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        node, depth = target, depth + 1
    return node


def _fields(spec: dict, schema: Any, prefix: str = "", depth: int = 0):
    """(dotted path, name) of every property in a schema; array items add ".0"."""
    schema = _resolve(spec, schema)
    if not isinstance(schema, dict) or depth > 8:
        return
    for key in ("allOf", "oneOf", "anyOf"):
        for part in schema.get(key) or []:
            yield from _fields(spec, part, prefix, depth + 1)
    if isinstance(schema.get("items"), dict):
        yield from _fields(spec, schema["items"], f"{prefix}.0" if prefix else "0", depth + 1)
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        yield path, name
        yield from _fields(spec, sub, path, depth + 1)


def _shallowest(fields: list[tuple[str, str]], keys: tuple[str, ...]) -> str:
    found = [(p.count("."), p) for p, name in fields if _norm(name) in keys]
    return min(found)[1] if found else ""


def _content_schema(spec: dict, node: Any) -> Any:
    node = _resolve(spec, node)
    if not isinstance(node, dict):
        return None
    if "schema" in node:  # Swagger 2
        return node["schema"]
    content = node.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), None)
    return media.get("schema") if isinstance(media, dict) else None


def _is_flag(name: str, schema: Any) -> bool:
    norm = _norm(name)
    if norm in _FLAG_NORMS:
        return True
    return isinstance(schema, dict) and schema.get("type") == "boolean" and any(w in norm for w in _FLAG_WORDS)


def _template(path: str) -> str:
    return re.sub(r"\\\{[^/]*?\\\}", "[^/]+", re.escape(path.rstrip("/")))


def analyze_openapi(spec: dict, chat_url: str, method: str) -> dict:
    """What the docs say about the chat endpoint: where tool calls and sources
    sit in its response, request flags worth trying, and tool-list endpoints."""
    found = {"tools_path": "", "context_path": "", "flags": [], "tool_endpoints": []}
    path = urlsplit(chat_url).path.rstrip("/") or "/"
    bases = [urlsplit(s.get("url", "")).path.rstrip("/") for s in spec.get("servers") or [] if isinstance(s, dict)]
    bases += [str(spec.get("basePath") or "").rstrip("/"), ""]
    paths = spec.get("paths") or {}
    op, path_item, base = None, None, ""
    for candidate in dict.fromkeys(bases):
        for p, item in paths.items():
            if isinstance(item, dict) and isinstance(item.get(method.lower()), dict) \
                    and re.fullmatch(re.escape(candidate) + _template(p), path):
                op, path_item, base = item[method.lower()], item, candidate
                break
        if op is not None:
            break
    if op is None:
        return found

    responses = op.get("responses") or {}
    response = next((responses[c] for c in ("200", "201", "default") if c in responses), None)
    fields = list(_fields(spec, _content_schema(spec, response)))
    found["tools_path"] = _shallowest(fields, _TOOLS_KEYS)
    found["context_path"] = _shallowest(fields, _CONTEXT_KEYS)

    parameters = [_resolve(spec, p) for p in (path_item.get("parameters") or []) + (op.get("parameters") or [])]
    if "requestBody" in op:
        body = _content_schema(spec, op["requestBody"])
    else:  # Swagger 2 keeps the body among the parameters
        body = next((p.get("schema") for p in parameters if isinstance(p, dict) and p.get("in") == "body"), None)
    body = _resolve(spec, body)
    properties = body.get("properties") or {} if isinstance(body, dict) else {}
    for name, sub in properties.items():
        if _is_flag(name, _resolve(spec, sub)):
            found["flags"].append({"name": name, "in": "body"})
    for p in parameters:
        if isinstance(p, dict) and p.get("in") == "query" and _is_flag(p.get("name", ""), p.get("schema") or p):
            found["flags"].append({"name": p["name"], "in": "query"})

    origin = _origin(chat_url)
    for p, item in paths.items():
        last = p.rstrip("/").rsplit("/", 1)[-1]
        if "{" not in p and _norm(last) in _TOOL_LIST_SEGMENTS and isinstance(item, dict) and "get" in item:
            found["tool_endpoints"].append(origin + base + p)
    return found


def _tool_list(data: Any) -> list[dict]:
    """{name, description} from a tool-list response ({"tools": [...]} or a bare list)."""
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    tools = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict):
            source = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            name = source.get("name")
            if isinstance(name, str) and name.strip():
                tools.append({"name": name.strip(), "description": str(source.get("description") or "").strip()})
    return tools


# --- 3. Request variants -----------------------------------------------------

def variant_configs(config: dict, flags: list[dict]) -> list[tuple[dict, dict]]:
    """(flag, config) for each flag that can be added: a JSON body key set to
    true, or ?name=true on the URL."""
    variants = []
    try:
        body = json.loads(config.get("body_template") or "")
    except ValueError:
        body = None
    url = config.get("url", "")
    for flag in flags:
        name = flag["name"]
        if flag.get("in") == "query":
            if f"{name}=" in urlsplit(url).query:
                continue
            variant = {**config, "url": f"{url}{'&' if '?' in url else '?'}{name}=true"}
        elif isinstance(body, dict) and name not in body:
            variant = {**config, "body_template": json.dumps({**body, name: True})}
        else:
            continue
        variants.append((flag, variant))
    return variants


def _ask(client, question: str) -> tuple[Any, str, str]:
    """(response JSON or None, reply text, error) for one message."""
    try:
        response = client.send(question)
        reply = client.read_reply(response).reply
    except Exception as e:  # noqa: BLE001 - a variant the chatbot rejects is simply not kept
        return None, "", f"{type(e).__name__}: {e}"
    try:
        data = response.json()
    except ValueError:
        data = None
    return data, (reply or "").strip(), ""


def _paths(data: Any) -> dict:
    if data is None:
        return {"tools_path": "", "context_path": ""}
    return {"tools_path": find_tools_path(data), "context_path": find_context_path(data)}


def _item(kind: str, label: str, value: Any, status: str, source: str, evidence: Any = None) -> dict:
    return {"id": kind, "kind": kind, "label": label, "value": value, "status": status,
            "source": source, "evidence": evidence or []}


# --- 4. Rules and tools ------------------------------------------------------

def _infer(judge, answers, pages, api_tools) -> tuple[list[dict], list[dict]]:
    prompt = INFER_PROMPT.format(
        answers="\n".join(f"Q: {q}\nA: {a}" for q, a in answers) or "(no answers)",
        pages="\n\n".join(f"[{url}]\n{text[:PAGE_TEXT_CHARS]}" for url, text in pages) or "(none found)",
        tools="\n".join(f"- {t['name']}: {t['description']}" for t in api_tools) or "(none listed)",
    )
    data = ask_json(judge, prompt)
    rules = [{"rule": str(r.get("rule", "")).strip(), "evidence": str(r.get("evidence", "")).strip()}
             for r in data.get("rules") or [] if isinstance(r, dict) and str(r.get("rule", "")).strip()][:12]
    return rules, _tool_list(data.get("tools") or [])[:15]


# --- Report ------------------------------------------------------------------

def coverage(config: dict, items: list[dict]) -> list[dict]:
    """Per metric group: ready (the chatbot already gives it), found (approve
    to use), inferred (judge-drafted, review first) or black_box."""
    kinds = {i["kind"]: i for i in items}
    request = (kinds.get("request") or {}).get("value") or {}

    def path_status(field: str) -> tuple[str, str]:
        if config.get(field):
            return "ready", f"The chatbot returns them at '{config[field]}'."
        if field in kinds or request.get(field):
            where = kinds[field]["value"] if field in kinds else request[field]
            return "found", f"Found at '{where}'; approve it below to switch these metrics on."
        return "black_box", "Not in the chatbot's responses; these metrics stay unavailable."

    rules = target_instructions.resolve(config)
    if rules.source in ("chatbot", "request", "inferred"):
        label = {"chatbot": "typed on the chatbot", "request": "from its request",
                 "inferred": "inferred and approved"}[rules.source]
        prompt = ("ready", f"Judged against {len(rules)} rules ({label}).")
    elif "instructions" in kinds:
        prompt = ("inferred", f"{len(kinds['instructions']['value'])} rules inferred; review and approve them below.")
    else:
        prompt = ("black_box", f"No rules known: judged against {len(rules)} standard production rules.")
    statuses = {
        "reply": ("ready", "Scored from the reply the cURL already returns."),
        "prompt_alignment": prompt,
        "agent": path_status("tools_path"),
        "contextual": path_status("context_path"),
    }
    return [{"group": group, "title": GROUPS[group][0], "metrics": GROUPS[group][1], "status": status,
             "note": note, "ask": ASK_TEAM.get(group, "") if status != "ready" else ""}
            for group, (status, note) in statuses.items()]


def discover(config: dict, *, client, make_client, judge=None, fetch=None, find_help_pages=None,
             send_headers: bool = False, budget: Budget | None = None, questions=None) -> dict:
    """Looks for what the metrics need and returns the report (nothing is changed;
    `apply` uses the items the user approves). `questions` are the chatbot's own
    golden questions: a greeting rarely makes a bot call a tool or retrieve."""
    fetch = fetch or fetch_json
    find_pages = find_help_pages or _find_help_pages
    budget = budget or Budget()
    items: list[dict] = []
    notes: list[str] = []
    asked = [q for q in questions or [] if q and q.strip()][:MAX_QUESTIONS] or [QUESTION]

    # 1. What the reply already carries.
    seen = {"tools_path": "", "context_path": ""}
    answered, error = False, ""
    for question in asked:
        if not budget.take_message():
            break
        data, reply, error = _ask(client, question)
        if error:
            break  # down or refusing: asking more only repeats the error
        answered = answered or bool(reply)
        for field, path in _paths(data).items():
            seen[field] = seen[field] or path
        if all(seen.values()):
            break
    if error and not answered:
        notes.append(f"The chatbot did not answer ({error[:300]}), so its tool calls and sources could not be "
                     "checked. Fix the chatbot or paste a fresh cURL, then run this again.")
    for field, label in (("tools_path", "Tool calls path"), ("context_path", "Retrieved sources path")):
        if seen[field] and not config.get(field):
            items.append(_item(field, label, seen[field], "found", "the chatbot's reply"))

    # 2. API docs.
    spec_url, spec = find_openapi(config, fetch=fetch, budget=budget, send_headers=send_headers)
    api = analyze_openapi(spec, config.get("url", ""), config.get("method") or "POST") if spec else \
        {"tools_path": "", "context_path": "", "flags": [], "tool_endpoints": []}
    notes.append(f"API docs found at {spec_url}." if spec else
                 "No API docs (OpenAPI / Swagger) found on the chatbot's site.")

    # 3. Request variants, for what neither the config nor the reply has.
    missing = [f for f in ("tools_path", "context_path") if not config.get(f) and not seen[f]]
    if missing and answered:
        best = None
        for flag, variant in variant_configs(config, api["flags"] or DEFAULT_FLAGS):
            if not budget.take_message():
                notes.append("Stopped trying request variants: message limit reached.")
                break
            data, reply, _ = _ask(make_client(variant), asked[0])
            if not reply:
                continue  # the chatbot did not answer this variant
            gained = {f: p for f, p in _paths(data).items() if f in missing and p}
            if gained and (best is None or len(gained) > len(best[2])):
                best = (flag, variant, gained)
            if best and len(best[2]) == len(missing):
                break
        if best:
            flag, variant, gained = best
            items.append(_item(
                "request", f"Send '{flag['name']}' with each question",
                {"flag": flag["name"], "url": variant["url"], "body_template": variant["body_template"], **gained},
                "found", f"request flag '{flag['name']}'" + (" from the API docs" if api["flags"] else "")))
        for field in missing:
            if api[field] and not (best and field in best[2]):
                notes.append(f"The API docs list '{api[field]}' in the response, but no request showed it; "
                             "ask the chatbot's team when it is filled.")

    # 4. Tools the API lists, then rules and tools inferred by the judge.
    headers = dict(config.get("headers") or {}) if send_headers else None
    api_tools: list[dict] = []
    for url in api["tool_endpoints"][:2]:
        if budget.take_fetch():
            try:
                api_tools += _tool_list(fetch(url, headers))
            except ValueError:
                pass
    tools = list(api_tools)
    if judge is None:
        notes.append("No judge available: the chatbot's rules and tools were not inferred.")
    else:
        answers = []
        for question in ASK:
            if not budget.take_message():
                break
            try:
                reply = client.chat(question).reply
            except Exception:  # noqa: BLE001 - an unreachable bot just means fewer answers
                break
            if reply and reply.strip():
                answers.append((question, reply.strip()))
        try:
            pages = find_pages(config)
        except Exception:  # noqa: BLE001 - no pages is fine
            pages = []
        try:
            rules, inferred_tools = _infer(judge, answers, pages, api_tools)
        except Exception as e:  # noqa: BLE001 - a judge failure is reported, the rest is kept
            notes.append(f"The judge could not infer rules and tools: {type(e).__name__}: {e}")
            rules, inferred_tools = [], []
        if rules and not config.get("instructions"):
            items.append(_item("instructions", "Chatbot rules (Prompt Alignment)", [r["rule"] for r in rules],
                               "inferred", "the chatbot's answers and help pages",
                               [f"{r['rule']} ← {r['evidence']}" for r in rules if r["evidence"]]))
        known = {t["name"] for t in tools}
        tools += [t for t in inferred_tools if t["name"] not in known]
    if tools:
        items.append(_item("available_tools", "Available tools", tools, "found" if api_tools else "inferred",
                           "its API" if api_tools else "the chatbot's answers"))

    return {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "api_docs": spec_url or "",
            "items": items, "metrics": coverage(config, items), "notes": notes,
            "used": {"fetches": budget.fetches, "messages": budget.messages}, "approved": []}


def apply(config: dict, ids: list[str]) -> dict:
    """The config with the approved report items used. Paths the user typed are
    never replaced; nothing unapproved changes."""
    report = config.get("discovery") or {}
    items = {i["id"]: i for i in report.get("items") or []}
    new = dict(config)
    for item_id in ids:
        item = items.get(item_id)
        if item is None:
            continue
        kind, value = item["kind"], item["value"]
        if kind in ("tools_path", "context_path"):
            new[kind] = new.get(kind) or value
        elif kind == "request":
            new["url"], new["body_template"] = value["url"], value["body_template"]
            for field in ("tools_path", "context_path"):
                if value.get(field) and not new.get(field):
                    new[field] = value[field]
        elif kind == "instructions":
            new["inferred_instructions"] = list(value)
        elif kind == "available_tools":
            new["available_tools"] = list(value)
    new["discovery"] = {**report, "approved": [i for i in ids if i in items]}
    new["discovery"]["metrics"] = coverage(new, list(items.values()))
    return new
