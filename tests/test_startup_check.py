"""Start-up summary: DeepEval / DeepTeam versions and the red-team payloads (backend/startup_check.py)."""
from __future__ import annotations

from unittest.mock import patch

from backend import startup_check
from tests.test_app import app_module, client  # noqa: F401  (fixtures)


class _Probes:
    def __init__(self):
        self.reset = 0

    def reset_to_defaults(self):
        self.reset += 1
        return 3

    def load_probes(self):
        return [{"id": "sp_dt_a", "metric": "bfla"}, {"id": "sp_dt_b", "metric": "bfla"},
                {"id": "sp_dt_c", "metric": "ssrf"}, {"id": "sp_g1", "metric": "jailbreak"}]


def test_on_start_reloads_payloads_and_reports_versions(capsys):
    probes = _Probes()
    with patch.object(startup_check.deepteam_sync, "ensure_current") as ensure:
        status = startup_check.on_start(probes)
    ensure.assert_called_once()
    assert probes.reset == 1
    assert status["deepteam_payloads"] == 3 and status["deepteam_payload_metrics"] == 2
    assert status["payloads_total"] == 4 and status["deepeval"]
    out = capsys.readouterr().out
    assert "Frameworks loaded:" in out and "DeepEval" in out and "DeepTeam payloads for 2 vulnerabilities" in out


def test_on_start_never_fails_the_start():
    class Broken(_Probes):
        def reset_to_defaults(self):
            raise OSError("disk full")

    with patch.object(startup_check.deepteam_sync, "ensure_current", side_effect=RuntimeError("offline")):
        status = startup_check.on_start(Broken())
    assert status["deepteam_payloads"] == 3


def test_status_lists_the_frameworks(client):  # noqa: F811
    frameworks = client.get("/api/status").json()["frameworks"]
    assert frameworks["deepeval"] and "deepteam_catalog" in frameworks and "deepteam_payloads" in frameworks
