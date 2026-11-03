# Auto Test Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Right after a chatbot / agent / MCP server is saved, the app profiles it, finds which metrics lack test data, fills exactly those gaps with evaluation-grade data (verified against the target, grounded in help pages / docs), and lists what it still could not fill.

**Architecture:** Extend the existing "Build all test data" pipeline (`backend/datasets/prepare_all.py` `build_testdata`, run by `app._run_build`). Add a `profile` step and a budget wrapper; turn each step from "ran once → skip" into "count gaps → fill to minimum"; verify tool / RAG cases by sending them once; return learned config for `app` to save; add `leftovers` to the build view; the side panel starts the build automatically.

**Tech Stack:** Python 3.13, FastAPI, DeepEval, pytest (offline fakes), vanilla JS + `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-02-auto-test-data-design.md`

## Global Constraints

- Never overwrite a user-typed value: paths, `persona`, `instructions`, goldens, non-empty scenario fields.
- Caps per build: `BUILD_MAX_TARGET_MESSAGES` = 30 (env `JUDGE_BUILD_MAX_MESSAGES`), `BUILD_MAX_JUDGE_CALLS` = 40 (env `JUDGE_BUILD_MAX_JUDGE_CALLS`); reaching one marks the remaining steps `skipped` with the reason, data already written stays.
- Minimums: `GOLDENS_MIN` = 5 general goldens, `SCENARIOS_MIN` = 3, `TOOL_GOLDENS_PER_TOOL` = 2, `TOOL_ATTEMPTS` = 2, `PROBE_TOOL_MESSAGES` = 3, `CASES_MIN` = 3, `REFERENCE_GOLDENS_MIN` = 3 short-answer goldens (≤ 15 words).
- `expected_answer` never copied from the target's reply; target replies only verify.
- Golden `source`: `auto_chatbot` (from material), `auto_unverified` (no material), `auto_tools` (tool cases, existing).
- `JUDGE_AUTO_BUILD=0` turns the automatic start off (panel shows the old question).
- Target config flag `auto_built: true` after the first automatic build; existing chatbots with leftovers get one automatic build on first select.
- Offline tests only (fake judge / fake bot); `pytest -m smoke` and the full offline suite stay green.
- Any change to backend / web-app files updates `docs/WORKFLOW.md`, `docs/workflow.html` (footer date) and republishes it to https://claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv (CLAUDE.md rule).

## Review Focus

1. **Target unreachable or timing out mid-build** → each step reports `error`/`skipped`, the build ends `done`, nothing half-written; test in Task 6.
2. **Replies carry tools/sources at paths the config lacks** → `client.discovered` paths are saved after the build (never over typed); test in Task 7.
3. **User-typed scenario with some fields filled** → repair fills only empty `chatbot_role` / `expected_outcome`, typed text untouched; test in Task 3.
4. **Budget runs out halfway** → later steps `skipped` with "Build budget reached", earlier data kept; test in Task 6.
5. **Judge returns malformed JSON or invents tools** → that step errors / drops invalid rows, other steps proceed; tests in Tasks 3 and 5.

---

### Task 1: Budget wrappers and duplicate check

**Files:**
- Create: `backend/datasets/build_kit.py`
- Test: `tests/test_build_kit.py`

**Interfaces:**
- Produces:
  - `class BudgetSpent(RuntimeError)`
  - `class Budget: __init__(self, max_messages: int, max_judge_calls: int); messages: int; judge_calls: int`
  - `class BudgetedJudge: __init__(self, judge, budget: Budget)`; `generate(prompt, schema=None)` raises `BudgetSpent` when `budget.judge_calls >= max_judge_calls`, else counts and delegates; `__getattr__` delegates the rest (`get_model_name`, …).
  - `class BudgetedClient: __init__(self, client, budget: Budget)`; `chat(message, history=None)` same rule on `messages`; `__getattr__` delegates (`discovered`, `list_tools`, `describe`, `close`).
  - `def caps_from_env() -> tuple[int, int]` → `(JUDGE_BUILD_MAX_MESSAGES or 30, JUDGE_BUILD_MAX_JUDGE_CALLS or 40)`; invalid values → defaults.
  - `def normalise(text: str) -> str` (lowercase, drop punctuation, collapse spaces).
  - `def is_duplicate(question: str, existing: list[str], threshold: float = 0.9) -> bool` — equal normalised text, or token Jaccard ≥ threshold.

