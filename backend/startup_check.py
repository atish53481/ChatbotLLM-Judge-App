"""What the backend loaded at start: DeepEval / DeepTeam versions and the red-team payloads.

update-packages.bat (run by run-backend.bat before uvicorn) upgrades DeepEval
and DeepTeam to their newest allowed release. At import, deepteam_sync rebuilds
its catalog when the installed DeepTeam is newer. on_start() then makes sure the
catalog matches the installed DeepTeam and reloads the shipped probe store, so
DeepTeam's template payloads (one set per vulnerability type) are the ones of
the installed version from the first request on, not only after the side panel
loads. It prints a one-screen summary in the backend window and keeps it for
GET /api/status ("frameworks").
"""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from backend.datasets import deepteam_sync

log = logging.getLogger("llm_judge.startup")


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def framework_status(probes_store) -> dict:
    """Versions and payload counts, read now (cheap: the catalog is cached)."""
    catalog = deepteam_sync.load_catalog()
    try:
        probes = probes_store.load_probes()
    except Exception:  # noqa: BLE001 - a broken probe file must not break /api/status
        probes = []
    deepteam_probes = [p for p in probes if str(p.get("id", "")).startswith("sp_dt_")]
    return {
        "deepeval": _version("deepeval"),
        "deepteam": _version("deepteam"),
        "deepteam_catalog": catalog.get("deepteam_version"),
        "deepteam_vulnerabilities": len(catalog.get("vulnerabilities", [])),
        "deepteam_attacks": len(catalog.get("single_turn_attacks", [])) + len(catalog.get("multi_turn_attacks", [])),
        "deepteam_payloads": len(deepteam_probes),
        "deepteam_payload_metrics": len({p.get("metric") for p in deepteam_probes}),
        "payloads_total": len(probes),
    }


def on_start(probes_store) -> dict:
    """Catalog in step with the installed DeepTeam, payloads reloaded, summary printed."""
    try:
        deepteam_sync.ensure_current()
    except Exception as e:  # noqa: BLE001 - the saved catalog keeps working
        log.warning("DeepTeam catalog check failed, keeping the saved one: %s", e)
    try:
        probes_store.reset_to_defaults()
    except Exception as e:  # noqa: BLE001 - the current probe file keeps working
        log.warning("Reloading the red-team payloads failed: %s", e)
    status = framework_status(probes_store)
    stale = status["deepteam"] and status["deepteam_catalog"] != status["deepteam"]
    lines = [
        "Frameworks loaded:",
        f"  DeepEval {status['deepeval'] or 'NOT INSTALLED'}",
        f"  DeepTeam {status['deepteam'] or 'NOT INSTALLED'} (catalog {status['deepteam_catalog']}"
        f"{', OUT OF DATE' if stale else ''}): {status['deepteam_vulnerabilities']} vulnerabilities, "
        f"{status['deepteam_attacks']} attacks",
        f"  Red-team payloads: {status['payloads_total']} shipped probes, of which "
        f"{status['deepteam_payloads']} DeepTeam payloads for {status['deepteam_payload_metrics']} vulnerabilities",
    ]
    print("\n".join(lines), flush=True)
    log.info(" | ".join(lines))
    return status
