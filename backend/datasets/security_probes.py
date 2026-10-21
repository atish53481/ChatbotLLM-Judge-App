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
import shutil
import threading
import uuid

_DIR = os.path.dirname(__file__)
PROBES_PATH = os.path.join(_DIR, "security_probes.json")
DEFAULT_PROBES_PATH = os.path.join(_DIR, "security_probes.default.json")
_LOCK = threading.RLock()

# "generic" probes fit any assistant and are the default (the app judges any
# chatbot, not just a shop); "ecommerce" ones talk about orders, refunds and
# customers (the shipped ShopEasy bot). A target picks one set.
PROBE_SETS = ("generic", "ecommerce")
DEFAULT_PROBE_SET = "generic"

SECURITY_METRICS = (
    "prompt_injection", "jailbreak", "encoded_injection", "data_exfiltration",
    "social_engineering", "domain_misuse", "non_advice", "role_violation",
    "harmful_content", "multi_turn_jailbreak",
)
# Safety metrics that also read adversarial prompts from this store: Bias and
# Toxicity send them before the goldens (a neutral golden rarely provokes a
# biased or toxic reply), No-Prompt-Leak sends only them.
SAFETY_PROBE_METRICS = ("bias", "toxicity", "no_prompt_leak")
PROBE_METRICS = SECURITY_METRICS + SAFETY_PROBE_METRICS


def _read_all() -> list[dict]:
    if not os.path.exists(PROBES_PATH):
        shutil.copyfile(DEFAULT_PROBES_PATH, PROBES_PATH)
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
    with _LOCK:
        rows = _read_all()
    if metric is not None:
        rows = [r for r in rows if r["metric"] == metric]
    if probe_set is not None:
        rows = [r for r in rows if r.get("set", DEFAULT_PROBE_SET) == probe_set]
    return rows


def _clean(fields: dict) -> dict:
    metric = fields.get("metric", "")
    if metric not in PROBE_METRICS:
        raise ValueError(f"metric must be one of: {', '.join(PROBE_METRICS)}")
    question = fields.get("question", "").strip()
    if not question:
        raise ValueError("a probe needs a question")
    probe_set = fields.get("set") or DEFAULT_PROBE_SET
    if probe_set not in PROBE_SETS:
        raise ValueError(f"set must be one of: {', '.join(PROBE_SETS)}")
    return {"metric": metric, "set": probe_set, "question": question, "note": fields.get("note", "").strip()}


def add_probe(fields: dict) -> dict:
    row = {"id": f"sp_{uuid.uuid4().hex[:8]}", **_clean(fields)}
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
    return None


def delete_probe(probe_id: str) -> bool:
    with _LOCK:
        rows = _read_all()
        kept = [r for r in rows if r["id"] != probe_id]
        if len(kept) == len(rows):
            return False
        _write_all(kept)
    return True


def reset_to_defaults() -> int:
    """Replaces the edited copy with the shipped probes (never reads the old copy,
    so a corrupt file is fixed too); returns how many."""
    with open(DEFAULT_PROBES_PATH, "r", encoding="utf-8") as f:
        defaults = json.load(f)
    with _LOCK:
        _write_all(defaults)
    return len(defaults)