- [ ] **Step 1: Write failing tests**

```python
def test_budgeted_judge_stops_at_cap():
    b = Budget(max_messages=1, max_judge_calls=2)
    j = BudgetedJudge(FakeJudge(), b)
    j.generate("a"); j.generate("b")
    with pytest.raises(BudgetSpent):
        j.generate("c")
    assert b.judge_calls == 2 and j.get_model_name() == "fake"

def test_budgeted_client_stops_at_cap_and_delegates():
    b = Budget(1, 1); c = BudgetedClient(FakeBot(discovered={"tools_path": "t"}), b)
    c.chat("hi")
    with pytest.raises(BudgetSpent):
        c.chat("again")
    assert c.discovered == {"tools_path": "t"}

def test_caps_from_env(monkeypatch):
    assert caps_from_env() == (30, 40)
    monkeypatch.setenv("JUDGE_BUILD_MAX_MESSAGES", "5"); monkeypatch.setenv("JUDGE_BUILD_MAX_JUDGE_CALLS", "x")
    assert caps_from_env() == (5, 40)

@pytest.mark.parametrize("q,dup", [("Do you ship to Canada?", True), ("do you ship to canada", True),
                                   ("How do refunds work?", False)])
def test_is_duplicate(q, dup):
    assert is_duplicate(q, ["Do you ship to Canada?!"]) is dup
```

- [ ] **Step 2:** Run `.venv\Scripts\python -m pytest tests/test_build_kit.py -v` → FAIL (module missing).
- [ ] **Step 3:** Implement `backend/datasets/build_kit.py` per Interfaces.
- [ ] **Step 4:** Run same command → PASS.
- [ ] **Step 5:** Commit `feat(build): budget wrappers and duplicate check for test-data builds`.

---

### Task 2: Profile step (kind, learned tools, persona)

**Files:**
- Create: `backend/datasets/profile.py`
- Modify: `backend/datasets/autogen.py` (add `PERSONA_PROMPT`, `TOOL_PROBE_PROMPT`; `interview(..., on_reply=None)`)
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: `autogen.interview`, `backend.judges.ask.ask_json`, `ChatReply.tools_called` / `.retrieval_context`.
- Produces:
  - `@dataclass class Profile: kind: str = ""; tools: list[dict] = field(default_factory=list); persona: str = ""; sources_seen: bool = False; tools_seen: bool = False; tool_reasoning_seen: bool = False`
  - `def observe(profile: Profile, reply: ChatReply) -> None` — sets `sources_seen` on non-empty `retrieval_context`; each tool call with a new name appends `{"name", "description": "", "parameters": sorted(input_parameters keys)}` and sets `tools_seen`; non-empty `reasoning` sets `tool_reasoning_seen`.
  - `def profile_target(client, judge, config: dict, is_mcp: bool, material: str, profile: Profile) -> Profile` — `profile` already holds what the interview's replies showed (the caller passes `on_reply=lambda r: observe(profile, r)` to `autogen.interview`). It then:
    - adds known tools first: `config["available_tools"]`, or MCP `client.list_tools()`;
    - if `config.get("tools_path")`, not MCP and no tool seen: one judge call (`TOOL_PROBE_PROMPT` → `{"messages": [...]}`) for `PROBE_TOOL_MESSAGES` action-style user messages from `material`, sends each, observes;
    - persona: `config["persona"]` if set (no judge call), else one judge call (`PERSONA_PROMPT` → `{"persona": "<one sentence>"}`);
    - kind: `"mcp"` if is_mcp, `"agent"` if any tool known, `"rag"` if `config.get("context_path")` or `sources_seen`, else `"chat"`.
