"""DeepTeam's own red-teaming engine, run against a saved chatbot.

The app's red-team metrics (metrics_catalog security group) use DeepTeam's
catalog, attacks and scorer as parts inside the app's runner. This module runs
DeepTeam itself instead, exactly as its docs describe: a RedTeamer with the
judge as simulator and evaluation model, the chatbot wrapped as DeepTeam's
model_callback (returning an RTTurn with the reply, retrieved context and tool
calls), DeepTeam vulnerabilities / attacks or a framework (OWASP, NIST, MITRE,
EU AI Act, BeaverTails, Aegis), attacks_per_vulnerability_type, an optional
AttackEngine, run_all_attacks and exposure (CVSS). The result is DeepTeam's
RiskAssessment, stored as JSON in the deepteam_runs table.

Nothing here touches the app's metrics, jobs or scans.
"""
from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone

from backend import cancel
from backend.datasets import deepteam_sync

log = logging.getLogger("llm_judge.deepteam_engine")

# Frameworks red_team() takes in place of vulnerabilities + attacks.
# key: (class in deepteam.frameworks, label, kind). "dataset" ones load their
# prompts from Hugging Face (BeaverTails, Aegis) and take num_attacks.
FRAMEWORKS = {
    "owasp_llm": ("OWASPTop10", "OWASP Top 10 for LLMs 2025", "categories"),
    "owasp_agentic": ("OWASP_ASI_2026", "OWASP Top 10 for Agents 2026", "categories"),
    "nist": ("NIST", "NIST AI RMF", "categories"),
    "mitre": ("MITRE", "MITRE ATLAS", "categories"),
    "eu_ai_act": ("EUAIAct", "EU AI Act", "categories"),
    "beavertails": ("BeaverTails", "BeaverTails (dataset)", "dataset"),
    "aegis": ("Aegis", "Aegis (dataset)", "dataset"),
}
EXPOSURES = ("LOW", "MEDIUM", "HIGH")
MAX_ATTACKS_PER_TYPE = 10
MAX_DATASET_ATTACKS = 100
_SKIP_CLASSES = {"BaseVulnerability", "CustomVulnerability", "BaseAttack", "BaseSingleTurnAttack",
                 "BaseMultiTurnAttack", "StopReason", "BehaviorShiftDetector"}


def _dt(module: str):
    return deepteam_sync._import(module)


def available() -> str | None:
    """None when deepteam is installed, else why not."""
    try:
        _dt("deepteam.red_teamer")
        return None
    except Exception as e:  # noqa: BLE001 - any import failure means "not available"
        return f"deepteam is not installed ({e}); run update-packages.bat"


# --- What can be picked ------------------------------------------------------
def _attack_classes(module: str) -> list[type]:
    mod = _dt(module)
    base = getattr(_dt("deepteam.attacks.base_attack"), "BaseAttack")
    out = []
    for name in sorted(dir(mod)):
        cls = getattr(mod, name)
        # Base64 is an attack; the abstract bases are skipped by name (and isabstract).
        if (inspect.isclass(cls) and name not in _SKIP_CLASSES and cls is not base
                and issubclass(cls, base) and not inspect.isabstract(cls)):
            out.append(cls)
    return out


def _supplied(cls, purpose: str) -> dict:
    """Required constructor arguments the app can fill (a description of the chatbot)."""
    args = {}
    for name in deepteam_sync._required_args(cls):
        if name in ("target_information", "purpose"):
            args[name] = purpose.strip() or "An AI assistant answering users' messages."
        else:
            raise ValueError(f"DeepTeam {cls.__name__} needs '{name}', which the app cannot supply")
    return args


def _usable(cls) -> bool:
    try:
        _supplied(cls, "x")
        return True
    except ValueError:
        return False


def options() -> dict:
    """Everything a DeepTeam run can use, from the installed deepteam."""
    problem = available()
    if problem:
        return {"available": False, "error": problem}
    catalog = deepteam_sync.load_catalog()
    vulnerabilities = [
        {"class": v["class"], "title": v.get("title") or v["class"], "category": v.get("category", ""),
         "description": v.get("description", ""), "types": list(v.get("types") or [])}
        for v in catalog.get("vulnerabilities", [])
    ]
    single = [{"class": c.__name__, "kind": "single"} for c in _attack_classes("deepteam.attacks.single_turn")
              if _usable(c)]
    multi = [{"class": c.__name__, "kind": "multi"} for c in _attack_classes("deepteam.attacks.multi_turn")
             if _usable(c)]
    frameworks = [{"key": k, "label": label, "kind": kind} for k, (_, label, kind) in FRAMEWORKS.items()]
    return {
        "available": True,
        "version": deepteam_sync.installed_version(),
        "vulnerabilities": vulnerabilities,
        "attacks": single + multi,
        "frameworks": frameworks,
        "exposures": list(EXPOSURES),
        "max_attacks_per_type": MAX_ATTACKS_PER_TYPE,
    }


