# Future work: Evaluate mode, production monitoring, more metrics

Status: **parked**. Agreed in conversation on 2026-10-26; not built yet. When picking
this up, read CLAUDE.md first and follow the `llm-judge-feature-roadmap` skill rules
(additive only, TDD, offline tests, new branch).

Suggested order: **A → B (P1 → P2 → P3) → C**. D is a separate, larger project.

---

## Background: `.measure()` vs `evaluate()` (why the app works the way it does)

Both come from DeepEval (github.com/confident-ai/deepeval, installed as `deepeval`
4.3.1 from PyPI). Scores are identical either way: `evaluate()` calls each metric's
`.measure()` / `a_measure()` internally, with the same judge.

| | `metric.measure(case)` | `deepeval.evaluate(test_cases, metrics)` |
|---|---|---|
| Scope | 1 test case x 1 metric | all cases x all metrics |
| Returns | score / reason / success on the metric object | `EvaluationResult` with every case |
| Concurrency | whatever the caller builds | built in (`AsyncConfig`, max_concurrent 20 by default) |
| Extras | none | console table, local cache, `.deepeval` files, Confident AI upload when logged in |
| Replies | the caller gets them however it likes | must already be in the test cases |

Where the app uses DeepEval's runners today:

- `backend/dashboard/runner.py` (`run_spec`, line ~424): `metric.measure(case)` per case.
  The chatbot is asked inside the loop, so progress is live, Stop works between calls,
  rows are stored as they finish, and `JUDGE_CONCURRENCY` / Parallel metrics /
  `send_delay` / not-applicable skips all apply.
- `backend/export_suite.py`: the exported pytest suite uses `assert_test` (for the user's CI;
  the app does not run it).
- `evaluate()`: **not used anywhere**.

Answer Relevancy, for reference: spec in `backend/metrics_catalog.py` (`SPEC_ANSWER_RELEVANCY`);
DeepEval's `measure` in `.venv/Lib/site-packages/deepeval/metrics/answer_relevancy/answer_relevancy.py`
(statements -> verdicts -> score -> reason, 3 judge calls); score formula
`deepeval/metrics/utils/qag.py` `score_qag_verdicts` = relevant statements / all statements
(yes and idk count as relevant; strict mode clamps below-threshold to 0).
`async_mode=True` runs the same three calls via `a_measure` with `await`; they depend on
each other, so for this metric it changes nothing. Our judge's `a_generate` routes to the
sync path (`backend/judges/judge.py`), keeping retries, timeout, token counting and Stop.

How larger companies usually evaluate LLM apps (common patterns, not a standard):

1. CI gate on every PR (pytest + `assert_test` / `deepeval test run` on a small golden set).
2. Offline regression runs before releases, results on a dashboard (Confident AI,
   LangSmith, Braintrust or in-house) to compare versions.
3. An in-house harness built on `.measure()` for control over rate limits, cost and storage.
4. Production monitoring: tracing live traffic and scoring a sample (online evals) with alerts.
5. Human review samples to calibrate the judge, plus scheduled red teaming.

This app covers 1, 2, 3 and 5. **4 is the gap** (part B below).

---

## A. Two judging modes, chosen per run

New setting **Judging mode**, saved like "Run mode" and sent with each job as a job option
(e.g. `engine: "metrics" | "evaluate"`, default `"metrics"`, so old jobs behave as today).

| | **Metrics mode** (today, default) | **Evaluate mode** (new) |
|---|---|---|
| How | runner asks the chatbot, then `metric.measure(case)` per case | phase 1: ask the chatbot every question; phase 2: one `deepeval.evaluate(test_cases, metrics)` per group of metrics sharing a dataset |
| Concurrency | `JUDGE_CONCURRENCY` threads + Parallel metrics | DeepEval `AsyncConfig(run_async=True, max_concurrent=JUDGE_CONCURRENCY)` |
| Progress / Stop | live per case; Stop between any two calls | per phase; Stop between phases, judge calls still cancel via our judge wrapper |
| Good for | day-to-day runs | matching a plain DeepEval script exactly; DeepEval's own async scheduling |

Rules for Evaluate mode:

- Convert `EvaluationResult` back into the app's normal result rows (`input`, `actual_output`,
  `expected_output`, `context`, `context_source`, `score`, `passed`, `reason`, `golden_id`,
  `latency_s`, ...) and store one aggregated run per metric as today, so the runs page, PDF
  report, Compare, history and the Confident AI upload keep working unchanged.
