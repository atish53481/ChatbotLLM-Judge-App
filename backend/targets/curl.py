"""Turns a browser's "Copy as cURL (bash)" command into an HTTP target config.

Chatbots on real websites have no documented API, but the browser's network
tab shows the exact request the page sends. Pasting that request here gives
the URL, headers (cookies, CSRF tokens, auth) and body the site expects.
The question the user typed is found and replaced with {{message}}, earlier
turns are dropped, and the reply's location is found in a sample response.
"""
from __future__ import annotations

import json
import shlex
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

MESSAGE_PLACEHOLDER = "{{message}}"

_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"}
_HEADER_FLAGS = {"-H", "--header"}
_METHOD_FLAGS = {"-X", "--request"}
_COOKIE_FLAGS = {"-b", "--cookie"}
_URL_FLAGS = {"--url"}
_UA_FLAGS = {"-A", "--user-agent"}
_REFERER_FLAGS = {"-e", "--referer"}
# Flags whose argument we skip (they don't change what the chatbot receives).
_IGNORED_WITH_ARG = {
    "-o", "--output", "-m", "--max-time", "--connect-timeout", "-u", "--user",
    "-x", "--proxy", "--retry", "-w", "--write-out", "--cacert", "--cert",
}


_ANSI_C_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
    "e": "\x1b", "E": "\x1b", "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _read_ansi_c(text: str, start: int) -> tuple[str, int]:
    """Decodes a bash $'...' string whose opening quote is at start; returns (value, end)."""
    out = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "'":
            return "".join(out), i + 1
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _ANSI_C_ESCAPES:
            out.append(_ANSI_C_ESCAPES[nxt])
            i += 2
        elif nxt in "uUx":
            width = {"u": 4, "U": 8, "x": 2}[nxt]
            digits = ""
            j = i + 2
            while j < len(text) and len(digits) < width and text[j] in "0123456789abcdefABCDEF":
                digits += text[j]
                j += 1
            out.append(chr(int(digits, 16)) if digits else "\\" + nxt)
            i = j
        elif nxt in "01234567":
            j = i + 1
            while j < len(text) and j < i + 4 and text[j] in "01234567":
                j += 1
            out.append(chr(int(text[i + 1:j], 8)))
            i = j
        else:
            out.append("\\" + nxt)
            i += 2
    raise ValueError("a $'...' string is not closed")


def _expand_ansi_c_quotes(text: str) -> str:
    """Rewrites bash $'...' strings (Chrome uses them when a body holds a quote
    or an escape) as plain quoted strings shlex understands."""
    out = []
    i = 0
    quote = None
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
            i += 1
        elif ch == "$" and text[i + 1:i + 2] == "'":
            value, i = _read_ansi_c(text, i + 1)
            out.append(shlex.quote(value))
        else:
            if ch in "'\"":
                quote = ch
            elif ch == "\\" and i + 1 < len(text):
                out.append(ch)
                i += 1
                ch = text[i]
            out.append(ch)
            i += 1
    return "".join(out)


