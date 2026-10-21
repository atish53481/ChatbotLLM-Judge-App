"""Run-vs-run regression diff: two stored runs of one metric, case by case.

Cases are matched by golden id (rows carry it since the roadmap work), else by
the question text, so runs from before still compare. A case that passed in the
first run and fails in the second is a regression; the reverse is a fix.
"""
from __future__ import annotations

import json

from backend.dashboard.stats import summary_stats


def _case_key(row: dict) -> str:
    return f"id:{row['golden_id']}" if row.get("golden_id") else f"q:{row.get('question', '')}"


def _summary(run: dict, result: dict) -> dict:
    rows = result.get("rows") or []
    return {
        "id": run["id"],
        "ts": run["ts"],
        "metric_key": run["metric_key"],
        "score": run["score"],
        "passed": bool(run["passed"]),
        "judge_model": run.get("judge_model"),
        "stats": result.get("stats") or summary_stats([r.get("score") for r in rows],
                                                       [bool(r.get("passed")) for r in rows]),
    }


def _change(a: dict | None, b: dict | None) -> str:
    if a is None:
        return "added"
    if b is None:
        return "removed"
    if a.get("passed") and not b.get("passed"):
        return "regressed"
    if not a.get("passed") and b.get("passed"):
        return "fixed"
    return "same"


def compare_runs(run_a: dict, run_b: dict) -> dict:
    """run_a / run_b are storage rows (with result_json). Raises ValueError when
    they score different metrics or a run kept no cases."""
    if run_a["metric_key"] != run_b["metric_key"]:
        raise ValueError("pick two runs of the same metric")
    results = []
    for run in (run_a, run_b):
        raw = run.get("result_json")
        if not raw:
            raise ValueError(f"run {run['id']} kept no cases (it is older than case storage)")
        results.append(json.loads(raw))
    rows_a = {_case_key(r): r for r in results[0].get("rows") or []}
    rows_b = {_case_key(r): r for r in results[1].get("rows") or []}
    cases = []
    for key in list(rows_a) + [k for k in rows_b if k not in rows_a]:
        a, b = rows_a.get(key), rows_b.get(key)
        score_a = a.get("score") if a else None
        score_b = b.get("score") if b else None
        cases.append({
            "key": key,
            "question": (a or b).get("question", ""),
            "score_a": score_a,
            "score_b": score_b,
            "delta": round(score_b - score_a, 4) if score_a is not None and score_b is not None else None,
            "passed_a": a.get("passed") if a else None,
            "passed_b": b.get("passed") if b else None,
            "reason_b": b.get("reason", "") if b else "",
            "change": _change(a, b),
        })
    counts = {c: sum(1 for case in cases if case["change"] == c)
              for c in ("regressed", "fixed", "same", "added", "removed")}
    a_sum, b_sum = _summary(run_a, results[0]), _summary(run_b, results[1])
    delta = (round(b_sum["score"] - a_sum["score"], 4)
             if a_sum["score"] is not None and b_sum["score"] is not None else None)
    return {"a": a_sum, "b": b_sum, "delta": delta, "counts": counts,
            # Regressions first: they are what a reviewer looks for.
            "cases": sorted(cases, key=lambda c: ("regressed", "fixed", "added", "removed", "same").index(c["change"]))}