- `autogen.interview(target, on_reply: Callable[[ChatReply], None] | None = None)` calls `on_reply(reply)` per reply; default keeps old behaviour.

- [ ] **Step 1: Write failing tests**

```python
def test_tools_learned_from_replies():
    p = Profile()
    observe(p, ChatReply("ok", "m", "http", tools_called=[{"name": "lookup_order", "input_parameters": {"order_id": "1"}}]))
    assert p.tools == [{"name": "lookup_order", "description": "", "parameters": ["order_id"]}]
    assert p.tools_seen and not p.tool_reasoning_seen

def test_probe_messages_sent_only_when_tools_path_and_none_seen():
    bot = ToolBot(calls_tool_on="ORD-1001")
    judge = FakeJudge(tool_probe=["Where is ORD-1001?", "Hi", "Price of SKU-1?"], persona="Shop support bot.")
    p = profile_target(bot, judge, {"tools_path": "tools_called"}, False, "material", Profile())
    assert bot.asked == ["Where is ORD-1001?", "Hi", "Price of SKU-1?"]
    assert p.kind == "agent" and p.persona == "Shop support bot."

def test_typed_persona_kept_without_judge_call():
    judge = FakeJudge()
    p = profile_target(PlainBot(), judge, {"persona": "Typed."}, False, "", Profile())
    assert p.persona == "Typed." and judge.calls == [] and p.kind == "chat"

def test_rag_kind():
    assert profile_target(PlainBot(), FakeJudge(persona="X."), {"context_path": "sources"}, False, "", Profile()).kind == "rag"
    assert profile_target(PlainBot(), FakeJudge(persona="X."), {}, False, "", Profile(sources_seen=True)).kind == "rag"

def test_mcp_kind_and_tools_from_list_tools():
    p = profile_target(FakeMcp(tools=[{"name": "echo"}]), FakeJudge(persona="X."), {}, True, "", Profile())
    assert p.kind == "mcp" and [t["name"] for t in p.tools] == ["echo"]
```

- [ ] **Step 2:** Run `.venv\Scripts\python -m pytest tests/test_profile.py -v` → FAIL.
- [ ] **Step 3:** Implement `profile.py`, the two prompts, `interview(on_reply=)`.
- [ ] **Step 4:** Run `tests/test_profile.py tests/test_autogen.py` → PASS.
- [ ] **Step 5:** Commit `feat(build): profile step learns target kind, tools and persona`.

---

### Task 3: Diverse, deduplicated, grounded goldens; scenario repair

**Files:**
- Modify: `backend/datasets/autogen.py` (`WRITE_PROMPT`, `write_dataset`; new `check_grounded`, `REPAIR_PROMPT`, `repair_scenarios`)
- Test: `tests/test_autogen.py`

**Interfaces:**
- Consumes: `build_kit.is_duplicate`.
- Produces:
  - `write_dataset(judge, answers, pages, persona="", *, n_goldens: int = GOLDENS_WANTED, n_scenarios: int = SCENARIOS_WANTED, n_short: int = 0, existing: Sequence[str] = ()) -> tuple[list[dict], list[dict]]` — prompt asks for a mix of `happy_path`, `edge_case`, `out_of_scope`, `multi_intent`, `follow_up` (stored in `categories`); `n_short` single-fact goldens with `expected_answer` ≤ 15 words (category `fact`); scenarios of 2–4 turns, one fact stated early that a later turn depends on, non-empty `chatbot_role` and `expected_outcome`; expected answers from the material / the bot's stated scope, never quoting its replies (prompt says so). Drops duplicates vs `existing` and within the batch; drops `fact` rows over 15 words. Defaults keep `prepare-goldens` callers working.
  - `check_grounded(judge, rows: list[dict], material: str) -> list[bool]` — one judge call, `{"verdicts": ["supported"|"unsupported", ...]}` in row order; empty `material` → all `False`, no call; length mismatch → all `False`.
  - `repair_scenarios(judge, scenarios: list[dict], persona: str, material: str) -> list[tuple[str, dict]]` — `(id, fields)` for scenarios with empty `chatbot_role` (← `persona`, no judge call; skipped when persona empty) and/or empty `expected_outcome` (one judge call for all, `{"outcomes": {id: text}}`); never returns a field already non-empty.

