"""Daily refresh of the "latest" probe set: the judge writes 10 probes per
probe metric from published standards (OWASP Top 10 for LLM / Agentic / MCP,
DeepTeam's red-teaming docs). Offline: the fetch and the judge are fakes."""
import ast
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import metrics_catalog, storage
from backend.datasets import deepteam_sync, probe_refresh
from backend.datasets import security_probes
from tests.test_app import app_module  # noqa: F401  (fixture)

# The app's own probe metrics: refreshed every day whatever was run.
APP_METRICS = security_probes.SECURITY_METRICS + security_probes.SAFETY_PROBE_METRICS


@pytest.fixture
def latest_path(tmp_path, monkeypatch):
    path = tmp_path / "security_probes.latest.json"
    monkeypatch.setattr(probe_refresh, "LATEST_PATH", str(path))
    return path


class Judge:
    """Answers every prompt with `count` probes; fails for metrics in `broken`."""

    def __init__(self, count=12, broken=(), prefix=""):
        self.count, self.broken, self.prompts, self.prefix = count, set(broken), [], prefix

    def generate(self, prompt):
        self.prompts.append(prompt)
        metric = prompt.split("METRIC: ", 1)[1].split("\n", 1)[0]
        if metric in self.broken:
            return "not json", 0.0
        source = prompt.split("SOURCE ", 1)[1].split(" ", 1)[0]
        probes = [{"question": f"{self.prefix}{metric} attack {i}", "source": source} for i in range(self.count)]
        probes += [{"question": "  ", "source": source},
                   {"question": f"{self.prefix}{metric} attack 0", "source": source}]
        return json.dumps({"probes": probes}), 0.0


def _fetch(url):
    return f"Standard text from {url}. " * 20


def test_every_probe_metric_has_standards_to_learn_from():
    assert set(probe_refresh.SOURCES) == set(security_probes.PROBE_METRICS)
    for metric, entry in probe_refresh.SOURCES.items():
        assert entry["goal"] and entry["sources"], metric
        for source in entry["sources"]:
            # A published page, or the installed DeepTeam's own text (read from its catalog).
            assert source["url"].startswith("https://raw.githubusercontent.com/") or source.get("text"), source


def test_refresh_writes_ten_tagged_probes_per_metric(latest_path):
    judge = Judge()
    result = probe_refresh.refresh(judge, fetch=_fetch)
    saved = json.loads(latest_path.read_text(encoding="utf-8"))
    assert result["errors"] == [] and saved["refreshed_at"]
    for metric in security_probes.PROBE_METRICS:
        rows = [p for p in saved["probes"] if p["metric"] == metric]
        assert len(rows) == probe_refresh.PER_METRIC, metric
        assert len({p["question"] for p in rows}) == len(rows)  # blanks and repeats dropped
        assert all(p["set"] == "latest" for p in rows)
        source_ids = {s["id"] for s in probe_refresh.SOURCES[metric]["sources"]}
        assert all(p["note"].split(" · ")[0] in source_ids for p in rows)
    # Each prompt carries the standard's own text.
    assert "Standard text from https://raw.githubusercontent.com/" in judge.prompts[0]


def test_a_failed_metric_keeps_its_previous_probes(latest_path):
    probe_refresh.refresh(Judge(), fetch=_fetch)
    result = probe_refresh.refresh(Judge(broken={"bias"}), fetch=_fetch)
    assert any("bias" in e for e in result["errors"])
    saved = json.loads(latest_path.read_text(encoding="utf-8"))
    assert len([p for p in saved["probes"] if p["metric"] == "bias"]) == probe_refresh.PER_METRIC


def test_unreachable_standards_are_reported_not_fatal(latest_path):
    def down(url):
        raise ValueError("offline")

    result = probe_refresh.refresh(Judge(), fetch=down)
    probes = json.loads(latest_path.read_text(encoding="utf-8"))["probes"]
    # Offline, only metrics with a standard that needs no fetch (DeepTeam's catalog text) get probes.
    offline = {m for m, e in probe_refresh.SOURCES.items() if any(s.get("text") for s in e["sources"])}
    assert result["errors"] and {p["metric"] for p in probes} == offline


