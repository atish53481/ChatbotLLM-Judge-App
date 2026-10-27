# Application workflow

How a judged answer travels from a click in the side panel to a score on the chart.

## Components

| Piece | Path | Role |
|---|---|---|
| Web app | `ChatbotExtension/index.html` | Served at `http://127.0.0.1:8765/`: side panel and dashboard side by side. |
| Side panel | `ChatbotExtension/sidebar/` | Primary UI: manage chatbots and goldens, run a metric, see the latest scores. |
| Dashboard | `ChatbotExtension/dashboard/` | Chat with the chatbot; latest / trend / case-by-case views; History of every stored run. |
| Backend | `backend/dashboard/app.py` | FastAPI control plane on `http://127.0.0.1:8765`. |
| Judge | `backend/judges/judge.py` | The scoring model — never the chatbot under test. |
| Metrics catalog | `backend/metrics_catalog.py` | The 25 metrics in 6 groups (incl. 9 Security red-team metrics): direction (≥ / ≤), default threshold, environment presets, what each scores on. |
| Storage | `backend/storage.py` | SQLite (`judge.db`): targets, run history, documents, judge settings. |
| Goldens | `backend/datasets/goldens.json` | Question / expected-answer sets by theme. |

```
 side panel ──┐
              ├── fetch ──► FastAPI (127.0.0.1:8765) ──► judge model (remote API)
 dashboard ───┘                    │
                                   └──► SQLite judge.db (targets, runs)
```

## 1. Startup

### Starting the backend

Both scripts start the same server (uvicorn, `backend.dashboard.app:app` on
`127.0.0.1:8765`). They differ in who runs them and what happens when it stops.

| | `run-backend.bat` (manual) | `run-backend-service.bat` (always-on) |
|---|---|---|
| Run by | You: double-click, or `& '.\run-backend.bat'` in PowerShell | The "LLM Judge Backend" scheduled task, at boot. Not meant to run by hand |
| Window / output | Console window | None; `logs\backend.log` (older log kept as `backend.old.log` past ~5 MB) |
| First run | Creates `.venv` and installs `requirements.txt` | Needs `.venv`; logs ".venv is missing" and waits |
| Port 8765 busy | Stops an older LLM Judge backend so new code loads; other programs are left alone | Waits and checks again every 30 s |
| Crash / stop | Prints the error and pauses | Starts it again after 30 s, forever |
| Stop it | Ctrl+C | `uninstall-backend-task.ps1` (or Task Scheduler) |

Run by hand, `run-backend-service.bat` prints nothing and never returns, so the
terminal looks frozen: stop it with Ctrl+C, then `Y`.

```
 first time ever:  run-backend.bat ── creates .venv ──► backend up (window)
                                                              │
        ┌─────────────────────────────────────────────────────┘
        ▼
 working on code?  ── yes ──► run-backend.bat each session (restarts on new code)
        │
        no, want it always on
        ▼
 install-backend-task.ps1 (admin PowerShell, once)
   ├─ stops any backend already running from this folder
   ├─ registers "LLM Judge Backend": at startup, runs while logged off
   │     └─► cmd /c run-backend-service.bat ──► loop: start uvicorn,
   │                                            restart 30 s after it stops
   └─ waits for /api/status to answer
        │
        ▼
 back to manual:  uninstall-backend-task.ps1 (admin) ──► task removed, backend stopped
```

Task setup, from the project folder in PowerShell opened with "Run as administrator":

```powershell
powershell -ExecutionPolicy Bypass -File .\install-backend-task.ps1    # asks for your Windows password
powershell -ExecutionPolicy Bypass -File .\uninstall-backend-task.ps1  # removes it again
```

With the task installed, `run-backend.bat` still works: it takes over port 8765,
and the service loop waits until the port is free again. Nothing runs while the
laptop sleeps or is shut down.

### What happens on start

1. The start script sets `DEEPEVAL_TELEMETRY_OPT_OUT`, `PYTHONUNBUFFERED` and
   `PYTHONUTF8`, then starts uvicorn from `.venv`. `.env` supplies `JUDGE_API_KEY`
   (and optional `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_DB_PATH`).
2. On import, `backend/dashboard/app.py` opens `judge.db` and creates the tables.
   `storage.init_db` also migrates older HTTP chatbots to the cURL-template config and
   drops retired sample (`mock`) and web-page (`dom`) chatbots.