- [ ] **Step 1: Write failing tests**

```python
def test_write_dataset_drops_duplicates_and_long_facts():
    judge = FakeJudge(dataset={"goldens": [
        {"question": "Do you ship to Canada?", "expected_answer": "Yes, 10-14 days.", "category": "happy_path"},
        {"question": "do you ship to canada", "expected_answer": "Yes.", "category": "edge_case"},
        {"question": "Refund window?", "expected_answer": "30 days.", "category": "fact"},
        {"question": "Express cost?", "expected_answer": " ".join(["word"] * 16), "category": "fact"}],
        "scenarios": []})
    goldens, _ = autogen.write_dataset(judge, [], [("u", "text")], n_goldens=5, n_scenarios=3, n_short=3,
                                       existing=["Refund window?"])
    assert [g["question"] for g in goldens] == ["Do you ship to Canada?"]
    assert goldens[0]["categories"] == ["happy_path"]

def test_check_grounded():
    judge = FakeJudge(verdicts=["supported", "unsupported"])
    rows = [{"question": "a", "expected_answer": "b"}] * 2
    assert autogen.check_grounded(judge, rows, "material") == [True, False]
    no_call = FakeJudge()
    assert autogen.check_grounded(no_call, rows[:1], "") == [False] and no_call.calls == []

def test_repair_fills_only_empty_fields():
    sc = [{"id": "c1", "chatbot_role": "", "expected_outcome": "Typed outcome", "user_turns": ["a", "b"]},
          {"id": "c2", "chatbot_role": "Typed role", "expected_outcome": "", "user_turns": ["a", "b"]}]
    judge = FakeJudge(outcomes={"c2": "Gets the refund timeline."})
    assert dict(autogen.repair_scenarios(judge, sc, "Shop bot.", "m")) == {
        "c1": {"chatbot_role": "Shop bot."}, "c2": {"expected_outcome": "Gets the refund timeline."}}

def test_write_dataset_malformed_json_raises():
    with pytest.raises(Exception):
        autogen.write_dataset(FakeJudge(raw="not json"), [], [("u", "t")], n_goldens=5, n_scenarios=3, n_short=3)
```

- [ ] **Step 2:** Run `.venv\Scripts\python -m pytest tests/test_autogen.py -v` → new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_autogen.py tests/test_prepare_goldens.py` → PASS.
- [ ] **Step 5:** Commit `feat(build): diverse, deduplicated, grounded goldens and scenario repair`.

---

### Task 4: Uploaded docs as material; context goldens when there is none

**Files:**
- Modify: `backend/datasets/prepare_all.py` (`_chatbot_material` gains `documents`; new `WRITE_CONTEXT_PROMPT`, `write_unverified_context_goldens`)
- Test: `tests/test_prepare_all.py`

**Interfaces:**
- Produces:
  - `build_testdata(..., documents: Sequence[tuple[str, str]] = ())` — `(filename, text)` of the theme's `ready` documents, added to the material of every judge prompt (each clipped to 1500 chars, at most 3).
  - `write_unverified_context_goldens(judge, theme: str, material: str, n: int) -> int` — used by the context step when nothing was queued (no new pages, no instructions, no documents): one judge call writes `n` goldens, each with a `context` passage of ≥ 60 words; rows under 60 words dropped; `source="auto_unverified"`; returns count.
  - Context step reports `done` with "N Context goldens written without a source document (unverified): add a Help / FAQ URL or a document for grounded ones." when it used this path.
  - Skip condition: the theme already has ≥ `CASES_MIN` goldens with context → `skipped`, no call.

- [ ] **Step 1: Failing tests** (`Judge` in the test file gains `self.prompts` and a `"context"` kind matched on a phrase of `WRITE_CONTEXT_PROMPT`)

```python
def test_uploaded_documents_reach_the_judge():
    judge = Judge()
    _build({"id": 21, "type": "http", "config": {"persona": "p"}}, judge, Bot(), theme="docs_shop",
           documents=[("policy.pdf", "Returns accepted within 45 days.")])
    assert any("Returns accepted within 45 days." in p for p in judge.prompts)