def test_a_refresh_that_wrote_nothing_is_retried_on_the_next_start(latest_path):
    probe_refresh.refresh(Judge(broken=set(security_probes.PROBE_METRICS)), fetch=_fetch)
    assert probe_refresh.is_due()  # e.g. a bad judge key: try again next start, not in 24 h


def test_refresh_is_due_once_a_day(latest_path):
    assert probe_refresh.is_due()
    probe_refresh.refresh(Judge(), fetch=_fetch)
    assert not probe_refresh.is_due()
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    data = json.loads(latest_path.read_text(encoding="utf-8"))
    latest_path.write_text(json.dumps({**data, "refreshed_at": old}), encoding="utf-8")
    assert probe_refresh.is_due()


def test_start_on_boot_can_be_switched_off(latest_path, monkeypatch):
    monkeypatch.setenv("JUDGE_PROBE_REFRESH", "0")
    assert probe_refresh.start_on_boot(lambda: Judge()) is None


def test_latest_set_is_read_like_the_shipped_sets(latest_path):
    probe_refresh.refresh(Judge(), fetch=_fetch)
    rows = security_probes.load_probes(metric="bias", probe_set="latest")
    assert len(rows) == probe_refresh.PER_METRIC and rows[0]["question"] == "bias attack 0"
    spec = metrics_catalog.get_spec("prompt_injection")
    assert [c["question"] for c in spec.cases(probe_set="latest")][:1] == ["prompt_injection attack 0"]


def test_latest_set_falls_back_to_generic_before_any_refresh(latest_path):
    generic = security_probes.load_probes(metric="bias", probe_set="generic")
    assert security_probes.load_probes(metric="bias", probe_set="latest") == generic


# --- What a refresh costs: the app's 13 probe metrics, plus DeepTeam ones in use ---

