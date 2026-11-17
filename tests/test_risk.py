"""Risk Assessment (backend/dashboard/risk.py): one scan's red-team results as a
security report: KPIs, risk score, overview bars, vulnerabilities, OWASP, cases."""
from backend.dashboard import risk
from tests.test_app import _create_target, app_module, client  # noqa: F401 - fixtures
from tests.test_scans import _scan


def _row(gid, passed, enhancement=None):
    return {"golden_id": gid, "input": f"attack {gid}", "actual_output": "reply", "score": 1.0 if passed else 0.0,
            "passed": passed, "reason": "why", "probe": True, "enhancement": enhancement}


def _res(key, rows, status=None):
    status = status or ("pass" if all(r["passed"] for r in rows) else "fail")
    return {"key": key, "status": status, "score": 0.5, "direction": "higher", "rows": rows}


SCAN = {
    "job_id": 7, "version": 3, "target_id": 1, "status": "done",
    "results": [
        _res("prompt_injection", [_row("sp_pi_1", True), _row("sp_pi_2", False, "base64"),
                                  _row("sp_pi_3", True, "base64")]),
        _res("pii_leakage", [_row("g1", True), _row("g2", True)]),
        _res("faithfulness", [_row("g3", False)]),  # not red-team: left out
    ],
}


def test_assessment_counts_scores_and_overview():
    out = risk.assessment(SCAN, {"prompt_injection": "Prompt Injection"},
                          {"prompt_injection": "security"}, {"sp_pi_2": "fake system turn · OWASP LLM01"})
    s = out["summary"]
    assert (s["total"], s["passed"], s["failed"]) == (5, 4, 1)
    assert s["pass_rate"] == 0.8 and s["vulnerabilities"] == 2 and s["attack_vectors"] == 2
    # one high-severity (prompt_injection) failure: 7.5 over 5 cases
    assert s["risk_score"] == 1.5 and s["critical_high"] == 1
    assert s["most_common"]["title"] == "Prompt Injection"
    assert s["most_successful_attack"] == {"label": risk.attack_enhancers.ENHANCERS["base64"][0], "rate": 0.5}
    failed = next(c for c in out["cases"] if not c["passed"])
    assert failed["type"] == "fake system turn" and failed["priority"] == "P1" and failed["severity"] == "high"
    assert {c["vulnerability_key"] for c in out["cases"]} == {"prompt_injection", "pii_leakage"}
    assert [v["key"] for v in out["vulnerabilities"]] == ["prompt_injection", "pii_leakage"]
    assert out["vulnerabilities"][0]["outcomes"] == [True, False, True]
    assert sum(b["count"] for b in out["overview"]["score_bands"]) == 5
    assert out["overview"]["impact"][1] == {"label": "High", "count": 1, "of": 1, "rate": 1.0}
    assert [f["id"] for f in out["frameworks"]] == ["LLM01", "LLM02"]
    assert len(out["critical_findings"]) == 1


def test_each_vulnerability_explains_its_impact():
    out = risk.assessment(SCAN, {}, {"prompt_injection": "security"})
    by_key = {v["key"]: v for v in out["vulnerabilities"]}
    pi, pii = by_key["prompt_injection"]["impact"], by_key["pii_leakage"]["impact"]
    assert pi["confirmed"] and pi["status"].startswith("Confirmed: 1 of 3")
    assert "overrides the bot's instructions" in pi["if_exploited"] and pi["business"]
    assert not pii["confirmed"] and pii["status"].startswith("Not shown: all 2")
    assert "GDPR" in pii["business"]
    assert out["mitigations"][0]["impact"] == pi
    # A key without its own text falls back to its DeepTeam category's.
    assert risk.impact.of("custom_x", "Custom")["if_exploited"] == risk.impact.CATEGORY_IMPACT["Custom"][0]


def test_deepteam_probe_type_is_its_subtype():
    row = {"golden_id": "sp_dt_bola_1"}
    notes = {"sp_dt_bola_1": "DeepTeam 1.0.10 · BOLA / object_access_bypass (template example)"}
    assert risk._type_of(row, notes) == "object access bypass"


def test_assessment_of_scan_without_red_team_is_empty():
    out = risk.assessment({"results": [_res("faithfulness", [_row("g", True)])]}, {}, {})
    assert out["summary"]["total"] == 0 and out["summary"]["risk_score"] is None
    assert out["summary"]["most_common"] is None and out["cases"] == []


def test_risk_endpoint(app_module, client):
    bot = _create_target(client, name="Risk Bot", config={"url": "http://127.0.0.1:9/risk"})
    assert client.get("/api/scans/999999/risk").status_code == 404
    job = _scan(app_module._conn, bot["id"], SCAN["results"])
    body = client.get(f"/api/scans/{job}/risk").json()
    assert body["job_id"] == job and body["summary"]["total"] == 5
    assert body["framework"].startswith("OWASP")


def test_assessment_adds_cvss_categories_matrix_attacks_and_mitigations():
    out = risk.assessment(SCAN, {"prompt_injection": "Prompt Injection"}, {"prompt_injection": "security"},
                          {}, {"prompt_injection": {"chatbot": ["Own advice first."]}})
    s = out["summary"]
    assert s["cvss"] == 1.5 and s["cvss_rating"] == "Low"
    pi = next(v for v in out["vulnerabilities"] if v["key"] == "prompt_injection")
    assert pi["deepteam_category"] == "Security" and pi["cvss"] == 2.5 and pi["cvss_rating"] == "Low"
    assert {c["name"] for c in out["categories"]} == {"Security", "Data Privacy"}
    m = out["matrix"]
    assert [a["key"] for a in m["attacks"]] == ["base64", "baseline"]
    assert m["rows"][0]["key"] == "prompt_injection" and m["rows"][0]["cells"][0] == {"total": 2, "failed": 1}
    base64 = next(a for a in out["attack_catalog"] if a["key"] == "base64")
    assert base64["status"] == "broke_through" and base64["recommended"]
    assert any(a["turns"] == "multi" and a["status"] == "not_run" for a in out["attack_catalog"])
    assert s["attacks_not_run"] == s["attacks_available"] - 1
    mit = out["mitigations"]
    assert [x["key"] for x in mit] == ["prompt_injection"] and mit[0]["priority"] == "P1"
    assert mit[0]["steps"][0] == "Own advice first." and len(mit[0]["steps"]) > 1
    assert mit[0]["example"]["attack"] == risk.attack_enhancers.ENHANCERS["base64"][0]
    assert out["gaps"] and out["gaps"][0]["kind"] == "attacks"