def test_no_material_writes_unverified_context_goldens():
    judge = Judge()
    _out, reports, queued = _build({"id": 22, "type": "http", "config": {"persona": "p"}}, judge, Bot(),
                                   theme="bare", find_pages=lambda c: [])
    rows = [g for g in goldens_store.load_goldens(theme="bare") if g["source"] == "auto_unverified" and g["context"]]
    assert len(rows) == prepare_all.CASES_MIN
    assert all(len(" ".join(g["context"]).split()) >= 60 for g in rows)
    assert _last(reports)["context"][0] == "done" and queued == []
```

- [ ] **Step 2:** Run `.venv\Scripts\python -m pytest tests/test_prepare_all.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(build): use uploaded docs as material; unverified context goldens when none`.

---

### Task 5: Verified tool goldens, per-tool gaps

**Files:**
- Modify: `backend/datasets/prepare_all.py` (`write_tool_goldens`)
- Test: `tests/test_prepare_all.py`

**Interfaces:**
- Consumes: `profile.observe` (Task 2), `build_kit.is_duplicate`.
- Produces: `write_tool_goldens(judge, theme, tools, persona="", interview=None, *, client=None, profile: Profile | None = None, existing: Sequence[dict] = ()) -> int`
  - per tool, gap = `TOOL_GOLDENS_PER_TOOL` − goldens in `existing` naming it; only tools with a gap go in the prompt (`n` = total gap); no gap → 0, no judge call;
  - unknown tool names dropped (existing behaviour); duplicate questions dropped;
  - with `client`: each candidate is sent once, the reply observed into `profile`; kept only if the reply has ≥ 1 tool call; up to `TOOL_ATTEMPTS` judge rounds while gaps remain;
  - `client=None`: old unverified behaviour.

- [ ] **Step 1: Failing tests**

```python
def test_tool_goldens_kept_only_when_the_bot_calls_a_tool():
    bot = ToolBot(calls_tool_on="Repeat")  # tools_called=[{"name": "echo"}] when the message contains it
    judge = Judge(tool_rows=[{"question": "Repeat 'hi'", "expected_answer": "hi", "expected_tools": ["echo"]},
                             {"question": "Tell a joke", "expected_answer": "-", "expected_tools": ["echo"]}])
    added = prepare_all.write_tool_goldens(judge, "t1", [{"name": "echo"}], client=bot, profile=Profile())
    assert added == 1 and "Tell a joke" in bot.asked
    assert judge.calls.count("tools") == prepare_all.TOOL_ATTEMPTS  # second round for the remaining gap

def test_no_gap_no_judge_call():
    existing = [{"expected_tools": ["echo"]}] * prepare_all.TOOL_GOLDENS_PER_TOOL
    judge = Judge()
    assert prepare_all.write_tool_goldens(judge, "t2", [{"name": "echo"}], existing=existing) == 0
    assert judge.calls == []
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `.venv\Scripts\python -m pytest tests/test_prepare_all.py -v` → PASS.
- [ ] **Step 5:** Commit `feat(build): verified, per-tool gap-driven Tool Correctness goldens`.

---

### Task 6: `build_testdata` integration (profile step, gaps, budget, outcome)