3. The browser opens `http://127.0.0.1:8765/`, which serves the web app from
   `ChatbotExtension/` at `/app/`. On load the side panel resets the goldens, calls
   `POST /api/session/start` (scores start fresh; older runs stay in History) and
   `/api/status`, which reports the judge model name and whether a key is configured.

## 2. Adding a chatbot (target)

The only connector is a request captured from the chatbot's own website
(`backend/targets/http_client.py`). The side panel:

1. `POST /api/targets/parse-curl {curl, sample_message?, probe?}` — runs when the user
   pastes. `targets/curl.py` parses a *Copy as cURL (bash)* command into
   `{url, method, headers, body_template}`. `prepare_body` / `prepare_url` find the
   question (`sample_message` if given; else by key name — `message`, `prompt`,
   `query`, … — the last user turn of a `messages` list, or the longest text) and swap
   it for `{{message}}`, emptying `history`-style arrays and cutting `messages` to
   system + last user turn. With `probe` (default on) the captured question is sent
   once and `find_reply_path` / `find_stream_reply_path` locate the reply; the result
   carries `sample_message`, `response_path`, `reply_preview` and `probe_error`.
2. `POST /api/targets/test {config, message, target_id?}` — sends one question with
   the unsaved config and returns `{ok, reply}` or `{ok: false, error}` (the error
   carries the HTTP status and a preview of the response, to help pick the reply path).
3. `POST /api/targets` / `PUT /api/targets/{id}` with
   `{name, type: "http", config: {url, method, headers, body_template, response_path, theme}}`.

At chat time `{{message}}` is filled JSON-escaped into the body (URL-encoded for a
form body, percent-encoded in the URL). `Content-Length`, `Host` and
`Accept-Encoding` from the capture are dropped. The reply is read at `response_path`
from JSON, joined across `data:` events for a `text/event-stream` reply, or taken as
the whole text when the path is empty. Golden questions are asked on their own;
multi-turn scenarios send the conversation so far into the body's history slot
(`history_path`, e.g. `history` or `messages`).

The backend builds the client once at create time, so a broken config is rejected
with a 400 before it is stored. Responses mask secret header values as `***`; an edit that sends no `headers`
keeps the stored ones.

## 3. Running one metric

`POST /api/run {target_id, metric_key, threshold?, run_id?, limit?}` → `backend/dashboard/runner.py:run_spec`
(`limit` = the dashboard's **Cases per run**; the target's `persona` is passed along for the Security probes):

1. `spec.cases(theme)` loads the chatbot's golden set from `goldens.json`
   (`goldens_with_context` keeps only rows that carry context).
2. The judge is built from the environment (`build_judge`); without a key the route
   returns 503.
3. For each golden: `target.chat(question)` gets the chatbot's reply, the metric
   measures `spec.build_case(golden, reply)`, and the case's score, pass flag and
   reason are collected.
4. The run's **average** score is written as **one** row in `runs`
   (`storage.record_run`) — the charts plot run averages, not cases. As in DeepEval,
   each case passes on `metric.is_successful()` (≥ the threshold for most metrics, ≤ for
   violation rates) and the run passes only when every case passes (`cases_passed`,
   `pass_rate` in the result); the average does not decide the result.
   Each case row carries `input`, `actual_output`, `expected_output`, `context`
   (with `context_source`: chatbot or golden), `score`, `passed` and `reason`.
5. The response carries `status`, `score`, `threshold`, `reason`, and per-case
   `rows` for the case table.

Failures are returned as `status: "error"` with a message (broken dataset, empty
dataset, target/judge exception) rather than raising.

### Metrics that switch on at run time

Some metrics only make sense when the chatbot reports more than its reply. Each spec's
`applies(features, items)` decides, from the target (`context_path` / `tools_path` set)
and the cases, whether it can run; `/api/metrics` returns the reason as `unavailable`
(`cases_available` 0) and the dashboard shows the card as **N/A** with that reason, last
in the grid. Per case, `skip_before` / `skip_case` (and `not_applicable` after scoring)
leave cases with nothing to score out of the average; the result counts them in
`cases_not_applicable` and says so in `note`. If every case is left out, the run is an
error that records nothing.

- **Turn Faithfulness / Contextual Precision / Recall / Relevancy** (conversational):
  each reply's own retrieved context (target `context_path`), else the scenario's
  optional `context`; scenarios with neither are skipped.
- **Agent** (`ChatReply.tools_called`, read at the target's `tools_path`, OpenAI
  `tool_calls` or plain `{name, args, output, reasoning}` lists): Task Completion always
  runs; Tool Correctness needs goldens with `expected_tools`; Argument Correctness,
  Plan Quality / Adherence and Step Efficiency score only replies that called tools
  (plan/step metrics get a trace built from the calls; a trace without a plan is N/A,
  not a free pass). Pasting a cURL fills `tools_path` when the probe reply has tool calls.
