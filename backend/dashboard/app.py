"""FastAPI backend: the control panel behind the Chrome sidebar and dashboard."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import storage, usage
from backend.dashboard.jobs import JobFailed, JobQueue
from backend.dashboard.runner import NEEDS_OWN_SOURCES, RunCancelled, judge_one, run_spec
from backend.datasets import conversations as conversations_store
from backend.datasets import goldens as goldens_store
from backend.datasets import security_probes as probes_store
from backend.judges.judge import build_judge, judge_config
from backend.metrics_catalog import (
    ALL_SPECS, DEFAULT_THEME, ENVIRONMENTS, GROUPS, PROBE_DATASETS, SPECS_BY_KEY, UI_CATEGORIES,
    UI_CATEGORY_LABELS,
)
from backend.rag.enhance import enhance_document
from backend.rag.fetch import fetch_page_text
from backend.targets.base import ChatbotClient
from backend.targets.curl import (
    MESSAGE_PLACEHOLDER,
    find_context_path,
    find_history_path,
    find_reply_path,
    find_stream_reply_path,
    parse_curl,
    prepare_body,
    prepare_url,
)
from backend.targets.http_client import HttpTargetClient, is_stream, sse_events
from backend.targets import presets as target_presets

DB_PATH = os.getenv("JUDGE_DB_PATH", "judge.db")
TARGET_TYPES = ("http",)
SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".docx", ".md", ".markdown", ".mdx"}
DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "datasets" / "documents"
# The extension's pages, also served as the web app at /app/ (same origin as the
# API, so the browser needs no CORS for it).
WEB_APP_DIR = Path(__file__).resolve().parent.parent.parent / "ChatbotExtension"


def extension_origin_regex(extension_id: str | None) -> str:
    """Origins allowed to call the API: one pinned extension id, or any extension."""
    if extension_id:
        return rf"^chrome-extension://{re.escape(extension_id)}$"
    return r"^chrome-extension://[a-z]{32}$"


def generate_goldens_from_document(*args, **kwargs) -> int:
    # Imported on first use: sentence-transformers/transformers take ~40s to
    # import, which would otherwise stall server startup.
    from backend.rag.generate import generate_goldens_from_document as generate

    return generate(*args, **kwargs)


app = FastAPI(title="Chrome Sidebar LLM Judge")
# Only the extension may call this API: with "*", any page the user visits
# could read stored target API keys and spend judge credits. Request bodies
# must be JSON (FastAPI refuses other content types), which forces a CORS
# preflight, so other sites cannot send POSTs either.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=extension_origin_regex(os.getenv("JUDGE_EXTENSION_ID")),
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)
# Blocks DNS-rebinding pages that reach 127.0.0.1 under their own host name.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
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


def _run_job_metric(job: dict, metric_key: str, on_progress) -> dict:
    """Runs one metric of a background job, exactly as /api/run does."""
    row = storage.get_target(_conn, job["target_id"])
    if row is None:
        raise JobFailed("chatbot deleted")
    spec = SPECS_BY_KEY.get(metric_key)
    if spec is None:
        return {"key": metric_key, "status": "error", "error": "metric not found", "score": None, "rows": []}
    try:
        judge = build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise JobFailed(str(e)) from e
    try:
        target = _client_or_400(row)
    except HTTPException as e:
        raise JobFailed(str(e.detail)) from e
    options, config = job["options"], row["config"]
    return run_spec(
        spec, judge, target, job["target_id"], _conn,
        theme=config.get("theme") or DEFAULT_THEME,
        threshold=(options.get("thresholds") or {}).get(metric_key),
        on_progress=on_progress, persona=config.get("persona", ""), limit=options.get("limit"),
        probe_set=config.get("probe_set", ""), check_consistency=bool(options.get("check_consistency")),
    )


# Background runs: one job at a time on a worker thread (backend/dashboard/jobs.py).
# Tests run queued jobs themselves (JUDGE_START_JOB_WORKER=0) instead of racing it.
jobs = JobQueue(_conn, _run_job_metric)
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
        "history_path": find_history_path(body_template),
        "reply_preview": "",
        "probe_error": None,
    }
    if result["message_marked"] and body.probe:
        result.update(_probe_reply_path(result, message))
    return result


def _probe_reply_path(parsed: dict, message: str) -> dict:
    """Sends the captured question once and finds where the reply sits in the answer."""
    config = {k: parsed[k] for k in ("url", "method", "headers", "body_template")}
    try:
        client = HttpTargetClient(config)
        response = client.send(message or "Hello")
        context_path = ""
        if is_stream(response):
            path = find_stream_reply_path(sse_events(response.text))
        else:
            try:
                data = response.json()
                path = find_reply_path(data)
                # Retrieved documents, when the chatbot returns them (RAG metrics).
                context_path = find_context_path(data)
            except ValueError:
                path = ""  # plain-text answer: the whole body is the reply
        client.response_path = path
        reply = client.read_reply(response).reply
    except Exception as e:  # noqa: BLE001 - the fields are still filled; the user can fix the path
        return {"probe_error": f"{type(e).__name__}: {e}"}
    return {"response_path": path, "context_path": context_path, "reply_preview": reply[:500]}


@app.post("/api/targets/parse-llm")
def api_parse_llm(body: LlmImport):
    """Fills the chatbot form for an OpenAI-compatible LLM API, so another model
    can be judged without capturing a request from a website."""
    try:
        return target_presets.build_llm_config(
            base_url=body.base_url, model=body.model,
            api_key=body.api_key, system_prompt=body.system_prompt,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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


@app.post("/api/documents")
def api_upload_document(
    theme: str = Form(...), file: UploadFile = File(...), enhance: bool = Form(True)
):
    """Saves the upload and starts golden generation in the background: the
    response is the document row (status "queued"); poll GET /api/documents/{id}."""
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
    _start_job(lambda: _run_document_job(document_id, dest, theme, safe_filename, judge, enhance))
    return storage.get_document(_conn, document_id)


def _start_job(job) -> None:
    """Generation makes many judge calls (minutes), so it runs off the request.
    Tests swap this for an inline call."""
    threading.Thread(target=job, daemon=True).start()


def _run_document_job(document_id: int, dest: Path, theme: str, source: str, judge, enhance: bool) -> None:
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
    _generate_for_document(document_id, source_path, theme, source, judge)


def _storable_stem(name: str) -> str:
    """The file name's stem reduced to [A-Za-z0-9_-]. DeepEval names a Chroma
    collection after the saved file, and Chroma rejects spaces, brackets and
    the like — the Synthesizer then silently finds 0 chunks and makes nothing."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_-")
    return stem or "document"