**Files:**
- Modify: `backend/datasets/prepare_all.py` (`STEPS`, `build_testdata`, module docstring)
- Modify: `tests/test_prepare_all.py` (update `test_build_fills_every_dataset_once_and_asks_the_bot_once` and `test_an_mcp_server_is_learned_from_its_own_description` for the new steps and minimums; the fake judge returns enough rows to meet every minimum)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces:
  - `STEPS = (("profile", "What this chatbot is: kind, tools and role"), ("goldens", "Golden answers"), ("scenarios", "Conversation scenarios with role and expected outcome"), ("context", "Context goldens from help pages, documents and instructions"), ("tools", "Tool Correctness cases verified against the chatbot"), ("probes", "Security and safety probes fitted to this chatbot"))`
  - `build_testdata(target_row, *, theme, judge, client, queue_document, report=None, known_documents=(), wait_documents=None, find_pages=autogen.find_help_pages, documents=(), budget: Budget | None = None) -> dict` returns the old keys plus
    - `"learned": {"persona": str, "available_tools": list[dict]}` — only values the config lacks;
    - `"profile": {"kind": str, "sources_seen": bool, "tools_seen": bool, "tool_reasoning_seen": bool}`.
  - Wraps `judge` / `client` in `BudgetedJudge` / `BudgetedClient` (budget from `caps_from_env()` when None). `BudgetSpent` in a step → that step and every later one reported `skipped` with `"Build budget reached (N messages / M judge calls); press Build all test data to continue."` (context documents already queued are still waited for).
  - goldens step: count the theme's non-starter goldens without `expected_tools`; below `GOLDENS_MIN` (or fewer than `REFERENCE_GOLDENS_MIN` with category `fact`) → `write_dataset(n_goldens=gap, n_short=fact gap, existing=theme questions)`; with material, rows failing `check_grounded` are dropped; without material, saved with `source="auto_unverified"`.
  - scenarios step: below `SCENARIOS_MIN` → write the gap (same `write_dataset` call when both are short: one judge call); then `repair_scenarios` over every scenario of the theme, applied with `conversations_store.update_conversation`. For each new scenario when `profile.kind in ("rag", "agent", "mcp")`: send its first turn once; no sources in the reply and material present → set its `context` to up to 3 material passages (the same repair call returns `{"contexts": {id: [passages]}}`).
  - A full theme (all minimums met, no empty scenario fields, no tool gaps, fitted probes present, persona set) → second build: `bot.asked` equals only the interview (profile needs it) and `judge.calls == []`.

- [ ] **Step 1: Failing tests**

```python
def test_unreachable_bot_reports_and_finishes():
    out, reports, _ = _build({"id": 31, "type": "http", "config": {"persona": "p"}}, Judge(), DeadBot(), theme="dead")
    steps = _last(reports)
    assert set(steps) == {k for k, _ in prepare_all.STEPS}
    assert all(s in ("done", "skipped", "error") for s, _d in steps.values())

def test_budget_reached_skips_the_rest_and_keeps_data():
    budget = Budget(max_messages=len(autogen.INTERVIEW), max_judge_calls=2)
    _out, reports, _ = _build({"id": 32, "type": "http", "config": {"persona": "p"}}, Judge(), Bot(),
                              theme="tight", budget=budget)
    status, detail = _last(reports)["probes"]
    assert status == "skipped" and "Build budget reached" in detail
    assert goldens_store.load_goldens(theme="tight")

def test_outcome_reports_learned_tools_and_profile():
    out, _r, _q = _build({"id": 33, "type": "http", "config": {"tools_path": "tools_called"}},
                         Judge(persona="Shop bot."), ToolBot(calls_tool_on="ORD"), theme="agent_shop")
    assert out["profile"]["kind"] == "agent" and out["learned"]["persona"] == "Shop bot."
    assert [t["name"] for t in out["learned"]["available_tools"]] == ["lookup_order"]

def test_typed_scenario_fields_survive_repair():
    conversations_store.add_conversation({"theme": "keep", "name": "Mine", "scenario": "", "chatbot_role": "Typed role",
                                          "expected_outcome": "", "user_turns": ["a", "b"]})
    _build({"id": 34, "type": "http", "config": {"persona": "Shop bot."}}, Judge(), Bot(), theme="keep")
    mine = next(c for c in conversations_store.load_conversations(theme="keep") if c["name"] == "Mine")
    assert mine["chatbot_role"] == "Typed role" and mine["expected_outcome"]
```

