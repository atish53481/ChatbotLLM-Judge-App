"""Test-session defaults shared by every test module."""
import os
import shutil
from pathlib import Path

import pytest

# A local tool should not send DeepEval usage telemetry from test runs.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
# Tests must not append to the real logs/traffic-*.log.
os.environ["JUDGE_TRAFFIC_FILE"] = ""

FIXTURE_GOLDENS = Path(__file__).parent / "data" / "goldens.json"


@pytest.fixture(autouse=True)
def fixed_goldens(request, tmp_path, monkeypatch):
    """Point the golden store at a fixed copy, so editing the real
    backend/datasets/goldens.json from the side panel never breaks the suite.
    Smoke tests keep checking the real file."""
    if request.node.get_closest_marker("smoke"):
        return
    from backend.datasets import goldens

    copy = tmp_path / "fixture-goldens.json"
    shutil.copyfile(FIXTURE_GOLDENS, copy)
    monkeypatch.setattr(goldens, "GOLDENS_PATH", str(copy))


@pytest.fixture(autouse=True)
def serial_cases(monkeypatch):
    """Cases and a job's metrics run one at a time, so doubles that hand out
    scores in order stay deterministic; the parallel paths have their own tests
    (test_runner.py, test_jobs.py)."""
    from backend.dashboard import jobs, runner

    monkeypatch.setattr(runner, "JUDGE_CONCURRENCY", 1)
    monkeypatch.setattr(jobs, "METRIC_CONCURRENCY", 1)


@pytest.fixture(autouse=True)
def fresh_send_pacing():
    """send_delay pacing is shared per chatbot URL; start every test without it."""
    from backend.targets import http_client

    http_client._PACES.clear()


@pytest.fixture(autouse=True)
def fixed_conversations(tmp_path, monkeypatch):
    """Tests edit a throwaway copy of the conversation scenarios, never the user's."""
    from backend.datasets import conversations

    monkeypatch.setattr(conversations, "CONVERSATIONS_PATH", str(tmp_path / "conversations.json"))


FIXTURE_PROBES = Path(__file__).parent / "data" / "security_probes.json"


@pytest.fixture(autouse=True)
def fixed_security_probes(tmp_path, monkeypatch):
    """Tests edit a throwaway copy of the probes; defaults point at the fixture."""
    from backend.datasets import security_probes

    monkeypatch.setattr(security_probes, "PROBES_PATH", str(tmp_path / "security_probes.json"))
    monkeypatch.setattr(security_probes, "DEFAULT_PROBES_PATH", str(FIXTURE_PROBES))


@pytest.fixture(autouse=True)
def fixed_custom_metrics(tmp_path, monkeypatch):
    """Tests edit a throwaway copy of the user's own metrics, never theirs."""
    from backend.datasets import custom_metrics

    monkeypatch.setattr(custom_metrics, "CUSTOM_METRICS_PATH", str(tmp_path / "custom_metrics.json"))
