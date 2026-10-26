"""FastAPI backend: the control panel behind the Chrome sidebar and dashboard."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import cancel, confident_upload, storage, traffic, usage
from backend.dashboard import hardening
from backend.dashboard import scans as scan_history
from backend.dashboard.compare import compare_runs
from backend.dashboard import jobs as jobs_module
from backend.dashboard.jobs import MAX_METRIC_CONCURRENCY, MIN_METRIC_CONCURRENCY, JobFailed, JobQueue
from backend.dashboard.pairwise import run_pairwise
from backend.dashboard.runner import (
    NEEDS_OWN_SOURCES, RunCancelled, missing_target_fields, judge_one, run_spec,
)
from backend.datasets import attack_enhancers
from backend.datasets import autogen
from backend.datasets import conversations as conversations_store
from backend.datasets import custom_metrics as custom_store
from backend.datasets import goldens as goldens_store
from backend.datasets import simulator
from backend.datasets import security_probes as probes_store
from backend.judges.judge import JUDGE_CONCURRENCY, build_judge, judge_config
from backend.metrics_catalog import (
    DEFAULT_THEME, ENVIRONMENTS, GROUPS, PROBE_DATASETS, SPECS_BY_KEY, UI_CATEGORIES,
    UI_CATEGORY_LABELS, all_specs, ask, get_spec as _catalog_spec, metric_options,
)
from backend.rag.enhance import enhance_document
from backend.export_suite import build_suite, zip_suite
from backend.redteam_coverage import coverage_report
from backend.rag.fetch import fetch_page_text
from backend.datasets.autogen import find_help_pages
from backend.rag.generation_options import EVOLUTION_NAMES
from backend.targets.base import ChatbotClient
from backend.targets.curl import (
    MESSAGE_PLACEHOLDER,
    find_context_path,
    find_history_path,
    find_reply_path,
    find_stream_reply_path,
    find_tools_path,
    parse_curl,
    prepare_body,
    prepare_url,
)
from backend.targets.http_client import HttpTargetClient, is_stream, sse_events
from backend.targets.shared import SharedReplies
from backend.targets import instructions as target_instructions
from backend.targets import presets as target_presets

DB_PATH = os.getenv("JUDGE_DB_PATH", "judge.db")
TARGET_TYPES = ("http",)
SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".docx", ".md", ".markdown", ".mdx"}
DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "datasets" / "documents"
# The web app's pages, served at /app/ (same origin as the API, so no CORS).
WEB_APP_DIR = Path(__file__).resolve().parent.parent.parent / "ChatbotExtension"


def get_spec(key: str):
    """A shipped metric, else one of the user's own (custom_metrics)."""
    return SPECS_BY_KEY.get(key) or _catalog_spec(key)


def generate_goldens_from_document(*args, **kwargs) -> int:
    # Imported on first use: sentence-transformers/transformers take ~40s to
    # import, which would otherwise stall server startup.
    from backend.rag.generate import generate_goldens_from_document as generate

    return generate(*args, **kwargs)


def generate_goldens_from_scratch(*args, **kwargs) -> list[dict]:
    # Imported on first use, like the document generator (DeepEval's Synthesizer).
    from backend.rag.scratch import generate_goldens_from_scratch as generate

    return generate(*args, **kwargs)


app = FastAPI(title="Chrome Sidebar LLM Judge", version=hardening.APP_VERSION)
# No CORS middleware on purpose: only the web app (same origin) may call this
# API. Allowing other origins would let any page the user visits read stored
# target API keys and spend judge credits. Request bodies must be JSON (FastAPI
# refuses other content types), which forces a preflight that other sites fail,
# so they cannot send POSTs either.
# Blocks DNS-rebinding pages that reach 127.0.0.1 under their own host name.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
# Request log (paths and status only), body size limit, security headers and a
# JSON 500 for unexpected errors (backend/dashboard/hardening.py).
hardening.install(app)
app.mount("/app", StaticFiles(directory=WEB_APP_DIR, html=True), name="web-app")


@app.get("/", include_in_schema=False)
def web_app_home() -> RedirectResponse:
    """Opening the backend's address in a browser opens the web app."""
    return RedirectResponse("/app/")

_conn = storage.init_db(DB_PATH)
# Progress of runs in flight, keyed by the caller's run_id (see /api/run/progress),
# and the ones asked to stop (see /api/run/cancel).
_progress: dict[str, dict] = {}
_cancelled: set[str] = set()
_progress_lock = threading.Lock()


# job id -> its chatbot client, shared by the job's metrics so each question is
# sent once and every metric scores that one reply (DeepEval's test case).
_job_clients: dict[int, SharedReplies] = {}
_job_clients_lock = threading.Lock()


def _job_client(job: dict, row: dict) -> SharedReplies:
    with _job_clients_lock:
        client = _job_clients.get(job["id"])
        if client is None:
            client = _job_clients[job["id"]] = SharedReplies(_client_or_400(row))
        return client


def _run_job_metric(job: dict, metric_key: str, on_progress) -> dict:
    """Runs one metric of a background job, exactly as /api/run does. A job
    with a "kind" option is a pairwise comparison instead (/api/pairwise)."""
    if job["options"].get("kind") == "pairwise":
        return _run_pairwise_job(job, on_progress)
    row = storage.get_target(_conn, job["target_id"])
    if row is None:
        raise JobFailed("chatbot deleted")
    spec = get_spec(metric_key)
    if spec is None:
        return {"key": metric_key, "status": "error", "error": "metric not found", "score": None, "rows": []}
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise JobFailed(str(e)) from e
    try:
        target = _job_client(job, row)
    except HTTPException as e:
        raise JobFailed(str(e.detail)) from e
    options, config = job["options"], row["config"]
    try:
        return run_spec(
            spec, judge, target, job["target_id"], _conn,
            theme=config.get("theme") or DEFAULT_THEME,
            threshold=(options.get("thresholds") or {}).get(metric_key),
            on_progress=on_progress, persona=config.get("persona", ""), limit=options.get("limit"),
            probe_set=config.get("probe_set", ""), check_consistency=bool(options.get("check_consistency")),
            metric_kwargs=metric_options(spec, (options.get("settings") or {}).get(metric_key), config),
            enhancements=options.get("enhancements") or [], enhancement_options=options.get("enhancement_options"),
            max_latency_s=options.get("max_latency_s"), price_per_mtok=_judge_price(),
            calibration_examples=_calibration_examples(job["target_id"], metric_key)
            if options.get("use_calibration") else None,
            deepeval_options=options.get("deepeval"),
        )
    finally:
        _save_found_paths(job, target)


def _run_pairwise_job(job: dict, on_progress) -> dict:
    options = job["options"]
    row_a = storage.get_target(_conn, job["target_id"])
    row_b = storage.get_target(_conn, options["target_b"])
    if row_a is None or row_b is None:
        raise JobFailed("chatbot deleted")
    try:
        judge = build_judge(**_judge_overrides())
        target_a, target_b = _client_or_400(row_a), _client_or_400(row_b)
    except RuntimeError as e:
        raise JobFailed(str(e)) from e
    except HTTPException as e:
        raise JobFailed(str(e.detail)) from e
    theme = row_a["config"].get("theme") or DEFAULT_THEME
    items = goldens_store.load_goldens(theme=theme)
    if options.get("limit"):
        items = items[:options["limit"]]
    if not items:
        return {"key": "pairwise", "status": "error", "error": f"No goldens in theme {theme!r}.",
                "score": None, "rows": []}
    result = run_pairwise(judge, target_a, target_b, items, prompt=ask,
                          criteria=options.get("criteria", ""), on_progress=on_progress,
                          names=(row_a["name"], row_b["name"]))
    result["theme"] = theme
    if result["status"] == "done":
        result["id"] = storage.add_pairwise_run(_conn, row_a["id"], row_b["id"], result)
    return result


# Paths a job found in the chatbot's replies (HttpTargetClient.discovered) that the
# chatbot had none saved for, and the metrics they switched on:
# job id -> {"paths": {"tools_path": ...}, "keys": [...]}. Read by _after_job.
_found: dict[int, dict] = {}
_found_lock = threading.Lock()
_FOUND_LABELS = {"tools_path": "tool calls", "context_path": "sources"}


def _runnable_keys(config: dict) -> set[str]:
    """Metrics that can run for this chatbot config and have cases to run."""
    theme, persona, probe_set, unavailable = _availability(config)
    return {s.key for s in all_specs()
            if not unavailable(s) and _cases_available(s, theme, persona, probe_set)}


def _save_found_paths(job: dict, target) -> None:
    """Saves the paths the chatbot's replies showed and its config lacks (a path
    the user typed is never replaced), and notes the metrics they switch on."""
    found = getattr(target, "discovered", None)
    if not found:
        return
    with _found_lock:
        row = storage.get_target(_conn, job["target_id"])
        if row is None:
            return
        config = row["config"]
        new = {field: path for field, path in found.items() if not config.get(field)}
        if not new:
            return
        updated = {**config, **new}
        before, after = _runnable_keys(config), _runnable_keys(updated)
        storage.update_target(_conn, row["id"], row["name"], row["type"], updated)
        entry = _found.setdefault(job["id"], {"paths": {}, "keys": []})
        entry["paths"].update(new)
        entry["keys"] += [s.key for s in all_specs()
                          if s.key in after and s.key not in before and s.key not in entry["keys"]]
    for field, path in new.items():
        traffic.log("== job %s: found %s at %r in the chatbot's replies; saved on the chatbot",
                    job["id"], _FOUND_LABELS.get(field, field), path)