def test_recent_metric_keys_lists_what_ran_in_the_last_days(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def stamp(days_ago):  # the runner's own format (runner._now_iso)
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    storage.record_run(conn, 1, "ssrf", 1.0, True, stamp(1))
    storage.record_run(conn, 2, "bfla", 0.4, False, stamp(29))  # any chatbot counts
    storage.record_run(conn, 1, "bola", 1.0, True, stamp(31))
    assert storage.recent_metric_keys(conn, 30, now=now) == {"ssrf", "bfla"}
    assert storage.recent_metric_keys(conn, 0, now=now) == set()


def test_only_the_app_metrics_and_deepteam_ones_in_use_are_refreshed(monkeypatch):
    monkeypatch.delenv("JUDGE_PROBE_REFRESH_ALL", raising=False)
    asked = []

    def recent(days):
        asked.append(days)
        return {"ssrf", "bfla", "faithfulness", "jailbreak"}

    assert len(APP_METRICS) == 13 and len(security_probes.PROBE_METRICS) == 45
    picked = probe_refresh.metrics_to_refresh(recent)
    assert asked == [probe_refresh.USED_DAYS] == [30]
    assert picked[:13] == APP_METRICS and sorted(picked[13:]) == ["bfla", "ssrf"]
    # A fresh install (no runs yet): 13 judge calls a day instead of 45.
    assert probe_refresh.metrics_to_refresh(lambda days: set()) == APP_METRICS
    # A caller that passes nothing, and JUDGE_PROBE_REFRESH_ALL=1: every probe metric, as before.
    assert probe_refresh.metrics_to_refresh() == security_probes.PROBE_METRICS
    monkeypatch.setenv("JUDGE_PROBE_REFRESH_ALL", "1")
    assert probe_refresh.metrics_to_refresh(lambda days: set()) == security_probes.PROBE_METRICS


def test_runs_that_cannot_be_read_mean_the_app_metrics_only(monkeypatch):
    monkeypatch.delenv("JUDGE_PROBE_REFRESH_ALL", raising=False)

    def locked(days):
        raise sqlite3.OperationalError("database is locked")

    assert probe_refresh.metrics_to_refresh(locked) == APP_METRICS


def test_metrics_left_out_keep_their_previous_probes(latest_path):
    probe_refresh.refresh(Judge(), fetch=_fetch)
    judge = Judge(prefix="new ")
    result = probe_refresh.refresh(judge, fetch=_fetch, metrics=("bias", "ssrf"))
    assert result["errors"] == [] and len(judge.prompts) == 2  # one judge call per refreshed metric
    by_metric = {}
    for p in json.loads(latest_path.read_text(encoding="utf-8"))["probes"]:
        by_metric.setdefault(p["metric"], []).append(p["question"])
    assert set(by_metric) == set(security_probes.PROBE_METRICS)
    assert by_metric["bias"][0] == "new bias attack 0" and by_metric["ssrf"][0] == "new ssrf attack 0"
    assert by_metric["bfla"][0] == "bfla attack 0" and len(by_metric["bfla"]) == probe_refresh.PER_METRIC


def test_a_metric_never_refreshed_falls_back_to_the_shipped_probes(latest_path, monkeypatch):
    shipped = deepteam_sync._read(deepteam_sync.DEFAULT_CATALOG_PATH)
    monkeypatch.setattr(security_probes, "deepteam_defaults", lambda: deepteam_sync.catalog_probes(shipped))
    security_probes.reset_to_defaults()
    fetched = []
    probe_refresh.refresh(Judge(), fetch=lambda url: fetched.append(url) or _fetch(url), metrics=APP_METRICS)
    assert fetched  # the app metrics' standards are still read
    assert not [p for p in probe_refresh.load_latest()["probes"] if p["metric"] == "bfla"]
    generic = security_probes.load_probes(metric="bfla", probe_set="generic")
    assert len(generic) == 10 and security_probes.load_probes(metric="bfla", probe_set="latest") == generic
    fetched.clear()
    probe_refresh.refresh(Judge(), fetch=lambda url: fetched.append(url) or _fetch(url), metrics=("bfla",))
    assert fetched == []  # DeepTeam's text comes from its catalog: nothing to fetch


def test_start_on_boot_refreshes_the_metrics_in_use(latest_path, monkeypatch):
    monkeypatch.setenv("JUDGE_PROBE_REFRESH", "1")
    monkeypatch.delenv("JUDGE_PROBE_REFRESH_ALL", raising=False)
    monkeypatch.setattr(probe_refresh, "fetch_text", _fetch)
    judge = Judge()
    probe_refresh.start_on_boot(lambda: judge, lambda days: {"ssrf", "context_relevancy"}).join(timeout=60)
    assert len(judge.prompts) == len(APP_METRICS) + 1
    assert {p["metric"] for p in probe_refresh.load_latest()["probes"]} == set(APP_METRICS) | {"ssrf"}


def test_start_on_boot_without_a_source_refreshes_every_metric(latest_path, monkeypatch):
    monkeypatch.setenv("JUDGE_PROBE_REFRESH", "1")
    monkeypatch.setattr(probe_refresh, "fetch_text", _fetch)
    judge = Judge()
    probe_refresh.start_on_boot(lambda: judge).join(timeout=60)
    assert len(judge.prompts) == len(security_probes.PROBE_METRICS)


def test_the_app_passes_the_runs_of_its_own_database(app_module, monkeypatch):  # noqa: F811
    # The start_on_boot statement at the end of app.py, run again with recorders.
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    statement = next(node for node in ast.parse(source).body
                     if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                     and ast.unparse(node.value.func) == "probe_refresh.start_on_boot")
    seen = {}
    monkeypatch.setattr(probe_refresh, "start_on_boot",
                        lambda build_judge, recent_metrics=None: seen.update(recent=recent_metrics))
    monkeypatch.setattr(storage, "recent_metric_keys",
                        lambda conn, days: seen.update(conn=conn, days=days) or {"ssrf"})
    exec(compile(ast.Module([statement], type_ignores=[]), app_module.__file__, "exec"), vars(app_module))
    assert seen["recent"](probe_refresh.USED_DAYS) == {"ssrf"}
    assert seen["conn"] is app_module._conn and seen["days"] == 30
