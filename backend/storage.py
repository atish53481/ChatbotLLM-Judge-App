"""SQLite persistence for targets, judge run history and one-time setup flags.

FastAPI serves requests from a thread pool and all of them share one
connection, so every function holds the module lock: a sqlite3 connection
is not safe for concurrent use even with check_same_thread=False.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.RLock()

RUN_EXTRA_COLUMNS = (
    ("cases_run", "INTEGER"),
    ("judge_model", "TEXT"),
    ("judge_tokens", "INTEGER"),
    ("judge_calls", "INTEGER"),
    ("target_calls", "INTEGER"),
    ("duration_s", "REAL"),
    ("judge_spread", "REAL"),
    ("cases_skipped", "INTEGER"),
    ("result_json", "TEXT"),
    # Chatbot reply latency (seconds) and judge cost (USD, when a price is set).
    ("latency_p50", "REAL"),
    ("latency_p95", "REAL"),
    ("judge_cost_usd", "REAL"),
)


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # Wait (up to 10 s) instead of failing when another connection holds a lock.
    conn.execute("PRAGMA busy_timeout = 10000")
    with _LOCK:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                metric_key TEXT NOT NULL,
                score REAL,
                passed INTEGER NOT NULL,
                ts TEXT NOT NULL,
                cases_run INTEGER
            )
            """
        )
        # Older databases lack the later columns: cases per run, and how each run
        # was judged (judge model, its tokens and calls, chatbot calls, duration,
        # and the widest gap between two scorings of one case).
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        for column, kind in RUN_EXTRA_COLUMNS:
            if column not in run_columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_target_metric ON runs (target_id, metric_key)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                theme TEXT NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                goldens_created INTEGER,
                enhanced_path TEXT
            )
            """
        )
        # Databases from before background generation lack these columns.
        doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        for column, kind in (("goldens_created", "INTEGER"), ("enhanced_path", "TEXT")):
            if column not in doc_columns:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {kind}")
        # A generation job lives in the backend process: one still running when
        # the backend stopped will never finish.
        conn.execute(
            "UPDATE documents SET status = 'error', error = ? "
            "WHERE status IN ('queued', 'processing', 'enhancing', 'generating')",
            ("interrupted: the backend stopped before this document finished; upload it again",),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                metric_keys TEXT NOT NULL,
                options TEXT NOT NULL DEFAULT '{}',
                results TEXT NOT NULL DEFAULT '[]',
                current TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        # A job lives in the backend process: one still queued or running when
        # the backend stopped will never finish.
        conn.execute(
            "UPDATE jobs SET status = 'interrupted', error = ?, current = NULL "
            "WHERE status IN ('queued', 'running')",
            (JOB_INTERRUPTED,),
        )
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Human scores for judged cases (judge calibration): one per run case.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS human_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                metric_key TEXT NOT NULL,
                run_id INTEGER NOT NULL,
                case_index INTEGER NOT NULL,
                question TEXT,
                actual_output TEXT,
                judge_score REAL,
                judge_passed INTEGER,
                threshold REAL,
                direction TEXT,
                human_score REAL NOT NULL,
                note TEXT,
                ts TEXT NOT NULL,
                UNIQUE (run_id, case_index)
            )
            """
        )
        # Pairwise A/B comparisons (backend/dashboard/pairwise.py): one row per
        # comparison, kept apart from runs so the per-metric trend stays clean.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pairwise_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_a INTEGER NOT NULL,
                target_b INTEGER NOT NULL,
                ts TEXT NOT NULL,
                wins_a INTEGER,
                wins_b INTEGER,
                ties INTEGER,
                result_json TEXT
            )
            """
        )
        _migrate_targets(conn)
        conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    with _LOCK:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """Stores a setting; None removes it."""
    with _LOCK:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        conn.commit()


def _legacy_to_template(config: dict) -> dict:
    """Rewrites a pre-cURL HTTP config (base_url + chat_path + message_field,
    or the OpenAI messages shape) as the url + body_template the connector uses."""
    if "url" in config or "base_url" not in config:
        return config
    url = config["base_url"].rstrip("/") + config.get("chat_path", "/chat")
    if config.get("request_format") == "openai_messages":
        body = {"messages": [{"role": "user", "content": "{{message}}"}]}
        if config.get("model"):
            body["model"] = config["model"]
    else:
        body = {config.get("message_field", "message"): "{{message}}"}
    migrated = {
        "url": url,
        "method": "POST",
        "headers": {"Content-Type": "application/json", **config.get("headers", {})},
        "body_template": json.dumps(body),
        "response_path": config.get("response_path", "reply"),
    }
    if "theme" in config:
        migrated["theme"] = config["theme"]
    return migrated


def _migrate_targets(conn: sqlite3.Connection) -> None:
    """Only cURL-style HTTP chatbots remain: legacy HTTP configs are rewritten,
    and the retired sample (mock) and web-page (dom) chatbots are removed."""
    retired = [r["id"] for r in conn.execute("SELECT id FROM targets WHERE type != 'http'")]
    for target_id in retired:
        conn.execute("DELETE FROM runs WHERE target_id = ?", (target_id,))
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
    for row in conn.execute("SELECT id, config_json FROM targets").fetchall():
        config = json.loads(row["config_json"])
        migrated = _legacy_to_template(config)
        if migrated is not config:
            conn.execute(
                "UPDATE targets SET config_json = ? WHERE id = ?",
                (json.dumps(migrated), row["id"]),
            )


def _target_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "config": json.loads(row["config_json"]),
    }


def add_target(conn: sqlite3.Connection, name: str, type_: str, config: dict) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO targets (name, type, config_json) VALUES (?, ?, ?)",
            (name, type_, json.dumps(config)),
        )
        conn.commit()
        return cur.lastrowid


def get_target(conn: sqlite3.Connection, target_id: int) -> dict[str, Any] | None:
    with _LOCK:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
    return None if row is None else _target_row(row)


def list_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    with _LOCK:
        rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
    return [_target_row(r) for r in rows]


def delete_target(conn: sqlite3.Connection, target_id: int) -> None:
    with _LOCK:
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        conn.commit()


def update_target(conn: sqlite3.Connection, target_id: int, name: str, type_: str, config: dict) -> bool:
    """Update a target in place, keeping its id (and so its run history)."""
    with _LOCK:
        cur = conn.execute(
            "UPDATE targets SET name = ?, type = ?, config_json = ? WHERE id = ?",
            (name, type_, json.dumps(config), target_id),
        )
        conn.commit()
        return cur.rowcount > 0


def clear_runs(conn: sqlite3.Connection, target_id: int) -> int:
    """Wipe a target's run history only — the target itself and its goldens stay.

    Mainly for after editing a target's type: old scores were produced by
    whatever chatbot the target used to be, and no longer describe it.
    """
    with _LOCK:
        cur = conn.execute("DELETE FROM runs WHERE target_id = ?", (target_id,))
        conn.commit()
        return cur.rowcount


def record_run(
    conn: sqlite3.Connection,
    target_id: int,
    metric_key: str,
    score: float | None,
    passed: bool,
    ts: str,
    cases_run: int | None = None,
    judge_model: str | None = None,
    judge_tokens: int | None = None,
    judge_calls: int | None = None,
    target_calls: int | None = None,
    duration_s: float | None = None,
    judge_spread: float | None = None,
    cases_skipped: int | None = None,
    result_json: str | None = None,
    latency_p50: float | None = None,
    latency_p95: float | None = None,
    judge_cost_usd: float | None = None,
) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO runs (target_id, metric_key, score, passed, ts, cases_run, judge_model,"
            " judge_tokens, judge_calls, target_calls, duration_s, judge_spread, cases_skipped,"
            " result_json, latency_p50, latency_p95, judge_cost_usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_id, metric_key, score, int(passed), ts, cases_run, judge_model,
             judge_tokens, judge_calls, target_calls, duration_s, judge_spread, cases_skipped,
             result_json, latency_p50, latency_p95, judge_cost_usd),
        )
        conn.commit()
        return cur.lastrowid


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    with _LOCK:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else dict(row)


def add_pairwise_run(conn: sqlite3.Connection, target_a: int, target_b: int, result: dict) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO pairwise_runs (target_a, target_b, ts, wins_a, wins_b, ties, result_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target_a, target_b, _now_iso(), result.get("wins_a"), result.get("wins_b"),
             result.get("ties"), json.dumps(result, default=str)),
        )
        conn.commit()
        return cur.lastrowid


def upsert_label(conn: sqlite3.Connection, label: dict) -> int:
    """One human score per run case; labelling the same case again replaces it."""
    fields = ("target_id", "metric_key", "run_id", "case_index", "question", "actual_output",
              "judge_score", "judge_passed", "threshold", "direction", "human_score", "note")
    values = [label.get(f) for f in fields] + [_now_iso()]
    with _LOCK:
        conn.execute(
            f"INSERT INTO human_labels ({', '.join(fields)}, ts) VALUES ({', '.join('?' * (len(fields) + 1))}) "
            "ON CONFLICT(run_id, case_index) DO UPDATE SET human_score = excluded.human_score, "
            "note = excluded.note, ts = excluded.ts",
            values,
        )
        row = conn.execute("SELECT id FROM human_labels WHERE run_id = ? AND case_index = ?",
                           (label["run_id"], label["case_index"])).fetchone()
        conn.commit()
    return row["id"]


def list_labels(conn: sqlite3.Connection, target_id: int, metric_key: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        if metric_key:
            rows = conn.execute("SELECT * FROM human_labels WHERE target_id = ? AND metric_key = ? ORDER BY id DESC",
                                (target_id, metric_key)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM human_labels WHERE target_id = ? ORDER BY id DESC",
                                (target_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_label(conn: sqlite3.Connection, label_id: int) -> bool:
    with _LOCK:
        cur = conn.execute("DELETE FROM human_labels WHERE id = ?", (label_id,))
        conn.commit()
    return cur.rowcount > 0


def list_pairwise_runs(conn: sqlite3.Connection, target_id: int) -> list[dict[str, Any]]:
    """Comparisons a chatbot took part in (as A or B), newest first."""
    with _LOCK:
        rows = conn.execute(
            "SELECT * FROM pairwise_runs WHERE target_a = ? OR target_b = ? ORDER BY id DESC",
            (target_id, target_id),
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        raw = row.pop("result_json", None)
        row["result"] = json.loads(raw) if raw else None
        out.append(row)
    return out


def latest_runs(conn: sqlite3.Connection, target_id: int, since_id: int = 0) -> list[dict[str, Any]]:
    """Each metric's newest run; `since_id` hides runs made before a session began."""
    with _LOCK:
        rows = conn.execute(
            """
            SELECT r.* FROM runs r
            INNER JOIN (
                SELECT metric_key, MAX(id) AS max_id
                FROM runs WHERE target_id = ? AND id > ?
                GROUP BY metric_key
            ) latest ON r.id = latest.max_id
            ORDER BY r.metric_key
            """,
            (target_id, since_id),
        ).fetchall()
    return [dict(r) for r in rows]


