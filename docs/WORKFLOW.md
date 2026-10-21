# Application workflow

How a judged answer travels from a click in the side panel to a score on the chart.

## Components

| Piece | Path | Role |
|---|---|---|
| Side panel | `ChatbotExtension/sidebar/` | Primary UI: manage chatbots and goldens, run a metric, see the latest scores. |
| Dashboard tab | `ChatbotExtension/dashboard/` | Chat with the chatbot; latest / trend / case-by-case views. |
| Service worker | `ChatbotExtension/background.js` | Opens the side panel and the dashboard tab. |
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
3. The extension loads from `ChatbotExtension/`. The side panel calls `/api/status`,
   which reports the judge model name and whether a key is configured.

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
   (`storage.record_run`) — the charts plot run averages, not cases. The run passes
   when that average meets the threshold (≥ for most metrics, ≤ for violation rates).
   Each case row carries `input`, `actual_output`, `expected_output`, `context`
   (with `context_source`: chatbot or golden), `score`, `passed` and `reason`.
5. The response carries `status`, `score`, `threshold`, `reason`, and per-case
   `rows` for the case table.

Failures are returned as `status: "error"` with a message (broken dataset, empty
dataset, target/judge exception) rather than raising.

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

- The side panel and dashboard sync selections through `chrome.storage.local`
  (`selectedTargetId`, `selectedMetric`, `lastRun`); the latest run is shared that
  way rather than re-fetched.
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
  `GET` reports agreement; with `use_calibration` the labels are appended to G-Eval criteria.
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
