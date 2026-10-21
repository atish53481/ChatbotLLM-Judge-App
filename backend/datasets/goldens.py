"""JSON-backed golden dataset store — the 'golden rule' reference answers.

Single-user tool: read-modify-write the whole file under a lock. Reads take
the lock too (FastAPI serves requests from several threads), and writes go
to a temp file that then replaces the original, so a crash mid-write can
never truncate the user's hand-written goldens.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime, timezone

GOLDENS_PATH = os.path.join(os.path.dirname(__file__), "goldens.json")
# The shipped golden sets. The side panel restores them on every load, so edits
# and deletes last one session (see reset_to_defaults).
DEFAULT_GOLDENS_PATH = os.path.join(os.path.dirname(__file__), "goldens.default.json")
_LOCK = threading.RLock()


def _read_all() -> list[dict]:
    # goldens.json is runtime state (it is git-ignored, like the DB and the
    # edited conversations/probes). On a fresh checkout it does not exist yet,
    # so seed it from the shipped defaults.
    if not os.path.exists(GOLDENS_PATH):
        shutil.copyfile(DEFAULT_GOLDENS_PATH, GOLDENS_PATH)
    with open(GOLDENS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(rows: list[dict]) -> None:
    try:
        before = _read_all()
    except (OSError, ValueError):
        before = []
    tmp_path = GOLDENS_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, GOLDENS_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    # The caller (add_golden, delete_golden, reset_to_defaults, ...) names the action.
    _log_change(sys._getframe(1).f_code.co_name, before, rows)


def _changes_log_path() -> str:
    return os.path.join(os.path.dirname(GOLDENS_PATH), "goldens_changes.log")


def _log_change(action: str, before: list[dict], after: list[dict]) -> None:
    """Appends one JSON line saying what a write changed, so rows that vanish can
    be traced to what removed them. Logging never blocks the write itself."""
    old = {r["id"]: r for r in before if "id" in r}
    new = {r["id"]: r for r in after if "id" in r}
    added = [i for i in new if i not in old]
    removed = [i for i in old if i not in new]
    changed = [i for i in new if i in old and new[i] != old[i]]
    if not (added or removed or changed):
        return
    themes = sorted({r.get("theme") for r in [*before, *after] if r.get("id") in {*added, *removed, *changed}})
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "added": added,
        "removed": removed,
        "changed": changed,
        "themes": {
            t: {"before": sum(r.get("theme") == t for r in before),
                "after": sum(r.get("theme") == t for r in after)}
            for t in themes
        },
    }
    try:
        with open(_changes_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def shipped_health() -> list[dict]:
    """Per shipped theme: how many default rows ship and how many are present."""
    with open(DEFAULT_GOLDENS_PATH, "r", encoding="utf-8") as f:
        defaults = json.load(f)
    with _LOCK:
        present_ids = {r["id"] for r in _read_all()}
    health: dict[str, dict] = {}
    for row in defaults:
        h = health.setdefault(row["theme"], {"theme": row["theme"], "shipped": 0, "present": 0, "missing": 0})
        h["shipped"] += 1
        h["present" if row["id"] in present_ids else "missing"] += 1
    return list(health.values())


def reset_to_defaults() -> int:
    """Puts every shipped theme back to its default goldens; returns how many.

    This file is the reference set. The panel calls this on every load, so hand
    edits to a shipped theme last one session. A document's generated goldens
    are kept: the document row outlives the session, and deleting or
    regenerating it is what removes them (delete_generated_for). A theme the
    shipped set does not include is a chatbot's own golden set, and keeps all
    of its rows.
    """
    with open(DEFAULT_GOLDENS_PATH, "r", encoding="utf-8") as f:
        defaults = json.load(f)
    shipped = {row["theme"] for row in defaults}
    with _LOCK:
        kept = [row for row in _read_all()
                if row["theme"] not in shipped or row.get("source") == "synthesized"]
        _write_all(defaults + kept)
    return len(defaults)


def load_goldens(theme: str | None = None) -> list[dict]:
    with _LOCK:
        rows = _read_all()
    if theme is None:
        return rows
    return [r for r in rows if r["theme"] == theme]


def add_golden(
    theme: str,
    question: str,
    expected_answer: str,
    context: list[str] | None = None,
    categories: list[str] | None = None,
    source: str = "manual",
    source_document: str | None = None,
    source_document_id: int | None = None,
    extra: dict | None = None,
) -> dict:
    """extra: optional fields stored with the row (a generated golden's
    evolutions, status "draft" while it waits for review)."""
    with _LOCK:
        rows = _read_all()
        new_row = {
            "id": f"g_{uuid.uuid4().hex[:8]}",
            "theme": theme,
            "question": question,
            "expected_answer": expected_answer,
            "context": context or [],
            "categories": categories or [],
            "source": source,
            "source_document": source_document,
            "source_document_id": source_document_id,
            **{k: v for k, v in (extra or {}).items() if v not in (None, [], "")},
        }
        rows.append(new_row)
        _write_all(rows)
        return new_row


def delete_generated_for(
    theme: str, source_document: str | None = None, source_document_id: int | None = None
) -> int:
    """Removes the goldens a document generated, so re-uploading it or asking the
    panel to regenerate replaces the set instead of stacking a second one on top.
    Matches on the document's id, or on its file name — a re-upload is a new
    document row, so the name is what ties the two together. Returns how many."""
    with _LOCK:
        rows = _read_all()

        def generated_by_it(row: dict) -> bool:
            if row.get("source") != "synthesized" or row.get("theme") != theme:
                return False
            if source_document_id is not None and row.get("source_document_id") == source_document_id:
                return True
            return source_document is not None and row.get("source_document") == source_document

        remaining = [row for row in rows if not generated_by_it(row)]
        if len(remaining) != len(rows):
            _write_all(remaining)
        return len(rows) - len(remaining)


def update_golden(
    golden_id: str,
    theme: str,
    question: str,
    expected_answer: str,
    context: list[str] | None = None,
    categories: list[str] | None = None,
) -> dict | None:
    """Replaces one row's fields in place, keeping its id. None when unknown."""
    with _LOCK:
        rows = _read_all()
        for row in rows:
            if row["id"] == golden_id:
                row.update({
                    "theme": theme,
                    "question": question,
                    "expected_answer": expected_answer,
                    "context": context or [],
                    "categories": categories or [],
                })
                _write_all(rows)
                return dict(row)
        return None


DRAFT = "draft"


def is_draft(row: dict) -> bool:
    """A generated golden waiting for review: listed, but never sent in a run."""
    return row.get("status") == DRAFT


def review_goldens(golden_ids: list[str], action: str) -> int:
    """accept: the drafts become ordinary goldens; reject: they are deleted.
    Returns how many rows changed."""
    if action not in ("accept", "reject"):
        raise ValueError("action must be accept or reject")
    wanted = set(golden_ids)
    with _LOCK:
        rows = _read_all()
        hits = [r for r in rows if r["id"] in wanted and is_draft(r)]
        if not hits:
            return 0
        if action == "reject":
            rows = [r for r in rows if r not in hits]
        else:
            for r in hits:
                r.pop("status", None)
        _write_all(rows)
    return len(hits)


def delete_golden(golden_id: str) -> bool:
    with _LOCK:
        rows = _read_all()
        remaining = [r for r in rows if r["id"] != golden_id]
        if len(remaining) == len(rows):
            return False
        _write_all(remaining)
        return True
