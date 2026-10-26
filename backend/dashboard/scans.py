"""Scans: the run history of a chatbot as numbered versions.

Every Run started from the web app is one job (jobs.py) holding all the metrics
it ran. Seen as a scan, that job is one cycle with one overall result
(passed / failed / incomplete) plus its metric breakdown, numbered v1, v2, ...
per chatbot so each cycle can be named, opened and compared with another.
Pairwise (A/B) jobs compare two chatbots and are not scans of either.
"""
from __future__ import annotations

from backend import storage

# A metric result's status -> how it counts in the scan.
_PASSED, _FAILED = "pass", "fail"
# What a path found in the chatbot's replies (jobs.discovered) points at.
_FOUND_LABELS = {"tools_path": "tool calls", "context_path": "sources"}


def _numbered(conn, target_id: int) -> list[tuple[int, dict]]:
    jobs = [j for j in storage.target_jobs(conn, target_id) if (j["options"] or {}).get("kind") != "pairwise"]
    return list(enumerate(jobs, 1))


def _found(paths: dict) -> str:
    return " and ".join(f"{_FOUND_LABELS.get(field, field)} at {path!r}" for field, path in paths.items())


def _notes(numbered: list[tuple[int, dict]]) -> dict[int, str]:
    """job id -> a line on paths a scan found in the chatbot's replies, or on the
    follow-up scan it started for the metrics those paths switched on."""
    versions = {job["id"]: version for version, job in numbered}
    follow_ups = {}
    notes = {}
    for version, job in numbered:
        auto = (job["options"] or {}).get("auto_discovered")
        if auto:
            follow_ups[auto.get("from_job")] = version
            source = versions.get(auto.get("from_job"))
            notes[job["id"]] = (f"Started automatically: {f'scan {source}' if source else 'a scan'} found "
                                f"{_found(auto.get('paths') or {})} in the chatbot's replies, so these "
                                "metrics can run now.")
    for _, job in numbered:
        if job.get("discovered"):
            then = (f"; scan {follow_ups[job['id']]} runs the metrics this switched on"
                    if job["id"] in follow_ups else "")
            line = f"Found {_found(job['discovered'])} in the chatbot's replies and saved it on the chatbot{then}."
            notes[job["id"]] = f"{line} {notes[job['id']]}" if job["id"] in notes else line
    return notes


def _goodness(result: dict) -> float | None:
    """The score on a higher-is-better scale ("lower" metrics are violation rates)."""
    score = result.get("score")
    if score is None:
        return None
    return 1 - score if result.get("direction") == "lower" else score


def _judge_model(results: list[dict]) -> str:
    model = next((r["judge"]["model"] for r in results if (r.get("judge") or {}).get("model")), "")
    return model.replace(" (Local Model)", "")


def _metric_score(result: dict) -> dict:
    """One metric's score in a scan and its spread (population std) over the
    test cases it scored, for the runs table and its error bars."""
    scores = [r["score"] for r in result.get("rows") or [] if r.get("score") is not None]
    mean = sum(scores) / len(scores) if scores else None
    std = (sum((x - mean) ** 2 for x in scores) / len(scores)) ** 0.5 if scores else None
    return {"key": result["key"], "score": result.get("score"), "status": result.get("status"),
            "direction": result.get("direction", "higher"),
            "std": round(std, 3) if std is not None else None, "cases": len(scores)}


# Job options that say how a scan was run (its "hyperparameters" in the Overview).
_RUN_OPTIONS = ("limit", "metric_concurrency", "deepeval", "check_consistency", "enhancements", "max_latency_s")


def _summary(version: int, job: dict, note: str = "") -> dict:
    results = job["results"] or []
    passed = sum(r.get("status") == _PASSED for r in results)
    failed = sum(r.get("status") == _FAILED for r in results)
    errors = len(results) - passed - failed
    scores = [g for g in map(_goodness, results) if g is not None]
    # DeepEval's test run: a test case passes only when every metric that scored it passes.
    cases = list(_cases_of(results).values())
    cases_passed = sum(1 for c in cases if all(m["passed"] for m in c["metrics"]))
    if job["status"] in ("queued", "running"):
        result = job["status"]
    elif failed:
        result = "failed"
    elif errors or not passed or job["status"] != "done":
        result = "incomplete"
    else:
        result = "passed"
    return {
        "version": version,
        "job_id": job["id"],
        "status": job["status"],
        "result": result,
        "metrics": len(job["metric_keys"] or []),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "test_cases": len(cases),
        "test_cases_passed": cases_passed,
        # Metrics the job left out, each with why (not applicable, or no test case).
        "skipped": (job["options"] or {}).get("skipped") or [],
        "average": round(sum(scores) / len(scores), 3) if scores else None,
        "metric_scores": [_metric_score(r) for r in results],
        "judge_model": _judge_model(results),
        "judge_tokens": sum((r.get("judge") or {}).get("tokens") or 0 for r in results),
        "started_at": job.get("started_at") or job.get("created_at"),
        "finished_at": job.get("finished_at"),
        "note": note,
    }


def list_scans(conn, target_id: int) -> list[dict]:
    """One row per scan of this chatbot, newest first."""
    numbered = _numbered(conn, target_id)
    notes = _notes(numbered)
    return [_summary(version, job, notes.get(job["id"], "")) for version, job in reversed(numbered)]


