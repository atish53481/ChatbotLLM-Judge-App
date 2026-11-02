"""Red-team probes for the Security metrics and adversarial prompts for the
safety metrics (Bias, Toxicity, No-Prompt-Leak).

Each probe is one attack message sent to the chatbot; the judge scores whether
the reply resisted it. Like the golden answers: the shipped probes are in
security_probes.default.json, edits go to security_probes.json, and
reset_to_defaults() (called with the goldens reset on every side panel load)
restores the shipped set.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid

from backend.datasets import deepteam_sync

_DIR = os.path.dirname(__file__)
PROBES_PATH = os.path.join(_DIR, "security_probes.json")
DEFAULT_PROBES_PATH = os.path.join(_DIR, "security_probes.default.json")
_LOCK = threading.RLock()

# "generic" probes fit any assistant and are the default (the app judges any
# chatbot, not just a shop); "ecommerce" ones talk about orders, refunds and
# customers (the shipped ShopEasy bot); "latest" ones are written once a day
# by the judge from OWASP / DeepTeam standards (backend/datasets/probe_refresh.py).
# A target picks one set.
LATEST_SET = "latest"
# Sets shipped in security_probes.default.json; "latest" is written at run time.
SHIPPED_PROBE_SETS = ("generic", "ecommerce")
PROBE_SETS = SHIPPED_PROBE_SETS + (LATEST_SET,)
DEFAULT_PROBE_SET = "generic"
# A chatbot's own probes, written for it by the test-data build
# (backend/datasets/prepare_all.py): set "target_<id>", kept in its own file
# next to the latest one, so the panel reset never wipes it.
TARGET_SET_PATTERN = re.compile(r"^target_\d+$")


def is_target_set(probe_set: str | None) -> bool:
    return bool(probe_set and TARGET_SET_PATTERN.match(probe_set))


def target_set(target_id: int) -> str:
    return f"target_{int(target_id)}"


def target_probes_path(probe_set: str) -> str:
    """Where a chatbot's own probe set lives: security_probes.target_<id>.json,
    next to the edited copy (so tests, which move that copy, move these too)."""
    if not is_target_set(probe_set):
        raise ValueError(f"not a chatbot's own probe set: {probe_set!r}")
    return os.path.join(os.path.dirname(PROBES_PATH), f"security_probes.{probe_set}.json")


_TARGET_ID = re.compile(r"^sp_t(\d+)_")

SECURITY_METRICS = (
    "prompt_injection", "jailbreak", "encoded_injection", "data_exfiltration",
    "social_engineering", "domain_misuse", "non_advice", "role_violation",
    "harmful_content", "multi_turn_jailbreak",
)
# Safety metrics that also read adversarial prompts from this store: Bias and
# Toxicity send them before the goldens (a neutral golden rarely provokes a
# biased or toxic reply), No-Prompt-Leak sends only them.
SAFETY_PROBE_METRICS = ("bias", "toxicity", "no_prompt_leak")
# One metric per DeepTeam vulnerability the app had no metric for
# (backend/datasets/deepteam_sync.py): their shipped probes are DeepTeam's own
# template examples, added to the "generic" set whenever the store is seeded or
# reset, so a newer deepteam brings its new test cases with it.
DEEPTEAM_METRICS = deepteam_sync.metric_keys()
PROBE_METRICS = SECURITY_METRICS + SAFETY_PROBE_METRICS + DEEPTEAM_METRICS


def deepteam_defaults() -> list[dict]:
    """The DeepTeam metrics' shipped probes (tests replace this)."""
    return deepteam_sync.catalog_probes()


def _defaults() -> list[dict]:
    with open(DEFAULT_PROBES_PATH, "r", encoding="utf-8") as f:
        rows = json.load(f)
    ids = {r["id"] for r in rows}
    return rows + [r for r in deepteam_defaults() if r["id"] not in ids]


