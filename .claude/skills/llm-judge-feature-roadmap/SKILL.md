---
name: llm-judge-feature-roadmap
description: Use when adding a new feature to the LLM Judge app (Chrome side panel + FastAPI backend) — new DeepEval metrics, A/B or regression comparison, red-team attack enhancers, conversation simulation, synthetic-data evolutions, performance/cost tracking, CI export. Holds the prioritized backlog distilled from Confident AI's evaluation guides and the rules for adding features without breaking the working app.
---

# LLM Judge — feature roadmap and build rules

The app works. Every feature here is **additive**: new spec, new endpoint, new
UI section, new optional setting. Never change the behaviour of an existing
metric, endpoint, payload field or stored column.

Sources this backlog was distilled from (re-read when a feature needs detail):
- https://www.confident-ai.com/docs/llm-evaluation/quickstart
- https://www.confident-ai.com/blog/llm-evaluation-metrics-everything-you-need-for-llm-evaluation
- https://www.confident-ai.com/blog/why-llm-as-a-judge-is-the-best-llm-evaluation-method
- https://www.confident-ai.com/blog/llm-chatbot-evaluation-explained-top-chatbot-evaluation-metrics-and-testing-techniques
- https://www.confident-ai.com/blog/llm-testing-in-2024-top-methods-and-strategies
- https://www.confident-ai.com/blog/the-definitive-guide-to-synthetic-data-generation-using-llms
- https://www.confident-ai.com/blog/red-teaming-llms-a-step-by-step-guide

## 1. Ground rules (read before touching code)

1. Read `CLAUDE.md` and `docs/WORKFLOW.md` first. They are the contract.
2. Work on a new branch off the current one (`feat/<feature-slug>`). One feature per branch.
3. TDD: write the failing offline test first (`tests/`, uses `tests/fakes.py` canned chatbot,
   no judge tokens). `.venv\Scripts\python -m pytest` must be green before and after.
   Run `node --test tests/js/report.test.js` if the PDF report JS changed.
4. Hard constraints from CLAUDE.md that must survive every feature:
   - `metrics_catalog.py` stays the single source of truth for metrics. New metric = new `MetricSpec`
     with `group`, `direction`, `threshold`, `presets()` for all 4 `ENVIRONMENTS`, `kind`,
     `ui_category`, `scale_hint`, `question`, `scores_on`, `criteria`, `needs`, dataset.
   - "lower" metrics go through `deepeval_threshold` / `reported_score`. Never invert scores by hand.
   - No CORS middleware (web app is same-origin), `TrustedHostMiddleware` stays `127.0.0.1`/`localhost`.
   - `sentence_transformers` stays lazily imported behind `generate_goldens_from_document`.
   - `pytest.ini` plugin disables stay.
   - Runs go through `jobs.py` + `runner.run_spec`; failures are `status: "error"` dicts, not exceptions.
   - Runtime JSON (`goldens.json`, `conversations.json`, `security_probes.json`) stays gitignored;
     shipped data goes in the `*.default.json` files.
   - Header values stay masked as `***` in responses.
   - The judge is never the chatbot under test.
5. New stored data → add columns via `storage.RUN_EXTRA_COLUMNS`-style migration (additive, nullable),
   never drop or rename.
6. UI changes must work in the web app (standalone pages and the two-pane `index.html`) and at narrow widths.
7. Keep the metric budget advice: the guides recommend ≤ 5 metrics per use case
   (1–2 custom G-Eval/DAG + 2–3 system metrics). Presets should reflect that.
8. Update `CLAUDE.md` architecture notes and `docs/WORKFLOW.md` when a feature adds an endpoint,
   a dataset, or a spec field.

## 2. What already exists (do not rebuild)

- 25 metric specs in 6 groups: safety, conversational, rag_retrieval, rag_quality, rag_geval, security.
  Includes answer_relevancy, faithfulness, contextual precision/recall/relevancy, hallucination,
  correctness, helpfulness, summarization, citation_quality, bias, toxicity, pii_leakage,
  no_prompt_leak, knowledge_retention, conversation_completeness.
- 12 probe metric types in `security_probes.default.json`: prompt_injection, jailbreak,
  encoded_injection, data_exfiltration, domain_misuse, harmful_content, non_advice, role_violation,
  social_engineering, no_prompt_leak, bias, toxicity. Probe sets: `generic`, `ecommerce`; target `persona`.
