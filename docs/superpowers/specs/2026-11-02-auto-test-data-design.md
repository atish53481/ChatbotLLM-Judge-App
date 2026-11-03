# Auto test data: every metric runnable from a cURL alone

Date: 2026-11-02 · Status: draft for approval

## Why

In production the app will judge many different targets: plain chatbots, RAG bots,
AI agents with tools, MCP servers. Users usually paste a cURL (or MCP details) and
bring **no golden set**. Today the first scan of such a target shows many metrics as
"Not run" / "Not applicable" because the test data they need does not exist.

Seen on `localhost:5173` (ShopEasy), scan 38: the set held only 2 small-talk goldens
and 2 scenarios typed by hand, so 10 metrics never scored, although the bot returns
both `sources` and `tools_called`:

| Metric(s) | Missing data |
|---|---|
| Role Adherence | no scenario with a `chatbot_role` |
| Tool Correctness | no golden with `expected_tools` |
| Argument Correctness, Plan Quality, Plan Adherence, Step Efficiency | no question that makes the bot call a tool |
| Turn Faithfulness / Contextual Relevancy | scenarios off-topic, so no `sources` and no scenario `context` |
| Turn Contextual Precision / Recall | same, plus no `expected_outcome` |

**Goal:** after a target is saved, the app works out what kind of target it is,
finds which metrics lack data, fills exactly those gaps (config, goldens,
conversation scenarios), using help/FAQ pages and uploaded reference docs, and
tells the user what it still could not fill and why. Success = the first scan of
any target runs every metric that target can support; "Not run" remains only for
what the target itself does not expose (e.g. no tool reasoning for Plan Quality).

## Goals

1. **Automatic**: the build starts by itself right after a chatbot is saved (user
   decision: no "Build test data?" question). The Build all test data button stays
   for re-runs.
2. **Profile the target**: plain / RAG / agent / MCP, decided from the cURL config
   and a few real messages.
3. **Gap-driven**: each step checks what is missing *per metric* and writes only that;
   re-running after the user deletes data fills it again. No step is skipped just
   because it ran once before.
4. **Config filled in**: paths found in replies, tool list learned from replies,
   persona / role inferred from the bot's own answers (never over a typed value).
5. **Data that actually triggers the metric**: tool and RAG test cases are sent to the
   bot once during the build and kept only if the bot really called a tool / returned
   sources (or the case carries its own context).
6. **Reference material**: help/FAQ pages found automatically (exists), the target's
   `help_url`, and documents already uploaded to the target's theme.
7. **Honest leftovers**: a list of metrics still without data, each with a one-line
   reason and what to add.

## Non-goals

- Changing how metrics score or how scans run.
- Changing the chatbot under test (e.g. adding `reasoning` to ShopEasy's tool calls is
  a separate change in `01_Chatbot_Shopeasy_chatbot`).
- A new UI page. Progress stays in the side panel's existing build box.

## Anti-goals (would be a failure even if it "works")

- Overwriting anything the user typed (paths, persona, instructions, goldens).
- Unbounded spend: the build must cap messages to the target and judge calls.
- Silent invention presented as fact: judge-written data without a source document is
  marked as such.
- Breaking the offline test suite or existing build / prepare-goldens behaviour.

## Decisions taken

- **Auto-start without asking** (user).
- **No material found → the judge still writes plausible data** from the bot's own
  interview answers, marked unverified, and the leftovers list says that adding a
  help URL or doc would make it better (my call; it keeps "every metric runs" as the
  priority).
- Build on `backend/datasets/prepare_all.py` + `app._run_build`; no second pipeline.

## Coverage: every target type, every metric group

User requirement: works for **any** chatbot, RAG bot, AI agent or MCP server, across
**all** metrics (44 app metrics in 9 groups + 32 DeepTeam), with evaluation-grade data.
The build reads this matrix: for the profiled kind it fills every "needs" cell; a
cell marked N/A is reported as "does not apply to this target", never as a gap.

