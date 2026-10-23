"""Production guard rails for the backend: logging, request size limit,
security headers, a JSON error for unexpected failures, and a health check.

Settings (environment / .env):
- JUDGE_LOG_LEVEL: DEBUG, INFO (default), WARNING, ERROR.
- JUDGE_MAX_BODY_MB: largest request body accepted, in MB (default 25).
- JUDGE_TRAFFIC_FILE: where the llm_judge logs (chatbot/judge traffic, errors)
  are also written, however the backend was started; watch-traffic.bat reads it.
  Default logs/traffic-YYYY-MM-DD.log; empty turns the file off.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("llm_judge")

APP_VERSION = "1.1.0"
DEFAULT_MAX_BODY_MB = 25
# Polled every second or two by the pages: logged at DEBUG so INFO stays readable.
_QUIET_PREFIXES = ("/api/jobs/", "/api/run/progress", "/api/documents/", "/api/status", "/api/health")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # index.html frames the side panel and the dashboard from this same origin.
    "X-Frame-Options": "SAMEORIGIN",
}


def configure_logging() -> None:
    level = os.getenv("JUDGE_LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        level = "INFO"
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.setLevel(level)
    _add_traffic_file()


def _add_traffic_file() -> None:
    default = f"logs/traffic-{time.strftime('%Y-%m-%d')}.log"
    name = os.getenv("JUDGE_TRAFFIC_FILE", default).strip()
    if not name:
        return
    path = Path(name)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    if any(getattr(h, "baseFilename", None) == str(path) for h in logger.handlers):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        logger.warning("Traffic log file %s not writable: %s", path, exc)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)


def max_body_bytes() -> int:
    try:
        mb = float(os.getenv("JUDGE_MAX_BODY_MB", DEFAULT_MAX_BODY_MB))
    except ValueError:
        mb = DEFAULT_MAX_BODY_MB
    return int(max(1.0, mb) * 1024 * 1024)


def install(app: FastAPI) -> None:
    """Adds the middleware and the error handler; call once. Added last, the
    guard is the outermost middleware: it refuses oversized bodies before
    anything reads them and stamps its headers on every response, the CORS
    and trusted-host refusals included (it never loosens either check)."""
    configure_logging()
    limit = max_body_bytes()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        started = time.monotonic()
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse(status_code=413, content={
                "detail": f"request body too large (over {limit // (1024 * 1024)} MB); set JUDGE_MAX_BODY_MB to allow more"})
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        path = request.url.path
        if path.startswith("/app"):
            # The web app's own files: revalidate on every load (a 304 when
            # unchanged), so an updated script is never shadowed by a cached one.
            response.headers.setdefault("Cache-Control", "no-cache")
        if path.startswith("/api/"):
            # API answers carry chatbot replies and settings: never cache them.
            response.headers.setdefault("Cache-Control", "no-store")
            quiet = request.method == "GET" and path.startswith(_QUIET_PREFIXES)
            # Paths and status codes only: bodies hold questions, replies and keys.
            logger.log(logging.DEBUG if quiet else logging.INFO, "%s %s -> %s (%.0f ms)",
                       request.method, path, response.status_code, (time.monotonic() - started) * 1000)
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={
            "detail": f"internal error ({type(exc).__name__}); see the backend log for details"})


def health(conn, judge_configured: bool, worker_alive: bool | None) -> tuple[int, dict]:
    """(HTTP status, body) for /api/health: 200 when the database answers."""
    try:
        conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        db_ok = False
    try:
        from importlib.metadata import version

        deepeval_version = version("deepeval")
    except Exception:  # noqa: BLE001 - informational only
        deepeval_version = None
    body = {
        "status": "ok" if db_ok else "error",
        "version": APP_VERSION,
        "database": db_ok,
        "judge_configured": judge_configured,
        "job_worker": worker_alive,
        "deepeval": deepeval_version,
    }
    return (200 if db_ok else 503), body
