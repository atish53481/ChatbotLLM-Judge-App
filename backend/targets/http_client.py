"""The chatbot connector: replays a request captured from a real website.

A chatbot on a website has no documented API, but the browser's network tab
shows the exact request its page sends. The user pastes that request as
cURL (see targets/curl.py); the config keeps its url, method, headers and
body, with {{message}} marking where the question goes. The reply is read
from the JSON response at a dotted path (list indices allowed as numeric
segments, e.g. "choices.0.message.content"), from a server-sent-events
stream by joining that path across events, or as the raw text when no path
is given.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

import requests

from backend import traffic, usage
from backend.targets.base import ChatbotClient, ChatReply
from backend.targets.curl import MESSAGE_PLACEHOLDER, context_texts, find_history_path
from backend.targets.reply_check import ChatbotUnavailable, service_message

TIMEOUT = 60
METHODS = ("GET", "POST", "PUT", "PATCH")
_TEXT_KEYS = ("url", "method", "body_template", "response_path", "context_path", "history_path")
# Headers from the captured request that would be wrong for a new body.
_DROPPED_HEADERS = ("content-length", "host", "accept-encoding")
_PREVIEW_CHARS = 400
# A busy chatbot (429/503) is retried this many times, waiting its Retry-After
# (capped) or else 2, 4, 8 seconds.
BUSY_STATUSES = (429, 503)
BUSY_RETRIES = 3
MAX_RETRY_WAIT = 30.0
MAX_SEND_DELAY = 60
# Patched in tests so retries and spacing cost no real time.
_sleep = time.sleep
_clock = time.monotonic

# send_delay pacing per chatbot URL, shared by every client and thread (runs of
# one job go side by side): {"lock": held while waiting and sending, "last": time}.
_PACES: dict[str, dict] = {}
_PACES_LOCK = threading.Lock()


def _pace_for(url: str) -> dict:
    with _PACES_LOCK:
        return _PACES.setdefault(url, {"lock": threading.Lock(), "last": None})


class MessageTooLong(ValueError):
    """The message is over the chatbot's max_message_length, so it was not sent."""


def _retry_wait(r: requests.Response, attempt: int) -> float:
    try:
        return min(float(r.headers.get("Retry-After", "")), MAX_RETRY_WAIT)
    except ValueError:
        return float(2 ** (attempt + 1))  # no (or a date-valued) Retry-After


def _status_error(r: requests.Response, url: str) -> ValueError:
    host = urlsplit(url).netloc
    if r.status_code in (401, 403):
        return ValueError(
            f"Chatbot rejected the request (HTTP {r.status_code} from {host}): its session or "
            "cookie has probably expired. Copy a fresh cURL from the chatbot's site and edit "
            f"the chatbot. Response: {_preview(r.text)}"
        )
    if r.status_code in BUSY_STATUSES:
        return ValueError(
            f"Chatbot is busy (HTTP {r.status_code} from {host}) and still was after "
            f"{BUSY_RETRIES} retries. Try again later, or set a delay between messages. "
            f"Response: {_preview(r.text)}"
        )
    return ValueError(f"HTTP {r.status_code} from {url}: {_preview(r.text)}")


def _get_path(obj: Any, dotted: str) -> Any:
    current = obj
    try:
        for part in dotted.split("."):
            if isinstance(current, list):
                current = current[int(part)]
            else:
                current = current[part]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ValueError(
            f"reply not found at response_path {dotted!r} ({type(e).__name__}: {e})"
        ) from e
    return current