| Group (metrics) | Data each needs | Chat | RAG | Agent | MCP |
|---|---|---|---|---|---|
| rag_quality (faithfulness, answer relevancy, hallucination, correctness, summarization, prompt alignment, robustness, self-check) | goldens with grounded `expected_answer`; `context` for faithfulness/hallucination when bot returns none; instructions (resolve fallback exists); a long source text for summarization | goldens + Context goldens from material | same, bot's own sources used first | same | same (agent model answers) |
| rag_retrieval (contextual precision / recall / relevancy) | bot's `retrieval_context` (grades the retriever) + `expected_answer` | N/A (no retriever) | goldens on topics material covers, verified to return sources | if it returns sources, as RAG; else N/A | as Agent |
| rag_geval (citation quality, helpfulness) | goldens; context for citations | goldens | goldens + sources | goldens | goldens |
| reference (exact match, ROUGE-L, BLEU-1) | short, single-fact `expected_answer` phrased as an answer | ≥ 3 short-answer goldens | same | same | same |
| safety (bias, toxicity, PII, prompt leak) | adversarial probes fitted to the domain | fitted probes (exists) | same | same + tool-aware rubric (exists) | same |
| security + DeepTeam (incl. multi-turn jailbreak) | fitted probes; persona for context | fitted probes | same | same; probes that try to misuse tools | same |
| conversational (completeness, knowledge retention, role adherence, turn relevancy) | ≥ 3 scenarios, 2–4 turns, `chatbot_role`, `expected_outcome`, one turn recalling a fact from an earlier turn | scenarios | same | scenarios include tool tasks | same |
| conversational turn RAG (4) | per-turn sources or scenario `context`; `expected_outcome` (precision/recall) | scenario `context` from material | on-topic turns, verified sources | as RAG if sources, else scenario context | as Agent |
| agent (task completion, tool correctness, argument correctness, plan quality, plan adherence, step efficiency) | goldens naming `expected_tools`, verified to trigger a tool call; plan text in tool calls for plan metrics | task completion only (reply-based); rest N/A | as Chat | ≥ 2 verified goldens per tool | tools from `list_tools()`; agent model's choice gives the calls; plan from its reasoning |
| custom (user G-Eval / DAG) | goldens or scenarios per its `kind` | covered by the above | | | |

Minimum per metric: `CASES_MIN` (3) applicable cases, so a scan with "Run mode" ≥ 3
scores every metric on real cases, not one.

## Quality bar ("excellent evaluation")

- **No circular answers**: an `expected_answer` comes from the material (pages, docs,
  instructions) or tool definitions, never copied from the target's own reply. The
  reply only *verifies* that a case triggers tools / sources.
- **Grounded**: each written golden is checked by the judge against its material
  (supported / not); unsupported ones are dropped. No material → `auto_unverified`.
- **Diverse**: each batch mixes happy path, edge case, out-of-scope, multi-intent and
  follow-up questions; near-duplicates (same normalised question, or ≥ 0.9 token
  overlap) are dropped, also against the existing set.
- **Fits the metric**: short single-fact answers for reference metrics; long passages
  for summarization; knowledge-retention scenarios state a fact early and recall it.
- **Valid for the target**: each sent case that errors, is too long
  (`max_message_length`) or gets a service message (`reply_check`) is dropped.
- **Independent judge**: build refuses when judge and target are the same model
  (existing rule).
- **Inspectable**: every auto row keeps `source` and, where known, `source_document`,
  so users can review/delete in the panel.

## Design

### Flow

```
save chatbot (panel) ──► POST /prepare-all {generate:true}   (automatic now)
                            │
   0. profile   ── interview + probe messages; save found paths, learned tools, persona
   1. goldens   ── general goldens if < GOLDENS_MIN
   2. scenarios ── fill to SCENARIOS_MIN; repair missing chatbot_role / expected_outcome;
                   RAG targets: on-topic turns, verified to return sources, else scenario context
   3. context   ── Context goldens from pages + uploaded docs (exists)
   4. tools     ── tool-triggering goldens with expected_tools, verified against the bot
   5. probes    ── fitted probes (exists)
   6. leftovers ── metrics still without data, reason + hint
```

### 0. Profile (new step `profile`)

- Reuse `autogen.interview(client)` answers (already sent once per build).
- While sending, collect what replies contain through `HttpTargetClient.discovered`
  (exists for scans) and save found `tools_path` / `context_path` with
  `app._save_found_paths` (never over a typed path).
- **Learn tools from replies**: every `tools_called` entry seen during the build adds
  `{name, parameters: arg keys seen}` to `config.available_tools` when the name is not
  already there. Discovery-approved tools keep priority. MCP keeps `list_tools()`.
- If the interview showed no tools but the config has `tools_path`, send up to
  `PROBE_TOOL_MESSAGES` (3) judge-written messages likely to need an action
  ("check my order ORD-1001", built from interview + pages) to surface tools.
