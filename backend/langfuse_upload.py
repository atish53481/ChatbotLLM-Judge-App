"""Send a finished scan to Langfuse (Langfuse Cloud or a self-hosted Langfuse) as one trace.

Optional, like the Confident AI upload: without both keys nothing is sent. The
trace is built from what the scan stored, so nothing is judged again:

* the trace "LLM Judge scan <n> · <chatbot>" (its root span): the scan's metrics
  and run options as input, its overall result as output, the judge model,
  judge tokens and duration as metadata;
* under it one span per metric run (score, pass/fail, threshold, judge model,
  judge tokens, duration, note), laid out in time the way the job ran them, and
  under each of those one span per test case: what was sent to the chatbot, its
  reply, the expected output and context when there are any, where the context
  came from, the reply's latency and, for red-team cases, the attack used;
* Langfuse scores: one per metric run on the trace (the run's score, with
  pass/fail and its note as comment) and one per case on the case's span (the
  case's score, with the judge's reason as comment).

How it goes out, with plain `requests` and HTTP basic auth (public key : secret
key), so the keys travel only in the Authorization header: never in a payload,
a log line or an error kept with the scan.

* The spans go to Langfuse's OpenTelemetry endpoint, POST {host}/api/public/otel/v1/traces
  (OTLP/HTTP JSON; header x-langfuse-ingestion-version: 4 so they show at once).
  Langfuse's legacy ingestion API stops taking trace and span events on
  Langfuse Cloud on 2026-11-16, and a self-hosted Langfuse v4 refuses them by default.
* The scores go to POST {host}/api/public/ingestion as score-create events,
  which Langfuse keeps supporting.

Large scans go in several requests of at most MAX_REQUEST_BYTES each. Every id
comes from the scan itself, so sending a scan again updates its trace in
Langfuse instead of adding a second one.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

DEFAULT_HOST = "https://cloud.langfuse.com"  # Langfuse Cloud (EU)
OTEL_PATH = "/api/public/otel/v1/traces"
INGESTION_PATH = "/api/public/ingestion"
ENV_PUBLIC_KEY, ENV_SECRET_KEY, ENV_HOST = "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"
TIMEOUT_S = 30
# Per request; Langfuse takes up to 3.5 MB per ingestion batch.
MAX_REQUEST_BYTES = 1_000_000
# Characters kept of one text (a question, reply, context item or reason).
MAX_TEXT = 10_000
MAX_CONTEXT_ITEMS = 20

_http = requests  # tests swap in a fake
# Trace, span and score ids are uuid5 values in this namespace, made from the scan's own ids.
_NAMESPACE = uuid.UUID("0c6f6a1e-8a52-4d0e-9b5c-4f1d2b7e9a31")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# A metric run's status -> how its span and score name it.
_VERDICTS = {"pass": "passed", "fail": "failed", "error": "error", "cancelled": "stopped"}
# Row fields that say how a red-team case attacked the chatbot, kept on its span as they are.
_RED_TEAM_FIELDS = ("enhancement", "base_probe_id", "vulnerability", "vulnerability_type")


class LangfuseError(RuntimeError):
    """Langfuse could not be reached or refused the trace; the message says why (never with a key)."""


def env_settings() -> dict:
    """LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST from the environment (.env)."""
    def read(name: str) -> str | None:
        return (os.getenv(name) or "").strip() or None

    return {"public_key": read(ENV_PUBLIC_KEY), "secret_key": read(ENV_SECRET_KEY), "host": read(ENV_HOST)}


def normalize_host(host: str | None) -> str:
    """The Langfuse address requests go to: Langfuse Cloud when empty, https:// when no
    scheme is given, without a trailing slash or a pasted /api/public path. Anything
    that is not a plain http(s) address raises a ValueError saying so."""
    text = (host or "").strip()
    if not text:
        return DEFAULT_HOST
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or any(c.isspace() for c in text):
        raise ValueError(f"The Langfuse host must be a web address such as {DEFAULT_HOST}, "
                         "https://us.cloud.langfuse.com or http://localhost:3000.")
    if parsed.username or parsed.password:
        raise ValueError("Put the Langfuse keys in their own fields, not in the host address.")
    path = parsed.path.split("/api/public", 1)[0].rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def trace_url(host: str | None, trace_id: str) -> str:
    """Where Langfuse shows the trace (it finds the trace's project itself)."""
    return f"{normalize_host(host)}/trace/{trace_id}"


def safe_message(text, *secrets) -> str:
    """An error message with any of the given keys blanked out, fit to keep or log."""
    message = str(text)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


# --- Building the trace --------------------------------------------------------

def _hex(*parts, size: int = 32) -> str:
    return uuid.uuid5(_NAMESPACE, "/".join(str(p) for p in parts)).hex[:size]


def _number(value) -> float | None:
    """A finite number as a float (scores, seconds, thresholds); None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value) -> str | None:
    """Text cut to MAX_TEXT characters; None when there is nothing to show."""
    if value is None or value == "" or value == []:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TEXT else f"{text[:MAX_TEXT]}… (+{len(text) - MAX_TEXT} chars)"


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _model(name) -> str | None:
    return name.replace(" (Local Model)", "") if isinstance(name, str) and name else None


def _moment(stamp) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _nanos(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _seconds_ns(seconds: float) -> int:
    return int(round(seconds * 1_000_000_000))


def _iso(nanos: int) -> str:
    """An ISO 8601 UTC time with milliseconds, as Langfuse's events carry."""
    moment = _EPOCH + timedelta(microseconds=nanos // 1_000)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1_000:03d}Z"


def _any(value) -> dict:
    """One OTLP AnyValue in its JSON encoding (64-bit integers as strings)."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return {"arrayValue": {"values": [{"stringValue": v} for v in value]}}
    return {"stringValue": _json_text(value)}


def _attributes(values: dict) -> list[dict]:
    """OTLP attributes, leaving out the empty ones."""
    return [{"key": key, "value": _any(value)} for key, value in values.items()
            if value is not None and value != "" and value != []]


def _metadata(prefix: str, values: dict) -> dict:
    """Langfuse reads metadata from attributes named <prefix>.<key>."""
    return {f"{prefix}.{key}": value for key, value in values.items()}


def _span(trace_id: str, span_id: str, parent_id: str | None, name: str, start: int, end: int,
          attributes: dict, error: str | None = None) -> dict:
    span = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,  # SPAN_KIND_INTERNAL
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start, end)),
            "attributes": _attributes(attributes),
            # STATUS_CODE_ERROR = 2, STATUS_CODE_OK = 1
            "status": {"code": 2, "message": error} if error else {"code": 1}}
    if parent_id:
        span["parentSpanId"] = parent_id
    return span


def _schedule(results: list[dict], parallel) -> list[tuple[float, float]]:
    """(start, end) of each metric run, in seconds from the scan's start, laid out the
    way the job ran them: `parallel` at a time, each next one starting as soon as one
    finished (a run records how long it took, not when it started)."""
    lanes = [0.0] * (parallel if isinstance(parallel, int) and parallel > 0 else 1)
    out = []
    for result in results:
        lane = lanes.index(min(lanes))
        begin = lanes[lane]
        lanes[lane] = begin + max(0.0, _number(result.get("duration_s")) or 0.0)
        out.append((begin, lanes[lane]))
    return out


def _judge_model(results: list[dict]) -> str | None:
    return next((m for m in (_model((r.get("judge") or {}).get("model")) for r in results) if m), None)


def _attack(row: dict, key: str, attacks: dict, enhancements: dict) -> str | None:
    """How a red-team case attacked the chatbot: the enhancer that disguised the probe,
    "Baseline" for the probe as written, or the multi-turn attack its metric ran."""
    if row.get("enhancement"):
        return enhancements.get(row["enhancement"], row["enhancement"])
    if row.get("probe"):
        return "Baseline"
    return attacks.get(key)


def _case_name(index: int, question) -> str:
    line = " ".join(str(question or "").split())
    if not line:
        return f"Case {index + 1}"
    return f"Case {index + 1}: {line[:60]}{'…' if len(line) > 60 else ''}"


def _verdict_line(verdict: str, score: float | None, threshold: float | None) -> str:
    parts = [f"score {score:g}" if score is not None else None,
             f"threshold {threshold:g}" if threshold is not None else None]
    shown = ", ".join(p for p in parts if p)
    return f"{verdict} ({shown})" if shown else verdict


def build_trace(scan: dict, chatbot: str, theme: str, titles: dict, *, settings: dict | None = None,
                attacks: dict | None = None, enhancements: dict | None = None, parallel=1) -> dict:
    """The scan (scans.get_scan) as one Langfuse trace: {"trace_id", "name", "timestamp",
    "spans", "scores"}. spans are OTLP spans: the root, then each metric run followed by
    its test cases; scores are score-create bodies. titles: metric key -> title;
    settings: the job's per-metric settings (shown in the trace's input); attacks: metric
    key -> the multi-turn attack its cases ran; enhancements: attack enhancer key ->
    label; parallel: how many metrics the job ran at once."""
    results = scan.get("results") or []
    attacks, enhancements = attacks or {}, enhancements or {}
    trace_id = _hex("scan", scan.get("target_id"), scan.get("job_id"), scan.get("started_at"))
    name = f"LLM Judge scan {scan.get('version')} · {chatbot}"
    started = _moment(scan.get("started_at")) or _moment(scan.get("finished_at")) or datetime.now(timezone.utc)
    finished = _moment(scan.get("finished_at"))
    t0 = _nanos(started)
    duration = max(0.0, (finished - started).total_seconds()) if finished else None
    schedule = _schedule(results, parallel)

    def title(key):
        return titles.get(key) or key

    keys = [r.get("key") for r in results]
    chosen = {k: v for k, v in (settings or {}).items() if k in keys and v}
    trace_input = {
        "chatbot": chatbot, "golden_set": theme,
        "metrics": [{"key": r.get("key"), "title": title(r.get("key")), "threshold": _number(r.get("threshold"))}
                    for r in results],
        "options": scan.get("run_options") or {},
        **({"settings": chosen} if chosen else {}),
        **({"skipped": scan["skipped"]} if scan.get("skipped") else {}),
    }
    trace_output = {
        "result": scan.get("result"), "metrics_passed": scan.get("passed"), "metrics_failed": scan.get("failed"),
        "metric_errors": scan.get("errors"), "test_cases": scan.get("test_cases"),
        "test_cases_passed": scan.get("test_cases_passed"), "average": scan.get("average"),
        **({"note": scan["note"]} if scan.get("note") else {}),
    }
    cost = sum(c for c in (_number(r.get("judge_cost_usd")) for r in results) if c is not None)
    trace_meta = {
        "chatbot": chatbot, "golden_set": theme, "scan": scan.get("version"), "job_id": scan.get("job_id"),
        "result": scan.get("result"), "judge_model": _judge_model(results),
        "judge_tokens": sum(_count((r.get("judge") or {}).get("tokens")) or 0 for r in results),
        "duration_s": duration, "judge_cost_usd": round(cost, 6) if cost else None,
    }
    # Langfuse filters on trace name, tags and metadata only when every span carries them.
    shared = {"langfuse.trace.name": name, "langfuse.trace.tags": ["llm-judge", str(scan.get("result") or "unknown")],
              **_metadata("langfuse.trace.metadata", trace_meta)}
    input_text, output_text = _json_text(trace_input), _json_text(trace_output)
    root_id = _hex(trace_id, "root", size=16)
    end = t0 + _seconds_ns(max([duration or 0.0] + [finish for _, finish in schedule]))
    spans = [_span(trace_id, root_id, None, name, t0, end, {
        **shared,
        "langfuse.trace.input": input_text, "langfuse.trace.output": output_text,
        # Langfuse v4 shows the root span's own input and output as the trace's.
        "langfuse.observation.type": "span",
        "langfuse.observation.input": input_text, "langfuse.observation.output": output_text,
        "langfuse.observation.level": "DEFAULT" if scan.get("result") == "passed" else "WARNING",
    })]
    scores: list[dict] = []
    for index, (result, (begin, finish)) in enumerate(zip(results, schedule)):
        key = result.get("key") or f"metric {index + 1}"
        metric_title, status = title(key), result.get("status")
        metric_id = _hex(trace_id, "metric", index, key, size=16)
        rows = result.get("rows") or []
        score, threshold = _number(result.get("score")), _number(result.get("threshold"))
        judge, latency = result.get("judge") or {}, result.get("latency") or {}
        note = _text(result.get("note"))
        error = (_text(result.get("error")) or "error") if status == "error" else None
        verdict = _VERDICTS.get(status, status or "unknown")
        metric_meta = {
            "key": key, "score": score, "passed": status == "pass", "status": status, "threshold": threshold,
            "direction": result.get("direction"), "judge_model": _model(judge.get("model")),
            "judge_tokens": _count(judge.get("tokens")), "judge_calls": _count(judge.get("calls")),
            "duration_s": _number(result.get("duration_s")), "cases_run": _count(result.get("cases_run")),
            "cases_passed": _count(result.get("cases_passed")), "latency_p50_s": _number(latency.get("p50")),
            "latency_p95_s": _number(latency.get("p95")), "judge_cost_usd": _number(result.get("judge_cost_usd")),
            "attack": attacks.get(key), "note": note, "error": error,
        }
        spans.append(_span(trace_id, metric_id, root_id, metric_title, t0 + _seconds_ns(begin),
                           t0 + _seconds_ns(finish), {
            **shared,
            "langfuse.observation.type": "span",
            "langfuse.observation.input": _json_text({"metric": metric_title, "key": key, "threshold": threshold,
                                                      "direction": result.get("direction"), "cases": len(rows)}),
            "langfuse.observation.output": _json_text({"score": score, "passed": status == "pass", "status": status,
                                                       "cases_passed": result.get("cases_passed"),
                                                       "cases_run": result.get("cases_run")}),
            **_metadata("langfuse.observation.metadata", metric_meta),
            "langfuse.observation.level": ("ERROR" if status == "error"
                                           else "DEFAULT" if status == "pass" else "WARNING"),
            "langfuse.observation.status_message": error or (None if status == "pass"
                                                             else _verdict_line(verdict, score, threshold)),
        }, error=error))
        if score is not None:
            scores.append({"id": _hex(trace_id, "score", index, key), "traceId": trace_id, "name": metric_title,
                           "value": score, "dataType": "NUMERIC",
                           "comment": _text(f"{_verdict_line(verdict, None, threshold)}"
                                            f"{f' · {note}' if note else ''}")})
        slot = (finish - begin) / len(rows) if rows else 0.0
        for row_index, row in enumerate(rows):
            case_id = _hex(trace_id, "case", index, row_index, size=16)
            case_begin = begin + row_index * slot
            wait = _number(row.get("latency_s"))
            passed, case_score = bool(row.get("passed")), _number(row.get("score"))
            context = [t for t in (_text(c) for c in (row.get("context") or [])[:MAX_CONTEXT_ITEMS]) if t]
            case_meta = {
                "golden_id": row.get("golden_id"), "score": case_score, "passed": passed,
                "expected_output": _text(row.get("expected_output")), "context": context or None,
                "context_source": row.get("context_source"), "latency_s": wait,
                "tools_called": _text(row.get("tools_called")),
                "attack": _attack(row, key, attacks, enhancements),
                **{field: row.get(field) for field in _RED_TEAM_FIELDS},
            }
            spans.append(_span(trace_id, case_id, metric_id, _case_name(row_index, row.get("question") or row.get("input")),
                               t0 + _seconds_ns(case_begin), t0 + _seconds_ns(case_begin + (wait if wait is not None else slot)), {
                **shared,
                "langfuse.observation.type": "span",
                "langfuse.observation.input": _text(row.get("input") or row.get("question")),
                "langfuse.observation.output": _text(row.get("actual_output")),
                **_metadata("langfuse.observation.metadata", case_meta),
                "langfuse.observation.level": "DEFAULT" if passed else "WARNING",
                "langfuse.observation.status_message": None if passed else _verdict_line(
                    "failed", case_score, threshold),
            }))
            if case_score is not None:
                scores.append({"id": _hex(trace_id, "score", index, key, row_index), "traceId": trace_id,
                               "observationId": case_id, "name": metric_title, "value": case_score,
                               "dataType": "NUMERIC",
                               **({"comment": _text(row["reason"])} if row.get("reason") else {})})
    return {"trace_id": trace_id, "name": name, "timestamp": _iso(_nanos(finished or started)),
            "spans": spans, "scores": scores}


# --- Sending -------------------------------------------------------------------

def _otlp(spans: list[dict]) -> dict:
    """An OTLP ExportTraceServiceRequest (JSON encoding) holding these spans."""
    return {"resourceSpans": [{"resource": {"attributes": _attributes({"service.name": "llm-judge"})},
                               "scopeSpans": [{"scope": {"name": "llm-judge"}, "spans": spans}]}]}


def _batch(events: list[dict]) -> dict:
    return {"batch": events}


def _chunks(items: list[dict], wrap) -> list[list[dict]]:
    """items in groups whose request body stays under MAX_REQUEST_BYTES (an item
    bigger than that goes on its own)."""
    base = len(json.dumps(wrap([])))
    chunks: list[list[dict]] = []
    current, size = [], base
    for item in items:
        length = len(json.dumps(item)) + 1
        if current and size + length > MAX_REQUEST_BYTES:
            chunks.append(current)
            current, size = [], base
        current.append(item)
        size += length
    if current:
        chunks.append(current)
    return chunks


def _detail(body) -> str:
    if not isinstance(body, dict):
        return ""
    found = body.get("message") or body.get("error") or body.get("detail") or ""
    return " ".join(str(found if isinstance(found, str) else json.dumps(found)).split())[:200]


def _post(url: str, payload: dict, auth: tuple[str, str], headers: dict | None = None) -> dict:
    response = _http.post(url, json=payload, auth=auth, headers=headers or {}, timeout=TIMEOUT_S)
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    if status in (401, 403):
        raise LangfuseError(f"Langfuse refused the keys (HTTP {status}). Check the public key, the secret key "
                            f"and the host: keys work only on the Langfuse they were made on (EU {DEFAULT_HOST}, "
                            "US https://us.cloud.langfuse.com, or your own server).")
    if not 200 <= status < 300:
        detail = _detail(body) or " ".join(str(getattr(response, "text", "") or "").split())[:200]
        hint = " Is the host a Langfuse server (v3.22 or newer)?" if status == 404 else ""
        raise LangfuseError(f"Langfuse answered HTTP {status}{f': {detail}' if detail else ''}.{hint}")
    if not isinstance(body, dict):
        raise LangfuseError("The host did not answer like Langfuse (no JSON reply): check the Langfuse host.")
    return body


def upload(scan: dict, chatbot: str, theme: str, titles: dict, public_key: str, secret_key: str,
           host: str | None = None, **trace_options) -> dict:
    """Sends the scan as one trace (build_trace takes trace_options); returns {"trace_id",
    "url", "spans", "scores"}, plus "rejected_scores" and "warning" when Langfuse turned
    some scores down. Raises LangfuseError when Langfuse cannot be reached or refuses."""
    if not (public_key and secret_key):
        raise LangfuseError("Langfuse needs both keys: the public key (pk-lf-…) and the secret key (sk-lf-…).")
    try:
        base = normalize_host(host)
    except ValueError as error:
        raise LangfuseError(str(error)) from error
    trace = build_trace(scan, chatbot, theme, titles, **trace_options)
    auth = (public_key, secret_key)
    events = [{"id": uuid.uuid4().hex, "timestamp": trace["timestamp"], "type": "score-create", "body": body}
              for body in trace["scores"]]
    rejected, warnings = 0, []
    try:
        for spans in _chunks(trace["spans"], _otlp):
            body = _post(f"{base}{OTEL_PATH}", _otlp(spans), auth, {"x-langfuse-ingestion-version": "4"})
            partial = body.get("partialSuccess") or {}
            if isinstance(partial, dict) and partial.get("rejectedSpans"):
                warnings.append(f"Langfuse turned down {partial['rejectedSpans']} spans: "
                                f"{_detail({'message': partial.get('errorMessage')})}")
        for batch in _chunks(events, _batch):
            body = _post(f"{base}{INGESTION_PATH}", _batch(batch), auth)
            errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
            if errors:
                rejected += len(errors)
                warnings.append(_detail(errors[0]) or f"HTTP {errors[0].get('status')}")
    except requests.RequestException as error:
        raise LangfuseError(f"Could not reach Langfuse at {base} ({type(error).__name__}).") from error
    sent = {"trace_id": trace["trace_id"], "url": trace_url(base, trace["trace_id"]),
            "spans": len(trace["spans"]), "scores": len(trace["scores"])}
    if rejected:
        sent["rejected_scores"] = rejected
    if warnings:
        sent["warning"] = (f"Langfuse turned down {rejected} of {len(events)} scores: " if rejected else "") \
            + "; ".join(warnings)[:300]
    return sent