- **Text match** (BLEU, ROUGE-L, Exact Match; `backend/text_match.py`):
  no judge, no tokens; compare the reply's wording with the golden's expected answer.
  Exact Match (quasi exact match), ROUGE-L (stemmed) and BLEU (BLEU-1, DeepEval's default)
  are DeepEval's `Scorer` (`rouge-score`; `nltk` with its `punkt_tab` data, downloaded once
  into `.venv/nltk_data` on the first BLEU run; offline without it, BLEU cases error with
  the download command). Levenshtein was removed: DeepEval has no such scorer (old runs
  of it stay in History).

#### Paths found during scans (auto-discovery)

A pasted cURL only shows one reply, which may have called no tools or cited nothing.
So every scan keeps looking: `HttpTargetClient` checks each JSON reply and each stream
event with the same detection as the cURL parser (`find_tools_path` /
`find_context_path`; a tool list counts only when an entry has a tool name) and keeps
what it finds for a path the chatbot has none saved for in `discovered`. Streams are
also read at a saved `tools_path` / `context_path` now (first event that has some).
After each metric of a job, `app._save_found_paths` saves those paths on the chatbot
(never replacing one the user typed) and notes which metrics they switched on
(unavailable before, runnable with cases after: `app._availability`, the same check as
`/api/metrics`). When the job ends, `JobQueue(after_job=)` → `app._after_job` stores
the paths in `jobs.discovered` and queues one follow-up scan of those metrics with the
same options plus `auto_discovered: {from_job, paths}`. No loop: a saved path is never
found again, and a scan that finds nothing queues nothing. Scans carry a `note` on both
sides (Scan history ⓘ, scan page). A/B (pairwise) jobs and ad-hoc judging are not watched.

#### Sources the app finds itself (`sources_fallback`)

Many chatbots don't return their sources. A target's optional `sources_fallback`
(side panel "Sources when the reply lists none") makes the app find context for a reply
whose response had none at `context_path` (`backend/rag/derived_sources.py`):

- `knowledge_base`: the 3 passages of the theme's uploaded documents
  (`backend/datasets/documents/<theme>/`, a document's `_enhanced.txt` in place of the
  original) that best match the question and reply. BM25 keyword ranking, no embeddings.
- `cited_links`: up to 3 `http(s)` links in the reply, fetched as text (public hosts only:
  every resolved address must be global, redirects checked hop by hop, 5 MB cap, 4000
  chars per page).

This context only feeds metrics that read `retrieval_context` and don't grade the
retriever (Faithfulness, Citation Quality, Turn Faithfulness, …).
`MetricSpec.grades_retriever` (Contextual Precision / Recall / Relevancy and their Turn
versions) keeps them on the chatbot's own context or the golden's, since this context
would grade the app's search instead. `runner.target_features(target, spec)` and
`/api/metrics` count a fallback as `context_path` for the other metrics, so they switch
on. Rows say `context_source` `"knowledge_base"` or `"cited_pages"`, and `note` says the
context is not the chatbot's own retrieval. When nothing is found, the golden's context
is scored as before; a case with no golden context either is not applicable. The chatbot's
own sources always win.

### Security metrics

Each Security metric (Prompt Injection, Jailbreak, Encoded Injection, Data Exfiltration,
Social Engineering, Domain Misuse, Non-Advice, Role Violation, Harmful Content) sends its probes from
`security_probes.json` (shipped: `security_probes.default.json`, restored by the goldens
reset) to the chatbot. Each case's context is the target's **Chatbot role** (`persona`),
or a note telling the judge to use the role the bot claims; G-Eval scores 1.0 when the
reply resisted the attack.

The same file holds adversarial prompts for three safety metrics, picked by the same
probe set: **No-Prompt-Leak** sends only them; **Bias** and **Toxicity** send them first,
then the goldens (`MetricSpec.cases`, datasets `prompt_leak_probes` / `safety_probes`).

### Dashboard: Run all visible

The category chips filter the cards; **Run all visible** runs every shown card that has
cases, one `/api/run` at a time (with `limit` from **Cases per run**), polling
`/api/run/progress` for the status line and `/api/usage` every 2 s for the **Tokens
used** tile. **Stop** calls `/api/run/cancel` and skips the remaining cards. The
**Average score** tile averages the latest score of each shown card, counting
lower-is-better metrics as 1 − score; a finished batch reports its own average too.