def _read_all() -> list[dict]:
    if not os.path.exists(PROBES_PATH):
        _write_all(_defaults())
    with open(PROBES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(rows: list[dict]) -> None:
    tmp_path = PROBES_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, PROBES_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def load_probes(metric: str | None = None, probe_set: str | None = None) -> list[dict]:
    if probe_set == LATEST_SET:
        # The daily refresh lives in its own file (probe_refresh.LATEST_PATH), so
        # the reset on every panel load never wipes it. Before the first refresh,
        # or for a metric it could not write, the shipped generic probes stand in.
        from backend.datasets import probe_refresh  # lazy: probe_refresh imports this module
        latest = [r for r in probe_refresh.load_latest().get("probes", [])
                  if metric is None or r["metric"] == metric]
        with _LOCK:  # plus any a user added to the "latest" set by hand
            added = [r for r in _read_all()
                     if r.get("set") == LATEST_SET and (metric is None or r["metric"] == metric)]
        return latest + added or load_probes(metric, DEFAULT_PROBE_SET)
    if is_target_set(probe_set):
        # A chatbot's own probes (written for it by the test-data build, or added
        # to its set by hand): their own file survives the panel reset. A metric
        # it has none of uses the shipped generic probes. Without a metric: its
        # own probes only.
        with _LOCK:
            own = _read_target_probes(probe_set)
        if metric is None:
            return own
        return [r for r in own if r["metric"] == metric] or load_probes(metric, DEFAULT_PROBE_SET)
    with _LOCK:
        rows = _read_all()
    if metric is not None:
        rows = [r for r in rows if r["metric"] == metric]
    if probe_set is not None:
        picked = [r for r in rows if r.get("set", DEFAULT_PROBE_SET) == probe_set]
        # DeepTeam's probes fit any assistant: a set without its own uses them.
        if not picked and metric in DEEPTEAM_METRICS and probe_set != DEFAULT_PROBE_SET:
            picked = [r for r in rows if r.get("set", DEFAULT_PROBE_SET) == DEFAULT_PROBE_SET]
        rows = picked
    return rows


def _read_target_probes(probe_set: str) -> list[dict]:
    path = target_probes_path(probe_set)
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _write_target_probes(probe_set: str, rows: list[dict]) -> None:
    path = target_probes_path(probe_set)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _target_row(target_id: int, fields: dict) -> dict:
    return {"id": f"sp_t{int(target_id)}_{uuid.uuid4().hex[:8]}", "metric": fields["metric"],
            "set": target_set(target_id), "question": fields["question"], "note": fields.get("note", "")}


def add_target_probes(target_id: int, rows: list[dict]) -> int:
    """Adds probes to a chatbot's own set (the build's fitted ones, kept beside
    any already there). Rows need a known metric and a question; returns how
    many were added."""
    clean = []
    for row in rows:
        if not isinstance(row, dict) or row.get("metric") not in PROBE_METRICS:
            continue
        question = str(row.get("question") or "").strip()
        if question:
            clean.append(_target_row(target_id, {"metric": row["metric"], "question": question,
                                                 "note": str(row.get("note") or "").strip()}))
    if clean:
        probe_set = target_set(target_id)
        with _LOCK:
            _write_target_probes(probe_set, _read_target_probes(probe_set) + clean)
    return len(clean)


def clear_target_probes(target_id: int) -> None:
    """Removes a chatbot's own probe set (when the chatbot is deleted)."""
    path = target_probes_path(target_set(target_id))
    with _LOCK:
        if os.path.exists(path):
            os.remove(path)


def _target_set_of(probe_id: str) -> str | None:
    match = _TARGET_ID.match(probe_id or "")
    return target_set(int(match.group(1))) if match else None


def _clean(fields: dict) -> dict:
    metric = fields.get("metric", "")
    if metric not in PROBE_METRICS:
        raise ValueError(f"metric must be one of: {', '.join(PROBE_METRICS)}")
    question = fields.get("question", "").strip()
    if not question:
        raise ValueError("a probe needs a question")
    probe_set = fields.get("set") or DEFAULT_PROBE_SET
    if probe_set not in PROBE_SETS and not is_target_set(probe_set):
        raise ValueError(f"set must be one of: {', '.join(PROBE_SETS)}")
    return {"metric": metric, "set": probe_set, "question": question, "note": fields.get("note", "").strip()}


def add_probe(fields: dict) -> dict:
    clean = _clean(fields)
    if is_target_set(clean["set"]):
        # A chatbot's own set lives in its own file (it survives the panel reset).
        target_id = int(clean["set"].split("_", 1)[1])
        row = _target_row(target_id, clean)
        with _LOCK:
            _write_target_probes(clean["set"], _read_target_probes(clean["set"]) + [row])
        return row
    row = {"id": f"sp_{uuid.uuid4().hex[:8]}", **clean}
    with _LOCK:
        rows = _read_all()
        rows.append(row)
        _write_all(rows)
    return row


def update_probe(probe_id: str, fields: dict) -> dict | None:
    clean = _clean(fields)
    with _LOCK:
        rows = _read_all()
        for i, row in enumerate(rows):
            if row["id"] == probe_id:
                rows[i] = {"id": probe_id, **clean}
                _write_all(rows)
                return rows[i]
        own_set = _target_set_of(probe_id)
        if own_set:  # one of a chatbot's own probes: edited in its file, in its set
            own = _read_target_probes(own_set)
            for i, row in enumerate(own):
                if row["id"] == probe_id:
                    own[i] = {**row, "metric": clean["metric"], "question": clean["question"],
                              "note": clean["note"]}
                    _write_target_probes(own_set, own)
                    return own[i]
    return None


def delete_probe(probe_id: str) -> bool:
    with _LOCK:
        rows = _read_all()
        kept = [r for r in rows if r["id"] != probe_id]
        if len(kept) != len(rows):
            _write_all(kept)
            return True
        own_set = _target_set_of(probe_id)
        if own_set:
            own = _read_target_probes(own_set)
            left = [r for r in own if r["id"] != probe_id]
            if len(left) != len(own):
                _write_target_probes(own_set, left)
                return True
    return False


def reset_to_defaults() -> int:
    """Replaces the edited copy with the shipped probes (never reads the old copy,
    so a corrupt file is fixed too); returns how many."""
    defaults = _defaults()
    with _LOCK:
        _write_all(defaults)
    return len(defaults)
