"""Background runs: a job is one batch of metrics for one chatbot.

Jobs run one at a time, in creation order, on a single worker thread, so a run
keeps going after the page that started it closes. Within a job up to
METRIC_CONCURRENCY metrics run side by side (their cases share the runner's
JUDGE_CONCURRENCY slots, so the load on the judge and chatbot stays the same).
Each run counts its own tokens (usage.meter). Jobs and their results live in
SQLite; live progress is kept in memory and merged in by get(): "current" is
the first metric still running, "running" lists them all.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from backend import storage, traffic
from backend.dashboard.runner import RunCancelled, _now_iso

logger = logging.getLogger(__name__)


def _metric_concurrency() -> int:
    try:
        return max(1, min(8, int(os.getenv("JUDGE_METRIC_CONCURRENCY", "2"))))
    except ValueError:
        return 2


# Metrics of one job run at once; 1 = one after another.
METRIC_CONCURRENCY = _metric_concurrency()


class JobFailed(Exception):
    """Raised by run_metric when the whole job cannot go on (judge missing, chatbot deleted)."""


class JobQueue:
    def __init__(self, conn: sqlite3.Connection, run_metric, after_job=None):
        self._conn = conn
        self._run_metric = run_metric
        # Called with each finished job's id (the app queues follow-up scans there).
        self._after_job = after_job
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._cancelled: set[int] = set()
        self._current: dict[int, dict[int, dict]] = {}
        self._thread: threading.Thread | None = None

    def submit(self, target_id: int, metric_keys: list[str], options: dict) -> int:
        job_id = storage.add_job(self._conn, target_id, list(metric_keys), dict(options))
        self._wake.set()
        return job_id

    def get(self, job_id: int) -> dict | None:
        job = storage.get_job(self._conn, job_id)
        if job is not None:
            with self._lock:
                live = [dict(c) for _, c in sorted(self._current.get(job_id, {}).items())]
            if live:
                job["current"], job["running"] = live[0], live
        return job

    def active(self, target_id: int) -> list[dict]:
        return [self.get(j["id"]) for j in storage.active_jobs(self._conn, target_id)]

    def cancel(self, job_id: int) -> bool:
        job = storage.get_job(self._conn, job_id)
        if job is None or job["status"] not in ("queued", "running"):
            return False
        with self._lock:
            self._cancelled.add(job_id)
        if job["status"] == "queued":
            storage.update_job(self._conn, job_id, status="cancelled", finished_at=_now_iso())
        return True

    def process_next(self) -> bool:
        job = storage.next_queued_job(self._conn)
        if job is None:
            return False
        job_id, keys = job["id"], job["metric_keys"]
        storage.update_job(self._conn, job_id, status="running", started_at=_now_iso())
        results: dict[int, dict] = {}
        outcome = {"status": "done", "error": None}
        # Set when one metric ends the job: the others stop at their next step.
        stop = threading.Event()

        def fail(error: str) -> None:
            with self._lock:
                if outcome["status"] != "error":
                    outcome.update(status="error", error=error)
            stop.set()

        def run_one(index: int, key: str) -> None:
            if self._is_cancelled(job_id) or stop.is_set():
                return
            traffic.log("== job %s: metric %d of %d: %s", job_id, index + 1, len(keys), key)
            base = {"metric_key": key, "index": index, "count": len(keys), "started_at": _now_iso()}
            self._set_current(job_id, index, {**base, "done": 0, "total": 0, "phase": "chat", "question": ""})

            def on_progress(done, total, phase, question):
                self._set_current(job_id, index, {**base, "done": done, "total": total, "phase": phase,
                                                  "question": question})
                if self._is_cancelled(job_id) or stop.is_set():
                    raise RunCancelled()

            try:
                result = self._run_metric(job, key, on_progress)
            except JobFailed as e:
                fail(str(e))
                return
            except Exception as e:  # noqa: BLE001 - never leave a job "running" forever
                logger.exception("LLM Judge: job %s failed on %s", job_id, key)
                fail(f"{type(e).__name__}: {e}")
                return
            finally:
                self._clear_current(job_id, index)
            traffic.log("== job %s: %s finished: %s, score %s", job_id, key, result.get("status"),
                        result.get("score"))
            with self._lock:
                results[index] = result
                done = [results[i] for i in sorted(results)]
            storage.update_job(self._conn, job_id, results=done)

        workers = 1 if job["options"].get("kind") == "pairwise" else min(METRIC_CONCURRENCY, len(keys))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"llm-judge-job{job_id}") as pool:
                for future in [pool.submit(run_one, i, key) for i, key in enumerate(keys)]:
                    future.result()
        else:
            for index, key in enumerate(keys):
                run_one(index, key)
        ordered = [results[i] for i in sorted(results)]
        status = outcome["status"]
        if status != "error" and (self._is_cancelled(job_id)
                                  or any(r.get("status") == "cancelled" for r in ordered)):
            status = "cancelled"
        with self._lock:
            self._current.pop(job_id, None)
            self._cancelled.discard(job_id)
        storage.update_job(self._conn, job_id, status=status, error=outcome["error"], results=ordered,
                           current=None, finished_at=_now_iso())
        if self._after_job is not None:
            try:
                self._after_job(job_id)
            except Exception:  # noqa: BLE001 - the job itself has finished either way
                logger.exception("LLM Judge: follow-up of job %s failed", job_id)
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="llm-judge-jobs", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                ran = self.process_next()
            except Exception:  # noqa: BLE001 - the worker must survive one bad job
                logger.exception("LLM Judge: job worker error")
                ran = False
            if not ran:
                self._wake.wait(timeout=5)
                self._wake.clear()

    def _set_current(self, job_id: int, index: int, current: dict) -> None:
        with self._lock:
            self._current.setdefault(job_id, {})[index] = current

    def _clear_current(self, job_id: int, index: int) -> None:
        with self._lock:
            self._current.get(job_id, {}).pop(index, None)

    def _is_cancelled(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._cancelled