- [ ] **Step 2:** Run `.venv\Scripts\python -m pytest tests/test_prepare_all.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_prepare_all.py tests/test_prepare_goldens.py tests/test_autogen.py tests/test_profile.py tests/test_build_kit.py` → PASS.
- [ ] **Step 5:** Commit `feat(build): gap-driven build with profile step and budget`.

---

### Task 7: App — save what the build learned, leftovers, auto-build setting

**Files:**
- Modify: `backend/dashboard/app.py` (`_run_build`, `_build_view`, new `_leftovers` + `LEFTOVER_HINTS`, `GET /api/build/settings`)
- Test: `tests/test_prepare_all.py` (API section)

**Interfaces:**
- Consumes: Task 6 outcome; `storage.list_documents`.
- Produces:
  - `_run_build` passes `documents` = `(filename, text)` of the theme's `ready` documents (text from wherever the document pipeline stores it; read the saved file under `backend/datasets/documents/<theme>/` if the row has no text). After the build, merges into the latest config, never over a non-empty value: `client.discovered` (`tools_path`, `context_path`), `learned.persona`, `learned.available_tools` (append names not present); always sets `profile` and `auto_built: True`.
  - `_leftovers(row: dict) -> list[dict]` → `[{key, title, reason, hint}]`:
    - every `_metrics_summary(row)["missing"]` item, `hint` from `LEFTOVER_HINTS` matched on the reason: tools → "Set 'Tool calls path', or run Find missing metric data."; context / sources → "Add a Help / FAQ URL or upload a document, then build again."; role / scenario → "Build again, or add a conversation scenario with a chatbot role."; otherwise "Press Build all test data again.";
    - when `profile.tools_seen and not profile.tool_reasoning_seen`: `plan_quality`, `plan_adherence` with hint "Ask the bot's team to return the agent's plan or reasoning with each tool call.";
    - kind-based N/A (`reason` "Does not apply to this target", `hint` ""): agent metrics except `task_completion` when kind is `chat`/`rag`; `contextual_*` and `turn_contextual_*` when kind is `chat`.
  - `_build_view` adds `"leftovers": _leftovers(row)` and `"kind": config.get("profile", {}).get("kind", "")`.
  - `GET /api/build/settings` → `{"auto_build": os.getenv("JUDGE_AUTO_BUILD", "1") != "0"}`.