## 5. Chat (dashboard)

`POST /api/chat {target_id, message, history}` calls `target.chat` and returns the
reply plus the model and mode. Any chatbot failure becomes a 502 with the error
type. The dashboard keeps the conversation per target in memory and sends the last
40 turns, so a real chatbot sees its own earlier replies; switching targets starts a
fresh conversation. This is a convenience view; the judged answer is the one the
metric run collects.

### Judging one answer on the spot

Every chatbot reply carries a **Judge this answer** button. It scores that single
answer with the selected metric(s) through `POST /api/judge`:

```
{metric_key, question, actual_output, theme?, expected_answer?, context?}
```

`judge_one` (`backend/dashboard/runner.py`) builds the same `LLMTestCase` the sweep
would, from the supplied question and answer — no dataset and no target call. Metrics
that cannot score without reference data declare it in `MetricSpec.needs`
(`correctness` → `expected_answer`; `faithfulness`, `hallucination` → `context`). When
the question is one of the theme's goldens, the route fills those in from the golden
row; otherwise the call returns `status: "error"` saying what is missing rather than
scoring against an empty reference.

Ad-hoc scores are **not stored**, so the trend charts stay a record of golden-set runs.

## 6. Views and history

- The side panel and dashboard sync selections through `localStorage`
  (`llmJudge:selectedTargetId`, `selectedMetric`, `lastRun`, `freshSinceRunId`) and
  the browser's `storage` event; the latest run is shared that way rather than re-fetched.
- Scores, trend and cases pass `since_id` (the session start) so they begin empty on
  each load; the dashboard's **History** keeps the full record as **scans**.

### Scan history and its pages

Every Run started from the web app is one job; `backend/dashboard/scans.py` shows
each (pairwise A/B jobs excepted) as a **scan**, numbered 1, 2, 3, … per chatbot,
with one overall result: **passed** (every metric passed), **failed** (at least one
failed), **incomplete** (some not scored, or the run was stopped), or running /
queued. The dashboard's History table (`GET /api/targets/{id}/scans`, newest first)
shows per scan: started, result with "X of Y", passed / failed / errors, average
score ("lower" metrics count as 1 − score) and judge; **Delete all**
(`DELETE /api/targets/{id}/runs`) removes the runs and the finished jobs.

Each row opens, in new tabs:

| Button | Page | Shows |
| --- | --- | --- |
| Open | `scan/scan.html?job=<job id>` | One scan in full (`GET /api/scans/{job id}`): overall result, started / duration / judge / tokens, the metric table, then every metric's reason and cases (question, reply, expected, score, verdict, judge's reason); links to the scans before and after. |
| Overview | `runs/runs.html?target=&job=#overview` | Test runs: **Evaluation** (`#evaluation`, every run of a chatbot: date range, metric chart with ±1 std error bars, % trends over the last 7 runs, runs table; tick two runs to compare), **Home**, a run's **Overview** (properties, results dial, run settings, radar, Histogram / Average / Median / Breakdown), its **Test Cases** (`#cases`: filters, columns, per-metric tabs) and **Compare Test Results** (`#compare&with=`: two runs side by side, matched by golden or input). |
| Compare with N | inline in the dashboard | Metric by metric against the scan before (`GET /api/scans/{id}/compare?with=`): better / worse / same / new / removed. |
| Regressions | `regressions/regressions.html?job=&with=` | The two scans case by case (`GET /api/scans/{id}/cases?with=`): improved, regressed, new, removed; filter by metric or search; click a case for every metric's verdict on both sides. |

Compare and Regressions appear once a chatbot has an earlier scan to compare with.
- `GET /api/runs/latest?target_id=` returns the newest run per metric
  (`storage.latest_runs`) for the latest-scores chart.
- `GET /api/history?target_id=&metric_key=` returns every run of one metric in
  order (`storage.history`) for the trend chart.
- Charts re-render on OS dark-mode changes; each dashboard chart has a table
  fallback.

## 6b. Advanced tools (roadmap features)

All on `ChatbotExtension/advanced/` (opened from the side panel or the dashboard header).

- **Run options** are saved in the `runOptions` setting; `ui.js runMetrics` adds them to every
  `POST /api/jobs` (`settings`, `enhancements`, `enhancement_options`, `max_latency_s`,
  `use_calibration`). The job passes them to `run_spec`; nothing saved means nothing sent.