def _after_job(job_id: int) -> None:
    """Records the paths a finished job found, and queues the metrics they switched
    on as one follow-up scan with the same settings (no loop: a saved path is
    never found again)."""
    with _job_clients_lock:
        _job_clients.pop(job_id, None)  # the job's shared replies are done with
    _auto_send_to_confident(job_id)
    with _found_lock:
        entry = _found.pop(job_id, None)
    if not entry:
        return
    storage.update_job(_conn, job_id, discovered=entry["paths"])
    job = storage.get_job(_conn, job_id)
    if not entry["keys"] or job["status"] == "cancelled":
        return  # a stopped job starts nothing new
    options = {**job["options"], "auto_discovered": {"from_job": job_id, "paths": entry["paths"]}}
    follow_up = jobs.submit(job["target_id"], entry["keys"], options)
    traffic.log("== job %s: queued job %s with %d metrics the found paths switched on",
                job_id, follow_up, len(entry["keys"]))


# Background runs: one job at a time on a worker thread (backend/dashboard/jobs.py).
# Tests run queued jobs themselves (JUDGE_START_JOB_WORKER=0) instead of racing it.
jobs = JobQueue(_conn, _run_job_metric, after_job=_after_job)
if os.getenv("JUDGE_START_JOB_WORKER", "1") != "0":
    jobs.start()


class TargetCreate(BaseModel):
    name: str = Field(min_length=1)
    type: str = "http"
    config: dict = Field(default_factory=dict)


class CurlParseRequest(BaseModel):
    curl: str = Field(min_length=1, max_length=200_000)
    # The question typed on the site when the request was captured; found
    # automatically when empty. It is swapped for {{message}}.
    sample_message: str = ""
    # Send the captured question once to find where the reply sits.
    probe: bool = True


class TargetTest(BaseModel):
    type: str = "http"
    config: dict = Field(default_factory=dict)
    message: str = Field(default="Hello", min_length=1)
    # Editing: reuse the stored headers when the form sends none (they are masked).
    target_id: int | None = None


class LlmImport(BaseModel):
    """An OpenAI-compatible LLM API to fill the chatbot form from (targets/presets.py)."""
    base_url: str = Field(default=target_presets.DEFAULT_BASE_URL, max_length=500)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=500)
    system_prompt: str = Field(default="", max_length=4000)


class GoldenCreate(BaseModel):
    theme: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_answer: str = Field(min_length=1)
    context: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    # Tools the chatbot should call for this question (Tool Correctness).
    # Omitted on an edit = keep the golden's; [] = remove them.
    expected_tools: list[str] | None = None

    def tool_names(self) -> list[str] | None:
        if self.expected_tools is None:
            return None
        return [t.strip() for t in self.expected_tools if t.strip()]


class RunRequest(BaseModel):
    target_id: int
    metric_key: str
    # Pass mark for this run only; the catalog default applies when omitted.
    threshold: float | None = Field(default=None, ge=0, le=1)
    # Chosen by the caller so it can poll /api/run/progress while the run works.
    run_id: str | None = Field(default=None, max_length=64)
    # Cases per run from the dashboard; None = every case in the dataset.
    limit: int | None = Field(default=None, ge=1)
    # Score every reply twice to measure how steady the judge is (~2x judge tokens).
    check_consistency: bool = False
    # The metric's tunable settings (e.g. Turn Relevancy's window_size); see /api/metrics.
    settings: dict = Field(default_factory=dict)
    # Red-team probes only: enhanced variants to add (see /api/security/enhancers).
    enhancements: list[str] = Field(default_factory=list, max_length=10)
    enhancement_options: dict = Field(default_factory=dict)
    # Fail the run when 95% of chatbot replies are not back within this many seconds.
    max_latency_s: float | None = Field(default=None, gt=0, le=600)
    # G-Eval metrics only: add this chatbot's human-scored cases as few-shot examples.
    use_calibration: bool = False


class RunCancel(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)


class JudgeOneRequest(BaseModel):
    metric_key: str
    question: str = Field(min_length=1)
    actual_output: str = Field(min_length=1)
    theme: str | None = None
    expected_answer: str = ""
    context: list[str] = Field(default_factory=list)
    threshold: float | None = Field(default=None, ge=0, le=1)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    target_id: int
    message: str = Field(min_length=1)
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)


def _build_target(row: dict) -> ChatbotClient:
    if row["type"] == "http":
        return HttpTargetClient(row["config"])
    raise ValueError(f"unknown target type {row['type']!r}")


def _client_or_400(row: dict) -> ChatbotClient:
    try:
        return _build_target(row)
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid target config: {e}") from e


def _target_or_404(target_id: int) -> dict:
    row = storage.get_target(_conn, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="target not found")
    return row


def _public(row: dict) -> dict:
    """A target as the extension sees it: header values (API keys) masked."""
    config = dict(row["config"])
    if config.get("headers"):
        config["headers"] = {name: "***" for name in config["headers"]}
    return {**row, "config": config}


def _keep_stored_headers(config: dict, existing: dict | None) -> dict:
    """The extension only ever sees masked headers, so an edit that sends none
    keeps the stored ones (cookies, API keys) instead of wiping them."""
    if existing is None or "headers" in config:
        return config
    stored = existing["config"].get("headers")
    return {**config, "headers": stored} if stored else config


@app.post("/api/targets/parse-curl")
def api_parse_curl(body: CurlParseRequest):
    try:
        parsed = parse_curl(body.curl)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    body_template, message = prepare_body(parsed["body"], body.sample_message)
    url = parsed["url"]
    if MESSAGE_PLACEHOLDER not in body_template:
        url, message = prepare_url(url, body.sample_message)
    result = {
        "url": url,
        "method": parsed["method"],
        "headers": parsed["headers"],
        "body_template": body_template,
        "sample_message": message,
        "message_marked": MESSAGE_PLACEHOLDER in url + body_template,
        "response_path": "",
        "context_path": "",
        "tools_path": "",
        "history_path": find_history_path(body_template),
        "reply_preview": "",
        "probe_error": None,
        "instructions": target_instructions.from_body(body_template),
        "existing": _saved_chatbot(url, parsed["method"]),
    }
    if result["message_marked"] and body.probe:
        result.update(_probe_reply_path(result, message))
    return result


def _saved_chatbot(url: str, method: str) -> dict | None:
    """The newest saved chatbot this request belongs to (same method and URL),
    so re-pasting a cURL updates it instead of adding a duplicate."""
    for target in reversed(storage.list_targets(_conn)):
        config = target["config"]
        if config.get("url") == url and (config.get("method") or "POST").upper() == method.upper():
            return {"id": target["id"], "name": target["name"],
                    "instructions": config.get("instructions") or []}
    return None


def _probe_reply_path(parsed: dict, message: str) -> dict:
    """Sends the captured question once and finds where the reply sits in the answer."""
    config = {k: parsed[k] for k in ("url", "method", "headers", "body_template")}
    try:
        client = HttpTargetClient(config)
        response = client.send(message or "Hello")
        context_path = tools_path = ""
        if is_stream(response):
            path = find_stream_reply_path(sse_events(response.text))
        else:
            try:
                data = response.json()
                path = find_reply_path(data)
                # Retrieved documents, when the chatbot returns them (RAG metrics).
                context_path = find_context_path(data)
                # Tool calls, when the chatbot is an agent that reports them (agent metrics).
                tools_path = find_tools_path(data)
            except ValueError:
                path = ""  # plain-text answer: the whole body is the reply
        client.response_path = path
        reply = client.read_reply(response).reply
    except Exception as e:  # noqa: BLE001 - the fields are still filled; the user can fix the path
        return {"probe_error": f"{type(e).__name__}: {e}"}
    return {"response_path": path, "context_path": context_path, "tools_path": tools_path,
            "reply_preview": reply[:500]}


@app.post("/api/targets/parse-llm")
def api_parse_llm(body: LlmImport):
    """Fills the chatbot form for an OpenAI-compatible LLM API, so another model
    can be judged without capturing a request from a website."""
    try:
        config = target_presets.build_llm_config(
            base_url=body.base_url, model=body.model,
            api_key=body.api_key, system_prompt=body.system_prompt,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {**config, "instructions": target_instructions.lines(body.system_prompt),
            "existing": _saved_chatbot(config["url"], config["method"])}


@app.post("/api/targets/test")
def api_test_target(body: TargetTest):
    """Sends one question with an unsaved config, so the form can be checked first."""
    if body.type not in TARGET_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type must be one of: {', '.join(TARGET_TYPES)}"
        )
    existing = storage.get_target(_conn, body.target_id) if body.target_id else None
    config = _keep_stored_headers(body.config, existing)
    client = _client_or_400({"id": body.target_id or 0, "type": body.type, "config": config})
    try:
        reply = client.chat(body.message)
    except Exception as e:  # noqa: BLE001 - any chatbot failure is shown in the form
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "reply": reply.reply}


# Judge settings saved from the side panel (meta table) override .env.
_JUDGE_META = {"api_key": "judge_api_key", "model": "judge_model", "base_url": "judge_base_url"}


def _judge_overrides() -> dict:
    stored = {name: storage.get_meta(_conn, key) for name, key in _JUDGE_META.items()}
    return {name: value for name, value in stored.items() if value}


def _key_hint(api_key: str) -> str:
    return f"••••{api_key[-4:]}" if len(api_key) > 8 else ("••••" if api_key else "")


class JudgeSettings(BaseModel):
    # Empty api_key keeps the saved one (it is never sent back to the extension).
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=500)
    clear_key: bool = False