def _generate_for_document(document_id: int, dest: Path, theme: str, source: str, judge) -> None:
    """Runs golden generation for a saved document and records the outcome.
    The run replaces whatever this document generated before, so re-uploading
    or regenerating changes the dataset instead of stacking a second copy on
    it. A run that creates nothing is an error the user needs to see."""
    goldens_store.delete_generated_for(theme, source_document=source, source_document_id=document_id)
    try:
        created = generate_goldens_from_document(str(dest), theme, source, judge, document_id=document_id)
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
    document_id = storage.add_document(_conn, body.theme, final_url)
    theme_dir = DOCUMENTS_DIR / body.theme
    theme_dir.mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9.-]", "_", urlparse(final_url).netloc) or "page"
    dest = theme_dir / f"{document_id}_{host}.txt"
    dest.write_text(text, encoding="utf-8")
    _start_job(lambda: _run_document_job(document_id, dest, body.theme, final_url, judge, body.enhance))
    return storage.get_document(_conn, document_id)


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
    _start_job(lambda: _run_document_job(document_id, dest, doc["theme"], doc["filename"], judge, body.enhance))
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
        body.theme, body.question, body.expected_answer, body.context, body.categories
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
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="golden not found")
    return updated


@app.delete("/api/goldens/{golden_id}")
def api_delete_golden(golden_id: str):
    if not goldens_store.delete_golden(golden_id):
        raise HTTPException(status_code=404, detail="golden not found")
    return {"deleted": golden_id}


def _cases_available(spec, theme: str, persona: str, probe_set: str) -> int:
    try:
        return len(spec.cases(theme=theme, persona=persona, probe_set=probe_set))
    except Exception:  # noqa: BLE001 - a broken dataset shows as 0 cases, not a 500
        return 0