- Per-environment threshold presets (local / pr / staging / production).
- Judge stability check (`check_consistency`, `judge_spread`, `UNSTABLE_SPREAD`).
- Per-run judge_model / tokens / calls / target_calls / duration_s.
- Document / URL → goldens via DeepEval `Synthesizer` with `ContextConstructionConfig` only.
- Conversation scenarios with `chatbot_role`; jobs queue; trend + latest charts; PDF report.

## Status (2026-10-21)

F1-F20 are implemented on branch `feat/eval-roadmap` (DeepEval 4.3.1), with the Advanced
tools page (`ChatbotExtension/advanced/`) and production guard rails
(`backend/dashboard/hardening.py`). See CLAUDE.md "Roadmap features" for where each lives.
Use this skill's rules for follow-up work on them.

## 3. Backlog (priority order)

Each item: **why** (source idea) → **where** → **done when**.

### P1 — Metrics gaps (small, high value)

**F1. Role Adherence** (conversational). Score = adherent turns / total turns, judged with prior turns
as context. Uses the scenario's existing `chatbot_role`.
- Where: new `MetricSpec` in `metrics_catalog.py` (`kind="conversation"`, DeepEval `RoleAdherenceMetric`),
  group `conversational`. Skip scenarios with no `chatbot_role` and say so in `note`.
- Done when: appears in `/api/metrics`, runs on shipped conversations, offline test with fake judge.

**F2. Turn / Conversation Relevancy with window size** (conversational). Sliding window of last N turns.
- Where: new spec using DeepEval `TurnRelevancyMetric` (or `ConversationRelevancyMetric` per installed
  DeepEval 4 name — check `deepeval.metrics` first). Expose `window_size` as an optional per-metric
  setting stored like `thresholds` in the web app's `settings` (localStorage), default 10.
- Done when: window size round-trips side panel → `/api/run` → metric.

**F3. Prompt Alignment** (rag_quality). Checks each instruction separately.
- Where: new spec using `PromptAlignmentMetric`; instructions come from a new optional target field
  `instructions: [str]` (side panel textarea, one per line). `needs` = instructions; spec reports
  `cases_available = 0` when the target has none.

**F4. DAG metrics for rule-based checks.** Deterministic decision-tree scoring for explicit criteria
(e.g. "reply must contain order number, then policy link, no competitor names").
- Where: new group `custom`; spec builder accepts a small JSON DAG definition
  (`binary` / `non_binary` judgement nodes, `verdict` leaves with score, optional G-Eval leaf).
  Store user DAGs like conversations (`custom_metrics.default.json` + gitignored edited copy + CRUD at
  `/api/custom-metrics`).
- Done when: one shipped example DAG, CRUD tests, run produces scores identical across two runs with
  the fake judge.

**F5. Custom G-Eval builder in the UI.** User writes name + criteria (+ optional evaluation steps,
strict/binary mode, which fields to score). Same storage as F4.

### P2 — Regression and A/B (core of "LLM testing")

**F6. Run-vs-run regression diff.** Pick two runs of the same metric/theme → per-case table:
score before, after, delta, passed→failed flips highlighted, plus avg / median / distribution.
- Where: `GET /api/runs/compare?a=<run_id>&b=<run_id>` reading `runs.result_json`; new dashboard view.
  Match cases by golden id, fall back to `input` text.
- Done when: flips flagged; works when case sets differ (show added/removed).

**F7. Pairwise A/B judge between two targets** (or two prompt/model versions). Same goldens sent to
target A and B; judge picks the better reply.
- Bias mitigation is mandatory: run both orderings (position swap); declare a winner only if both
  orderings agree, else `tie`. Add a verbosity note when the winner is >1.5× longer.
- Where: `runner.run_pairwise` (new function, reuses `HttpTargetClient`), job type `pairwise` in `jobs.py`,
  `POST /api/jobs` with `{type: "pairwise", target_a, target_b, theme, criteria}`; result = win/loss/tie
  counts + per-case reasons. Store as one row per run like other runs.
- Done when: offline test with two fake targets; swapped-order disagreement → tie.