_PRICE_META = "judge_price_per_mtok"


def _judge_price() -> float | None:
    value = storage.get_meta(_conn, _PRICE_META)
    try:
        return float(value) if value else None
    except ValueError:
        return None


@app.get("/api/judge/settings")
def api_get_judge_settings():
    overrides = _judge_overrides()
    config = judge_config(**overrides)
    return {
        "model": config["model"],
        "base_url": config["base_url"],
        "has_key": bool(config["api_key"]),
        "key_hint": _key_hint(config["api_key"]),
        # "panel" when saved from the side panel, "env" when it comes from .env.
        "key_source": "panel" if "api_key" in overrides else ("env" if config["api_key"] else None),
    }


class JudgePrice(BaseModel):
    # USD per million judge tokens, for the cost shown with each run; null clears it.
    price_per_mtok: float | None = Field(default=None, ge=0, le=1000)


@app.get("/api/judge/price")
def api_get_judge_price():
    return {"price_per_mtok": _judge_price()}


@app.put("/api/judge/price")
def api_put_judge_price(body: JudgePrice):
    price = body.price_per_mtok
    storage.set_meta(_conn, _PRICE_META, f"{price:g}" if price else None)
    return api_get_judge_price()


@app.put("/api/judge/settings")
def api_put_judge_settings(body: JudgeSettings):
    base_url = body.base_url.strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base URL must start with http:// or https://")
    if body.clear_key:
        storage.set_meta(_conn, _JUDGE_META["api_key"], None)
    elif body.api_key.strip():
        storage.set_meta(_conn, _JUDGE_META["api_key"], body.api_key.strip())
    # Empty model / base URL fall back to .env and the defaults.
    storage.set_meta(_conn, _JUDGE_META["model"], body.model.strip() or None)
    storage.set_meta(_conn, _JUDGE_META["base_url"], base_url or None)
    return api_get_judge_settings()


@app.post("/api/judge/settings/test")
def api_test_judge_settings(body: JudgeSettings):
    """One small call to the judge with the form's values (saved key if none typed)."""
    overrides = _judge_overrides()
    for name in ("api_key", "model", "base_url"):
        value = getattr(body, name).strip()
        if value:
            overrides[name] = value
    try:
        judge = build_judge(**overrides)
        reply = judge.generate('Reply with this JSON only: {"ok": true}')
    except Exception as e:  # noqa: BLE001 - shown in the settings form
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"}
    text = reply[0] if isinstance(reply, tuple) else reply
    return {"ok": True, "model": judge_config(**overrides)["model"], "reply": str(text)[:200]}


@app.get("/api/health")
def api_health():
    """Liveness for monitoring: 200 while the database answers, 503 if not."""
    worker = jobs._thread
    status, body = hardening.health(
        _conn, judge_configured=bool(judge_config(**_judge_overrides())["api_key"]),
        worker_alive=worker.is_alive() if worker is not None else None)
    return JSONResponse(status_code=status, content=body)


@app.get("/api/status")
def api_status():
    config = judge_config(**_judge_overrides())
    return {"judge": {"model": config["model"], "up": bool(config["api_key"])}}


@app.get("/api/targets")
def api_list_targets():
    return [_public(row) for row in storage.list_targets(_conn)]


@app.post("/api/targets")
def api_create_target(body: TargetCreate):
    if body.type not in TARGET_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type must be one of: {', '.join(TARGET_TYPES)}"
        )
    config = body.config
    # Build the client once so a broken config is rejected before it is stored.
    _client_or_400({"id": 0, "type": body.type, "config": config})
    target_id = storage.add_target(_conn, body.name, body.type, config)
    return _public(storage.get_target(_conn, target_id))


@app.put("/api/targets/{target_id}")
def api_update_target(target_id: int, body: TargetCreate):
    existing = _target_or_404(target_id)
    if body.type not in TARGET_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type must be one of: {', '.join(TARGET_TYPES)}"
        )
    config = _keep_stored_headers(body.config, existing)
    # Build the client once so a broken config is rejected before it is stored;
    # same validation POST does.
    _client_or_400({"id": target_id, "type": body.type, "config": config})
    storage.update_target(_conn, target_id, body.name, body.type, config)
    return _public(storage.get_target(_conn, target_id))


@app.delete("/api/targets/{target_id}")
def api_delete_target(target_id: int):
    _target_or_404(target_id)
    storage.delete_target(_conn, target_id)
    return {"deleted": target_id}


@app.delete("/api/targets/{target_id}/runs")
def api_clear_target_runs(target_id: int):
    _target_or_404(target_id)
    cleared = storage.clear_runs(_conn, target_id)
    return {"target_id": target_id, "cleared": cleared}


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: int):
    if not storage.delete_run(_conn, run_id):
        raise HTTPException(status_code=404, detail="run not found")
    return {"deleted": run_id}