- **Multi-turn attack** (`kind: "attack"`): for each goal, `runner._run_attack` asks the judge for
  the next escalating user turn (`ATTACKER_PROMPT`), sends it with the conversation so far, and
  the G-Eval rubric scores the whole transcript.
- **Robustness / Self-Check** (`kind: "multi_sample"`): `spec.variants(item)` gives the messages
  (typo + casual rewording, or the same question N times); the replies are numbered into one
  test case.
- **A/B**: `POST /api/pairwise` queues a job with `options.kind = "pairwise"`; `_run_job_metric`
  hands it to `pairwise.run_pairwise` (both orderings per case) and stores a `pairwise_runs` row.
- **Compare**: `GET /api/runs/compare?a=&b=` diffs two stored `result_json` row lists.
- **Simulate**: `POST /api/simulate` runs `simulator.simulate` (judge as user) synchronously,
  scores with the chosen conversational metrics, optionally saves the user turns as a scenario.
- **Generate & review**: `POST /api/documents/url` with `generation` builds Synthesizer
  `EvolutionConfig` / `FiltrationConfig` / `StylingConfig`; `review: true` saves drafts, which
  `MetricSpec.cases` skips until `POST /api/goldens/review` accepts them.
- **Calibration**: `POST /api/calibration` stores a human score for one case of a stored run;
  `GET` reports agreement; with `use_calibration` the labels are appended to G-Eval criteria
  (as one extra evaluation step for metrics with fixed steps, since DeepEval then scores from
  the steps alone).
- **G-Eval evaluation steps**: every shipped G-Eval metric (Correctness, Helpfulness, Citation
  Quality, Robustness, Self-Check, No-Prompt-Leak, the 9 security probes, Multi-turn Jailbreak)
  passes fixed `evaluation_steps` (`MetricSpec.evaluation_steps`, `STEPS_*` in
  `metrics_catalog.py`; probes share `_security_steps(criteria)`), as DeepEval recommends for
  production: steadier scores and one judge call less per case. `criteria` stays for the
  dashboard; the steps are listed in `/api/metrics`, the metric details and the exported suite.
  Custom G-Eval metrics use the steps typed for them, else DeepEval writes them.
- **Your own G-Eval metrics on the dashboard**: the G-Eval category starts with a builder card
  (name, criteria, evaluation steps one per line, evaluation params = the fields the judge
  reads, threshold, strict) with a live `GEval(...)` code preview. It saves through
  `/api/custom-metrics` (same store as the Advanced page), ticks the new metric to run, and
  sets `metricsChangedAt` so the side panel and other panes reload their metrics. Your own
  G-Eval cards get Edit and Delete; earlier runs of a deleted metric stay in History.
  Custom G-Eval metrics are listed under G-Eval, DAG metrics under Custom.
- **Export**: `GET /api/export/suite` returns a zip (`backend/export_suite.py`).
- **Health**: `GET /api/health` for monitoring (200 / 503).

## 7. Tests and CI

- `python -m pytest` — offline suite, no judge tokens.
- `python -m pytest -m smoke` — fast wiring checks only.
- `set RUN_LIVE_JUDGE=1` then `python -m pytest -m live` — every metric against the
  real judge (spends tokens).
- CI (`.github/workflows/ci.yml`) runs the offline suite on pushes to `master` and on
  pull requests; the live suite is a manual `workflow_dispatch` job that needs the
  `JUDGE_API_KEY` repository secret.

## Where to change behaviour

| Want to change | Edit |
|---|---|
| Add or retune a metric | `backend/metrics_catalog.py` (one `MetricSpec`, then `ALL_SPECS`) |
| What a metric needs to score | `MetricSpec.needs` in `backend/metrics_catalog.py` |
| Single-answer judging | `judge_one` in `backend/dashboard/runner.py`, `POST /api/judge` |
| Pass threshold | default: `PASS_THRESHOLD` in `backend/metrics_catalog.py`; per metric: the threshold next to its tick box under **Metrics to run** in the side panel, or a **Thresholds for** preset (`threshold` on `/api/run` and `/api/judge`) |
| Judge model / provider | `.env`: `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` |
| Golden answers | side panel (`POST` / `PUT` / `DELETE` on `/api/goldens`; reset to defaults on every panel load via `POST /api/goldens/reset`) |
| Default golden answers | `backend/datasets/goldens.default.json` |
| HTTP request / reply handling | `backend/targets/http_client.py`; cURL parsing in `backend/targets/curl.py` |
| API surface | `backend/dashboard/app.py` |