**F8. Score summary stats.** Add median, min, max, std and a histogram to the latest-run view and the
PDF report (the quickstart's "average, median, distribution"). Pure presentation over existing rows.

### P3 — Red teaming depth

**F9. Attack enhancers.** Turn each baseline probe into variants: Base64, ROT13, leetspeak,
multilingual (judge translates to e.g. Swahili / Welsh / Basque), disguised math, role-play framing,
prompt-injection wrapper. Deterministic encoders are pure Python; translation/framing use the judge.
- Where: `backend/datasets/attack_enhancers.py`; side panel multi-select "Enhance probes with…";
  runner expands probes before sending. Each result row keeps `enhancement` and `base_probe_id`.
  Report pass rate per enhancement (which obfuscation breaks the bot).
- Done when: deterministic encoders unit-tested; enhanced probes never stored back into
  `security_probes.json` unless the user saves them.

**F10. Multi-turn attacks** (linear jailbreak / crescendo). Attacker = judge model generates the next
escalating user turn from the bot's last reply, up to N turns, stop when the per-turn safety check fails.
- Where: new conversation-kind spec in group `security`; reuses conversation plumbing.
  Cap turns (default 5) and show token cost before running.

**F11. Many-shot jailbreak probe generator.** Builds one prompt with K fabricated unsafe Q/A pairs;
K configurable (8 / 32 / 64), respecting `max_message_length` (skip with reason when too long).

**F12. Vulnerability coverage report.** Map every probe metric to OWASP LLM Top 10 ids and the 5 risk
categories (Responsible AI, Illegal Activities, Brand Image, Data Privacy, Unauthorized Access).
Dashboard grid: category × pass rate; keep historical attacks to track regressions.

### P4 — Conversation simulation

**F13. Conversation simulator.** From a scenario + `expected_outcome` + user persona, the judge plays
the user turn by turn against the real target (max turns cap), producing a `ConversationalTestCase`
that the existing conversational metrics score. Option to save a simulated conversation as a scenario.
- Where: `backend/datasets/simulator.py` (DeepEval `ConversationSimulator` if available in the
  installed version, else a small loop). Job type `simulate`.

### P5 — Synthetic data quality

**F14. Synthesizer evolutions and filtration.** Expose DeepEval `EvolutionConfig`
(reasoning, multi-context, concretizing, constrained, comparative, hypothetical, in-breadth; number of
evolutions), `FiltrationConfig` (quality threshold, max retries) and `StylingConfig`
(input format, expected output format, task, scenario) in the document upload form.
- Where: `backend/rag/generate.py` builds the configs from optional request fields; defaults keep
  today's behaviour exactly. Each generated golden records its `evolutions` list.
- Done when: existing document tests still pass unchanged; new test passes configs through
  (patch `generate_goldens_from_document` as the existing tests do).

**F15. Human review queue for generated goldens.** Generated rows land as `status: "draft"`; side panel
lets the user accept / edit / reject expected outputs before they are used in runs.

### P6 — Performance and responsibility testing

**F16. Per-case latency and cost.** Record target reply latency per case (p50 / p95 per run) and judge
cost per run from tokens × configurable price per 1M tokens. Optional latency threshold that fails
the run. Additive columns only.

**F17. Robustness (perturbation) metric.** Send each golden plus 2 paraphrases / typo variants;
score consistency of the answers (judge compares). Flags "non-robust responses to small prompt changes".

**F18. Reference-less hallucination (SelfCheckGPT style).** Ask the same question K times (K=3),
judge checks whether each sentence of reply 1 is supported by the other samples. For goldens with
no context. Warn about K× target calls before running.

### P7 — CI and sharing

**F19. Export as pytest / `deepeval test run` suite.** Button that writes a `tests/evals/` folder for the
user's own repo: goldens JSON, chosen metrics and thresholds for the selected environment, a pytest
file calling the target through the same HTTP config (headers read from env vars, never written out),
and a GitHub Actions workflow template.
- Done when: generated suite runs green against `tests/fakes.py` in an offline test.

**F20. Judge calibration with few-shot examples.** Let the user mark a few results as "human score =
x"; feed them as few-shot examples to G-Eval criteria and show judge–human agreement % over time.

## 4. Build prompt (copy, fill the brackets, paste to Claude Code)

```
Use the llm-judge-feature-roadmap skill. Implement feature [F#: name] from its backlog.

Rules: the app works today, so change nothing existing — additive only. Follow section 1 of the
skill and CLAUDE.md. New branch feat/[slug]. Use TDD: failing offline test first, then the code,
then the full offline suite `.venv\Scripts\python -m pytest` green (plus `node --test
tests/js/report.test.js` if report JS changed). Do not spend judge tokens unless I say so.

Before writing code, show me: files you will add/change, new endpoint/payload shapes, new spec
fields or settings, and how the old behaviour stays the same. Wait for my OK.

After: update CLAUDE.md + docs/WORKFLOW.md, list what you verified, and do not commit until I ask.
```

Suggested order: F1 → F2 → F8 → F6 → F7 → F9 → F14 → F16 → F13 → F19 → rest.