- **Persona**: when `config.persona` is empty, the judge writes one sentence from the
  interview ("ShopSphere support bot: orders, refunds, shipping, products") and it is
  saved as `persona` (feeds probes and scenario roles).
- Target kind shown in the panel: `agent` if tools known, `rag` if `context_path`
  found or sources seen, `mcp` by type, else `chat`.

### 1–2. Goldens and scenarios (gap-driven)

Replace "skip if any `auto_chatbot` golden exists" with counts:

- General goldens: write until the theme has `GOLDENS_MIN` (5) non-starter goldens.
- Scenarios: write until `SCENARIOS_MIN` (3). Then **repair** every scenario of the
  theme (auto-built and typed alike, only empty fields):
  - empty `chatbot_role` → the persona;
  - empty `expected_outcome` → judge writes it from the turns + material.
- RAG targets (`context_path` known): scenarios are written on topics the material
  covers. Each new scenario's first turn is sent once; if the reply has no sources
  the scenario gets a `context` (≤ 3 chunks) from the pages/docs that cover it, so
  Turn RAG metrics still have context. Non-RAG targets get `context` the same way
  when material exists.

### 4. Tool goldens (gap-driven + verified)

- Gap: for each known tool, fewer than `TOOL_GOLDENS_PER_TOOL` (2) goldens naming it.
- `write_tool_goldens` (exists) writes candidates. Each is **sent once**; kept when
  the bot called at least one tool. `expected_tools` stays the judge's intended tool
  (that is what Tool Correctness checks); a candidate that called no tool is dropped
  and one more is tried, up to `TOOL_ATTEMPTS` (2) per tool.
- These goldens give Argument Correctness / Plan / Step Efficiency their cases too.

### Reference material

`_material` = help pages found (exists) + `help_url` + text of documents already
`ready` in the theme (`storage.list_documents`, clipped by `PROMPT_TEXT_CHARS`) +
typed / inferred instructions. Goldens written from it keep `source: auto_chatbot`;
those written with no material get `source: auto_unverified` (counted as auto data,
shown with an "unverified" tag in the panel's goldens list).

### 6. Leftovers

`_build_view` gains `leftovers: [{key, title, reason, hint}]`: `_metrics_summary`'s
missing list plus predicted case-level gaps, e.g.:

- Plan Quality / Adherence: tool calls carry no `reasoning` → "ask the bot's team to
  return the agent's plan or reasoning with each tool call".
- Tool metrics: no tools seen → "set Tool calls path, or run Find missing metric data".
- Turn RAG: no sources and no material → "add a Help/FAQ URL or upload a document".

The panel shows these under the build's step list.

### Budget and safety

- Caps per build: `BUILD_MAX_TARGET_MESSAGES` (default 30), `BUILD_MAX_JUDGE_CALLS`
  (default 40); hitting one ends the remaining steps as `skipped` with the reason.
- Respects `send_delay`, Stop (`backend/cancel.py`), `max_message_length`.
- `JUDGE_AUTO_BUILD=0` turns the auto-start off (panel then shows the old question).
- Caps are env-tunable (`JUDGE_BUILD_MAX_MESSAGES`, `JUDGE_BUILD_MAX_JUDGE_CALLS`).
- **Existing chatbots**: the first time one is selected after this ships, if its
  leftovers list is non-empty the build runs once for it automatically (flag
  `auto_built` on the target config stops repeats; Build all test data re-runs).
- Tests run with fakes only (`tests/fakes.py`, fake judge); no tokens.

### Files touched (expected)

- `backend/datasets/prepare_all.py`: new `profile` step, gap counts, scenario repair,
  verification sends, leftovers.
- `backend/datasets/autogen.py`: persona prompt, scenario-repair prompt, tool-probe prompt.
- `backend/dashboard/app.py`: `STEPS` incl. profile, saving learned tools/persona/paths
  after the build, `leftovers` in `_build_view`, `JUDGE_AUTO_BUILD` exposed to the panel.
- `ChatbotExtension/sidebar/sidebar.js` + `.html`: start the build right after save,
  show target kind and leftovers.
- Tests: `tests/test_prepare_all.py` (gaps, repair, verification, caps), panel JS test.
- Docs: `docs/WORKFLOW.md`, `docs/workflow.html` (+ republish), `CLAUDE.md`, `README.md`.

## Left to the builder

- Exact prompt wording; the constant values above are starting points.
- How the tool-probe messages are phrased per domain.
- Whether verification sends for scenarios and tool goldens share one budget counter
  or two.
