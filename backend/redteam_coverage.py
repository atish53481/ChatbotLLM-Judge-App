"""Red-team coverage: which risks the chatbot was tested for, and how it did.

Every adversarial metric maps to the OWASP Top 10 for LLM Applications (2025)
and to one of five risk categories. The report reads each metric's latest run,
so a category with no runs shows as untested rather than as safe.
"""
from __future__ import annotations

import json

OWASP_2025 = {
    "LLM01": "Prompt Injection",
    "LLM02": "Sensitive Information Disclosure",
    "LLM05": "Improper Output Handling",
    "LLM06": "Excessive Agency",
    "LLM07": "System Prompt Leakage",
    "LLM09": "Misinformation",
}

RISK_CATEGORIES = {
    "responsible_ai": "Responsible AI",
    "illegal_activities": "Illegal Activities",
    "brand_image": "Brand Image",
    "data_privacy": "Data Privacy",
    "unauthorized_access": "Unauthorized Access",
}

# metric key -> (risk category, OWASP ids)
COVERAGE = {
    "bias": ("responsible_ai", []),
    "toxicity": ("responsible_ai", []),
    "harmful_content": ("illegal_activities", ["LLM05"]),
    "jailbreak": ("illegal_activities", ["LLM01"]),
    "multi_turn_jailbreak": ("illegal_activities", ["LLM01"]),
    "domain_misuse": ("brand_image", ["LLM06"]),
    "non_advice": ("brand_image", ["LLM09"]),
    "role_violation": ("brand_image", ["LLM01"]),
    "hallucination": ("brand_image", ["LLM09"]),
    "pii_leakage": ("data_privacy", ["LLM02"]),
    "data_exfiltration": ("data_privacy", ["LLM02"]),
    "no_prompt_leak": ("data_privacy", ["LLM07"]),
    "prompt_injection": ("unauthorized_access", ["LLM01"]),
    "encoded_injection": ("unauthorized_access", ["LLM01"]),
    "social_engineering": ("unauthorized_access", ["LLM01", "LLM06"]),
}


def coverage_report(latest_runs: list[dict], titles: dict[str, str]) -> dict:
    """latest_runs: storage.latest_runs rows (with result_json). Returns the
    categories with their metrics, attack counts and pass rates, plus the OWASP ids."""
    by_key = {r["metric_key"]: r for r in latest_runs}
    categories = []
    for cat_key, label in RISK_CATEGORIES.items():
        metrics, attacks, resisted = [], 0, 0
        for key, (cat, owasp) in COVERAGE.items():
            if cat != cat_key:
                continue
            run = by_key.get(key)
            entry = {"key": key, "title": titles.get(key, key), "owasp": owasp, "tested": run is not None}
            if run is not None:
                result = json.loads(run["result_json"]) if run.get("result_json") else {}
                rows = result.get("rows") or []
                passed = sum(1 for row in rows if row.get("passed"))
                entry.update({"score": run["score"], "passed": bool(run["passed"]), "ts": run["ts"],
                              "attacks": len(rows), "resisted": passed,
                              "by_enhancement": result.get("by_enhancement")})
                attacks += len(rows)
                resisted += passed
            metrics.append(entry)
        tested = [m for m in metrics if m["tested"]]
        categories.append({
            "key": cat_key, "label": label, "metrics": metrics,
            "tested": len(tested), "total": len(metrics),
            "attacks": attacks, "resisted": resisted,
            "pass_rate": round(resisted / attacks, 4) if attacks else None,
        })
    return {"categories": categories, "owasp": OWASP_2025}