def get_scan(conn, job_id: int) -> dict | None:
    """One scan with its overall result and every metric's result."""
    job = storage.get_job(conn, job_id)
    if job is None:
        return None
    numbered = _numbered(conn, job["target_id"])
    version = next((v for v, j in numbered if j["id"] == job_id), None)
    if version is None:
        return None
    options = job["options"] or {}
    return {**_summary(version, job, _notes(numbered).get(job_id, "")), "target_id": job["target_id"],
            "run_options": {k: options[k] for k in _RUN_OPTIONS if k in options},
            # Set once the run was sent to Confident AI: {id, link, sent_at} or {error}.
            "confident": options.get("confident"), "results": job["results"] or []}


def compare_scans(conn, job_id: int, other_id: int) -> dict | None:
    """Metric by metric, how scan job_id did against scan other_id."""
    scan, other = get_scan(conn, job_id), get_scan(conn, other_id)
    if scan is None or other is None:
        return None
    before = {r["key"]: r for r in other["results"]}
    now = {r["key"]: r for r in scan["results"]}
    rows = []
    for key in list(now) + [k for k in before if k not in now]:
        a, b = now.get(key), before.get(key)
        if b is None:
            change = "new"
        elif a is None:
            change = "removed"
        else:
            ga, gb = _goodness(a), _goodness(b)
            if ga is None or gb is None:
                change = "same" if a.get("status") == b.get("status") else "changed"
            else:
                change = "better" if ga > gb + 1e-9 else "worse" if ga < gb - 1e-9 else "same"
        rows.append({"key": key, "change": change,
                     "now": {k: (a or {}).get(k) for k in ("status", "score")} if a else None,
                     "before": {k: (b or {}).get(k) for k in ("status", "score")} if b else None,
                     "direction": (a or b).get("direction", "higher")})
    return {"scan": {k: v for k, v in scan.items() if k != "results"},
            "other": {k: v for k, v in other.items() if k != "results"}, "metrics": rows}


def _case_key(row: dict) -> str:
    """A test case's identity across scans: its golden, else what was sent."""
    if row.get("golden_id"):
        return f"id:{row['golden_id']}"
    return f"q:{row.get('input') or row.get('question') or ''}"


def _cases_of(results: list[dict]) -> dict[str, dict]:
    """Every test case of one scan with each metric's verdict on it, in the
    order the cases first appear. The chatbot answers once per metric, so the
    reply (and tool calls) shown are those of the first metric that sent it."""
    cases: dict[str, dict] = {}
    for result in results:
        for row in result.get("rows") or []:
            case = cases.setdefault(_case_key(row), {
                "input": row.get("input") or row.get("question") or "",
                "actual_output": row.get("actual_output") or "",
                "expected_output": row.get("expected_output") or "",
                "context": row.get("context") or [],
                "tools_called": row.get("tools_called") or [],
                "metrics": [],
            })
            if not case["tools_called"] and row.get("tools_called"):
                case["tools_called"] = row["tools_called"]
            case["metrics"].append({"key": result["key"], "score": row.get("score"),
                                    "passed": bool(row.get("passed")), "reason": row.get("reason") or "",
                                    "direction": result.get("direction", "higher"),
                                    "actual_output": row.get("actual_output") or ""})
    return cases


def _show_reply_of(case: dict | None, key: str | None) -> None:
    """Shows the reply the given metric scored (the one whose verdict changed)."""
    if case is None or key is None:
        return
    reply = next((m["actual_output"] for m in case["metrics"] if m["key"] == key and m["actual_output"]), None)
    if reply:
        case["actual_output"] = reply


def _changed_metric(now: dict | None, before: dict | None) -> str | None:
    if now is None or before is None:
        return None
    was = {m["key"]: m["passed"] for m in before["metrics"]}
    return next((m["key"] for m in now["metrics"] if m["key"] in was and was[m["key"]] != m["passed"]), None)


def _case_change(now: dict | None, before: dict | None) -> str:
    if before is None:
        return "new"
    if now is None:
        return "removed"
    was = {m["key"]: m["passed"] for m in before["metrics"]}
    flips = [(was[m["key"]], m["passed"]) for m in now["metrics"] if m["key"] in was]
    if any(old and not new for old, new in flips):
        return "regressed"
    if any(new and not old for old, new in flips):
        return "improved"
    return "same"


def compare_scan_cases(conn, job_id: int, other_id: int) -> dict | None:
    """Test case by test case, scan job_id against scan other_id: each case
    with every metric's verdict on both sides. A case regressed when any metric
    that passed it in the other scan fails it now; improved when one that
    failed it passes now (and none regressed)."""
    scan, other = get_scan(conn, job_id), get_scan(conn, other_id)
    if scan is None or other is None:
        return None
    now, before = _cases_of(scan["results"]), _cases_of(other["results"])
    cases = []
    for key in list(now) + [k for k in before if k not in now]:
        a, b = now.get(key), before.get(key)
        changed = _changed_metric(a, b)
        _show_reply_of(a, changed)
        _show_reply_of(b, changed)
        cases.append({"key": key, "input": (a or b)["input"], "change": _case_change(a, b),
                      "now": a, "before": b})
    counts = {c: sum(1 for case in cases if case["change"] == c)
              for c in ("regressed", "improved", "same", "new", "removed")}
    return {"scan": {k: v for k, v in scan.items() if k != "results"},
            "other": {k: v for k, v in other.items() if k != "results"},
            "counts": counts, "cases": cases}