- [ ] **Step 1: Failing tests** (reuse the file's `_create_target` / `offline_judge` / monkeypatched client pattern from `test_prepare_all_builds_and_reports`)

```python
def test_build_saves_found_paths_tools_and_persona_but_not_over_typed(app_module, client, offline_judge, monkeypatch):
    # target typed persona "Typed"; the fake client's `discovered` = {"context_path": "sources"}; replies call lookup_order
    cfg = _run_fake_build(app_module, client, monkeypatch, config={"persona": "Typed", "tools_path": "tools_called"})
    assert cfg["persona"] == "Typed" and cfg["context_path"] == "sources"
    assert [t["name"] for t in cfg["available_tools"]] == ["lookup_order"] and cfg["auto_built"] is True

def test_leftovers_name_plan_metrics_without_reasoning(app_module, client, offline_judge, monkeypatch):
    tid = _run_fake_build(app_module, client, monkeypatch, config={"tools_path": "tools_called"}, return_id=True)
    view = client.get(f"/api/targets/{tid}/prepare-all").json()
    plan = {l["key"]: l for l in view["leftovers"]}["plan_quality"]
    assert "reasoning" in plan["hint"] and view["kind"] == "agent"

def test_build_settings_flag(client, monkeypatch):
    assert client.get("/api/build/settings").json() == {"auto_build": True}
    monkeypatch.setenv("JUDGE_AUTO_BUILD", "0")
    assert client.get("/api/build/settings").json() == {"auto_build": False}
```
(`_run_fake_build` is a small helper in the test file: creates the target, monkeypatches `_client_or_400` to the fake, POSTs prepare-all, runs the job inline, returns the saved config or the id.)

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `.venv\Scripts\python -m pytest tests/test_prepare_all.py -v` then `.venv\Scripts\python -m pytest -m smoke` → PASS.
- [ ] **Step 5:** Commit `feat(build): save learned config, report leftovers, auto-build setting`.

---

### Task 8: Side panel — automatic start, kind and leftovers

**Files:**
- Modify: `ChatbotExtension/sidebar/sidebar.js` (`askToLearn`, `showBuildFor`, build render), `ChatbotExtension/sidebar/sidebar.html` (`#testdata-kind`, `#testdata-leftovers`)
- Test: `tests/js/sidebar_build.test.js` (new, same harness style as `tests/js/sidebar_mcp.test.js`)

**Interfaces:**
- Consumes: `GET /api/build/settings`, `GET/POST /api/targets/{id}/prepare-all` (`kind`, `leftovers`).
- Produces:
  - `askToLearn(target)`: after `prepareGoldens`, if `auto_build` → `startBuild(target)` (question hidden); else the old question path.
  - `showBuildFor(target)`: build not running, `auto_build` on, `!target.config.auto_built`, and any leftover with a non-empty `hint` → `startBuild(target)` once per page load.
  - Render: `#testdata-kind` "Detected: Agent (calls tools)" / "RAG bot (returns sources)" / "Chatbot" / "MCP server"; `#testdata-leftovers` one `<li>` per leftover "`title`: `reason`. `hint`", the N/A ones under a "Does not apply" sub-heading.

- [ ] **Step 1: Failing JS tests**: new target + `auto_build: true` → POST prepare-all sent, question hidden; `auto_build: false` → question shown, no POST; existing target with `auto_built` false and a hinted leftover → one POST; `auto_built: true` → no POST; leftovers rendered with the hint text.
- [ ] **Step 2:** Run `node --test tests/js/sidebar_build.test.js` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `node --test tests/js/` and `.venv\Scripts\python -m pytest` → PASS.
- [ ] **Step 5:** Commit `feat(panel): start the test-data build automatically and show leftovers`.

---

### Task 9: Docs, republish, live check

**Files:**
- Modify: `docs/WORKFLOW.md` (Build all section: auto start, profile step, gaps, verification, budget, leftovers, env vars), `docs/workflow.html` (same + footer date), `CLAUDE.md` (MCP targets and Build all bullet), `README.md` (one paragraph), `.env.example` (commented `JUDGE_AUTO_BUILD`, `JUDGE_BUILD_MAX_MESSAGES`, `JUDGE_BUILD_MAX_JUDGE_CALLS`)

- [ ] **Step 1:** Edit the docs.
- [ ] **Step 2:** Republish: Artifact `action: "read"` on https://claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv, then publish `docs/workflow.html` with that `url`.
- [ ] **Step 3:** `python .claude/hooks/docs_guard.py` → no block.
- [ ] **Step 4: Live check** (spends judge tokens; ask the user first): restart the backend, select `localhost:5173` → one automatic build; then scan with Run mode ≥ 3. Expected: Role Adherence, Tool Correctness, Argument Correctness, Step Efficiency and the 4 Turn RAG metrics score; Plan Quality / Plan Adherence appear as leftovers with the reasoning hint.
- [ ] **Step 5:** Commit `docs: automatic gap-driven test-data build`.
