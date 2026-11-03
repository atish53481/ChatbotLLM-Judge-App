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