def parse_curl(command: str) -> dict:
    """Returns {url, method, headers, body} from a cURL command line.

    Accepts the bash flavour Chrome/Edge/Firefox copy, including $'...'
    strings. The Windows "cmd" flavour escapes quotes with ^ and is not supported.
    """
    text = command.strip().replace("\\\r\n", " ").replace("\\\n", " ")
    if '^"' in text or text.endswith("^"):
        raise ValueError('use "Copy as cURL (bash)", not "Copy as cURL (cmd)"')
    try:
        tokens = shlex.split(_expand_ansi_c_quotes(text))
    except ValueError as e:
        raise ValueError(f"could not read the cURL command: {e}") from e
    if not tokens or tokens[0] != "curl":
        raise ValueError("paste a command that starts with curl")

    url = None
    method = None
    headers: dict[str, str] = {}
    data_parts: list[str] = []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        arg = tokens[i + 1] if i + 1 < len(tokens) else None
        if token in _HEADER_FLAGS and arg is not None:
            name, _, value = arg.partition(":")
            if name.strip():
                headers[name.strip()] = value.strip()
            i += 2
        elif token in _DATA_FLAGS and arg is not None:
            data_parts.append(arg)
            if token == "--json":
                headers.setdefault("Content-Type", "application/json")
            i += 2
        elif token in _METHOD_FLAGS and arg is not None:
            method = arg.upper()
            i += 2
        elif token in _COOKIE_FLAGS and arg is not None:
            headers["Cookie"] = arg
            i += 2
        elif token in _UA_FLAGS and arg is not None:
            headers["User-Agent"] = arg
            i += 2
        elif token in _REFERER_FLAGS and arg is not None:
            headers["Referer"] = arg
            i += 2
        elif token in _URL_FLAGS and arg is not None:
            url = arg
            i += 2
        elif token in _IGNORED_WITH_ARG:
            i += 2
        elif token.startswith("-"):
            i += 1  # --compressed, -s, -L, -k, ... carry no argument
        else:
            url = url or token
            i += 1

    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("the cURL command has no http:// or https:// URL")
    body = "&".join(data_parts)
    # curl reads a body starting with @ from a file; a browser never copies one.
    if body.startswith("@"):
        raise ValueError("request bodies read from a file (@file) are not supported")
    return {
        "url": url,
        "method": method or ("POST" if body else "GET"),
        "headers": headers,
        "body": body,
    }


# --- Finding the question and the reply without asking the user -------------
# Keys compare lower-cased with "_" and "-" removed, in priority order.
_MESSAGE_KEYS = (
    "message", "prompt", "query", "question", "input", "userinput", "usermessage",
    "chatinput", "userquery", "text", "content", "msg", "utterance", "q",
)
# Earlier turns the page sent along; they are emptied so each golden question
# is asked on its own instead of replaying the captured conversation.
_HISTORY_KEYS = (
    "history", "chathistory", "conversationhistory", "messagehistory",
    "previousmessages", "pastmessages",
)
_REPLY_PATHS = (
    "choices.0.message.content", "choices.0.delta.content", "choices.0.text",
    "candidates.0.content.parts.0.text", "message.content", "content.0.text",
    "delta.text", "delta.content",
)
_REPLY_KEYS = (
    "reply", "answer", "response", "output", "outputtext", "generatedtext", "completion",
    "result", "text", "message", "content", "botreply", "botmessage", "data",
)