@app.delete("/api/scans/{job_id}")
def api_delete_scan(job_id: int):
    """Deletes one finished test run (scan) and the runs it recorded."""
    job = storage.get_job(_conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="scan not found")
    if job["status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="this run is still going; stop it first")
    return {"deleted": job_id, "runs_deleted": storage.delete_scan(_conn, job_id)}


# --- Confident AI: optional copy of each test run in the Confident AI portal ----
# The key is saved here (side panel) or read from CONFIDENT_API_KEY in .env; it is
# never sent back to the browser. Without a key nothing is ever sent anywhere.
_CONFIDENT_KEY_META = "confident_api_key"
_CONFIDENT_AUTO_META = "confident_auto"


def _confident_key() -> tuple[str | None, str | None]:
    """(key, where it came from: "panel" or "env")."""
    saved = storage.get_meta(_conn, _CONFIDENT_KEY_META)
    if saved:
        return saved, "panel"
    env = confident_upload.api_key()
    return (env, "env") if env else (None, None)


_CONFIDENT_PROJECT_META = "confident_project"


class ConfidentSettings(BaseModel):
    # Empty api_key keeps the saved one; auto None keeps the current choice.
    api_key: str = Field(default="", max_length=500)
    # Needed with an organization key: the project whose key the app creates.
    project: str = Field(default="", max_length=200)
    clear_key: bool = False
    auto: bool | None = None


@app.get("/api/confident/settings")
def api_get_confident_settings():
    key, source = _confident_key()
    return {"has_key": bool(key), "key_source": source, "key_hint": _key_hint(key or ""),
            "auto": bool(key) and storage.get_meta(_conn, _CONFIDENT_AUTO_META) == "1",
            "project": storage.get_meta(_conn, _CONFIDENT_PROJECT_META) if source == "panel" else None}


@app.put("/api/confident/settings")
def api_put_confident_settings(body: ConfidentSettings):
    auto = body.auto
    key, project = body.api_key.strip(), body.project.strip() or None
    if body.clear_key:
        storage.set_meta(_conn, _CONFIDENT_KEY_META, None)
        storage.set_meta(_conn, _CONFIDENT_PROJECT_META, None)
    elif key:
        if confident_upload.is_org_key(key):
            # Test runs need a project key: make one with the org key, keep only that.
            try:
                key, project = confident_upload.project_key_from_org(key, body.project)
            except confident_upload.ConfidentKeyError as error:
                raise HTTPException(status_code=400, detail=str(error))
            except Exception as error:  # noqa: BLE001 - network trouble, never the key
                raise HTTPException(status_code=502, detail=f"Could not reach Confident AI: {str(error)[:200]}")
        storage.set_meta(_conn, _CONFIDENT_KEY_META, key)
        storage.set_meta(_conn, _CONFIDENT_PROJECT_META, project)
        auto = True if auto is None else auto  # a new key means "send my runs"
    if auto is not None:
        storage.set_meta(_conn, _CONFIDENT_AUTO_META, "1" if auto else None)
    return api_get_confident_settings()


def _send_to_confident(job_id: int, key: str) -> dict:
    """Uploads one finished scan and keeps the link with its job."""
    scan = scan_history.get_scan(_conn, job_id)
    target = storage.get_target(_conn, scan["target_id"])
    name = target["name"] if target else f"chatbot {scan['target_id']}"
    theme = ((target or {}).get("config") or {}).get("theme") or DEFAULT_THEME
    titles = {s.key: s.title for s in all_specs()}
    sent = confident_upload.upload(scan, name, theme, titles, api_key=key)
    job = storage.get_job(_conn, job_id)
    storage.update_job(_conn, job_id, options={**job["options"], "confident": {
        "id": sent["id"], "link": sent["link"],
        "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}})
    traffic.log("== job %s: sent %d test cases to Confident AI", job_id, sent["test_cases"])
    return sent


def _auto_send_to_confident(job_id: int) -> None:
    """After a finished (not stopped) scan, when the user turned auto-send on."""
    key, _ = _confident_key()
    if not key or storage.get_meta(_conn, _CONFIDENT_AUTO_META) != "1":
        return
    job = storage.get_job(_conn, job_id)
    if job is None or job["status"] != "done" or (job["options"] or {}).get("kind") == "pairwise":
        return
    try:
        _send_to_confident(job_id, key)
    except Exception as error:  # noqa: BLE001 - the run itself is fine either way
        storage.update_job(_conn, job_id, options={**job["options"], "confident": {"error": str(error)[:300]}})
        traffic.log("== job %s: sending to Confident AI failed: %s", job_id, str(error)[:200])


@app.post("/api/scans/{job_id}/confident")
def api_send_scan_to_confident(job_id: int):
    """Sends one finished test run to Confident AI and returns its portal link."""
    key, _ = _confident_key()
    if not key:
        raise HTTPException(status_code=400, detail="No Confident AI key: add one in the side panel (Confident AI).")
    job = storage.get_job(_conn, job_id)
    if job is None or scan_history.get_scan(_conn, job_id) is None:
        raise HTTPException(status_code=404, detail="scan not found")
    if job["status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="this run is still going; send it when it finishes")
    try:
        return _send_to_confident(job_id, key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:  # noqa: BLE001 - Confident AI's own message, never the key
        raise HTTPException(status_code=502, detail=f"Confident AI: {str(error)[:300]}")


@app.post("/api/session/start")
def api_session_start():
    """Mark a fresh panel load: views then show only the runs after `since_id`.

    Every run stays in the database; the History view still lists all of them.
    """
    return {"since_id": storage.max_run_id(_conn)}


class GenerationOptions(BaseModel):
    """Optional Synthesizer settings (backend/rag/generate.py); all empty keeps
    DeepEval's defaults. review saves the goldens as drafts to accept first."""
    evolutions: list[str] = Field(default_factory=list, max_length=7)
    num_evolutions: int | None = Field(default=None, ge=1, le=5)
    quality_threshold: float | None = Field(default=None, ge=0, le=1)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    scenario: str = Field(default="", max_length=500)
    task: str = Field(default="", max_length=500)
    input_format: str = Field(default="", max_length=500)
    expected_output_format: str = Field(default="", max_length=500)
    review: bool = False
    # Goldens per document (rag/generate.py DEFAULT_MAX_GOLDENS = 2 when unset).
    max_goldens: int | None = Field(default=None, ge=1, le=20)


def _generation_options(options: GenerationOptions | None) -> dict:
    if options is None:
        return {}
    data = {k: v for k, v in options.model_dump().items() if v not in (None, "", [], False)}
    unknown = [e for e in data.get("evolutions", []) if e not in EVOLUTION_NAMES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown evolution: {', '.join(unknown)}")
    return data


@app.post("/api/documents")
def api_upload_document(
    theme: str = Form(...), file: UploadFile = File(...), enhance: str = Form("true"),
    generation: str = Form(""),
):
    """Saves the upload and starts golden generation in the background: the
    response is the document row (status "queued"); poll GET /api/documents/{id}.
    generation: optional JSON of GenerationOptions."""
    # Read as text: newer FastAPI turns an omitted bool form field into False, not its default.
    enhance = enhance.strip().lower() not in ("false", "0", "no", "off")
    try:
        options = _generation_options(GenerationOptions.model_validate_json(generation)) if generation else {}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid generation options: {e}") from e
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    safe_filename = Path(file.filename or "document").name
    extension = Path(safe_filename).suffix.lower()
    if extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type {extension!r}: supported types are "
            f"{', '.join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))}",
        )
    document_id = storage.add_document(_conn, theme, safe_filename)
    theme_dir = DOCUMENTS_DIR / theme
    theme_dir.mkdir(parents=True, exist_ok=True)
    dest = theme_dir / f"{document_id}_{_storable_stem(safe_filename)}{extension}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    _start_job(lambda: _run_document_job(document_id, dest, theme, safe_filename, judge, enhance, options))
    return storage.get_document(_conn, document_id)


def _start_job(job) -> None:
    """Generation makes many judge calls (minutes), so it runs off the request.
    Tests swap this for an inline call."""
    threading.Thread(target=job, daemon=True).start()


def _run_document_job(document_id: int, dest: Path, theme: str, source: str, judge, enhance: bool,
                      options: dict | None = None) -> None:
    """queued -> enhancing (the judge rewrites the text as clean reference prose)
    -> generating (DeepEval's Synthesizer) -> ready | error."""
    source_path = dest
    if enhance:
        storage.set_document_status(_conn, document_id, "enhancing")
        try:
            source_path = enhance_document(dest, judge)
        except Exception as e:  # noqa: BLE001 - reported on the document, not raised
            storage.set_document_status(
                _conn, document_id, "error", f"enhancing failed: {type(e).__name__}: {e}", goldens_created=0
            )
            return
        if source_path != dest:
            storage.set_document_enhanced(_conn, document_id, str(source_path))
    storage.set_document_status(_conn, document_id, "generating")
    _generate_for_document(document_id, source_path, theme, source, judge, options)


def _storable_stem(name: str) -> str:
    """The file name's stem reduced to [A-Za-z0-9_-]. DeepEval names a Chroma
    collection after the saved file, and Chroma rejects spaces, brackets and
    the like — the Synthesizer then silently finds 0 chunks and makes nothing."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_-")
    return stem or "document"


def _generate_for_document(document_id: int, dest: Path, theme: str, source: str, judge,
                           options: dict | None = None) -> None:
    """Runs golden generation for a saved document and records the outcome.
    The run replaces whatever this document generated before, so re-uploading
    or regenerating changes the dataset instead of stacking a second copy on
    it. A run that creates nothing is an error the user needs to see."""
    goldens_store.delete_generated_for(theme, source_document=source, source_document_id=document_id)
    try:
        extra = {"options": options} if options else {}
        created = generate_goldens_from_document(str(dest), theme, source, judge, document_id=document_id, **extra)
    except Exception as e:  # noqa: BLE001 - any generation failure is reported, not a 500
        storage.set_document_status(_conn, document_id, "error", f"{type(e).__name__}: {e}", goldens_created=0)
        return
    if created:
        storage.set_document_status(_conn, document_id, "ready", goldens_created=created)
    else:
        storage.set_document_status(
            _conn, document_id, "error",
            "DeepEval created no golden answers from this document: it found no usable "
            "passages (the text may be too short, or data such as JSON rather than prose).",
            goldens_created=0,
        )


class UrlDocument(BaseModel):
    theme: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    url: str = Field(min_length=1, max_length=2000)
    enhance: bool = True
    generation: GenerationOptions | None = None


@app.post("/api/documents/url")
def api_document_from_url(body: UrlDocument):
    """Fetches a help / FAQ page, keeps its text as the theme's reference
    document, and generates goldens from it like an uploaded file."""
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        text, final_url = fetch_page_text(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _queue_text_document(body.theme, final_url, _host_stem(final_url), text, judge,
                                body.enhance, _generation_options(body.generation))


def _host_stem(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]", "_", urlparse(url).netloc) or "page"


def _queue_text_document(theme: str, source: str, stem: str, text: str, judge,
                         enhance: bool = True, options: dict | None = None) -> dict:
    """Keeps text as one of the theme's reference documents and generates
    goldens (with Context) from it in the background."""
    document_id = storage.add_document(_conn, theme, source)
    theme_dir = DOCUMENTS_DIR / theme
    theme_dir.mkdir(parents=True, exist_ok=True)
    dest = theme_dir / f"{document_id}_{stem}.txt"
    dest.write_text(text, encoding="utf-8")
    _start_job(lambda: _run_document_job(document_id, dest, theme, source, judge, enhance, options))
    return storage.get_document(_conn, document_id)


class PrepareGoldens(BaseModel):
    help_url: str = Field(default="", max_length=2000)
    # AI generation spends judge tokens, so it runs only once the user says yes.
    generate: bool = False


INSTRUCTIONS_SOURCE = "chatbot instructions"
NEEDS_CONTEXT_MESSAGE = (
    "No help page or instructions to learn this chatbot's knowledge from. Add golden answers "
    "with Question, Expected answer and Context (the facts the answer should rest on, one per "
    "line) to run Faithfulness, Hallucination, Summarization and the Contextual metrics.")


def _own_theme(target: dict) -> str:
    """A chatbot on the shared default theme gets one of its own, named after it,
    so its goldens and scenarios persist and never mix with another chatbot's."""
    theme = target["config"].get("theme") or DEFAULT_THEME
    if theme != DEFAULT_THEME:
        return theme
    base = re.sub(r"[^a-z0-9]+", "_", target["name"].lower()).strip("_")[:48] or f"chatbot_{target['id']}"
    taken = {t["config"].get("theme") for t in storage.list_targets(_conn) if t["id"] != target["id"]}
    taken |= {g["theme"] for g in goldens_store.load_goldens()} - {base}
    taken |= {DEFAULT_THEME, "general_support"}
    theme, n = base, 2
    while theme in taken:
        theme, n = f"{base}_{n}", n + 1
    return theme


def _learn_chatbot(target: dict, theme: str, judge, pages: list, persona: str,
                   errors: list[str]) -> tuple[int, int] | None:
    """Asks the chatbot what it does, then has the judge write golden answers and
    conversation scenarios that fit it (and its help pages). Returns how many."""
    try:
        answers = autogen.interview(_client_or_400(target))
    except HTTPException as e:
        answers = []
        errors.append(f"Could not reach the chatbot: {e.detail}")
    if not answers and not pages:
        errors.append("The chatbot did not answer and no help pages were found, so no golden answers "
                      "were written for it.")
        return None
    try:
        goldens, scenarios = autogen.write_dataset(judge, answers, pages, persona)
    except Exception as e:  # noqa: BLE001 - a judge failure is reported, the rest still runs
        errors.append(f"Writing golden answers failed: {type(e).__name__}: {e}")
        return None
    for g in goldens:
        goldens_store.add_golden(theme, g["question"], g["expected_answer"], [], g["categories"],
                                 source=autogen.GOLDEN_SOURCE)
    for s in scenarios:
        conversations_store.add_conversation({**s, "theme": theme})
    return len(goldens), len(scenarios)


@app.post("/api/targets/{target_id}/prepare-goldens")
def api_prepare_goldens(target_id: int, body: PrepareGoldens):
    """Prepares a newly added chatbot's golden dataset: its own theme, goldens and
    scenarios learned from the chatbot and its help pages (the bot-agnostic starter
    goldens only when nothing was learned), and Context goldens generated in the
    background from its help / FAQ page and its instructions. With neither, the answer asks
    for Context goldens instead of guessing the chatbot's knowledge."""
    target = _target_or_404(target_id)
    config = target["config"]
    theme = _own_theme(target)
    help_url = body.help_url.strip() or config.get("help_url", "")
    updated = {**config, "theme": theme, **({"help_url": help_url} if help_url else {})}
    if updated != config:
        storage.update_target(_conn, target_id, target["name"], target["type"], updated)

    instructions = config.get("instructions") or []
    known = {d["filename"] for d in storage.list_documents(_conn, theme) if d["status"] != "error"}
    learned = any(g.get("source") == autogen.GOLDEN_SOURCE for g in goldens_store.load_goldens(theme=theme))
    # Sources not generated from yet: the chatbot itself (asked what it does),
    # the help page (or a search for one), then the instructions.
    pending = ([autogen.SOURCE_CHATBOT] if not learned else []) \
        + ([help_url] if help_url and help_url not in known else []) \
        + ([autogen.SOURCE_SEARCH] if not help_url and not updated.get("docs_searched") else []) \
        + ([INSTRUCTIONS_SOURCE] if instructions and INSTRUCTIONS_SOURCE not in known else [])
    documents, errors, pages = [], [], []
    learned_counts = None
    judge = None
    if body.generate and pending:
        try:
            judge = build_judge(**_judge_overrides())
        except RuntimeError as e:
            errors.append(str(e))
    if judge and help_url in pending:
        try:
            text, final_url = fetch_page_text(help_url)
        except ValueError as e:
            errors.append(f"Help page {help_url}: {e}")
        else:
            pages.append((final_url, text))
            documents.append(_queue_text_document(theme, final_url, _host_stem(final_url), text, judge))
    if judge and autogen.SOURCE_SEARCH in pending:
        # No help page given: look for the chatbot's FAQ / help / docs pages on its site.
        found = [(url, text) for url, text in find_help_pages(updated) if url not in known]
        pages += found
        for url, text in found:
            documents.append(_queue_text_document(theme, url, _host_stem(url), text, judge))
        updated = {**updated, "docs_searched": True, **({"help_url": found[0][0]} if found else {})}
        storage.update_target(_conn, target_id, target["name"], target["type"], updated)
        if not found:
            errors.append("No FAQ / help pages found on the chatbot's website; add a Help / FAQ page URL "
                          "if it has one.")
    if judge and autogen.SOURCE_CHATBOT in pending:
        learned_counts = _learn_chatbot(target, theme, judge, pages, updated.get("persona", ""), errors)
    if judge and INSTRUCTIONS_SOURCE in pending:
        text = "Rules and facts this chatbot follows:\n" + "\n".join(f"- {line}" for line in instructions)
        documents.append(_queue_text_document(theme, INSTRUCTIONS_SOURCE, "instructions", text, judge))
    can_generate = [] if body.generate else pending
    # Nothing is written before the user says yes (they may enter their own).
    # Then the bot-agnostic starters only fill a theme nothing was learned for:
    # seed_starter adds nothing once the chatbot's own goldens are there.
    seeded = goldens_store.seed_starter(theme) if body.generate else 0

    has_context = any(g["context"] for g in goldens_store.load_goldens(theme=theme))
    needs_context = not documents and not can_generate and not known and not has_context
    learned_text = (f"Learned this chatbot: {learned_counts[0]} golden answers and {learned_counts[1]} "
                    "conversation scenarios written for it. " if learned_counts else "")
    if needs_context:
        message = learned_text + NEEDS_CONTEXT_MESSAGE
    elif can_generate:
        message = (f"AI can build this chatbot's test data from {', '.join(can_generate)}: golden "
                   "answers and conversation scenarios that fit it, plus Context goldens from any help "
                   "pages (a few chatbot messages and judge calls). Generate them?")
    elif documents:
        message = (learned_text + "Generating Context goldens from "
                   f"{len(documents)} page{'s' if len(documents) != 1 else ''} in the background; they "
                   "appear under Golden answers when ready.")
    else:
        message = learned_text + "Golden dataset ready."
    return {"theme": theme, "seeded": seeded, "documents": documents, "errors": errors,
            "can_generate": can_generate, "needs_context": needs_context, "message": message}


@app.get("/api/documents")
def api_list_documents(theme: str | None = None):
    return storage.list_documents(_conn, theme)


@app.get("/api/documents/{document_id}")
def api_get_document(document_id: int):
    """One document's generation status, polled by the side panel while it works."""
    doc = storage.get_document(_conn, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


@app.get("/api/documents/{document_id}/enhanced")
def api_get_enhanced_text(document_id: int):
    """The AI-rewritten text golden generation actually read."""
    doc = storage.get_document(_conn, document_id)
    path = Path(doc["enhanced_path"]) if doc and doc.get("enhanced_path") else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="no enhanced text for this document")
    return {"text": path.read_text(encoding="utf-8")}


class DocumentRegenerate(BaseModel):
    """Empty on purpose: a JSON body forces the CORS preflight, like every other POST."""

    enhance: bool = True
    generation: GenerationOptions | None = None


@app.post("/api/documents/{document_id}/regenerate")
def api_regenerate_document(document_id: int, body: DocumentRegenerate):
    """Reruns an already-uploaded document's generation, replacing the goldens
    it produced last time — the dataset follows the document without the file
    having to be uploaded again."""
    doc = storage.get_document(_conn, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    dest = (DOCUMENTS_DIR / doc["theme"]
            / f'{document_id}_{_storable_stem(doc["filename"])}{Path(doc["filename"]).suffix}')
    if not dest.is_file():
        raise HTTPException(status_code=409, detail="the uploaded file is gone; upload it again")
    storage.set_document_status(_conn, document_id, "queued")
    options = _generation_options(body.generation)
    _start_job(lambda: _run_document_job(document_id, dest, doc["theme"], doc["filename"], judge, body.enhance,
                                         options))
    return storage.get_document(_conn, document_id)


@app.delete("/api/documents/{document_id}")
def api_delete_document(document_id: int):
    if storage.get_document(_conn, document_id) is None:
        raise HTTPException(status_code=404, detail="document not found")
    storage.delete_document(_conn, document_id)
    return {"deleted": document_id}


@app.get("/api/goldens")
def api_list_goldens(theme: str | None = None):
    return goldens_store.load_goldens(theme=theme)


class GoldenReview(BaseModel):
    golden_ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["accept", "reject"]


@app.post("/api/goldens/review")
def api_review_goldens(body: GoldenReview):
    """Accepts generated drafts (runs start sending them) or rejects them (deleted)."""
    return {"changed": goldens_store.review_goldens(body.golden_ids, body.action)}


@app.get("/api/goldens/health")
def api_goldens_health():
    """Per shipped theme, how many default goldens are missing (the panel offers Reset)."""
    return goldens_store.shipped_health()


class GoldenReset(BaseModel):
    """Empty on purpose: a JSON body forces the CORS preflight, like every other POST."""


@app.post("/api/goldens/reset")
def api_reset_goldens(_body: GoldenReset):
    """Restores the shipped golden sets, conversation scenarios and security
    probes; the side panel calls this on every load, so edits last one session."""
    return {
        "restored": goldens_store.reset_to_defaults(),
        "conversations_restored": conversations_store.reset_to_defaults(),
        "probes_restored": probes_store.reset_to_defaults(),
    }


class ConversationCreate(BaseModel):
    theme: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    scenario: str = Field(default="", max_length=2000)
    chatbot_role: str = Field(default="", max_length=500)
    expected_outcome: str = Field(default="", max_length=2000)
    user_turns: list[str] = Field(min_length=1, max_length=20)


def _conversation_fields(body: ConversationCreate) -> dict:
    if not any(turn.strip() for turn in body.user_turns):
        raise HTTPException(status_code=400, detail="a scenario needs at least one user turn")
    return body.model_dump()


@app.get("/api/conversations")
def api_list_conversations(theme: str | None = None):
    return conversations_store.load_conversations(theme=theme)


@app.post("/api/conversations")
def api_create_conversation(body: ConversationCreate):
    return conversations_store.add_conversation(_conversation_fields(body))


class ConversationsFromGoldens(BaseModel):
    theme: str = Field(min_length=1)
    chatbot_role: str = Field(default="", max_length=500)
    limit: int = Field(default=conversations_store.AUTO_SCENARIOS, ge=1, le=10)


@app.post("/api/conversations/from-goldens")
def api_conversations_from_goldens(body: ConversationsFromGoldens):
    """Saves scenarios built from the theme's goldens (two questions, then the
    first again), so they can be reviewed and edited like hand-written ones."""
    goldens = [g for g in goldens_store.load_goldens(theme=body.theme) if g.get("status") != "draft"]
    built = conversations_store.from_goldens(goldens, body.theme, body.chatbot_role, body.limit)
    if not built:
        raise HTTPException(status_code=400, detail=f"No goldens in theme {body.theme!r} to build "
                                                    "scenarios from. Add golden answers first.")
    return [conversations_store.add_conversation(row) for row in built]


@app.put("/api/conversations/{conversation_id}")
def api_update_conversation(conversation_id: str, body: ConversationCreate):
    row = conversations_store.update_conversation(conversation_id, _conversation_fields(body))
    if row is None:
        raise HTTPException(status_code=404, detail="conversation scenario not found")
    return row


@app.delete("/api/conversations/{conversation_id}")
def api_delete_conversation(conversation_id: str):
    if not conversations_store.delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="conversation scenario not found")
    return {"deleted": conversation_id}


@app.post("/api/conversations/reset")
def api_reset_conversations(_body: GoldenReset):
    return {"restored": conversations_store.reset_to_defaults()}


class ProbeFields(BaseModel):
    metric: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    note: str = Field(default="", max_length=500)
    set: str = Field(default=probes_store.DEFAULT_PROBE_SET, max_length=32)


@app.get("/api/security-probes")
def api_list_probes(metric: str | None = None, probe_set: str | None = None):
    return probes_store.load_probes(metric=metric, probe_set=probe_set)


@app.post("/api/security-probes")
def api_add_probe(body: ProbeFields):
    try:
        return probes_store.add_probe(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/api/security-probes/{probe_id}")
def api_update_probe(probe_id: str, body: ProbeFields):
    try:
        row = probes_store.update_probe(probe_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="probe not found")
    return row


@app.delete("/api/security-probes/{probe_id}")
def api_delete_probe(probe_id: str):
    if not probes_store.delete_probe(probe_id):
        raise HTTPException(status_code=404, detail="probe not found")
    return {"deleted": probe_id}


@app.post("/api/security-probes/reset")
def api_reset_probes(_body: GoldenReset):
    return {"restored": probes_store.reset_to_defaults()}


class CustomMetricFields(BaseModel):
    """One of the user's own metrics (backend/datasets/custom_metrics.py)."""
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["geval", "dag"] = "geval"
    description: str = Field(default="", max_length=300)
    criteria: str = Field(default="", max_length=4000)
    evaluation_steps: list[str] = Field(default_factory=list, max_length=20)
    params: list[str] = Field(default_factory=lambda: ["input", "actual_output"], max_length=5)
    strict: bool = False
    threshold: float = Field(default=0.7, ge=0, le=1)
    dataset: str = ""
    dag: dict | None = None


@app.get("/api/custom-metrics")
def api_list_custom_metrics():
    return custom_store.load_metrics()


@app.post("/api/custom-metrics")
def api_add_custom_metric(body: CustomMetricFields):
    try:
        return custom_store.add_metric(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/api/custom-metrics/{metric_id}")
def api_update_custom_metric(metric_id: str, body: CustomMetricFields):
    try:
        row = custom_store.update_metric(metric_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="custom metric not found")
    return row


@app.delete("/api/custom-metrics/{metric_id}")
def api_delete_custom_metric(metric_id: str):
    if not custom_store.delete_metric(metric_id):
        raise HTTPException(status_code=404, detail="custom metric not found")
    return {"deleted": metric_id}


@app.post("/api/custom-metrics/reset")
def api_reset_custom_metrics(_body: GoldenReset):
    """Restores the shipped examples; the user's other metrics are kept."""
    return {"restored": custom_store.reset_to_defaults()}


class UsageReset(BaseModel):
    """Empty on purpose: a JSON body forces the CORS preflight, like every other POST."""


@app.get("/api/usage")
def api_usage():
    """Judge tokens and calls since the backend started (or the last reset)."""
    return usage.snapshot()


@app.post("/api/usage/reset")
def api_usage_reset(_body: UsageReset):
    usage.reset()
    return usage.snapshot()


@app.post("/api/goldens")
def api_create_golden(body: GoldenCreate):
    return goldens_store.add_golden(
        body.theme, body.question, body.expected_answer, body.context, body.categories,
        extra={"expected_tools": body.tool_names()},
    )


@app.put("/api/goldens/{golden_id}")
def api_update_golden(golden_id: str, body: GoldenCreate):
    updated = goldens_store.update_golden(
        golden_id,
        body.theme,
        body.question,
        body.expected_answer,
        body.context,
        body.categories,
        expected_tools=body.tool_names(),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="golden not found")
    return updated


@app.delete("/api/goldens/{golden_id}")
def api_delete_golden(golden_id: str):
    if not goldens_store.delete_golden(golden_id):
        raise HTTPException(status_code=404, detail="golden not found")
    return {"deleted": golden_id}


class ScratchGoldens(BaseModel):
    target_id: int
    count: int = Field(default=5, ge=1, le=20)
    # Save them as drafts that runs skip until accepted.
    review: bool = False


@app.post("/api/goldens/generate-scratch")
def api_generate_scratch_goldens(body: ScratchGoldens):
    """Adds goldens without context (question + expected answer) to the chatbot's
    golden set with DeepEval's Synthesizer.generate_goldens_from_scratch; the
    reference-free metrics (Self-Check Consistency) run on these."""
    target = _target_or_404(body.target_id)
    config = target["config"]
    theme = config.get("theme") or DEFAULT_THEME
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        added = generate_goldens_from_scratch(theme, judge, target["name"], config.get("persona", ""),
                                              count=body.count, review=body.review)
    except Exception as e:  # noqa: BLE001 - a judge / Synthesizer failure is reported, not a 500
        raise HTTPException(status_code=502, detail=f"DeepEval Synthesizer failed: {type(e).__name__}: {e}") from e
    return {"theme": theme, "created": len(added), "goldens": added}


def _cases_available(spec, theme: str, persona: str, probe_set: str) -> int:
    try:
        return len(spec.cases(theme=theme, persona=persona, probe_set=probe_set))
    except Exception:  # noqa: BLE001 - a broken dataset shows as 0 cases, not a 500
        return 0


def _availability(config: dict | None):
    """(theme, persona, probe_set, unavailable) for a chatbot config, or for no
    chatbot (None): unavailable(spec) says why a metric cannot run for it."""
    theme, persona, probe_set = DEFAULT_THEME, "", ""
    # What the chatbot reports beside its reply; without a target, everything.
    features = {"context_path": True, "tools_path": True}
    fallback = ""  # where the app finds context itself (target sources_fallback)
    if config is not None:
        theme = config.get("theme") or DEFAULT_THEME
        persona = config.get("persona", "")
        probe_set = config.get("probe_set", "")
        features = {"context_path": bool(config.get("context_path")), "tools_path": bool(config.get("tools_path"))}
        fallback = config.get("sources_fallback") or ""

    def features_for(s) -> dict:
        # Same rule as runner.target_features: a fallback counts as context only
        # for metrics that read retrieval_context and do not grade the retriever.
        found = bool(fallback) and not s.grades_retriever and "retrieval_context" in s.scores_on
        return {**features, "context_path": features["context_path"] or found}

    def unavailable(s) -> str | None:
        if getattr(s, "needs_own_sources", False) and not features_for(s)["context_path"]:
            return NEEDS_OWN_SOURCES
        if config is not None and getattr(s, "needs_target", ()):
            missing = missing_target_fields(s, metric_options(s, None, config))
            if missing:
                return missing
        applies = getattr(s, "applies", None)
        if callable(applies):
            try:
                items = s.cases(theme=theme, persona=persona, probe_set=probe_set)
            except Exception:  # noqa: BLE001 - a broken dataset shows as 0 cases elsewhere
                items = []
            return applies(features_for(s), items)
        return None

    return theme, persona, probe_set, unavailable


def _skip_reason(spec, theme: str, persona: str, probe_set: str, unavailable) -> str | None:
    """Why a run leaves this metric out for the chatbot, or None when it runs."""
    if not callable(getattr(spec, "cases", None)):
        return None  # a spec without a dataset of its own (tests' stand-ins) always runs
    reason = unavailable(spec)
    if reason:
        return reason
    if not _cases_available(spec, theme, persona, probe_set):
        return spec.no_cases_reason(theme)
    return None


@app.get("/api/metrics")
def api_list_metrics(target_id: int | None = None):
    """The catalog as the UI shows it: grouped, with direction, default threshold,
    a threshold per environment (Local / PR / Staging / Production), the card
    copy, and how many cases each metric would run for the given target (its
    theme and persona)."""
    config = _target_or_404(target_id)["config"] if target_id is not None else None
    theme, persona, probe_set, unavailable = _availability(config)
    custom_ids = {row["key"]: row["id"] for row in custom_store.load_metrics()}
    return [
        {
            "key": s.key,
            "title": s.title,
            "threshold": s.threshold,
            "category": s.group,
            "group": s.group,
            "group_label": GROUPS[s.group],
            "direction": s.direction,
            "description": s.description,
            "kind": s.kind,
            "needs_retrieval": s.needs_retrieval,
            # What the judge reads for this metric, and the G-Eval rubric if it has one.
            "scores_on": list(s.scores_on),
            "criteria": s.criteria,
            "presets": s.presets(),
            "ui_category": s.ui_category,
            "ui_category_label": UI_CATEGORY_LABELS[s.ui_category],
            "scale_hint": s.scale_hint,
            "question": s.question,
            "dataset": s.dataset_name,
            "cases_available": 0 if unavailable(s) else _cases_available(s, theme, persona, probe_set),
            # Why this metric cannot run for this chatbot (None when it can).
            "unavailable": unavailable(s),
            # Where its questions come from: the golden set or a question set of its own.
            "question_source": s.question_source(theme, probe_set),
            # Why a run leaves it out (not applicable, or no test case): None when it runs.
            "skip_reason": _skip_reason(s, theme, persona, probe_set, unavailable),
            # Which security probe set this target sends (None for other datasets).
            "probe_set": (probe_set or probes_store.DEFAULT_PROBE_SET)
            if s.dataset_name in PROBE_DATASETS else None,
            # How to improve it when it fails: steps for the chatbot and for the test.
            "improve": s.improve,
            # Knobs the user may tune per run, and target fields the metric reads.
            "settings": s.settings,
            "needs_target": list(s.needs_target),
            # The user's own metric (editable at /api/custom-metrics), else None.
            "custom_id": custom_ids.get(s.key),
        }
        # Grouped the way both UIs show them: by dashboard category, in chip order.
        for s in sorted(all_specs(), key=lambda spec: UI_CATEGORIES.index(spec.ui_category))
    ]


@app.get("/api/metrics/environments")
def api_metric_environments():
    return [{"key": key, "label": label} for key, label in ENVIRONMENTS.items()]


@app.post("/api/run")
def api_run(req: RunRequest):
    row = _target_or_404(req.target_id)
    spec = get_spec(req.metric_key)
    if spec is None:
        raise HTTPException(status_code=404, detail="metric not found")
    target = _client_or_400(row)
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    theme = row["config"].get("theme") or DEFAULT_THEME

    def on_progress(done: int, total: int, phase: str, question: str) -> None:
        if req.run_id:
            with _progress_lock:
                if req.run_id in _cancelled:
                    raise RunCancelled()
                _progress[req.run_id] = {
                    "done": done, "total": total, "phase": phase, "question": question,
                }

    def stopped() -> bool:
        with _progress_lock:
            return bool(req.run_id) and req.run_id in _cancelled

    try:
        with cancel.watching(stopped):
            return run_spec(
                spec, judge, target, req.target_id, _conn, theme=theme, threshold=req.threshold,
                on_progress=on_progress, persona=row["config"].get("persona", ""), limit=req.limit,
                probe_set=row["config"].get("probe_set", ""), check_consistency=req.check_consistency,
                metric_kwargs=metric_options(spec, req.settings, row["config"]),
                enhancements=req.enhancements, enhancement_options=req.enhancement_options,
                max_latency_s=req.max_latency_s, price_per_mtok=_judge_price(),
                calibration_examples=_calibration_examples(req.target_id, req.metric_key)
                if req.use_calibration else None,
            )
    finally:
        if req.run_id:
            with _progress_lock:
                _progress.pop(req.run_id, None)
                _cancelled.discard(req.run_id)


@app.post("/api/run/cancel")
def api_run_cancel(body: RunCancel):
    """Stops a running /api/run at its next step (the judge call in flight finishes first)."""
    with _progress_lock:
        active = body.run_id in _progress
        # Remembered even before the run reports in, so an early Stop still lands.
        _cancelled.add(body.run_id)
    return {"cancelling": active}


@app.get("/api/run/progress")
def api_run_progress(run_id: str):
    """Where a running /api/run is: `done` of `total` goldens, and what it is doing."""
    with _progress_lock:
        state = _progress.get(run_id)
    return {"active": state is not None, **(state or {})}


class DeepEvalOptions(BaseModel):
    """DeepEval's own metric options (metrics_catalog.DEEPEVAL_OPTIONS)."""
    strict_mode: bool = False
    include_reason: bool = True
    async_mode: bool = False


class JobCreate(BaseModel):
    target_id: int
    metric_keys: list[str]
    thresholds: dict[str, float] = Field(default_factory=dict)
    limit: int | None = Field(default=None, ge=1)
    check_consistency: bool = False
    # Per metric key: that metric's tunable settings ({"turn_relevancy": {"window_size": 5}}).
    settings: dict[str, dict] = Field(default_factory=dict)
    enhancements: list[str] = Field(default_factory=list, max_length=10)
    enhancement_options: dict = Field(default_factory=dict)
    # Fail the run when 95% of chatbot replies are not back within this many seconds.
    max_latency_s: float | None = Field(default=None, gt=0, le=600)
    # G-Eval metrics only: add this chatbot's human-scored cases as few-shot examples.
    use_calibration: bool = False
    # DeepEval's strict_mode / include_reason / async_mode for every metric of the job.
    deepeval: DeepEvalOptions | None = None
    # Metrics of this job run at once (the web app's "Parallel metrics"); None = the server's default.
    metric_concurrency: int | None = Field(default=None, ge=MIN_METRIC_CONCURRENCY, le=MAX_METRIC_CONCURRENCY)


class JobCancel(BaseModel):
    """Empty on purpose: a JSON body forces the CORS preflight, like every other POST."""


@app.post("/api/jobs")
def api_create_job(body: JobCreate):
    """Queues a batch of metrics; it runs in the background, even with no page open.
    Metrics that cannot run for this chatbot (not applicable, or no test case in
    its golden set) are not sent: the job keeps them as `skipped` with the reason,
    so its scan and report can say why."""
    target = _target_or_404(body.target_id)
    if not body.metric_keys:
        raise HTTPException(status_code=400, detail="pick at least one metric")
    unknown = [k for k in body.metric_keys if get_spec(k) is None]
    if unknown:
        raise HTTPException(status_code=404, detail=f"metric not found: {', '.join(unknown)}")
    bad = [e for e in body.enhancements if e not in attack_enhancers.ENHANCERS]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown enhancement: {', '.join(bad)}")
    try:
        build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    theme, persona, probe_set, unavailable = _availability(target["config"])
    runnable, skipped = [], []
    for key in body.metric_keys:
        spec = get_spec(key)
        reason = _skip_reason(spec, theme, persona, probe_set, unavailable)
        if reason:
            skipped.append({"key": key, "title": spec.title, "reason": reason})
        else:
            runnable.append(key)
    job_id = jobs.submit(body.target_id, runnable, {
        "thresholds": body.thresholds, "limit": body.limit, "check_consistency": body.check_consistency,
        "settings": body.settings,
        "enhancements": body.enhancements, "enhancement_options": body.enhancement_options,
        "max_latency_s": body.max_latency_s, "use_calibration": body.use_calibration,
        **({"deepeval": body.deepeval.model_dump()} if body.deepeval else {}),
        **({"skipped": skipped} if skipped else {}),
        **({"metric_concurrency": body.metric_concurrency} if body.metric_concurrency else {}),
    })
    return {"job_id": job_id, "status": "queued", "skipped": skipped}


@app.get("/api/run-limits")
def api_run_limits():
    """What the web app's "Parallel metrics" may pick, and the server's caps."""
    return {
        "metric_concurrency": {"default": jobs_module.METRIC_CONCURRENCY,
                               "min": MIN_METRIC_CONCURRENCY, "max": MAX_METRIC_CONCURRENCY},
        # Judge calls in flight across the app: more parallel metrics wait for these.
        "judge_concurrency": JUDGE_CONCURRENCY,
    }


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: int):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/jobs")
def api_list_jobs(target_id: int, active: int = 1):
    """Jobs still queued or running for a chatbot, so a reopened page can reattach."""
    return jobs.active(target_id)


@app.get("/api/targets/{target_id}/scans")
def api_list_scans(target_id: int):
    """The chatbot's runs as numbered scans (v1, v2, ...), each with one overall result."""
    _target_or_404(target_id)
    return scan_history.list_scans(_conn, target_id)


@app.get("/api/scans/{job_id}")
def api_get_scan(job_id: int):
    scan = scan_history.get_scan(_conn, job_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return scan


@app.get("/api/scans/{job_id}/compare")
def api_compare_scans(job_id: int, with_: int = Query(alias="with")):
    """Metric by metric, this scan against another (better / worse / same / new / removed)."""
    result = scan_history.compare_scans(_conn, job_id, with_)
    if result is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return result


@app.get("/api/scans/{job_id}/cases")
def api_compare_scan_cases(job_id: int, with_: int = Query(alias="with")):
    """Test case by test case, this scan against another: every metric's verdict
    on each case on both sides (improved / regressed / same / new / removed)."""
    result = scan_history.compare_scan_cases(_conn, job_id, with_)
    if result is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return result


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: int, _body: JobCancel):
    return {"cancelling": jobs.cancel(job_id)}


@app.post("/api/jobs/cancel-all")
def api_cancel_all_jobs(_body: JobCancel):
    """The web app's Stop: every running and queued job of every chatbot, and any
    /api/run in flight, stop at their next chatbot or judge call."""
    with _progress_lock:
        _cancelled.update(_progress)
    return {"cancelled": jobs.cancel_all()}


@app.post("/api/judge")
def api_judge(body: JudgeOneRequest):
    """Scores one answer the user collected by hand. Nothing is persisted.

    Reference-based metrics need a golden answer or context; when the question
    is one of the theme's goldens, its row supplies them.
    """
    spec = get_spec(body.metric_key)
    if spec is None:
        raise HTTPException(status_code=404, detail="metric not found")
    expected, context = body.expected_answer, body.context
    if body.theme and (not expected or not context):
        golden = next(
            (g for g in goldens_store.load_goldens(theme=body.theme)
             if g["question"] == body.question),
            None,
        )
        if golden:
            expected = expected or golden["expected_answer"]
            context = context or golden["context"]
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return judge_one(
        spec, judge, body.question, body.actual_output, expected, context,
        threshold=body.threshold, metric_kwargs=metric_options(spec, None, {}),
    )


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    target = _client_or_400(_target_or_404(req.target_id))
    history = [turn.model_dump() for turn in req.history]
    try:
        reply = target.chat(req.message, history=history)
    except Exception as e:  # noqa: BLE001 - any chatbot failure is a bad gateway here
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
    return {"reply": reply.reply, "model": reply.model, "mode": reply.mode}


@app.get("/api/runs/latest")
def api_runs_latest(target_id: int, since_id: int = 0):
    rows = storage.latest_runs(_conn, target_id, since_id)
    # Each run's full result (cases, note, judge facts) for Details and the report.
    for row in rows:
        raw = row.pop("result_json", None)
        row["result"] = json.loads(raw) if raw else None
    return rows


@app.get("/api/history")
def api_history(target_id: int, metric_key: str, since_id: int = 0):
    rows = storage.history(_conn, target_id, metric_key, since_id)
    for row in rows:
        row.pop("result_json", None)  # the trend needs scores, not every case
    return rows


@app.get("/api/runs/compare")
def api_compare_runs(a: int, b: int):
    """Two runs of one metric, case by case: regressions, fixes, score deltas."""
    run_a, run_b = storage.get_run(_conn, a), storage.get_run(_conn, b)
    if run_a is None or run_b is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        return compare_runs(run_a, run_b)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class PairwiseCreate(BaseModel):
    target_a: int
    target_b: int
    criteria: str = Field(default="", max_length=2000)
    limit: int | None = Field(default=None, ge=1)


@app.post("/api/pairwise")
def api_create_pairwise(body: PairwiseCreate):
    """Queues an A/B comparison of two chatbots on chatbot A's goldens. Poll
    GET /api/jobs/{job_id}; each case is judged in both orders (position swap)."""
    if body.target_a == body.target_b:
        raise HTTPException(status_code=400, detail="pick two different chatbots")
    _target_or_404(body.target_a)
    _target_or_404(body.target_b)
    try:
        build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    job_id = jobs.submit(body.target_a, ["pairwise"], {
        "kind": "pairwise", "target_b": body.target_b, "criteria": body.criteria, "limit": body.limit,
    })
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/pairwise")
def api_list_pairwise(target_id: int):
    return storage.list_pairwise_runs(_conn, target_id)


@app.get("/api/security/enhancers")
def api_list_enhancers():
    """Attack enhancers a run can add to its red-team probes, and their options."""
    return {
        "enhancers": [{"key": key, "label": label, "uses_judge": uses_judge}
                      for key, (label, uses_judge) in attack_enhancers.ENHANCERS.items()],
        "many_shot_counts": list(attack_enhancers.MANY_SHOT_COUNTS),
        "languages": list(attack_enhancers.LANGUAGES),
    }


@app.get("/api/security/coverage")
def api_security_coverage(target_id: int):
    """Latest red-team results by risk category, mapped to the OWASP LLM Top 10."""
    _target_or_404(target_id)
    titles = {s.key: s.title for s in all_specs()}
    return coverage_report(storage.latest_runs(_conn, target_id), titles)


class SimulateRequest(BaseModel):
    target_id: int
    # A saved scenario to start from, or the fields below.
    conversation_id: str | None = None
    scenario: str = Field(default="", max_length=2000)
    expected_outcome: str = Field(default="", max_length=2000)
    chatbot_role: str = Field(default="", max_length=500)
    user_persona: str = Field(default="", max_length=500)
    max_turns: int = Field(default=simulator.DEFAULT_TURNS, ge=1, le=simulator.MAX_TURNS)
    # Conversational metrics to score the finished conversation with.
    metric_keys: list[str] = Field(default_factory=list, max_length=10)
    # Save the simulated user turns as a fixed scenario (for regression runs).
    save_as: str = Field(default="", max_length=200)


@app.post("/api/simulate")
def api_simulate(body: SimulateRequest):
    """The judge plays the user against the chatbot, then scores the conversation.
    Costs one judge call per turn plus the metrics; nothing goes on the trend."""
    row = _target_or_404(body.target_id)
    scenario = {"scenario": body.scenario, "expected_outcome": body.expected_outcome,
                "chatbot_role": body.chatbot_role, "user_persona": body.user_persona}
    theme = row["config"].get("theme") or DEFAULT_THEME
    if body.conversation_id:
        saved = next((c for c in conversations_store.load_conversations()
                      if c["id"] == body.conversation_id), None)
        if saved is None:
            raise HTTPException(status_code=404, detail="conversation scenario not found")
        theme = saved["theme"]
        scenario = {**{k: saved.get(k, "") for k in ("scenario", "expected_outcome", "chatbot_role", "name")},
                    **{k: v for k, v in scenario.items() if v}}
    if not (scenario.get("scenario") or scenario.get("expected_outcome")):
        raise HTTPException(status_code=400, detail="describe the scenario or the expected outcome")
    specs = [get_spec(k) for k in body.metric_keys]
    bad = [k for k, s in zip(body.metric_keys, specs) if s is None or s.kind != "conversation"]
    if bad:
        raise HTTPException(status_code=400, detail=f"not a conversational metric: {', '.join(bad)}")
    target = _client_or_400(row)
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        result = simulator.simulate(judge, target, scenario, body.max_turns)
    except Exception as e:  # noqa: BLE001 - the chatbot or the judge failed mid-conversation
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
    if not result["turns"]:
        raise HTTPException(status_code=502, detail="the simulated user wrote no message")
    result["scores"] = [simulator.score_conversation(s, judge, scenario, result["turns"],
                                                     metric_kwargs=metric_options(s, None, row["config"]))
                        for s in specs]
    if body.save_as.strip():
        result["saved"] = conversations_store.add_conversation({
            "theme": theme, "name": body.save_as, "scenario": scenario.get("scenario", ""),
            "chatbot_role": scenario.get("chatbot_role", ""),
            "expected_outcome": scenario.get("expected_outcome", ""), "user_turns": result["user_turns"],
        })
    return result


@app.get("/api/export/suite")
def api_export_suite(target_id: int, metrics: str = "", env: str = "pr"):
    """A zip with a pytest + GitHub Actions suite that runs these metrics on
    this chatbot's goldens with the environment's thresholds. Header values are
    left out (see backend/export_suite.py)."""
    row = _target_or_404(target_id)
    if env not in ENVIRONMENTS:
        raise HTTPException(status_code=400, detail=f"env must be one of: {', '.join(ENVIRONMENTS)}")
    keys = [k for k in metrics.split(",") if k.strip()]
    specs = [get_spec(k.strip()) for k in keys] if keys else list(all_specs())
    if any(s is None for s in specs):
        raise HTTPException(status_code=404, detail="metric not found")
    thresholds = {s.key: s.presets().get(env, s.threshold) for s in specs}
    theme = row["config"].get("theme") or DEFAULT_THEME
    files = build_suite(row, specs, thresholds, env, goldens_store.load_goldens(theme=theme))
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", row["name"]).strip("_") or "chatbot"
    return Response(zip_suite(files), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="llm-evals-{name}.zip"'})


def _calibration_examples(target_id: int, metric_key: str) -> list[dict]:
    return storage.list_labels(_conn, target_id, metric_key)


def _agreement(labels: list[dict]) -> dict | None:
    """How close the judge was to the human: mean absolute gap, share within
    0.2, and how often both gave the same pass/fail verdict."""
    scored = [lb for lb in labels if lb["judge_score"] is not None]
    if not scored:
        return None
    gaps = [abs(lb["judge_score"] - lb["human_score"]) for lb in scored]

    def human_passes(lb):
        if lb["threshold"] is None:
            return lb["human_score"] >= 0.5
        return lb["human_score"] <= lb["threshold"] if lb["direction"] == "lower" else lb["human_score"] >= lb["threshold"]

    same = [bool(lb["judge_passed"]) == human_passes(lb) for lb in scored]
    return {"n": len(scored), "mean_abs_gap": round(sum(gaps) / len(gaps), 4),
            "within_0_2": round(sum(g <= 0.2 for g in gaps) / len(gaps), 4),
            "verdict_agreement": round(sum(same) / len(same), 4)}


class LabelCreate(BaseModel):
    run_id: int
    case_index: int = Field(ge=0)
    # The score the human would give this case, on the metric's own scale.
    human_score: float = Field(ge=0, le=1)
    note: str = Field(default="", max_length=500)


@app.post("/api/calibration")
def api_add_label(body: LabelCreate):
    """Scores one judged case by hand; the label is used for agreement and,
    with use_calibration, as a few-shot example for G-Eval metrics."""
    run = storage.get_run(_conn, body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    result = json.loads(run["result_json"]) if run.get("result_json") else {}
    rows = result.get("rows") or []
    if body.case_index >= len(rows):
        raise HTTPException(status_code=404, detail="case not found in this run")
    case = rows[body.case_index]
    label_id = storage.upsert_label(_conn, {
        "target_id": run["target_id"], "metric_key": run["metric_key"], "run_id": run["id"],
        "case_index": body.case_index, "question": case.get("question", ""),
        "actual_output": case.get("actual_output", ""), "judge_score": case.get("score"),
        "judge_passed": int(bool(case.get("passed"))), "threshold": result.get("threshold"),
        "direction": result.get("direction"), "human_score": body.human_score, "note": body.note,
    })
    return {"id": label_id}


@app.get("/api/calibration")
def api_list_labels(target_id: int, metric_key: str | None = None):
    labels = storage.list_labels(_conn, target_id, metric_key)
    by_metric: dict[str, list[dict]] = {}
    for lb in labels:
        by_metric.setdefault(lb["metric_key"], []).append(lb)
    return {"labels": labels, "agreement": _agreement(labels),
            "by_metric": {k: _agreement(v) for k, v in by_metric.items()}}


@app.delete("/api/calibration/{label_id}")
def api_delete_label(label_id: int):
    if not storage.delete_label(_conn, label_id):
        raise HTTPException(status_code=404, detail="label not found")
    return {"deleted": label_id}
