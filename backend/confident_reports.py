"""Every report sent to Confident AI, kept for good.

Each upload (a scan's test run, its conversations run, a DeepTeam run as a risk
assessment or test run) adds one row to `confident_reports` and saves the exact
JSON that was sent under REPORTS_DIR, so a later re-send never replaces an
earlier report: the link and the copy of what Confident AI received both stay.
Uploads made before this store existed are added once from the links kept on
their jobs / DeepTeam runs (`backfill`); their JSON copy is rebuilt from the
stored results the first time it is downloaded (`rebuilt: true` in the file).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR_ENV = "JUDGE_CONFIDENT_REPORTS_DIR"
KINDS = ("scan", "deepteam")


def reports_dir() -> Path:
    return Path(os.getenv(DIR_ENV) or ROOT / "reports" / "confident")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS confident_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            ref_id INTEGER NOT NULL,
            target_id INTEGER,
            title TEXT NOT NULL,
            test_run_id TEXT,
            link TEXT,
            links_json TEXT,
            test_cases INTEGER,
            sent_as TEXT,
            sent_at TEXT NOT NULL,
            file TEXT
        )"""
    )
    conn.commit()


_COLS = ("id", "kind", "ref_id", "target_id", "title", "test_run_id", "link", "links_json",
         "test_cases", "sent_as", "sent_at", "file")


def _row(r) -> dict:
    row = dict(zip(_COLS, r))
    row["links"] = json.loads(row.pop("links_json") or "{}")
    row["saved"] = bool(row["file"]) and (reports_dir() / row["file"]).is_file()
    return row


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_name(report_id: int, kind: str, ref_id: int) -> str:
    return f"{report_id:05d}-{kind}-{ref_id}.json"


def write_file(conn: sqlite3.Connection, report_id: int, payloads: list, rebuilt: bool = False) -> str:
    """Saves the JSON sent for one report; returns the file name."""
    row = get(conn, report_id)
    name = _file_name(report_id, row["kind"], row["ref_id"])
    folder = reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    meta = {k: row[k] for k in ("id", "kind", "ref_id", "target_id", "title", "test_run_id", "link",
                                "links", "test_cases", "sent_as", "sent_at")}
    doc = {"report": meta, "rebuilt": rebuilt, "payloads": payloads}
    (folder / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    conn.execute("UPDATE confident_reports SET file = ? WHERE id = ?", (name, report_id))
    conn.commit()
    return name


def record(conn: sqlite3.Connection, kind: str, ref_id: int, target_id: int | None, title: str,
           sent: dict, payloads: list | None = None, sent_at: str | None = None) -> dict:
    """Adds one sent report (and its JSON copy when `payloads` is given)."""
    if kind not in KINDS:
        raise ValueError(f"unknown report kind {kind!r}")
    links = {k: v for k, v in {"conversations": sent.get("conversations_link"),
                               "multi_turn": sent.get("multi_turn_link")}.items() if v}
    cur = conn.execute(
        "INSERT INTO confident_reports (kind, ref_id, target_id, title, test_run_id, link, links_json, test_cases,"
        " sent_as, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, ref_id, target_id, title, sent.get("id"), sent.get("link"), json.dumps(links),
         sent.get("test_cases"), sent.get("as") or ("test_run" if kind == "scan" else "risk_assessment"),
         sent_at or sent.get("sent_at") or _now()))
    conn.commit()
    report_id = cur.lastrowid
    if payloads:
        write_file(conn, report_id, payloads)
    return get(conn, report_id)


def get(conn: sqlite3.Connection, report_id: int) -> dict | None:
    r = conn.execute(f"SELECT {', '.join(_COLS)} FROM confident_reports WHERE id = ?", (report_id,)).fetchone()
    return _row(r) if r else None


def list_reports(conn: sqlite3.Connection, target_id: int | None = None) -> list[dict]:
    sql = f"SELECT {', '.join(_COLS)} FROM confident_reports"
    args: tuple = ()
    if target_id is not None:
        sql += " WHERE target_id = ?"
        args = (target_id,)
    return [_row(r) for r in conn.execute(sql + " ORDER BY sent_at DESC, id DESC", args).fetchall()]


def read_file(row: dict) -> bytes | None:
    if not row.get("file") or not re.fullmatch(r"[\w.-]+\.json", row["file"]):
        return None
    path = reports_dir() / row["file"]
    return path.read_bytes() if path.is_file() else None


def backfill(conn: sqlite3.Connection) -> int:
    """Adds uploads made before this store existed, from the links kept on jobs
    and DeepTeam runs. Each test-run id is added once; returns how many."""
    known = {r[0] for r in conn.execute("SELECT test_run_id FROM confident_reports").fetchall() if r[0]}
    added = 0
    for job_id, target_id, options in conn.execute("SELECT id, target_id, options FROM jobs ORDER BY id").fetchall():
        sent = (json.loads(options or "{}") or {}).get("confident") or {}
        if sent.get("link") and sent.get("id") not in known:
            record(conn, "scan", job_id, target_id, f"Scan job {job_id}", sent)
            known.add(sent.get("id"))
            added += 1
    has_dt = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deepteam_runs'").fetchone()
    if has_dt:
        for run_id, target_id, result in conn.execute(
                "SELECT id, target_id, result_json FROM deepteam_runs ORDER BY id").fetchall():
            sent = (json.loads(result or "{}") or {}).get("confident") or {}
            if sent.get("link") and sent.get("id") not in known:
                record(conn, "deepteam", run_id, target_id, f"DeepTeam run #{run_id}", sent)
                known.add(sent.get("id"))
                added += 1
    return added