@app.get("/api/metrics")
def api_list_metrics(target_id: int | None = None):
    """The catalog as the UI shows it: grouped, with direction, default threshold,
    a threshold per environment (Local / PR / Staging / Production), the card
    copy, and how many cases each metric would run for the given target (its
    theme and persona)."""
    theme, persona, probe_set = DEFAULT_THEME, "", ""
    own_sources = True  # without a target, count every metric's cases
    if target_id is not None:
        config = _target_or_404(target_id)["config"]
        theme = config.get("theme") or DEFAULT_THEME
        persona = config.get("persona", "")
        probe_set = config.get("probe_set", "")
        own_sources = bool(config.get("context_path"))

    def unavailable(s) -> str | None:
        return NEEDS_OWN_SOURCES if s.needs_own_sources and not own_sources else None

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
            # Which security probe set this target sends (None for other datasets).
            "probe_set": (probe_set or probes_store.DEFAULT_PROBE_SET)
            if s.dataset_name in PROBE_DATASETS else None,
            # How to improve it when it fails: steps for the chatbot and for the test.
            "improve": s.improve,
        }
        # Grouped the way both UIs show them: by dashboard category, in chip order.
        for s in sorted(ALL_SPECS, key=lambda spec: UI_CATEGORIES.index(spec.ui_category))
    ]


@app.get("/api/metrics/environments")
def api_metric_environments():
    return [{"key": key, "label": label} for key, label in ENVIRONMENTS.items()]


@app.post("/api/run")
def api_run(req: RunRequest):
    row = _target_or_404(req.target_id)
    spec = SPECS_BY_KEY.get(req.metric_key)
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

    try:
        return run_spec(
            spec, judge, target, req.target_id, _conn, theme=theme, threshold=req.threshold,
            on_progress=on_progress, persona=row["config"].get("persona", ""), limit=req.limit,
            probe_set=row["config"].get("probe_set", ""), check_consistency=req.check_consistency,
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


class JobCreate(BaseModel):
    target_id: int
    metric_keys: list[str]
    thresholds: dict[str, float] = Field(default_factory=dict)
    limit: int | None = Field(default=None, ge=1)
    check_consistency: bool = False


class JobCancel(BaseModel):
    """Empty on purpose: a JSON body forces the CORS preflight, like every other POST."""


@app.post("/api/jobs")
def api_create_job(body: JobCreate):
    """Queues a batch of metrics; it runs in the background, even with no page open."""
    _target_or_404(body.target_id)
    if not body.metric_keys:
        raise HTTPException(status_code=400, detail="pick at least one metric")
    unknown = [k for k in body.metric_keys if k not in SPECS_BY_KEY]
    if unknown:
        raise HTTPException(status_code=404, detail=f"metric not found: {', '.join(unknown)}")
    try:
        build_judge(**_judge_overrides())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    job_id = jobs.submit(body.target_id, body.metric_keys, {
        "thresholds": body.thresholds, "limit": body.limit, "check_consistency": body.check_consistency,
    })
    return {"job_id": job_id, "status": "queued"}


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


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: int, _body: JobCancel):
    return {"cancelling": jobs.cancel(job_id)}


@app.post("/api/judge")
def api_judge(body: JudgeOneRequest):
    """Scores one answer the user collected by hand. Nothing is persisted.

    Reference-based metrics need a golden answer or context; when the question
    is one of the theme's goldens, its row supplies them.
    """
    spec = SPECS_BY_KEY.get(body.metric_key)
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
        threshold=body.threshold,
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
def api_runs_latest(target_id: int):
    rows = storage.latest_runs(_conn, target_id)
    # Each run's full result (cases, note, judge facts) for Details and the report.
    for row in rows:
        raw = row.pop("result_json", None)
        row["result"] = json.loads(raw) if raw else None
    return rows


@app.get("/api/history")
def api_history(target_id: int, metric_key: str):
    rows = storage.history(_conn, target_id, metric_key)
    for row in rows:
        row.pop("result_json", None)  # the trend needs scores, not every case
    return rows