- Same judge (`GroqJudge`): retries, 90 s timeout, token counting, `RunCancelled`.
- No side effects: `DisplayConfig(show_indicator=False, print_results=False)`,
  `CacheConfig(write_cache=False, use_cache=False)`, `ErrorConfig(ignore_errors=True)` (map
  errors to error rows), never open a browser. **DeepEval must not upload on its own** even if
  `CONFIDENT_API_KEY` is in `.env`: uploads stay with `backend/confident_upload.py`.
- Fallback to Metrics mode inside the same run for what `evaluate()` cannot do: attacks
  (Multi-turn Jailbreak), multi-sample (Robustness, Self-Check), the judge consistency check,
  per-case `skip_case` / `not_applicable`. The run's `note` names them.
- Shared replies: reuse `targets/shared.SharedReplies` so each question is still sent once.
- Tests: offline, fake judge, assert both modes give the same scores and rows for the same
  goldens; Stop between phases; fallback metrics noted.
- UI: side panel select next to "Run mode"; Hyperparameters card on the runs Overview shows it.

---

## B. Production monitoring (score real conversations)

```
 Your live chatbot                       LLM Judge app
 user <-> bot reply --(1) send trace--> POST /api/traces --> SQLite: traces
                     or JSONL export --> "Import logs" ------^         |
                                          (2) sampler picks e.g. 5% + every thumbs-down
                                          (3) monitor job on the existing JobQueue,
                                              metric.measure() on each picked trace
                                                                        |
                                          SQLite: trace_scores --> (4) Monitoring page
                                                                   (5) alerts
                                                                   (6) "Add to golden set"
```