def _preview(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


def is_stream(r: requests.Response) -> bool:
    return "text/event-stream" in r.headers.get("Content-Type", "") or r.text.lstrip().startswith("data:")


def sse_events(text: str) -> list[Any]:
    """Decoded `data:` payloads of a server-sent-events body, in order."""
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except ValueError:
            events.append(payload)
    return events


class HttpTargetClient(ChatbotClient):
    def __init__(self, config: dict):
        for key in _TEXT_KEYS:
            if key in config and not isinstance(config[key], str):
                raise ValueError(f"{key} must be a string")
        url = config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("url must be an http:// or https:// URL")
        headers = config.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in headers.items()
        ):
            raise ValueError("headers must map header names to string values")
        method = config.get("method", "POST").upper()
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        body_template = config.get("body_template", "")
        if MESSAGE_PLACEHOLDER not in url + body_template:
            raise ValueError(
                f"put {MESSAGE_PLACEHOLDER} in the body (or URL) where the question goes"
            )
        if method == "GET" and body_template:
            raise ValueError("a GET request has no body; put the placeholder in the URL")
        max_length = config.get("max_message_length")
        if max_length is not None and (
            isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1
        ):
            raise ValueError("max_message_length must be a whole number of characters above 0")
        send_delay = config.get("send_delay") or 0
        if isinstance(send_delay, bool) or not isinstance(send_delay, (int, float)) or not (
            0 <= send_delay <= MAX_SEND_DELAY
        ):
            raise ValueError(f"send_delay must be between 0 and {MAX_SEND_DELAY} seconds")
        check_replies = config.get("check_replies", True)
        if not isinstance(check_replies, bool):
            raise ValueError("check_replies must be true or false")

        self.url = url
        self.method = method
        self.headers = headers
        self.body_template = body_template
        self.response_path = config.get("response_path", "")
        # Where the response lists the retrieved documents (RAG metrics); "" = none.
        self.context_path = config.get("context_path", "")
        # Where the body carries earlier turns (multi-turn metrics). Older configs
        # have none saved, so it is read from the body ("history": [], "messages").
        self.history_path = config.get("history_path") or find_history_path(body_template)
        # Longest message the chatbot accepts (None = no limit); longer ones are not sent.
        self.max_message_length = max_length
        # Seconds to leave between two messages, for chatbots that rate-limit.
        self.send_delay = float(send_delay)
        # Treat service messages ("limit reached", "please log in") as errors.
        self.check_replies = check_replies
        self._pace = _pace_for(self.url) if self.send_delay else None

    def _encode_for_body(self, message: str) -> str:
        content_type = next(
            (v for k, v in self.headers.items() if k.lower() == "content-type"), ""
        ).lower()
        if "x-www-form-urlencoded" in content_type:
            return quote_plus(message)
        # json.dumps escapes quotes, newlines and backslashes, so the question
        # drops into a JSON string literal without breaking the body.
        return json.dumps(message, ensure_ascii=False)[1:-1]

    def chat(self, message: str, history: list[dict] | None = None) -> ChatReply:
        # Golden questions are asked on their own (no history); multi-turn
        # scenarios pass the conversation so far, which goes into history_path.
        host = urlsplit(self.url).netloc
        earlier = f" (+{len(history)} earlier turns)" if history else ""
        traffic.log("-> chatbot %s%s: %s", host, earlier, traffic.clip(message))
        started = time.monotonic()
        try:
            reply = self.read_reply(self.send(message, history))
        except Exception as e:
            traffic.log("x  chatbot %s failed after %.1fs: %s", host, time.monotonic() - started, e)
            raise
        traffic.log("<- chatbot %s in %.1fs: %s", host, time.monotonic() - started, traffic.clip(reply.reply))
        if self.check_replies:
            problem = service_message(reply.reply)
            if problem:
                raise ChatbotUnavailable(
                    f"{problem[0].upper()}{problem[1:]} instead of an answer, so nothing was "
                    f"scored: {_preview(reply.reply)!r}. If this is a real answer, turn off "
                    "'Treat service messages as errors' for this chatbot."
                )
        return reply

    def _with_history(self, body: str, history: list[dict]) -> str:
        """Puts earlier turns ({role, content}) where the captured body keeps them."""
        try:
            data = json.loads(body)
        except ValueError:
            return body  # form bodies carry no conversation
        if not isinstance(data, dict) or self.history_path not in data:
            return body
        turns = [{"role": t["role"], "content": t["content"]} for t in history]
        if self.history_path == "messages":
            # Chat-style body: earlier turns go between the system prompt and this question.
            current = data["messages"]
            system = [m for m in current[:-1] if isinstance(m, dict) and m.get("role") == "system"]
            data["messages"] = system + turns + current[-1:]
        else:
            data[self.history_path] = turns
        return json.dumps(data, ensure_ascii=False)

    def send(self, message: str, history: list[dict] | None = None) -> requests.Response:
        """Sends one question; raises ValueError on an HTTP error status."""
        if self.max_message_length and len(message) > self.max_message_length:
            raise MessageTooLong(
                f"message is {len(message)} characters; this chatbot's max message length "
                f"is {self.max_message_length}"
            )
        url = self.url.replace(MESSAGE_PLACEHOLDER, quote(message, safe=""))
        body = None
        if self.body_template:
            text = self.body_template.replace(MESSAGE_PLACEHOLDER, self._encode_for_body(message))
            if history and self.history_path:
                text = self._with_history(text, history)
            body = text.encode("utf-8")
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in _DROPPED_HEADERS
        }
        for attempt in range(BUSY_RETRIES + 1):
            if self._pace is None:
                r = self._request(url, body, headers)
            else:
                # One message at a time to a paced chatbot, send_delay apart.
                with self._pace["lock"]:
                    self._wait_for_send_delay()
                    r = self._request(url, body, headers)
            if r.status_code not in BUSY_STATUSES or attempt == BUSY_RETRIES:
                break
            _sleep(_retry_wait(r, attempt))
        if not r.ok:
            raise _status_error(r, url)
        return r

    def _wait_for_send_delay(self) -> None:
        last = self._pace["last"]
        if last is not None:
            wait = self.send_delay - (_clock() - last)
            if wait > 0:
                _sleep(wait)

    def _request(self, url: str, body: bytes | None, headers: dict) -> requests.Response:
        host = urlsplit(url).netloc
        try:
            r = requests.request(self.method, url, data=body, headers=headers, timeout=TIMEOUT)
        except requests.Timeout as e:
            raise ValueError(f"Chatbot at {host} did not answer within {TIMEOUT} s.") from e
        except requests.ConnectionError as e:
            raise ValueError(
                f"Could not reach the chatbot at {host}: check the URL and that the site is up. ({e})"
            ) from e
        finally:
            if self._pace is not None:
                self._pace["last"] = _clock()
        usage.record_target()
        return r

    def read_reply(self, r: requests.Response) -> ChatReply:
        text = r.text
        if is_stream(r):
            return ChatReply(reply=self._reply_from_stream(text), model="unknown", mode="http")
        if not self.response_path:
            return ChatReply(reply=text, model="unknown", mode="http")
        try:
            data = r.json()
        except ValueError as e:
            raise ValueError(f"response is not JSON: {_preview(text)}") from e
        try:
            reply_text = _get_path(data, self.response_path)
        except ValueError as e:
            raise ValueError(f"{e}. Response was: {_preview(text)}") from e
        # Some APIs answer with a JSON list, which carries no model name.
        model = data.get("model", "unknown") if isinstance(data, dict) else "unknown"
        retrieval = None
        if self.context_path:
            try:
                retrieval = context_texts(_get_path(data, self.context_path)) or None
            except ValueError:
                retrieval = None  # this answer came without sources
        return ChatReply(reply=str(reply_text), model=str(model), mode="http",
                         retrieval_context=retrieval)

    def _reply_from_stream(self, text: str) -> str:
        events = sse_events(text)
        if not self.response_path:
            return "".join(e if isinstance(e, str) else json.dumps(e) for e in events)
        pieces = []
        for event in events:
            try:
                piece = _get_path(event, self.response_path)
            except ValueError:
                continue  # role/usage/metadata events carry no text
            if isinstance(piece, str):
                pieces.append(piece)
        if not pieces:
            raise ValueError(
                f"no stream event has text at response_path {self.response_path!r}; "
                f"first events: {_preview(json.dumps(events[:3], ensure_ascii=False))}"
            )
        return "".join(pieces)
