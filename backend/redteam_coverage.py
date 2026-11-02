"""Red-team coverage: which risks the chatbot was tested for, and how it did.

Every adversarial metric maps to the OWASP Top 10 for LLM Applications (2025)
and to one of five risk categories. The report reads each metric's latest run,
so a category with no runs shows as untested rather than as safe.
"""
from __future__ import annotations

import json

from backend.datasets import deepteam_sync

OWASP_2025 = {
    "LLM01": "Prompt Injection",
    "LLM02": "Sensitive Information Disclosure",
    "LLM03": "Supply Chain",
    "LLM04": "Data and Model Poisoning",
    "LLM05": "Improper Output Handling",
    "LLM06": "Excessive Agency",
    "LLM07": "System Prompt Leakage",
    "LLM08": "Vector and Embedding Weaknesses",
    "LLM09": "Misinformation",
    "LLM10": "Unbounded Consumption",
}

RISK_CATEGORIES = {
    "responsible_ai": "Responsible AI",
    "illegal_activities": "Illegal Activities",
    "brand_image": "Brand Image",
    "data_privacy": "Data Privacy",
    "unauthorized_access": "Unauthorized Access",
    "agentic": "Agentic",
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


# --- DeepTeam metrics and the three OWASP lists --------------------------------
_CATALOG = deepteam_sync.load_catalog()
_LLM_ITEMS = {i["id"]: i for i in _CATALOG.get("frameworks", {}).get("owasp_llm", [])}
_ASI_ITEMS = _CATALOG.get("frameworks", {}).get("owasp_agentic", [])
_DT_KEYS = {v["class"]: v["key"] for v in _CATALOG.get("vulnerabilities", [])}

# Every DeepTeam metric: its risk category and the LLM Top 10 ids DeepTeam maps it to.
for _v in deepteam_sync.new_vulnerabilities(_CATALOG):
    COVERAGE.setdefault(_v["key"], (
        deepteam_sync.RISK_OF_CATEGORY.get(_v.get("category", ""), "unauthorized_access"),
        sorted(i for i, item in _LLM_ITEMS.items() if _v["class"] in item["vulnerabilities"])))

OWASP_AGENTIC_2026 = {i["id"]: i["title"] for i in _ASI_ITEMS}
# OWASP MCP Top 10 (2025, github.com/OWASP/www-project-mcp-top-10). DeepTeam has
# no MCP map, so this one is the app's: the metrics whose attacks reach each risk
# through the chat. MCP04, MCP08 and MCP09 can only be checked in part from the
# outside (PARTLY_TESTABLE).
OWASP_MCP_2025 = {
    "MCP01": ("Token Mismanagement and Secret Exposure", ["no_prompt_leak", "data_exfiltration", "system_reconnaissance"]),
    "MCP02": ("Privilege Escalation via Scope Creep", ["excessive_agency", "rbac", "bfla"]),
    "MCP03": ("Tool Poisoning", ["tool_metadata_poisoning", "indirect_instruction", "prompt_injection"]),
    "MCP04": ("Software Supply Chain Attacks and Dependency Tampering", ["tool_metadata_poisoning", "agent_identity_abuse"]),
    "MCP05": ("Command Injection and Execution", ["shell_injection", "sql_injection", "unexpected_code_execution", "ssrf"]),
    "MCP06": ("Intent Flow Subversion", ["goal_theft", "recursive_hijacking", "indirect_instruction", "prompt_injection"]),
    "MCP07": ("Insufficient Authentication and Authorization", ["bola", "bfla", "rbac", "agent_identity_abuse"]),
    "MCP08": ("Lack of Audit and Telemetry", ["tool_orchestration_abuse", "autonomous_agent_drift", "debug_access"]),
    "MCP09": ("Shadow MCP Servers", ["external_system_abuse", "insecure_inter_agent_communication", "tool_metadata_poisoning"]),
    "MCP10": ("Context Injection and Over-Sharing", ["cross_context_retrieval", "data_exfiltration", "indirect_instruction"]),
}
FRAMEWORK_LABELS = {
    "owasp_llm": "OWASP Top 10 for LLM Applications 2025",
    "owasp_agentic": "OWASP Top 10 for Agentic Applications 2026",
    "owasp_mcp": "OWASP MCP Top 10 2025",
}

# OWASP ids that chatting with the bot can check only in part: the risk lives
# mostly outside the conversation (dependencies, training data, servers, logs),
# so the metrics mapped to them test only what reaches the replies. Even every
# attack resisted is not full coverage. id -> why, in one line.
PARTLY_TESTABLE = {
    "LLM03": "the risk sits in the models, packages and plugins the bot is built on; "
             "the chat shows only what reaches its replies",
    "LLM04": "poisoned training, fine-tuning or document data is found by auditing that data; "
             "the chat shows only its effects on replies",
    "LLM10": "cost, rate limits and resource use show on the servers, not in the replies; "
             "the chat only probes how the bot handles runaway requests",
    "ASI04": "the agent's third-party tools, plugins and models are checked where they come from; "
             "the chat shows only what a compromised one makes the agent do",
    "ASI08": "failures that spread across agents and systems happen behind the chat; "
             "one conversation sees only the first agent's replies",
    "MCP04": "tampered MCP servers and dependencies are found by auditing what is installed; "
             "the chat shows only what a poisoned tool makes the bot do",
    "MCP08": "audit logs and telemetry live on the server; the chat can probe debug access and "
             "unchecked tool use, not whether actions are logged",
    "MCP09": "unapproved MCP servers are found in the deployment's inventory; the chat shows only "
             "whether the bot can be steered to outside systems",
}


def _keys(classes) -> list[str]:
    return [_DT_KEYS[c] for c in classes if c in _DT_KEYS]


def _partial(rid: str) -> dict:
    """{"partial": True, "partial_reason": why} for an id in PARTLY_TESTABLE, else {"partial": False}."""
    reason = PARTLY_TESTABLE.get(rid)
    return {"partial": True, "partial_reason": reason} if reason else {"partial": False}


def frameworks() -> dict:
    """{framework: {"label", "items": [{"id", "title", "metrics": [keys], "partial",
    "partial_reason"?}]}}: which metrics test each OWASP risk. LLM and Agentic ids
    come from DeepTeam's maps (plus the app's own metrics mapped in COVERAGE); MCP
    ids from OWASP_MCP_2025. partial: the chat can test the risk only in part."""
    known = set(COVERAGE)
    llm = []
    for rid, title in OWASP_2025.items():
        item = _LLM_ITEMS.get(rid, {})
        keys = _keys(item.get("vulnerabilities", [])) + [k for k, (_, ids) in COVERAGE.items() if rid in ids]
        llm.append({"id": rid, "title": title, "metrics": sorted(set(keys) & known), **_partial(rid)})
    asi = [{"id": i["id"], "title": i["title"], "metrics": sorted(set(_keys(i["vulnerabilities"])) & known),
            **_partial(i["id"])}
           for i in _ASI_ITEMS]
    mcp = [{"id": rid, "title": title, "metrics": sorted(set(keys) & known), **_partial(rid)}
           for rid, (title, keys) in OWASP_MCP_2025.items()]
    return {key: {"label": FRAMEWORK_LABELS[key], "items": items}
            for key, items in (("owasp_llm", llm), ("owasp_agentic", asi), ("owasp_mcp", mcp))}


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
    by_metric = {m["key"]: m for cat in categories for m in cat["metrics"]}
    framework_view = []
    for key, fw in frameworks().items():
        items = []
        for item in fw["items"]:
            entries = [by_metric[k] for k in item["metrics"] if k in by_metric]
            tested = [m for m in entries if m["tested"]]
            n = sum(m["attacks"] for m in tested)
            ok = sum(m["resisted"] for m in tested)
            items.append({**item, "tested": len(tested), "total": len(entries), "attacks": n, "resisted": ok,
                          "pass_rate": round(ok / n, 4) if n else None})
        framework_view.append({"key": key, "label": fw["label"], "items": items})
    return {"categories": categories, "owasp": OWASP_2025, "owasp_agentic": OWASP_AGENTIC_2026,
            "owasp_mcp": {k: t for k, (t, _) in OWASP_MCP_2025.items()}, "frameworks": framework_view}