**Open question (decides P1's shape): does the real chatbot run on the user's own PC
(can call 127.0.0.1:8765) or on a server?** The app only accepts `127.0.0.1`/`localhost`
(TrustedHostMiddleware, protects stored keys), so a server-hosted bot starts with log import;
exposing the app to a network needs authentication first (separate project).

1. **Intake**
   - Push: `POST /api/traces` `{target_id, conversation_id, turns: [{role, content,
     retrieval_context?, tools_called?}], feedback?, latency_ms?, at}`; `conversation_id`
     makes re-sends idempotent.
   - Import: JSONL / CSV chat-log upload for bots whose code cannot change.
   - Ingest token header `X-Judge-Ingest-Token`, required on `/api/traces`, separate from
     everything else; rate limit on ingest; existing body-size limit applies.
   - Privacy: mask emails, phone numbers, card numbers before storing; delete traces older
     than N days (default 30).
2. **Sampling** (cost control): random X% (default 5%); always score thumbs-down, slow replies
   and user keywords (e.g. "refund", "cancel"); daily judge-token budget stops scoring.
3. **Scoring**: reference-free metrics only (live traffic has no expected answer).
   - Works: Answer Relevancy, Helpfulness, Toxicity, Bias, PII Leakage, Prompt Alignment;
     Faithfulness / Contextual Relevancy when the trace carries retrieval context; Role
     Adherence, Conversation Completeness, Turn Relevancy for multi-turn.
   - Does not: Correctness, BLEU, ROUGE-L, Exact Match, Contextual Precision / Recall,
     Tool Correctness (need goldens / expected tools).
   - Reuse `metrics_catalog` specs (a trace stands in for golden + reply), same `.measure()`,
     new job kind `monitor` on the JobQueue (Stop, limits, token counting apply); a scheduler
     queues a monitor job every N minutes.
4. **Monitoring page** (new view in `runs.html`): per-metric hourly/daily average and failure
   rate with the existing date-range buttons; "worst conversations" with full transcript and
   judge reasons; live vs golden-set comparison.
5. **Alerts** (opt-in): rules like "Faithfulness 24h avg < 0.8" or "Toxicity failures > 2% in
   1h"; browser Notification (already used) and optional Slack/webhook URL.
6. **Feedback loop**: "Add to golden set" turns a failing conversation into a golden (user
   writes the right answer), so real failures become regression tests for the CI gate.
   Optionally send scored traces to Confident AI (existing upload code).

Phases:

| Phase | Contents | Size |
|---|---|---|
| P1 | `traces` table, `POST /api/traces`, JSONL import, PII masking, ingest token, manual "score these" | medium |
| P2 | sampler, scheduler, budget, Monitoring page | medium |
| P3 | alerts, "Add to golden set", Confident AI trace upload | small-medium |

Production-ready checklist for B: idempotent ingest, ingest token, rate limit, PII masking,
retention job, retry/backoff, health check covers the scheduler, structured logs, docs
(CLAUDE.md + docs/WORKFLOW.md), full offline tests.

Wider production gaps of the app (out of scope unless asked): no login / multi-user;
chatbot and judge keys stored unencrypted in SQLite; no HTTPS or Docker; no automatic SQLite
backup.

---

## C. More DeepEval metrics (proposed 2026-10-25, deferred)

Checked against DeepEval 4.3.1. Each is a new `MetricSpec` in `metrics_catalog.py`
(thresholds per environment, card copy, advice), tests first.

| Metric | How | Needs |
|---|---|---|
| JSON Correctness | DeepEval `JsonCorrectnessMetric` (score is schema validation, judge only for the reason); adapt a JSON Schema (jsonschema is installed) to its `expected_schema` | new optional chatbot field "Expected JSON schema"; unavailable without it |
| RAGAS | DeepEval's RAGAS needs the `ragas` + langchain packages (not installed), so build our own: average of Answer Relevancy, Faithfulness, Contextual Precision, Contextual Recall on the same case; reason lists the four | goldens with context; about 4x judge tokens |
| Goal Accuracy | DeepEval `GoalAccuracyMetric`, conversation kind | nothing new |
| Tool Use | DeepEval `ToolUseMetric(available_tools=...)`, conversation kind; turns need `tools_called` | "Tool calls path" + new field "Available tools" (`name: description` per line) |
| MCP Use | DeepEval `MCPUseMetric`; tool calls read as MCP calls (falls back to `tools_called`) | "Tool calls path" + new field "MCP server name" |
| MCP Task Completion, Multi-Turn MCP Use | DeepEval metrics on conversation scenarios (`mcp_servers` on the test case) | same as MCP Use |

Not in the list: JevEval (System One model metric) - skipped.

**Levenshtein Similarity (removed 2026-10-26, may come back).** It was a no-judge
reference metric (1 − character edits / longer text length, app's own code in
`backend/text_match.py`). Removed so every metric in the app is DeepEval's: DeepEval
4.3.1 has no Levenshtein scorer (`deepeval.scorer.Scorer` has exact match, ROUGE, BLEU,
BERTScore). Bring it back only if DeepEval adds one, or as a clearly labelled
"app's own" metric: spec key `levenshtein`, group `reference`, threshold 0.4, env column
`text_match`; the old code is in git history (commit 83b1a1b, `levenshtein_similarity`).
Old runs with key `levenshtein` are still stored and shown in History.

**Golden sets in Confident AI (idea, decide later).** The app does not use DeepEval's
`EvaluationDataset`: goldens live in `goldens.json` (`backend/datasets/goldens.py`), scenarios
in `conversations.json`, probes in `security_probes.json`; DeepEval's `Synthesizer` only
generates them. Optional addition: push a theme's goldens with
`EvaluationDataset(goldens=[Golden(input, expected_output, context), ...]).push(alias=<theme>)`
(project key from the Confident AI settings), like the test-run upload; maybe `pull` later.

## D. Multimodal and voice metrics (separate, larger project)

Text to Image, Image Editing, Image Coherence, Image Helpfulness, Image Reference and the
voice-agent metrics need the app to send and receive images / audio first (today the
connector only handles text). Scope it as its own design before any metric work.

## E. Langfuse tracing (requested 2026-10-27, parked)

Today nothing in the app or in ShopSphere uses a tracing service (no Langfuse, LangSmith,
LangGraph; `langchain-community` is only DeepEval's document loader). Tracing is the app's own
`traffic.py` log and `usage.py` meter. The user asked to keep Langfuse for a future build.

Why Langfuse: open source and self-hostable (Docker), not tied to a framework (plain
OpenAI/Groq SDK, which is what ShopSphere uses), traces every step (LLM call, tool call,
retrieval) with tokens, cost and latency.

1. **ShopSphere (sample chatbot)**: trace each `/chat` as one trace: retrieval, every tool-loop
   round, each LLM call and tool run, as child spans (Langfuse `@observe` / OpenAI wrapper).
   Optional: on only when `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` are in
   `01_chatbot/.env` (placeholders in `.env.sample`, never real keys); nothing changes without them.
2. **Judge app, as a trace source for B**: import Langfuse traces (Langfuse public API) as an
   intake next to `POST /api/traces` and JSONL import, so real conversations, with their
   retrieval context and tool calls, are scored by the monitor job. Mapping: trace input/output
   → turns, retrieval span output → `retrieval_context`, tool spans → `tools_called`.
3. **Optional, later**: write the judge's scores back to the Langfuse trace (Langfuse scores
   API), so the bot's team sees DeepEval scores next to each trace.

Rules: additive only, off by default, no key ever logged or exported (same as header values),
offline tests fake the Langfuse client (no network). Decide self-hosted vs cloud with the user
before building.