# --- Building DeepTeam objects -------------------------------------------------
def build_vulnerabilities(picked: list[dict], purpose: str) -> list:
    """[{class, types?}] -> DeepTeam vulnerability instances (all types when none given)."""
    module = _dt("deepteam.vulnerabilities")
    out = []
    for item in picked or []:
        name = str(item.get("class", ""))
        cls = getattr(module, name, None)
        if cls is None or name in _SKIP_CLASSES:
            raise ValueError(f"unknown DeepTeam vulnerability: {name!r}")
        args = _supplied(cls, purpose)
        types = [str(t) for t in item.get("types") or [] if str(t).strip()]
        if types:
            args["types"] = types
        try:
            out.append(cls(**args))
        except Exception as e:  # noqa: BLE001 - DeepTeam validates types itself
            raise ValueError(f"{name}: {e}") from e
    return out


def build_attacks(names: list[str], purpose: str, weights: dict | None = None) -> list:
    """Attack class names -> DeepTeam attack instances (single- or multi-turn)."""
    known = {c.__name__: c for c in
             _attack_classes("deepteam.attacks.single_turn") + _attack_classes("deepteam.attacks.multi_turn")}
    out = []
    for name in names or []:
        cls = known.get(str(name))
        if cls is None:
            raise ValueError(f"unknown DeepTeam attack: {name!r}")
        args = _supplied(cls, purpose)
        weight = (weights or {}).get(name)
        if weight and "weight" in inspect.signature(cls.__init__).parameters:
            args["weight"] = int(weight)
        out.append(cls(**args))
    return out


def build_framework(key: str, judge, num_attacks: int = 15, categories: list[str] | None = None):
    if key not in FRAMEWORKS:
        raise ValueError(f"unknown framework: {key!r}; one of {', '.join(FRAMEWORKS)}")
    class_name, _, kind = FRAMEWORKS[key]
    cls = getattr(_dt("deepteam.frameworks"), class_name)
    if kind == "dataset":
        n = max(1, min(int(num_attacks or 15), MAX_DATASET_ATTACKS))
        return cls(num_attacks=n, async_mode=False, evaluation_model=judge)
    return cls(categories=list(categories)) if categories else cls()


def make_callback(client, is_stopped=lambda: False):
    """The chatbot as DeepTeam's model_callback: (input, turns=None) -> RTTurn.
    Earlier turns (multi-turn attacks) go to the chatbot as history; the reply
    carries its retrieved context and tool calls when the chatbot reports them."""
    test_case = _dt("deepteam.test_case")
    RTTurn, ToolCall = test_case.RTTurn, test_case.ToolCall

    def model_callback(input: str, turns=None):  # noqa: A002 - DeepTeam's parameter name
        if is_stopped():
            raise cancel.RunCancelled()
        history = [{"role": t.role, "content": t.content} for t in (turns or [])
                   if getattr(t, "content", None)]
        reply = client.chat(input, history or None)
        tools = []
        for call in reply.tools_called or []:
            try:
                tools.append(ToolCall(name=str(call.get("name") or "tool"),
                                      input_parameters=call.get("input_parameters") or None,
                                      output=call.get("output")))
            except Exception:  # noqa: BLE001 - an odd tool record is left out, not fatal
                continue
        return RTTurn(role="assistant", content=reply.reply or "",
                      retrieval_context=list(reply.retrieval_context) if reply.retrieval_context else None,
                      tools_called=tools or None)

    return model_callback