def _norm(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def _lookup(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def _nodes(obj: Any):
    """(dotted path, key, value) for every dict entry and list item, shallowest first."""
    queue = [("", obj)]
    while queue:
        base, node = queue.pop(0)
        if isinstance(node, dict):
            items = list(node.items())
        elif isinstance(node, list):
            items = list(enumerate(node))
        else:
            items = []
        for key, value in items:
            child = f"{base}.{key}" if base else str(key)
            yield child, str(key), value
            if isinstance(value, (dict, list)):
                queue.append((child, value))


def _find_by_keys(obj: Any, keys: tuple[str, ...]) -> str | None:
    """Path of the shallowest non-empty string under one of keys (earlier keys win per depth)."""
    best = None
    for path, key, value in _nodes(obj):
        norm = _norm(key)
        if norm in keys and isinstance(value, str) and value.strip():
            rank = (path.count("."), keys.index(norm), path)
            if best is None or rank < best:
                best = rank
    return best[2] if best else None


def _longest_string(obj: Any) -> str | None:
    best = None
    for path, _key, value in _nodes(obj):
        if isinstance(value, str) and value.strip() and (best is None or len(value) > len(best[1])):
            best = (path, value)
    return best[0] if best else None


def _set_path(obj: Any, dotted: str, value: Any) -> None:
    *parents, last = dotted.split(".")
    for part in parents:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    if isinstance(obj, list):
        obj[int(last)] = value
    else:
        obj[last] = value


def _trim_conversation(node: Any) -> Any:
    """Empties history arrays and cuts chat-style `messages` to system + last user turn."""
    if isinstance(node, list):
        return [_trim_conversation(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if _norm(key) in _HISTORY_KEYS and isinstance(value, list):
            out[key] = []
        elif key == "messages" and isinstance(value, list) and value and all(
            isinstance(m, dict) and "role" in m for m in value
        ):
            users = [m for m in value if m.get("role") == "user"]
            out[key] = [m for m in value if m.get("role") == "system"] + users[-1:]
        else:
            out[key] = _trim_conversation(value)
    return out


def _message_path(data: Any) -> str | None:
    messages = data.get("messages") if isinstance(data, dict) else None
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict) \
            and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, str):
            return f"messages.{len(messages) - 1}.content"
        path = _find_by_keys(content, ("text",))
        if path:
            return f"messages.{len(messages) - 1}.content.{path}"
    return _find_by_keys(data, _MESSAGE_KEYS) or _longest_string(data)


def prepare_body(body: str, sample_message: str = "") -> tuple[str, str]:
    """Turns a captured request body into a template.

    Returns (body with {{message}} where the question goes, the question found).
    With sample_message the matching value is replaced; without it the question
    is found by key name (message, prompt, query, ...), by the last user turn of
    a chat-style `messages` list, or as the longest text value.
    """
    if not body:
        return body, ""
    try:
        data = json.loads(body)
    except ValueError:
        return _prepare_form(body, sample_message)
    data = _trim_conversation(data)
    sample = sample_message.strip()
    if sample:
        marked = [p for p, _k, v in _nodes(data) if isinstance(v, str) and v.strip() == sample]
        for path in marked:
            _set_path(data, path, MESSAGE_PLACEHOLDER)
        if marked:
            return json.dumps(data, ensure_ascii=False), sample
        return body.replace(sample_message, MESSAGE_PLACEHOLDER), sample
    path = _message_path(data)
    if not path:
        return json.dumps(data, ensure_ascii=False), ""
    found = _lookup(data, path)
    _set_path(data, path, MESSAGE_PLACEHOLDER)
    return json.dumps(data, ensure_ascii=False), found


def _prepare_form(body: str, sample_message: str) -> tuple[str, str]:
    pairs = parse_qsl(body, keep_blank_values=True)
    if not pairs or "=" not in body:
        if sample_message and sample_message in body:
            return body.replace(sample_message, MESSAGE_PLACEHOLDER), sample_message
        return body, ""
    target = None
    for index, (_key, value) in enumerate(pairs):
        if sample_message and value.strip() == sample_message.strip():
            target = index
            break
    if target is None and not sample_message:
        ranked = [(_MESSAGE_KEYS.index(_norm(k)), i) for i, (k, v) in enumerate(pairs)
                  if _norm(k) in _MESSAGE_KEYS and v.strip()]
        target = min(ranked)[1] if ranked else None
    if target is None:
        return body, ""
    parts = [
        f"{quote_plus(k)}={MESSAGE_PLACEHOLDER if i == target else quote_plus(v)}"
        for i, (k, v) in enumerate(pairs)
    ]
    return "&".join(parts), pairs[target][1]


def prepare_url(url: str, sample_message: str = "") -> tuple[str, str]:
    """Same as prepare_body for a question carried in the query string (GET chatbots)."""
    parts = urlsplit(url)
    if not parts.query:
        return url, ""
    query, found = _prepare_form(parts.query, sample_message)
    if not found:
        return url, ""
    return urlunsplit(parts._replace(query=query)), found


def find_reply_path(data: Any) -> str:
    """Dotted path of the reply text in a JSON response, "" when none is found."""
    for path in _REPLY_PATHS:
        value = _lookup(data, path)
        if isinstance(value, str) and value.strip():
            return path
    return _find_by_keys(data, _REPLY_KEYS) or _longest_string(data) or ""


def find_stream_reply_path(events: list[Any]) -> str:
    """The reply path that carries text in the most stream events."""
    counts: dict[str, int] = {}
    for event in events:
        if isinstance(event, (dict, list)):
            path = find_reply_path(event)
            if path:
                counts[path] = counts.get(path, 0) + 1
    return max(counts, key=counts.get) if counts else ""


# Where a chatbot's response carries what its retriever found (RAG metrics).
_CONTEXT_KEYS = (
    "retrievalcontext", "context", "contexts", "sources", "sourcedocuments", "documents",
    "docs", "citations", "chunks", "references", "passages",
)
_CONTEXT_TEXT_KEYS = ("text", "content", "pagecontent", "chunk", "snippet", "body", "passage")


def find_history_path(body_template: str) -> str:
    """The body key that carries earlier turns ("history", "messages", ...), or ""."""
    try:
        data = json.loads(body_template)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key, value in data.items():
        if isinstance(value, list) and (_norm(key) in _HISTORY_KEYS or key == "messages"):
            return key
    return ""


def find_context_path(data: Any) -> str:
    """Dotted path of the retrieved documents in a JSON response, "" when there are none."""
    best = None
    for path, key, value in _nodes(data):
        norm = _norm(key)
        if norm in _CONTEXT_KEYS and isinstance(value, list) and value:
            rank = (path.count("."), _CONTEXT_KEYS.index(norm), path)
            if best is None or rank < best:
                best = rank
    return best[2] if best else ""


_TOOLS_KEYS = ("toolcalls", "toolscalled", "toolsused", "functioncalls", "intermediatesteps")
_TOOL_NAME_KEYS = ("name", "tool", "toolname")
_TOOL_ARG_KEYS = ("arguments", "args", "input", "inputparameters", "parameters", "toolinput")
_TOOL_OUTPUT_KEYS = ("output", "result", "response", "observation")
_TOOL_REASON_KEYS = ("reasoning", "thought", "reason", "rationale")


def find_tools_path(data: Any) -> str:
    """Dotted path of the tool calls in a JSON response (agent metrics), "" when there are none."""
    best = None
    for path, key, value in _nodes(data):
        norm = _norm(key)
        if norm in _TOOLS_KEYS and isinstance(value, list) and value:
            rank = (path.count("."), _TOOLS_KEYS.index(norm), path)
            if best is None or rank < best:
                best = rank
    return best[2] if best else ""


def _first(item: dict, keys: tuple[str, ...]) -> Any:
    return next((item[k] for k in item if _norm(k) in keys and item[k] not in (None, "")), None)


def tool_calls(value: Any) -> list[dict]:
    """Turns a response's tool calls into {name, input_parameters?, output?, reasoning?}.
    Reads the OpenAI shape ({"function": {"name", "arguments": "<json>"}}) and
    plain ones ({"name", "args"|"input", "output"|"result"}); a bare string is a tool name."""
    items = value if isinstance(value, list) else [value]
    calls = []
    for item in items:
        if isinstance(item, str) and item.strip():
            calls.append({"name": item.strip()})
            continue
        if not isinstance(item, dict):
            continue
        source = item["function"] if isinstance(item.get("function"), dict) else item
        name = _first(source, _TOOL_NAME_KEYS)
        if not isinstance(name, str) or not name.strip():
            continue
        call: dict = {"name": name.strip()}
        args = _first(source, _TOOL_ARG_KEYS)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"input": args}
        if isinstance(args, dict) and args:
            call["input_parameters"] = args
        for field, keys in (("output", _TOOL_OUTPUT_KEYS), ("reasoning", _TOOL_REASON_KEYS)):
            found = _first(item, keys)
            if found is not None:
                call[field] = found if isinstance(found, str) else json.dumps(found, ensure_ascii=False)
        calls.append(call)
    return calls


def context_texts(value: Any) -> list[str]:
    """Turns a retrieved-documents value (strings or objects) into plain strings."""
    items = value if isinstance(value, list) else [value]
    texts = []
    for item in items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            found = next((item[k] for k in item if _norm(k) in _CONTEXT_TEXT_KEYS
                          and isinstance(item[k], str)), None)
            text = found if found is not None else json.dumps(item, ensure_ascii=False)
        elif item is None:
            continue
        else:
            text = str(item)
        if text.strip():
            texts.append(text)
    return texts
