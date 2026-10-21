"""The user's own metrics: G-Eval rubrics and DAG decision trees.

A G-Eval metric scores a subjective criterion ("is the tone polite?"); a DAG
metric scores explicit rules as a decision tree of narrow judge questions, so
the same reply always lands on the same verdict. Both run on the goldens like
the shipped metrics (metrics_catalog.custom_spec builds their MetricSpec).

The shipped examples are in custom_metrics.default.json and seed
custom_metrics.json on first use. Unlike goldens, these are the user's own
work, so nothing resets them on panel load; POST /api/custom-metrics/reset
restores the examples on request.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid

_DIR = os.path.dirname(__file__)
CUSTOM_METRICS_PATH = os.path.join(_DIR, "custom_metrics.json")
DEFAULT_CUSTOM_METRICS_PATH = os.path.join(_DIR, "custom_metrics.default.json")
_LOCK = threading.RLock()

KINDS = ("geval", "dag")
# Test-case fields a custom metric may score (DeepEval SingleTurnParams values).
PARAMS = ("input", "actual_output", "expected_output", "context", "retrieval_context")
DATASETS = ("goldens", "goldens_with_context")
KEY_PREFIX = "custom_"


def _read_all() -> list[dict]:
    if not os.path.exists(CUSTOM_METRICS_PATH):
        shutil.copyfile(DEFAULT_CUSTOM_METRICS_PATH, CUSTOM_METRICS_PATH)
    with open(CUSTOM_METRICS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(rows: list[dict]) -> None:
    tmp_path = CUSTOM_METRICS_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CUSTOM_METRICS_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def load_metrics() -> list[dict]:
    with _LOCK:
        try:
            return _read_all()
        except (OSError, ValueError):
            return []  # a broken file hides the custom metrics, it never breaks the catalog


def get_metric(key: str) -> dict | None:
    return next((row for row in load_metrics() if row["key"] == key), None)


def check_dag_safety(dag: dict) -> None:
    """DeepEval's loader can call any deepeval.metrics attribute with the
    document's kwargs (a "metric" child) and lets a G-Eval child pick its own
    model. Neither is needed here, and both would let a request run code or a
    model other than the configured judge, so both are refused."""
    for node_id, node in (dag.get("nodes") or {}).items():
        if not isinstance(node, dict):
            raise ValueError(f"invalid DAG: node {node_id!r} is not an object")
        child = node.get("child")
        if isinstance(child, dict):
            if child.get("type") == "metric":
                raise ValueError("invalid DAG: 'metric' children are not allowed; use a G-Eval child or a node")
            if "model" in child:
                raise ValueError("invalid DAG: a child may not set its own model (the judge scores everything)")
        if "model" in node:
            raise ValueError("invalid DAG: a node may not set its own model (the judge scores everything)")


def _validate_dag(dag) -> None:
    from deepeval.metrics.dag import dag_from_dict

    check_dag_safety(dag)
    try:
        dag_from_dict(dag)
    except Exception as e:  # noqa: BLE001 - DeepEval raises ValueError, KeyError, TypeError
        raise ValueError(f"invalid DAG: {e}") from e


def clean(fields: dict) -> dict:
    """The stored shape of a metric; raises ValueError naming the bad field."""
    name = str(fields.get("name", "")).strip()
    if not name:
        raise ValueError("a custom metric needs a name")
    kind = fields.get("kind", "geval")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    params = list(fields.get("params") or ["input", "actual_output"])
    if not params or any(p not in PARAMS for p in params):
        raise ValueError(f"params must be from: {', '.join(PARAMS)}")
    try:
        threshold = float(fields.get("threshold", 0.7))
    except (TypeError, ValueError):
        raise ValueError("threshold must be a number between 0 and 1") from None
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be a number between 0 and 1")
    dataset = fields.get("dataset") or ("goldens_with_context" if {"context", "retrieval_context"} & set(params)
                                         else "goldens")
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of: {', '.join(DATASETS)}")
    criteria = str(fields.get("criteria", "")).strip()
    steps = [str(s).strip() for s in fields.get("evaluation_steps") or [] if str(s).strip()]
    dag = fields.get("dag")
    if kind == "geval" and not (criteria or steps):
        raise ValueError("a G-Eval metric needs criteria or evaluation steps")
    if kind == "dag":
        if not isinstance(dag, dict):
            raise ValueError("a DAG metric needs a DAG: {\"nodes\": {...}}")
        _validate_dag(dag)
    return {
        "name": name[:80],
        "kind": kind,
        "description": str(fields.get("description", "")).strip()[:300],
        "criteria": criteria,
        "evaluation_steps": steps,
        "params": params,
        "strict": bool(fields.get("strict", False)),
        "threshold": threshold,
        "dataset": dataset,
        "dag": dag if kind == "dag" else None,
    }


def _unique_key(name: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "metric"
    key = KEY_PREFIX + slug[:40]
    candidate, n = key, 2
    while candidate in taken:
        candidate, n = f"{key}_{n}", n + 1
    return candidate


def add_metric(fields: dict) -> dict:
    row = clean(fields)
    with _LOCK:
        rows = _read_all()
        row = {"id": f"cm_{uuid.uuid4().hex[:8]}",
               "key": _unique_key(row["name"], {r["key"] for r in rows}), **row}
        rows.append(row)
        _write_all(rows)
    return row


def update_metric(metric_id: str, fields: dict) -> dict | None:
    """Keeps the id and key, so the metric's run history stays attached."""
    clean_row = clean(fields)
    with _LOCK:
        rows = _read_all()
        for i, row in enumerate(rows):
            if row["id"] == metric_id:
                rows[i] = {"id": metric_id, "key": row["key"], **clean_row}
                _write_all(rows)
                return rows[i]
    return None


def delete_metric(metric_id: str) -> bool:
    with _LOCK:
        rows = _read_all()
        kept = [r for r in rows if r["id"] != metric_id]
        if len(kept) == len(rows):
            return False
        _write_all(kept)
    return True


def reset_to_defaults() -> int:
    """Puts the shipped examples back, keeping the user's other metrics."""
    with open(DEFAULT_CUSTOM_METRICS_PATH, "r", encoding="utf-8") as f:
        defaults = json.load(f)
    shipped = {row["id"] for row in defaults}
    with _LOCK:
        kept = [row for row in load_metrics() if row["id"] not in shipped]
        _write_all(defaults + kept)
    return len(defaults)