def history(conn: sqlite3.Connection, target_id: int, metric_key: str,
            since_id: int = 0) -> list[dict[str, Any]]:
    with _LOCK:
        rows = conn.execute(
            "SELECT * FROM runs WHERE target_id = ? AND metric_key = ? AND id > ? ORDER BY id",
            (target_id, metric_key, since_id),
        ).fetchall()
    return [dict(r) for r in rows]


def max_run_id(conn: sqlite3.Connection) -> int:
    """The newest run id: a session started now shows only the runs made after it."""
    with _LOCK:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()
    return int(row[0])


def delete_run(conn: sqlite3.Connection, run_id: int) -> bool:
    """Remove one run from the stored history."""
    with _LOCK:
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()
        return cur.rowcount > 0


# Background runs (backend/dashboard/jobs.py): one row per batch of metrics.
JOB_INTERRUPTED = "the backend stopped before this run finished; run it again"
_JOB_JSON = {"metric_keys": [], "options": {}, "results": [], "current": None}


def _job(row) -> dict:
    job = dict(row)
    for key, empty in _JOB_JSON.items():
        job[key] = json.loads(job[key]) if job[key] else empty
    return job


def add_job(conn: sqlite3.Connection, target_id: int, metric_keys: list[str], options: dict) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO jobs (target_id, status, metric_keys, options, created_at) VALUES (?, 'queued', ?, ?, ?)",
            (target_id, json.dumps(metric_keys), json.dumps(options), _now_iso()),
        )
        conn.commit()
        return cur.lastrowid


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    with _LOCK:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    """Field names come from this codebase only (never from a request)."""
    encoded = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
    assignments = ", ".join(f"{k} = ?" for k in encoded)
    with _LOCK:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*encoded.values(), job_id))
        conn.commit()