def _dump(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    if isinstance(obj, dict):
        return {str(k): _dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_dump(v) for v in obj]
    if hasattr(obj, "value") and not callable(obj.value):  # Enum
        return obj.value
    if hasattr(obj, "__dict__"):
        return {k: _dump(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _model_name(judge) -> str | None:
    """The judge's model name, as DeepEval reports it (without " (Local Model)")."""
    try:
        name = judge.get_model_name()
    except Exception:  # noqa: BLE001 - a fake or odd judge simply has no name
        return None
    return str(name).replace(" (Local Model)", "") if name else None


def summarize(test_cases: list[dict]) -> dict:
    """Pass rate per vulnerability and per attack, from DeepTeam's test cases
    (score 1 = resisted, 0 = vulnerable, error = not scored)."""
    def bucket(key):
        out: dict[str, dict] = {}
        for tc in test_cases:
            name = tc.get(key) or "—"
            row = out.setdefault(name, {"passing": 0, "failing": 0, "errored": 0})
            if tc.get("error"):
                row["errored"] += 1
            elif tc.get("score") is not None and float(tc["score"]) >= 1:
                row["passing"] += 1
            else:
                row["failing"] += 1
        for row in out.values():
            scored = row["passing"] + row["failing"]
            row["pass_rate"] = round(row["passing"] / scored, 4) if scored else None
        return out

    scored = [tc for tc in test_cases if not tc.get("error") and tc.get("score") is not None]
    passing = sum(1 for tc in scored if float(tc["score"]) >= 1)
    cvss = [float(tc["cvss_score"]) for tc in test_cases if tc.get("cvss_score") is not None]
    return {
        "test_cases": len(test_cases),
        "passing": passing,
        "failing": len(scored) - passing,
        "errored": len(test_cases) - len(scored),
        "pass_rate": round(passing / len(scored), 4) if scored else None,
        "max_cvss": max(cvss) if cvss else None,
        "by_vulnerability": bucket("vulnerability"),
        "by_attack": bucket("attack_method"),
    }


def run(client, judge, opts: dict, is_stopped=lambda: False) -> dict:
    """One DeepTeam red-team run; returns {overview, test_cases, summary}."""
    purpose = str(opts.get("target_purpose") or "").strip()
    red_teamer_mod = _dt("deepteam.red_teamer")
    Level = _dt("deepteam.red_teamer.cvss").Level
    exposure = str(opts.get("exposure") or "MEDIUM").upper()
    if exposure not in EXPOSURES:
        raise ValueError(f"exposure must be one of: {', '.join(EXPOSURES)}")
    framework = None
    vulnerabilities = attacks = None
    if opts.get("framework"):
        framework = build_framework(opts["framework"], judge, opts.get("num_attacks", 15),
                                    opts.get("framework_categories"))
    else:
        vulnerabilities = build_vulnerabilities(opts.get("vulnerabilities") or [], purpose)
        attacks = build_attacks(opts.get("attacks") or [], purpose, opts.get("attack_weights"))
        if not vulnerabilities:
            raise ValueError("pick at least one vulnerability, or a framework")
    engine = None
    if opts.get("attack_engine"):
        AttackEngine = _dt("deepteam.attacks.attack_engine").AttackEngine
        guidelines = [str(g) for g in opts.get("generation_guidelines") or [] if str(g).strip()]
        engine = AttackEngine(simulator_model=judge, variations=max(1, min(int(opts.get("variations") or 1), 5)),
                              generation_guidelines=guidelines or None, purpose=purpose or None)
    per_type = max(1, min(int(opts.get("attacks_per_vulnerability_type") or 1), MAX_ATTACKS_PER_TYPE))
    red_teamer = red_teamer_mod.RedTeamer(
        simulator_model=judge, evaluation_model=judge, target_purpose=purpose,
        async_mode=False, max_concurrent=1, attack_engine=engine, exposure=Level[exposure],
    )
    kwargs = dict(
        model_callback=make_callback(client, is_stopped),
        vulnerabilities=vulnerabilities, attacks=attacks or None, framework=framework,
        attacks_per_vulnerability_type=per_type, ignore_errors=True,
        run_all_attacks=bool(opts.get("run_all_attacks")),
    )
    params = inspect.signature(red_teamer.red_team).parameters
    # Never print a rich table into the backend log, and never upload on its own:
    # Confident AI gets runs only through the app's own upload.
    for private in ("_print_assessment", "_upload_to_confident"):
        if private in params:
            kwargs[private] = False
    assessment = red_teamer.red_team(**kwargs)
    test_cases = _dump(getattr(assessment, "test_cases", None)) or []
    return {
        "overview": _dump(getattr(assessment, "overview", None)),
        "test_cases": test_cases,
        "summary": summarize(test_cases),
    }


# --- Storage: one row per run (its own table, created on first use) ----------------
def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deepteam_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            options_json TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )"""
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(r) -> dict:
    keys = ("id", "target_id", "status", "options_json", "result_json", "error", "created_at", "finished_at")
    row = dict(zip(keys, r))
    row["options"] = json.loads(row.pop("options_json") or "{}")
    raw = row.pop("result_json")
    row["result"] = json.loads(raw) if raw else None
    return row


_COLUMNS = "id, target_id, status, options_json, result_json, error, created_at, finished_at"


def get_run(conn, run_id: int) -> dict | None:
    ensure_table(conn)
    r = conn.execute(f"SELECT {_COLUMNS} FROM deepteam_runs WHERE id = ?", (run_id,)).fetchone()
    return _row(r) if r else None


def list_runs(conn, target_id: int) -> list[dict]:
    ensure_table(conn)
    rows = conn.execute(f"SELECT {_COLUMNS} FROM deepteam_runs WHERE target_id = ? ORDER BY id DESC",
                        (target_id,)).fetchall()
    out = []
    for r in rows:
        row = _row(r)
        result = row.pop("result")
        row["summary"] = (result or {}).get("summary")
        row["confident"] = (result or {}).get("confident")  # {link, sent_at} or {error} once sent
        out.append(row)
    return out


def delete_run(conn, run_id: int) -> bool:
    ensure_table(conn)
    cur = conn.execute("DELETE FROM deepteam_runs WHERE id = ? AND status NOT IN ('queued', 'running')", (run_id,))
    conn.commit()
    return cur.rowcount > 0


# --- Background runner: one DeepTeam run at a time ------------------------------
class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop: dict[int, threading.Event] = {}
        self._busy = threading.Lock()

    def start(self, conn, db_lock, target_id: int, opts: dict, make_client, make_judge,
              background: bool = True, on_done=None) -> int:
        """on_done(run_id), if given, runs after a run finishes with results (e.g. the Confident AI upload)."""
        ensure_table(conn)
        with db_lock:
            cur = conn.execute(
                "INSERT INTO deepteam_runs (target_id, status, options_json, created_at) VALUES (?, 'queued', ?, ?)",
                (target_id, json.dumps(opts), _now()))
            conn.commit()
            run_id = cur.lastrowid
        stop = threading.Event()
        with self._lock:
            self._stop[run_id] = stop
        work = lambda: self._work(conn, db_lock, run_id, opts, make_client, make_judge, stop, on_done)  # noqa: E731
        if background:
            threading.Thread(target=work, name=f"deepteam-run-{run_id}", daemon=True).start()
        else:
            work()
        return run_id

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            stop = self._stop.get(run_id)
        if stop is None:
            return False
        stop.set()
        return True

    def _set(self, conn, db_lock, run_id, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        with db_lock:
            conn.execute(f"UPDATE deepteam_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
            conn.commit()

    def _work(self, conn, db_lock, run_id, opts, make_client, make_judge, stop, on_done=None):
        with self._busy:  # one DeepTeam run at a time (each makes many judge calls)
            if stop.is_set():
                self._set(conn, db_lock, run_id, status="cancelled", finished_at=_now())
                return
            self._set(conn, db_lock, run_id, status="running")
            started = time.monotonic()
            client = None
            try:
                client = make_client()
                judge = make_judge()
                with cancel.watching(stop.is_set):
                    result = run(client, judge, opts, stop.is_set)
                result["duration_s"] = round(time.monotonic() - started, 1)
                result["judge_model"] = _model_name(judge)
                status = "cancelled" if stop.is_set() else "done"
                self._set(conn, db_lock, run_id, status=status, result_json=json.dumps(result),
                          finished_at=_now())
                if on_done is not None and status == "done":
                    try:
                        on_done(run_id)
                    except Exception as e:  # noqa: BLE001 - an upload never fails the run
                        log.warning("After DeepTeam run %s: %s", run_id, e)
            except cancel.RunCancelled:
                self._set(conn, db_lock, run_id, status="cancelled", finished_at=_now())
            except Exception as e:  # noqa: BLE001 - every failure is recorded on the run
                log.warning("DeepTeam run %s failed: %s\n%s", run_id, e, traceback.format_exc())
                self._set(conn, db_lock, run_id, status="error", error=str(e)[:2000], finished_at=_now())
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                with self._lock:
                    self._stop.pop(run_id, None)


def interrupt_unfinished(conn) -> None:
    """Runs left queued/running by a restart can never finish: mark them."""
    ensure_table(conn)
    conn.execute("UPDATE deepteam_runs SET status = 'interrupted', finished_at = ? "
                 "WHERE status IN ('queued', 'running')", (_now(),))
    conn.commit()


RUNNER = Runner()


def set_confident(conn, run_id: int, info: dict) -> None:
    """Keeps the Confident AI upload (link, or error) on the run's result."""
    ensure_table(conn)
    row = conn.execute("SELECT result_json FROM deepteam_runs WHERE id = ?", (run_id,)).fetchone()
    if not row or not row[0]:
        return
    result = json.loads(row[0])
    result["confident"] = info
    conn.execute("UPDATE deepteam_runs SET result_json = ? WHERE id = ?", (json.dumps(result), run_id))
    conn.commit()
