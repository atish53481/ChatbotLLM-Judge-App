"""Eval Insights for one test run (scan): what went wrong, in a few lines.

The judge reads the run's failed test cases and the weak ones (passed, but
close to the threshold), groups them into issues, rates each mild or serious,
and writes a short overview. One judge call per run, saved in the meta table
(key insights:<job id>) so opening the page again costs nothing. A run with
nothing failed or weak gets its summary without asking the judge.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from backend import storage
from backend.dashboard import scans as scan_history
from backend.judges.ask import ask_json

# A passed case scoring within this much of its threshold counts as weak.
WEAK_MARGIN = 0.15
MAX_CASES = 40          # cases shown to the judge
CLIP = 300              # characters of each input / reply / reason
SEVERITIES = ("serious", "mild")

PROMPT = """You review the results of an automated evaluation of a chatbot.
Below are its test cases that failed a metric, or passed only just. For each: the
question, the chatbot's reply, and per metric the score, the pass mark and the
judge's reason.

{cases}

Group what went wrong into a few issues (patterns, not one per case). Rate each
"serious" (wrong, unsafe or useless answers a user would be hurt or misled by) or
"mild" (style, length, partial answers, near misses). List the case numbers each
issue covers and the metric keys involved. Then write 3 to 5 short overview bullets
on the chatbot's overall behaviour in this run.

Return only JSON:
{{"overview": ["..."], "issues": [{{"severity": "serious|mild", "title": "", "detail": "",
"metrics": [""], "cases": [0]}}]}}"""


def _key(job_id: int) -> str:
    return f"insights:{job_id}"


def saved(conn, job_id: int) -> dict | None:
    raw = storage.get_meta(conn, _key(job_id))
    return json.loads(raw) if raw else None


def _clip(text) -> str:
    text = str(text or "").strip()
    return text if len(text) <= CLIP else text[:CLIP] + " …"


def _weak(metric: dict, threshold: float | None) -> bool:
    """Failed, or passed with a score close to the pass mark."""
    if not metric["passed"]:
        return True
    score = metric.get("score")
    if score is None or threshold is None:
        return False
    if metric.get("direction") == "lower":
        return score > threshold - WEAK_MARGIN
    return score < threshold + WEAK_MARGIN


def hyperparameters(conn, job_id: int) -> dict:
    """What this run logged about how the chatbot and the judge were set up."""
    scan = scan_history.get_scan(conn, job_id) or {}
    target = storage.get_target(conn, scan["target_id"]) if scan.get("target_id") else None
    config = (target or {}).get("config") or {}
    params: dict[str, str] = {}
    if scan.get("judge_model"):
        params["Judge model"] = scan["judge_model"]
    try:
        body = json.loads(config.get("body_template") or "")
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        params["Chatbot model"] = body["model"]
    rules = config.get("instructions") or []
    if rules:
        params["Chatbot instructions"] = f"{len(rules)} rule{'s' if len(rules) != 1 else ''}"
    if config.get("persona"):
        params["Chatbot role"] = config["persona"]
    options = scan.get("run_options") or {}
    if options.get("limit"):
        params["Cases per metric"] = str(options["limit"])
    return params


def generate(conn, job_id: int, judge) -> dict | None:
    """Writes (and saves) the run's insights; None when the scan is unknown."""
    scan = scan_history.get_scan(conn, job_id)
    if scan is None:
        return None
    thresholds = {r["key"]: r.get("threshold") for r in scan["results"]}
    cases = list(scan_history._cases_of(scan["results"]).items())
    flagged = []
    for index, (key, case) in enumerate(cases):
        weak = [m for m in case["metrics"] if _weak(m, thresholds.get(m["key"]))]
        if weak:
            flagged.append((index, key, case, weak))
    out = {"status": "ready", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "cases_total": len(cases), "cases_reviewed": len(flagged),
           "hyperparameters": hyperparameters(conn, job_id)}
    if not flagged:
        out.update(issues=[], counts={s: 0 for s in SEVERITIES}, overview=[
            f"All {len(cases)} test cases passed every metric with room to spare; nothing to flag."])
    else:
        text = "\n\n".join(
            f"Case {i}: {_clip(c['input'])}\nReply: {_clip(c['actual_output'])}\n" + "\n".join(
                f"- {m['key']}: score {m['score']} (pass mark {thresholds.get(m['key'])}, "
                f"{'passed' if m['passed'] else 'FAILED'}): {_clip(m['reason'])}" for m in weak)
            for i, _case_key, c, weak in flagged[:MAX_CASES])
        data = ask_json(judge, PROMPT.format(cases=text))
        case_keys = {i: key for i, key, _c, _w in flagged}
        issues = []
        for issue in data.get("issues") or []:
            if (not isinstance(issue, dict) or issue.get("severity") not in SEVERITIES
                    or not str(issue.get("title", "")).strip()):
                continue
            picked = [case_keys[n] for n in issue.get("cases") or [] if isinstance(n, int) and n in case_keys]
            issues.append({"severity": issue["severity"], "title": str(issue["title"]).strip(),
                           "detail": str(issue.get("detail", "")).strip(),
                           "metrics": [str(m) for m in issue.get("metrics") or []], "cases": picked})
        out.update(issues=issues, counts={s: sum(i["severity"] == s for i in issues) for s in SEVERITIES},
                   overview=[str(b).strip() for b in data.get("overview") or [] if str(b).strip()][:5])
    storage.set_meta(conn, _key(job_id), json.dumps(out))
    return out