def active_jobs(conn: sqlite3.Connection, target_id: int) -> list[dict]:
    with _LOCK:
        rows = conn.execute(
            # The running job first, then the queue in the order it will run.
            "SELECT * FROM jobs WHERE target_id = ? AND status IN ('queued', 'running') "
            "ORDER BY status = 'running' DESC, id",
            (target_id,),
        ).fetchall()
    return [_job(r) for r in rows]


def next_queued_job(conn: sqlite3.Connection) -> dict | None:
    with _LOCK:
        row = conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    return _job(row) if row else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_document(conn: sqlite3.Connection, theme: str, filename: str) -> int:
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO documents (theme, filename, uploaded_at, status, error) VALUES (?, ?, ?, ?, ?)",
            (theme, filename, _now_iso(), "queued", None),
        )
        conn.commit()
        return cur.lastrowid


def set_document_status(
    conn: sqlite3.Connection,
    document_id: int,
    status: str,
    error: str | None = None,
    goldens_created: int | None = None,
) -> None:
    """status: queued -> enhancing (optional) -> generating -> ready | error."""
    with _LOCK:
        conn.execute(
            "UPDATE documents SET status = ?, error = ?, goldens_created = ? WHERE id = ?",
            (status, error, goldens_created, document_id),
        )
        conn.commit()


def set_document_enhanced(conn: sqlite3.Connection, document_id: int, enhanced_path: str) -> None:
    with _LOCK:
        conn.execute("UPDATE documents SET enhanced_path = ? WHERE id = ?", (enhanced_path, document_id))
        conn.commit()


def get_document(conn: sqlite3.Connection, document_id: int) -> dict[str, Any] | None:
    with _LOCK:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    return None if row is None else dict(row)


def list_documents(conn: sqlite3.Connection, theme: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        if theme is None:
            rows = conn.execute("SELECT * FROM documents ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM documents WHERE theme = ? ORDER BY id DESC", (theme,)
            ).fetchall()
    return [dict(r) for r in rows]


def delete_document(conn: sqlite3.Connection, document_id: int) -> None:
    with _LOCK:
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        conn.commit()
