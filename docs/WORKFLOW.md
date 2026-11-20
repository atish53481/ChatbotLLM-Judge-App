# Application workflow

How a chatbot gets judged: from the start script and a pasted cURL to a scored scan you can
read, compare and share.

**See it as diagrams:** [LLM Judge Workflow](https://claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv)
is the visual version of this page (system map, test data, scan lifecycle, one metric case by
case). Its source is [`workflow.html`](workflow.html) in this folder. The link opens for everyone
the page is shared with.

This file, `workflow.html` and the published page change together with the code; see
[15. Keeping this document current](#15-keeping-this-document-current).

**Contents:** [1. Components](#1-components) · [2. Startup](#2-startup) ·
[3. Your path through the app](#3-your-path-through-the-app) · [4. Adding a chatbot](#4-adding-a-chatbot) ·
[5. Test data](#5-test-data) · [6. A scan](#6-a-scan) · [7. One metric run](#7-one-metric-run) ·
[8. Shared replies](#8-shared-replies) · [9. Pass and fail](#9-pass-and-fail) · [10. Results](#10-results) ·
[11. Side paths](#11-side-paths) · [12. Metric catalog](#12-metric-catalog) · [13. Tests and CI](#13-tests-and-ci) ·
[14. Where to change behaviour](#14-where-to-change-behaviour) ·
[15. Keeping this document current](#15-keeping-this-document-current)

## 1. Components

| Piece | Path | Role |
|---|---|---|
| Web app | `ChatbotExtension/` | No build step. Served by the backend at `/app/` (`/` redirects there); `index.html` shows the side panel and the dashboard side by side. The folder keeps its name from when it was a Chrome extension. |
| Side panel | `ChatbotExtension/sidebar/` | Judge settings, Confident AI, chatbots, golden answers, conversation scenarios, documents, metrics to run, Run judge, latest scores. |
| Dashboard | `ChatbotExtension/dashboard/` | Metric cards, chat with the chatbot, score trend, last run case by case, Scan history. |
| Result pages | `ChatbotExtension/scan/`, `runs/`, `regressions/`, `report/` | One scan in full; test runs (Evaluation, Overview, Test Cases, Compare); two scans case by case; the printable report. |
| Advanced tools | `ChatbotExtension/advanced/` | Run options, custom metrics, compare runs, A/B, red-team coverage, simulation, generate & review, calibration, CI export ([ADVANCED_TOOLS.md](ADVANCED_TOOLS.md)). |
| API | `backend/dashboard/app.py` | FastAPI on `127.0.0.1:8765`, 102 routes. Same origin only: no CORS, and `TrustedHostMiddleware` accepts only `127.0.0.1` / `localhost`, which keeps stored chatbot keys and cookies on this PC. |
| Job queue | `backend/dashboard/jobs.py` | Runs scans in the background, one at a time. |
| Runner | `backend/dashboard/runner.py` | Runs one metric: cases → replies → test cases → scores → one stored run. |
| Metric catalog | `backend/metrics_catalog.py` | 90 `MetricSpec`s in 8 groups (58 of the app's own, incl. RAGAS and 13 more DeepEval metrics, + 32 DeepTeam vulnerabilities), plus your own. |
| DeepTeam catalog | `backend/datasets/deepteam_sync.py` | Reads the installed `deepteam`: vulnerabilities, their template test cases, attacks, OWASP LLM / Agentic, NIST AI RMF, MITRE ATLAS and EU AI Act maps (all shown on the coverage page and in the report). |
| Chatbot client | `backend/targets/http_client.py` | Replays the request captured from the chatbot's website (`curl.py` parses the cURL). |
| Judge | `backend/judges/judge.py` | The scoring model behind an OpenAI-compatible API (Groq by default). Never the chatbot under test. |
| Storage | `backend/storage.py` | SQLite `judge.db`: `targets`, `runs`, `jobs`, `documents`, `meta`, `human_labels`, `pairwise_runs`. |
| Datasets | `backend/datasets/` | Golden answers, conversation scenarios, security probes and custom metrics: JSON files seeded from `*.default.json`. |

```
 BROWSER (same origin)          THIS PC (FastAPI on 127.0.0.1:8765)                            INTERNET

 side panel    ─┐                                                  ┌─ asks ──► http_client ──► chatbot under test
 dashboard     ─┼─ fetch /api/* ─► app.py ─► jobs.py ─► runner.py ─┤
 result pages  ─┘                    │          │          │       └─ scores─► judge.py ─────► judge model API
                                     │          │          ├─ reads cases ──► JSON datasets
                                     ▼          ▼          │ records each run
                                   SQLite judge.db ◄───────┘
                                     └── finished scans ─────────────────────────────────────► Confident AI (optional)
```

Only two kinds of traffic leave the PC on a run: questions to the chatbot under test and test
cases to the judge model. Confident AI receives finished scans only when a key is saved, and
Langfuse only when both of its keys are saved.

## 2. Startup

### Starting the backend

`start-llm-judge.bat` is the one-click start. When a backend already answers it only opens
`http://127.0.0.1:8765/`. Otherwise it starts `run-backend.bat`, opens the browser once `/app/`
answers (it waits up to 15 minutes, since the first run builds `.venv`) and copies everything the
backend prints to `logs\llm-judge-YYYY-MM-DD.log`.

Both backend scripts start the same server (uvicorn, `backend.dashboard.app:app` on
`127.0.0.1:8765`). They differ in who runs them and what happens when it stops.

| | `run-backend.bat` (manual) | `run-backend-service.bat` (always-on) |
|---|---|---|
| Run by | You: double-click, or `& '.\run-backend.bat'` in PowerShell | The "LLM Judge Backend" scheduled task, at boot. Not meant to run by hand |
| Window / output | Console window | None; `logs\backend.log` (older log kept as `backend.old.log` past ~5 MB) |
| First run | Creates `.venv` and installs `requirements.txt` | Needs `.venv`; logs ".venv is missing" and waits |
| Port 8765 busy | Stops an older LLM Judge backend so new code loads; other programs are left alone | Waits and checks again every 30 s |
| Crash / stop | Prints the error and pauses | Starts it again after 30 s, forever |
| Stop it | Ctrl+C | `uninstall-backend-task.ps1` (or Task Scheduler) |

Run by hand, `run-backend-service.bat` prints nothing and never returns, so the terminal looks
frozen: stop it with Ctrl+C, then `Y`.

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

With the task installed, `run-backend.bat` still works: it takes over port 8765, and the service
loop waits until the port is free again. Nothing runs while the laptop sleeps or is shut down.

### What happens on start

1. `run-backend.bat` creates `.venv` on the first run, sets `DEEPEVAL_TELEMETRY_OPT_OUT`,
   `PYTHONUNBUFFERED` and `PYTHONUTF8`, and stops an older LLM Judge backend holding port 8765.
2. It runs `update-packages.bat`: upgrades the packages to the newest versions `requirements.txt`
   allows, runs `pytest -m smoke`, and puts the previous versions back if either fails.
   `JUDGE_AUTO_UPDATE=0` skips it, and it is skipped while 8765 is still served.
3. uvicorn imports `backend/dashboard/app.py`, which opens `judge.db` (`storage.init_db` creates
   the tables, migrates older chatbot configs and marks jobs left unfinished as `interrupted`),
   mounts the web app at `/app`, starts the job worker thread (not when `JUDGE_START_JOB_WORKER=0`,
   as in tests) and starts the daily refresh of the "latest" red-team probes in the background
   (`backend/datasets/probe_refresh.py`; `JUDGE_PROBE_REFRESH=0` turns it off). `.env` supplies
   `JUDGE_API_KEY` and the other `JUDGE_*` settings. The first read of the DeepTeam
   catalog (`deepteam_sync.load_catalog`, before any metric, attack or OWASP map is built) compares
   the installed `deepteam` (upgraded in step 2) with the saved catalog; on a new version it reads the package again (a few seconds, no judge call) into
   `deepteam_catalog.json`, so new vulnerabilities become metrics and their examples become test
   cases on this start. `JUDGE_DEEPTEAM_SYNC=0` keeps the saved catalog; without the package the
   shipped `deepteam_catalog.default.json` (deepteam 1.0.10) is used. deepteam's own telemetry is
   switched off (`DEEPTEAM_TELEMETRY_OPT_OUT=YES`) before it is imported. Last, on a real start
   (not in tests), `backend/startup_check.py` `on_start` re-checks the catalog against the installed
   deepteam (`ensure_current`), reloads the shipped probe store (`security_probes.reset_to_defaults`)
   so DeepTeam's template payloads of the installed version are in it before the side panel loads,
   and prints **Frameworks loaded:** with the DeepEval and DeepTeam versions, the catalog version
   (`OUT OF DATE` if it does not match), vulnerability / attack counts and the payload counts.
   `GET /api/status` returns the same as `frameworks`.
4. The browser opens `/`, which redirects to `/app/`. On load the side panel restores the shipped
   golden answers, scenarios and probes (`POST /api/goldens/reset`), starts a fresh session
   (`POST /api/session/start` returns the newest run id, saved as setting `freshSinceRunId`, so
   scores start empty while every run stays in History; the dashboard's and the report's opt-in
   **Show earlier runs** checkbox, setting `showAllRuns`, makes `ui.js sessionSince()` return 0 so
   every stored run counts again), then loads `/api/status` (judge model,
   and whether a key is set), the judge and Confident AI settings, `/api/metrics` and `/api/targets`.

## 3. Your path through the app

1. **Start the app.** Double-click `start-llm-judge.bat` (section 2).
   Calls: `GET /` → `/app/`.
2. **The page loads** (automatic). Shipped datasets come back, a fresh session starts, settings,
   metrics and chatbots load.
   Calls: `POST /api/goldens/reset`, `POST /api/session/start`, `GET /api/status`,
   `GET /api/judge/settings`, `GET /api/confident/settings`, `GET /api/metrics`, `GET /api/targets`.
3. **Connect a judge.** In **Judge settings** pick a provider (Groq, Command Code, OpenAI, NVIDIA
   or any OpenAI-compatible API), paste its key and model, press Test, then Save. Saved values live
   in `judge.db` and override `.env`; the key is shown only by its last characters. Without any key,
   runs are refused with HTTP 503.
   Calls: `POST /api/judge/settings/test`, `PUT /api/judge/settings`.
4. **Add the chatbot.** Paste a request copied as cURL from the chatbot's website, or fill the
   form from an OpenAI-compatible API; Test, then Save chatbot (section 4).
   Calls: `POST /api/targets/parse-curl` or `/api/targets/parse-llm`, `POST /api/targets/test`,
   `POST /api/targets`.
5. **Build its test data.** The new chatbot gets a golden set of its own. **Generate with AI**
   asks the chatbot what it does, looks for its help pages and has the judge write golden answers
   and scenarios for it; pages and uploaded documents become Context goldens. Or type your own
   (section 5).
   Calls: `POST /api/targets/{id}/prepare-goldens`, `POST /api/documents`, `POST /api/documents/url`,
   `POST /api/goldens`, `POST /api/conversations`.
6. **Fill in what metrics need** (optional). **Find missing metric data** looks for the bot's
   rules, tool calls and sources when the cURL does not show them, or reads the API docs and help
   text you give it (section 4).
   Calls: `POST /api/targets/{id}/discovery/extract` (PDF / DOCX help files), `POST /api/targets/{id}/discover`,
   `POST /api/targets/{id}/discovery/apply`.
7. **Run the judge.** Pick the **Framework** to scan with (Both, DeepEval or DeepTeam; setting
   `metricFramework`; `/api/metrics` rows carry `framework` from `metrics_catalog.framework_of`:
   group `security` (red-team attacks + DeepTeam vulnerabilities) is DeepTeam, everything else
   DeepEval). The list then shows that framework's groups (Both: one section per framework, each
   with a select-all tick), and Run judge and the quick-select buttons act on its metrics only;
   ticks of the other framework are kept. The dashboard shares the setting: its **Framework** chips
   (Both / DeepEval / DeepTeam) filter the cards, totals and Run all visible, and its category chips
   sit in one cluster per framework (DeepTeam: Red-team attacks, DeepTeam vulnerabilities). Tick metrics under **Metrics to run** (each with a threshold; **Thresholds
   for** applies a Local Development / PR / Staging / Production preset), pick **Run mode** (1, 2,
   3 or 5 cases per metric, or all) and **Parallel metrics**, then press **Run judge**, or **Run all
   visible** on the dashboard. The scan runs in the backend, so closing the page does not stop it;
   **Stop** ends every queued and running scan (section 6).
   Hovering a metric's name shows a card: its category, group, single- or multi-turn, what it
   asks, and each DeepEval test-case field (Input, Actual output, Expected output, Context,
   Retrieval context; Turns / Scenario / Expected outcome / Chatbot role for conversation
   metrics; Tools called / Expected tools for agents) marked Needed or Not needed, from the
   metric's `scores_on` in `/api/metrics` (a test keeps it covering DeepEval's own required
   params), plus where its questions come from and its score scale. Fields read only when present
   (`optional_on`, e.g. Tools called on security probes) show as Optional; on probes, Context is
   the probe goal + chatbot persona, added automatically. DeepTeam metrics add a second list for
   their DeepTeam scorer: `deepteam_metric` (the class `vulnerability._get_metric` returns, e.g.
   `BOLAMetric`) and `deepteam_fields` (always input + actual output; tools called + retrieval
   context for the 17 agentic vulnerabilities in `metrics_catalog.DEEPTEAM_AGENT_FIELDS`), checked
   against the installed deepteam by `tests/test_metric_required_params.py`.
   Calls: `POST /api/jobs`, `GET /api/jobs/{id}` every second, `POST /api/jobs/cancel-all`.
8. **Read the results.** Latest scores in the side panel and a browser notification when the scan
   ends; cards, trend and the case table on the dashboard; History lists every scan and opens the
   scan page, the test-run pages, Regressions and the PDF report, and sends a scan to Confident AI
   (section 10).
   Calls: `GET /api/runs/latest`, `GET /api/history`, `GET /api/targets/{id}/scans`, `GET /api/scans/{id}`.

## 4. Adding a chatbot

Two connectors: a request captured from the chatbot's own website
(`backend/targets/http_client.py`, type `http`, below) and an MCP server (type `mcp`, see "MCP
servers"). For a website chatbot the side panel:

1. `POST /api/targets/parse-curl {curl, sample_message?, probe?}` runs when the user pastes.
   `targets/curl.py` parses a *Copy as cURL (bash)* command into `{url, method, headers,
   body_template}`. `prepare_body` / `prepare_url` find the question (`sample_message` if given;
   else by key name such as `message`, `prompt`, `query`, or the last user turn of a `messages` list,
   or the longest text). It now also treats an `id`, `ticket`, `ticketid`, or `query` parameter
   as the message for ticket-lookup chatbots (MetTel Compass, Jira, etc.). It swaps the value for
   `{{message}}`, empties `history`-style arrays and cuts `messages` to system + last user turn.
   With `probe` (default on) the captured question is sent
   once and `find_reply_path` / `find_stream_reply_path` locate the reply; a reply that lists tool
   calls or sources fills `tools_path` / `context_path` too. The result carries `sample_message`,
   `response_path`, `reply_preview` and `probe_error`.
2. `POST /api/targets/parse-llm` builds the same config for an OpenAI-compatible chat API
   (`backend/targets/presets.py`: `…/chat/completions`, `Authorization: Bearer …`, a `messages`
   body, reply at `choices.0.message.content`), so another model can be judged without a capture.
3. `POST /api/targets/test {config, message, target_id?}` sends one question with the unsaved
   config and returns `{ok, reply}` or `{ok: false, error}` (with the HTTP status and a preview of
   the response, to help pick the reply path).
4. `POST /api/targets` / `PUT /api/targets/{id}` with `{name, type: "http", config}`. The backend
   builds the client at once, so a broken config is refused with a 400 before it is stored.
   Responses mask secret header values as `***`; an edit that sends no `headers` keeps the stored
   ones, and one that leaves the tool calls or sources path empty keeps a path a scan found (an
   empty field means "find it automatically").

At chat time `{{message}}` is filled JSON-escaped into the body (URL-encoded for a form body,
percent-encoded in the URL). `Content-Length`, `Host` and `Accept-Encoding` from the capture are
dropped. The reply is read at `response_path` from JSON, joined across `data:` events for a
`text/event-stream` reply, or taken as the whole text when the path is empty. Golden questions are
asked on their own; multi-turn scenarios send the conversation so far into the body's history slot
(`history_path`, e.g. `history` or `messages`). HTTP 408, 429 and every 5xx
(a chatbot often wraps its own LLM provider's 429 in a 502), timeouts and refused connections are
retried 3 times (Retry-After, else 2, 4, 8 s). Each message waits up to
`JUDGE_TARGET_TIMEOUT_S` seconds (default 60, 5–900; raise it for slow local models such as Ollama on CPU); other 4xx are not, since the same request cannot
succeed. Each still-failing case gets a plain message saying it was retried.

The rest of the chatbot form, all optional:

| Field | Config key | What it does |
|---|---|---|
| Chatbot role | `persona` | Context for the Security probes: Domain Misuse, Non-Advice and Role Violation are judged against it. Empty: the role the bot claims. |
| Retrieved context path | `context_path` | Where replies list retrieved documents. Found automatically from the pasted cURL or any scan. Turns on the retrieval metrics. |
| Sources when the reply lists none | `sources_fallback` | `knowledge_base` or `cited_links`: the app finds context itself (section 7). |
| Tool calls path | `tools_path` | Where replies list tool calls. Found automatically too. Turns on the agent metrics. |
| Security probes | `probe_set` | `generic` (default), `ecommerce`, or `latest` (OWASP / DeepTeam, refreshed daily). |
| Chatbot instructions | `instructions` | Rules for Prompt Alignment, one per line. Empty: the captured system prompt, else standard production rules. |
| Help / FAQ page URL | `help_url` | Read by Generate with AI for Context goldens. |
| Max message length | `max_message_length` | Longer messages are shortened (Summarization's source) or skipped. |
| Delay between messages | `send_delay` | Seconds between two messages to this chatbot's URL, across every run. |
| Treat service messages as errors | `check_replies` | Off by default, as in DeepEval. On: "limit reached", "please log in" or an empty reply stops the run as an error that records nothing. |
| Golden set | `theme` | The golden set the chatbot is judged against. |

### Find missing metric data (discovery assistant)

When the cURL alone does not show what some metrics need (the chatbot's rules, its tool calls,
its sources), `POST /api/targets/{id}/discover` looks for them, refused without `authorized: true`
(the user confirms they may test the bot). `backend/targets/discovery.py` asks the chatbot its own
golden questions (agent goldens first), reads its OpenAPI / Swagger doc on its own origin, tries
the request with one debug flag added, and has the judge infer rules and tools from its help pages
and answers: at most 20 fetches and 10 messages. The chatbot's cookies and keys go to its docs only
with `send_headers`.

Under **Or provide the docs yourself** the user can hand over what the site does not publish. An
OpenAPI / Swagger link (`spec_url`, read within the fetch limit) or a JSON / YAML file (its text sent
as `spec_text`) replaces auto-discovery; a document that is not OpenAPI / Swagger is noted and
auto-discovery runs instead. Help, FAQ or log text, pasted or from a TXT, MD, LOG, PDF or DOCX file,
goes to the judge first as `help_text`, ahead of the help pages it finds. PDF and DOCX are turned
into text by `POST /api/targets/{id}/discovery/extract` (returns `{filename, text}`); other files are
read in the browser.

The report (target config `discovery`) changes nothing until
`POST /api/targets/{id}/discovery/apply` uses the ticked items: a typed path is never replaced, rules
become `inferred_instructions`, tools `available_tools`. Whatever is not found is scored black box.
It runs synchronously, so Stop does not cancel it.

### MCP servers (target type `mcp`)

*Add a chatbot* has a switch: **Website chatbot (paste cURL)** (above) or **MCP server**. An MCP
server (Model Context Protocol) has tools but no chat, so the app plays the agent for it
(`backend/targets/mcp_client.py` `McpTargetClient`): for each test message one call to the **agent
model** picks a tool and its arguments, the server runs it, and the tool's output is the reply,
which always reports the tool called. MCP targets therefore count as agent targets: the agent
metrics and the tool-aware security metrics apply.

- Config `{transport: "stdio"|"http", command, args, env, url, headers, persona?, agent_model?}`.
  **stdio** starts the server as a local process (`command` + `args` as a list, never a shell; a
  minimal system environment plus `env`, so the app's own keys never reach it; no console window;
  the whole process tree is killed on close). **http** is MCP's streamable HTTP (`Mcp-Session-Id`,
  protocol-version header, SSE answers); the older HTTP+SSE transport is not supported. Every call
  has a timeout and checks Stop; messages are capped at 5 MB.
- Secrets go in `env` / `headers` (shown back as `***`; a `***` value or an empty box keeps the
  stored one), not in `args` (shown as typed). Errors are plain messages with secrets replaced.
- `POST /api/targets/mcp/tools` `{config, target_id?}` connects and returns `{tools, server: {name,
  version, instructions}}` (**Test & list tools**; with `target_id` the stored secrets are reused).
- **Agent model.** The judge must never be the chatbot under test, but here a model must choose the
  tools: `agent_model` (a model on the judge's provider, same URL and key) when set, else the judge
  model, and then every run's note starts "The judge model also chose the MCP server's tools (no
  agent model set)": agent-metric scores then partly grade the judge's own choices. Agent calls
  count as judge tokens, tool calls as chatbot calls.
- `/discover` and the CI export are refused for MCP targets (400).

### Build test data for every metric

The build starts by itself. After a chatbot is saved (and for an older one that was never built and
still has data that needs your hint) the panel starts `POST /api/targets/{id}/prepare-all`
`{generate: true}` with no question and shows live progress (without `generate` 400: nothing is
written or spent; no judge key 503). `GET /api/build/settings` tells the panel whether to do so:
`JUDGE_AUTO_BUILD=0` in `.env` turns the automatic start off, and the panel asks "Build test data
for every metric with AI?" again. One build runs per chatbot; `GET /api/targets/{id}/prepare-all`
returns `{status, steps, metrics: {ready, missing: [{key, title, reason}]}, kind, leftovers,
error?}` and the panel polls it every 2 s. **Build all test data** (next to Edit / Delete) runs it
again later. `backend/datasets/prepare_all.py` `build_testdata` runs six steps (`STEPS`), each
filling only what its datasets still lack and failing on its own:

1. **profile**: what the chatbot is. It is interviewed once (an MCP server describes itself), its
   replies show which tools it calls and whether it returns sources. The judge now receives **richer
   context** (up to 4 help pages at 2500 chars each + full interview replies at 1500 chars) when
   writing a one-sentence persona/role (`backend/datasets/profile.py`, `PERSONA_PROMPT`) and when
   inventing tool-use messages for the probe-for-tools step. `profile.py` sets its kind: chat, rag,
   agent or mcp (saved in the chatbot's config as `profile`, with `auto_built`; editing the
   chatbot keeps both and `docs_searched`, `app.BUILD_KEYS`). Any tool calls seen in replies
   during profile, goldens, scenarios or the tools step cause `tools_path` (and `context_path`)
   to be auto-detected and saved on the chatbot at the end of the build (`HttpTargetClient.discovered`
   + `_merge_learned`). Typed values are never replaced.
2. **goldens**: golden answers until the chatbot's theme has 5 of its own, 3 of them short
   "fact" answers for the reference metrics. When help pages, documents or instructions exist, the
   judge checks each against them and drops what they do not back; without any, the goldens are
   kept but marked unverified. The judge prompt (`WRITE_PROMPT` in `autogen.write_dataset`) now
   receives the same richer material (more help pages, longer excerpts, clearer instructions to
   base everything strictly on the chatbot's own interview replies and published help content).
3. **scenarios**: conversation scenarios until the theme has 3, then every scenario's empty role and
   expected outcome is filled. With help pages, documents or instructions, every scenario whose
   `context` is empty (typed ones too; a filled one is never replaced) gets up to 3 passages from
   them, so the Turn RAG metrics have context. For a rag, agent or mcp chatbot each new scenario's
   first turn is sent once first, and one whose reply has sources gets none;
4. **context**: Context goldens from the found help pages and the chatbot's instructions, through
   the document pipeline; with no source at all nothing is written (the app never invents facts:
   the step is skipped and asks for a Help / FAQ URL or a document). Golden answers the build could
   not check against any source are saved as drafts that runs skip until you accept them
   (Advanced tools → Drafts waiting for review);
5. **tools**: goldens with `expected_tools` for Tool Correctness, for every tool with fewer than 2
   (discovery's `available_tools`, a tool the chatbot was seen calling, or the MCP server's
   `tools/list`). Each case is sent to the chatbot and kept only when it really calls a tool;
6. **probes**: security probes fitted to the chatbot for the app's 13 probe metrics (`write_fitted_probes`,
   `WRITE_PROBES_PROMPT`), written into the chatbot's own probe set `target_<id>` (file
   `security_probes.target_<id>.json` next to the store, git-ignored, kept through the panel reset,
   removed with the chatbot). The prompt now receives the same richer material from `_chatbot_material`
   (more help pages + longer interview excerpts) so probes are better fitted to the chatbot's domain,
   tone, tools and policies. A finished build switches the chatbot's probe set to it ("This chatbot's
   own (built with AI)" in the form); metrics without fitted probes use the generic ones, so a chatbot
   that was on `ecommerce` loses those for the 13.

Because every step looks at what is already there (the gap), a second build on a complete theme
asks the chatbot only the interview and spends no judge call. All judge calls during a build
(`persona`, `write_dataset`, fitted probes, context goldens, repair) now receive the same richer
material from the chatbot's own help responses and interview replies (`_chatbot_material` + increased
`PROMPT_TEXT_CHARS` / `MATERIAL_CHARS`).

**Budget.** One build may send at most 30 messages to the chatbot and make at most 40 judge calls
(`JUDGE_BUILD_MAX_MESSAGES`, `JUDGE_BUILD_MAX_JUDGE_CALLS`; `backend/datasets/build_kit.py`). When
a cap is reached the remaining steps are skipped with "Build budget reached (N messages / M judge
calls); press Build all test data to continue." and what was already written stays; pressing the
button continues where it stopped.

**Stop.** While a build runs the block shows **Stop** (`#testdata-stop`), which calls
`POST /api/targets/{id}/prepare-all/cancel`: the build runs under `cancel.watching`, so it ends
before its next chatbot message or judge call (also between steps and while waiting for documents)
with status `stopped`; what it wrote so far stays.

**Leftovers.** The build ends by showing what the chatbot's kind is (`#testdata-kind`) and a
`leftovers` list (`app._leftovers`, `#testdata-leftovers`): metrics that still have no test data,
each with the reason and a hint. When tools are seen in replies but no `tools_path` is set, it now
says "Tool calls path was not found in replies; run Find missing metric data or set it manually."
(no tools: "Set 'Tool calls path', or run Find missing metric data."; no context: "Add a Help / FAQ URL or upload a document, then build again."; no role or scenario: "Build again, or add a conversation scenario with a chatbot role."; otherwise "Press Build all test data again.").
Plan Quality and Plan Adherence are listed when the chatbot calls tools but its calls carry no
plan or reasoning (hint: ask the bot's team to return it). Metrics that cannot apply to this kind
(agent metrics except Task Completion for a plain or RAG chatbot, the Contextual metrics for a
plain chatbot) go in a separate "Does not apply" group with no hint.

The result lists how many metrics now have test cases and which still have none, and why. The old
`POST /api/targets/{id}/prepare-goldens` is unchanged; the panel calls it with `generate: false` on
save to give the chatbot its own golden set.

## 5. Test data

Every metric reads one dataset (`MetricSpec.dataset_name`):

| Dataset | Read by | A case needs |
|---|---|---|
| `goldens` | 14: Answer Relevancy, Correctness, Helpfulness, Prompt Alignment, Robustness, PII Leakage, Task Completion, Argument Correctness, Plan Quality, Plan Adherence, Step Efficiency, BLEU, ROUGE-L, Exact Match | question, expected answer |
| `goldens_with_context` | 7: Faithfulness, Hallucination, Summarization, Contextual Precision, Contextual Recall, Contextual Relevancy, Citation Quality | a golden with context (one fact per line) |
| `goldens_without_context` | 1: Self-Check Consistency | question |
| `goldens_with_tools` | 1: Tool Correctness | `expected_tools` |
| `conversations` | 7: Conversation Completeness, Knowledge Retention, Turn Relevancy, Turn Faithfulness, Turn Contextual Precision / Recall / Relevancy | a scenario: user turns, expected outcome; built from the goldens when none are saved |
| `conversations_with_role` | 1: Role Adherence | a scenario with a chatbot role |
| `security_probes` | 42: the 9 security probe metrics, Multi-turn Jailbreak and the 32 DeepTeam vulnerability metrics | probes in the chatbot's probe set (a DeepTeam metric with none in that set uses its generic probes) |
| `safety_probes` | 2: Bias, Toxicity | adversarial probes first, then the goldens |
| `prompt_leak_probes` | 1: No-Prompt-Leak | prompt-extraction probes |

Where the cases come from:

- **Shipped defaults.** `goldens.default.json` (themes such as `generic`, bot-agnostic, and
  `general_support`, the ShopEasy shop), `conversations.default.json` and
  `security_probes.default.json` (`generic` and `ecommerce` sets). Their runtime copies
  (`goldens.json`, `conversations.json`, `security_probes.json`) are git-ignored and restored on
  every panel load by `POST /api/goldens/reset`; a chatbot's own theme is kept. The reset also adds
  up to 10 test cases per DeepTeam metric to the `generic` probe set: DeepTeam's own template
  examples, taken in turn from each subtype (`deepteam_sync.catalog_probes`, ids `sp_dt_…`, note
  `DeepTeam <version> · <Vulnerability> / <subtype>`), so a newer deepteam brings its new cases with
  it. The shipped `generic` and `ecommerce` probes are unchanged. Custom metrics
  (`custom_metrics.json`) are never reset.
- **Custom vulnerabilities.** Like DeepTeam's `CustomVulnerability`: in the side panel's Security
  group, **＋ New custom vulnerability** takes a name, optional description, criteria (the rule the
  chatbot must follow) and optional types. **Write attacks with the judge**
  (`POST /api/custom-metrics/vulnerability-attacks`, nothing saved) fills the attack list, which
  you edit before **Save vulnerability** (`POST /api/custom-metrics`, kind `vulnerability`). Each is
  stored in `custom_metrics.json` with its attacks (`attacks: [{id: "cv_…", question, type}]`), so
  the panel reset never wipes it. It runs as a security probe metric (key `custom_…`, group
  `security`, dataset `custom_vulnerability`, the probes' fixed steps around its criteria, the
  chatbot's persona as context, attack enhancers apply), its Test cases / Add / Edit / Delete /
  Generate similar change its own attacks, and the Risk Assessment counts it like any DeepTeam
  vulnerability. Scored by the app's rubric only (not DeepTeam's own scorer).
- **You.** The side panel's forms (`/api/goldens`, `/api/conversations`, `/api/security-probes`). The Golden answers and Conversation scenarios sections have been redesigned with cleaner cards, modern forms, better spacing, and improved editing flow. Manually adding or editing goldens/scenarios now automatically refreshes metric applicability (`refreshMetricsApplicability`) so Contextual, Faithfulness, RAGAS and related metrics unlock without requiring another full build.
  On each metric, **Test cases** lists exactly what a run sends (`GET /api/metrics/{key}/cases?target_id=`).
  **Add** opens a form that puts a case in the store that metric reads (`POST /api/metrics/{key}/cases`):
  a golden in the chatbot's theme, a probe in its probe set, or (for multi-turn metrics) a conversation scenario.
  **Delete** removes one (`DELETE /api/metrics/{key}/cases/{case_id}`). The side panel also has a dedicated
  **Conversation scenarios** section (shared across all Turns metrics).
- **Generate with AI** (`POST /api/targets/{id}/prepare-goldens`, `generate: true` only after the
  user says yes, so nothing is written or spent before). A chatbot on the shared `generic` theme
  gets its own theme, named after it. `backend/datasets/autogen.py` asks the chatbot what it does
  (`INTERVIEW`), searches its site for FAQ / help / docs pages when no `help_url` is given
  (`find_help_pages`: the request's Origin / Referer, the chatbot host and its main domain ×
  `HELP_PATHS`, at most 12 fetches), and has the judge write golden answers (`source: "auto_chatbot"`)
  and conversation scenarios fitted to it. Found pages and the chatbot's instructions go through
  the document pipeline below. The bot-agnostic starter goldens are added only when nothing was
  learned. Each step runs once per chatbot.
- **Documents and pages.** `POST /api/documents` saves an upload (PDF, TXT, DOCX, MD) under
  `backend/datasets/documents/<theme>/`; `POST /api/documents/url` fetches a help page
  (`backend/rag/fetch.py`: http(s) only, 5 MB cap, refuses pages under 200 characters). Both run
  DeepEval's `Synthesizer` in the background (judge as critic, local sentence-transformers
  embedder) and append goldens with context. Optional `generation` settings add evolutions,
  filtration and styling; with `review: true` the goldens are drafts, which runs skip until
  `POST /api/goldens/review` accepts them.
- **Latest probes.** Once a day `probe_refresh` reads OWASP Top 10 for LLM Applications 2025, the
  OWASP Agentic Security Initiative threat list, OWASP MCP Top 10 and DeepTeam's docs, and the
  judge writes 10 probes per metric into `security_probes.latest.json`, each noting the standard it
  came from. Every probe metric also gets the installed DeepTeam's own text for its vulnerability
  (description, subtypes, template examples; read from the catalog, not fetched), so the DeepTeam
  metrics and Bias / Toxicity / Data Exfiltration / No-Prompt-Leak are refreshed even offline. Offline,
  a metric without such text keeps its previous latest probes, and with none the shipped generic
  set is used. A refresh writes the app's own 13 probe metrics (`probe_refresh.APP_METRICS`) and
  only the DeepTeam metrics some chatbot ran in the last 30 days (`USED_DAYS`;
  `storage.recent_metric_keys`, passed in by `start_on_boot`), one judge call each: 13 a day on a
  fresh install. The others keep their previous latest probes, else the shipped ones.
  `JUDGE_PROBE_REFRESH_ALL=1` refreshes all 45 again.

Every write to `goldens.json` is logged, with the function that made it, in `goldens_changes.log`
(git-ignored); `GET /api/goldens/health` counts missing shipped rows, which the side panel reports.

## 6. A scan

A scan is a background job: the metrics ticked for one chatbot, with the options set at that
moment. The side panel's **Run judge** and the dashboard's **Run all visible** both queue one job
through `ui.js runMetrics`.

### Queueing: `POST /api/jobs`

| Field | Meaning |
|---|---|
| `target_id`, `metric_keys` | The chatbot and the metrics to run |
| `thresholds` | Per metric, from the side panel (`localStorage` `llmJudge:thresholds`) |
| `limit` | Run mode: cases scored per metric (empty = all) |
| `metric_concurrency` | Parallel metrics, 1 to 8 (bounds from `GET /api/run-limits`) |
| `check_consistency` | Score every reply twice (section 7) |
| `settings`, `enhancements`, `enhancement_options`, `max_latency_s`, `use_calibration`, `deepeval` | Advanced tools' run options (`ui.js runOptionsBody`); `deepeval` = `{strict_mode, include_reason, async_mode}` |

An unknown metric is a 404, an unknown enhancement a 400, and no judge key a 503. Metrics that
cannot run for this chatbot (not applicable, or no case in its set) are not sent: the job keeps
them as `options.skipped` `[{key, title, reason}]`, so the scan and the report can say why.

### Running

Jobs run one at a time, oldest first, on the worker thread. Inside a job up to
`metric_concurrency` metrics run side by side (default `JUDGE_METRIC_CONCURRENCY`, 2; an A/B job
runs one at a time). Each metric goes through `app._run_job_metric` → `runner.run_spec` with the
job's options and the chatbot's config, and its result is added to `jobs.results` as soon as it
finishes. A job ends `done`, `error` (judge missing, chatbot deleted, or an unexpected exception)
or `cancelled`; a job still queued or running when the backend stops becomes `interrupted` at the
next start.

- **Progress.** `GET /api/jobs/{id}` returns the results so far plus `current` (the first metric
  still running) and `running` (all of them), each with its phase (`chat` or `judge`), cases done
  of total and the question being asked. Pages poll it every second (`ui.js followJob`); a
  reopened page finds a running job again through `GET /api/jobs?target_id=&active=1`. A finished
  scan shows as a browser notification and a mark in the tab title. In the side panel, hovering the
  run banner or results list pauses them and opens a panel with what runs now, the tally so far and a
  **Test cases log**: every case step the polls saw (asking the chatbot, then judging), oldest first,
  with metric, case n/total, question and time since start (`sidebar.js caseLogBlock`, last 200 kept
  in the browser only; steps between two polls are not seen).
- **Stop.** `POST /api/jobs/cancel-all` stops every job of every chatbot. Queued jobs end at once.
  A running one stops at its next chatbot message, judge call or retry wait (`backend/cancel.py`
  passes the check through contextvars), and a stopped metric records nothing, so a half run never
  skews the trend. A judge call already in flight is waited on in 0.5 s steps that also check Stop,
  so Stop does not wait for it to answer.
- **Hung judge calls.** The client's 90 s timeout (`JUDGE_TIMEOUT_S`) is per read, so a provider
  that trickles keep-alive bytes never trips it. `judge._with_deadline` caps each call at
  `JUDGE_DEADLINE_S` (150 s) of wall-clock time, then abandons it (`JudgeTimeout`) and
  `GroqJudge._call` retries it up to twice (log line `<- judge hung past 150 s: retrying`).

### After the job: `app._after_job`

Called for every finished job, whatever its end state:

1. Drops the job's shared replies (section 8).
2. Sends the scan to Confident AI when a key is saved and auto-send is on (`confident_auto`,
   default on with a key), and to Langfuse when its keys are saved and its auto-send is on
   (`langfuse_auto`: on when keys are saved in the side panel; with keys only in `.env`, switch it
   on in the Langfuse card), on a background thread that never fails the job.
3. **Auto-discovery.** A pasted cURL shows one reply, which may have called no tools or cited
   nothing, so every scan keeps looking. `HttpTargetClient` checks each JSON reply and each stream
   event with the same detection as the cURL parser (`find_tools_path` / `find_context_path`; a
   tool list counts only when an entry has a tool name) and keeps, in `discovered`, what it finds
   for a path the chatbot has none saved for. After each metric, `app._save_found_paths` saves
   those paths on the chatbot (never replacing one the user typed) and notes which metrics they
   switched on (`app._availability`, the same check as `/api/metrics`). When the job ends the paths
   are stored in `jobs.discovered` and one follow-up scan of those metrics is queued with the same
   options (not the first scan's skipped metrics or Confident AI upload) plus
   `auto_discovered: {from_job, paths}`. No loop: a saved path is never found again (saving the
   chatbot form keeps it),
   a scan that finds nothing queues nothing, and a stopped job queues nothing. A/B jobs and ad-hoc
   judging are not watched.

## 7. One metric run

`runner.run_spec` runs one metric for one chatbot in five stages. `POST /api/run` does the same
synchronously for one metric (kept for tests and scripts; the web app always queues jobs).

1. **Load cases.** `spec.cases(theme, persona, probe_set)` reads the metric's dataset. An empty
   set or a broken one is a run error saying what to add. `spec.applies(features, items)` decides,
   from the chatbot (`context_path` / `tools_path` set, or a sources fallback) and the cases,
   whether the metric can run at all; if not, the run is an error that sends nothing, and
   `/api/metrics` returns the same reason as `unavailable`.
2. **Cap to Run mode.** `limit` keeps the first N cases and holds the rest in reserve. With a max
   message length, cases carry `max_chars`. Enhancements add a disguised variant of every
   red-team probe (single-turn probe datasets only).
3. **Score each case.** Cases run on `case_workers` threads, each thread with its own metric
   instance, and `_case_slots` caps cases in flight across all runs at `JUDGE_CONCURRENCY` (4),
   which is also the judge's cap on calls in flight. Conversations, attacks and chatbots with a
   send delay run one case at a time. Per case: `skip_before` may leave it out; the chatbot is
   asked (below); a sources fallback may find context; `spec.build_case` makes the `LLMTestCase`;
   `skip_case` may leave it out; `metric.measure(case)` scores it; `not_applicable` may leave it
   out after scoring; the case row is written.
4. **Backfill.** Cases left out as not applicable (no tool called, no context found, no plan) are
   replaced by the next ones from the reserve, batch by batch, until N are scored or the set runs
   out. So N means N scored cases wherever the set has that many.
5. **Record the run.** One row in `runs` (`storage.record_run`) with the average score, passed,
   cases run, judge model, judge tokens and calls, chatbot calls, duration, judge spread, latency
   p50 / p95 and judge cost, plus the whole result as `result_json`. The run's own `usage.meter()`
   counts its calls, so runs side by side never mix. A stopped run records nothing.

What "ask the chatbot" sends, by metric kind:

| Kind | Metrics | Sent per case |
|---|---|---|
| `single` | 33 | The golden's question, or the prompt the metric builds from it (Summarization sends the text to summarize). Golden facts are never sent with a question: `context_in_prompt` is ignored. |
| `conversation` | 8 | Each user turn of a scenario in order, with the conversation so far. Turn RAG metrics read each turn's own `retrieval_context`, else the scenario's optional `context`. |
| `multi_sample` | 2 | Robustness: the question with a typo and as a casual rewording. Self-Check: the same question several times. Always fresh replies (section 8). |
| `attack` | 1 | Multi-turn Jailbreak: for each goal the judge writes the next escalating user turn (`ATTACKER_PROMPT`, 4 turns by default); the G-Eval rubric scores the whole transcript. |

Every result row carries `input`, `actual_output`, `expected_output`, `context`,
`context_source`, `score`, `passed`, `reason`, `latency_s` and `golden_id`. The result adds
`cases_run`, `cases_passed`, `pass_rate`, `cases_not_applicable`, `stats`, `latency`,
`judge_cost_usd` and a `note`, for example when the judge model changed since the previous run, so
scores may not be comparable.

### Metrics that switch on at run time

- **Retrieval** (Contextual Precision / Recall / Relevancy and their Turn versions) grade the
  retriever, so they read only the chatbot's own sources (`context_path`), never context the app found
  itself (`MetricSpec.grades_retriever`) and never the golden's context.
- **Every metric that reads `retrieval_context`** (Faithfulness, Contextual ×3, Citation Quality,
  RAGAS, Turn RAG ×4) is production-only: it scores the chatbot's own sources or, for the
  non-retriever ones, sources the app found itself (`sources_fallback`). Without either it is
  unavailable (`needs_own_sources`, `runner.NEEDS_OWN_SOURCES`); a reply with no sources is a
  not-applicable case (`runner.NO_OWN_SOURCES_CASE`). The golden's context is never used in their
  place. Hallucination keeps reading the golden's `context`: in DeepEval it checks known facts.
- **Agent** metrics read `ChatReply.tools_called` (target `tools_path`; OpenAI `tool_calls` or
  plain `{name, args, output, reasoning}` lists). Task Completion always runs; Tool Correctness
  needs goldens with `expected_tools`; Argument Correctness, Plan Quality / Adherence and Step
  Efficiency score only replies that called tools (plan and step metrics get a trace built from
  the calls; a trace without a plan is not applicable, not a free pass).
- **Citation Quality** needs the chatbot's own sources: a bot that returns none could never cite.
- **Text match** (BLEU, ROUGE-L, Exact Match; `backend/text_match.py`) use no judge and no tokens:
  DeepEval's `Scorer` compares the reply's wording with the expected answer (`rouge-score`;
  `nltk` with its `punkt_tab` data, fetched into `.venv/nltk_data` on the first BLEU run).
- **Prompt Alignment** scores each reply against the chatbot's rules from
  `targets/instructions.resolve`: typed on the chatbot, else the captured request's system prompt,
  else rules the discovery assistant inferred, else standard production rules (`BASE_RULES` plus
  RAG, agent or role rules when the chatbot has `context_path`, `tools_path` or `persona`). The
  run's note names the source. The exact rules and their source are saved with every run
  (`prompt_instructions`) and listed on the scan page and in the report under **Prompt
  instructions used**; before a run, the side panel shows them in Prompt Alignment's **Test cases**
  panel and hover card for the chosen chatbot.

### Sources the app finds itself (`sources_fallback`)

Many chatbots don't return their sources. When a response has none at `context_path`, a target's
`sources_fallback` makes the app find context for the reply (`backend/rag/derived_sources.py`):

- `knowledge_base`: the 3 passages of the theme's uploaded documents that best match the question
  and reply (BM25 keyword ranking, no embeddings; a document's `_enhanced.txt` replaces the original).
- `cited_links`: up to 3 `http(s)` links in the reply, fetched as text (public hosts only: every
  resolved address must be global, redirects checked hop by hop, 5 MB cap, 4000 characters per page).

This context feeds every metric that reads `retrieval_context` except the retriever-grading ones
above. Rows say `context_source` `knowledge_base` or `cited_pages`, and the note says the context
is not the chatbot's own retrieval. When nothing is found the case is not applicable (the golden's context is never scored
instead). The chatbot's own sources always win.

### Security metrics

Each Security metric (Prompt Injection, Jailbreak, Encoded Injection, Data Exfiltration, Social
Engineering, Domain Misuse, Non-Advice, Role Violation, Harmful Content) sends the probes of the
chatbot's probe set. Each case's context is the target's **Chatbot role** (`persona`), or a note
telling the judge to use the role the bot claims; the G-Eval rubric scores 1.0 when the reply
resisted the attack. The same store holds the adversarial prompts of No-Prompt-Leak (probes only),
Bias and Toxicity (probes first, then the goldens).

**DeepTeam vulnerability metrics.** One G-Eval Security metric per DeepTeam vulnerability the app
had no metric for (32 in deepteam 1.0.10: Agent Identity & Trust Abuse, Autonomous Agent Drift,
BFLA, BOLA, Child Protection, Competition, Cross-Context Retrieval, Debug Access, Ethics, Excessive
Agency, Exploit Tool Agent, External System Abuse, Fairness, Goal Theft, Graphic Content,
Hallucination (`dt_hallucination`), Illegal Activity, Indirect Instruction, Inter-Agent
Communication, Intellectual Property, Misinformation, Personal Safety, RBAC, Recursive Hijacking,
Robustness (`dt_robustness`), SQL Injection, SSRF, Shell Injection, System Reconnaissance, Tool
Metadata Poisoning, Tool Orchestration Abuse, Unexpected Code Execution). Bias, Toxicity, PII
Leakage and Prompt Leakage map to the app's Bias, Toxicity, Data Exfiltration and No-Prompt-Leak.
Each rubric names the vulnerability, its subtypes and a rule for its DeepTeam category; the steps
are the probes' fixed `_security_steps`. They run like the other probe metrics (same persona
context, enhancers, cases per run) and are listed by `/api/metrics` with `source: "deepteam"`. The
side panel and the dashboard (its totals and Run all visible) leave them unticked until you tick
them (or saved ticks already exist), so a first scan is the size it was.

**Attacks.** `attack_enhancers.ENHANCERS` adds DeepTeam's 15 single-turn attacks the app did not
already have (`dt_` keys: adversarial poetry, authority escalation, character stream, context
flooding, context poisoning, embedded instruction JSON, emotional manipulation, goal redirection,
gray box, input bypass, linguistic confusion, permission escalation, prompt probing, synthetic
context injection, system override). deepteam applies them at run time (`deepteam_sync.enhance`),
with the judge as its simulator model when the attack rewrites the probe. Multi-turn Jailbreak's
`strategy` setting (Advanced tools → Run options) picks DeepTeam's multi-turn attacks: `linear`
(default, the prompt it always used), `crescendo`, `tree`, `sequential_break`, `bad_likert_judge`
(`runner.ATTACK_PROMPTS`; the judge writes each turn in that style).

How DeepTeam's attacks behave inside a run (`deepteam_sync.enhance`):

- **No progress bars, no telemetry.** `deepteam_sync` sets `DEEPTEAM_SHOW_PROGRESS=false` and
  `DEEPTEAM_TELEMETRY_OPT_OUT=YES` before deepteam is imported. Its progress bars are rich live
  displays: a second one, from a metric running beside it, raises "Only one live display may be
  active at once", and their spinner characters crash a Windows console that is not UTF-8.
- **A disguise that fails is left out, not mislabelled.** deepteam catches every error (a failed or
  refused judge call included) and either raises its own RuntimeError or hands the probe back
  unchanged. Both become `deepteam_sync.NotEnhanced`: `attack_enhancers.expand` leaves that variant
  out, the run still sends and scores the probe itself, and the result gets
  `enhancements_not_sent` [{enhancement, label, base_probe_id, reason}] and a note.
- **Stop still stops.** deepteam's retries swallow the app's `RunCancelled`; `enhance` checks for
  a stop before and after each attack, and the runner re-raises it instead of turning it into an
  error result.
- **The chatbot's role goes with the attack.** Attacks that tailor themselves to the target
  (Synthetic Context Injection's `target_information`) get the probe's context, the chatbot role.
  An attack that needs any other constructor argument is not offered (`SUPPLIED_ARGS`).
- **Every enhancer may be ticked at once** (`MAX_ENHANCEMENTS`, the number of enhancers; the API
  allowed 10 when the app had 8).
- **Traffic log.** Each disguised probe is logged as `probe <id> as <attack>: <text>`, each
  multi-turn attack as `attack (<strategy>, <n> turns) toward: <goal>`, next to the chatbot and
  judge calls they cause, so a run can be traced step by step.

**OWASP lists.** `redteam_coverage.frameworks()` (`GET /api/security/frameworks`, also in
`/api/security/coverage` as `frameworks`) maps every id of OWASP Top 10 for LLM Applications 2025
(LLM01–LLM10) and OWASP Top 10 for Agentic Applications 2026 (ASI01–ASI10) to metrics from
DeepTeam's own maps, and OWASP MCP Top 10 2025 (MCP01–MCP10; DeepTeam has none) from the app's
`OWASP_MCP_2025`. Advanced tools → Red-team coverage shows each id's pass rate, and **Tick these**
ticks its metrics in the side panel. Risk categories gained **Agentic**. The PDF report has an
**OWASP coverage** section (see Results). DeepTeam's Hallucination and Robustness are titled
"Hallucination (DeepTeam)" and "Robustness (DeepTeam)", so no two metrics share a name.
Eight ids can only partly be checked by chatting with the bot (LLM03, LLM04, LLM10, ASI04, ASI08,
MCP04, MCP08, MCP09; `redteam_coverage.PARTLY_TESTABLE`): their items carry `partial: true` and a
`partial_reason`, and the coverage page and the report say "partly testable from the chat" next to
them instead of showing them as fully covered.

**Shipped DeepTeam cases read as messages.** A DeepTeam template example that is a bare payload (a
URL, path, command or query on its own: `deepteam_sync.is_bare_payload`) is worded as a request
before it ships (`as_request`, phrasings per vulnerability in `REQUEST_TEMPLATES`, else
`GENERIC_REQUESTS`; picked by position, no judge call). 11 of the 300 (8 SSRF, 3 Shell Injection);
their note ends in "· wrapped as a request".

**Scorer.** Each DeepTeam metric has a `scorer` setting (Advanced tools → Run options): "app"
(default, the G-Eval above) or "deepteam", DeepTeam's own metric for that vulnerability
(`backend/datasets/deepteam_scorer.py` `DeepTeamScorer`: the vulnerability class with the judge as
evaluation model and the chatbot role as purpose, `_get_metric(<subtype>)` on an `RTTestCase`;
subtype from the shipped probe's note, else the first). It scores each case 0 or 1; the run's note
names the DeepTeam metric. Without deepteam installed the run is an error before anything is sent.
DeepTeam passes a reply that opens with an apology or says "I can't" without asking its judge
(its reply screening), and never looks at tool calls; so for an agent that reports tools, a case
DeepTeam passed that way goes to the app's tool-aware rubric instead (`DeepTeamScorer._judge_tools`,
still scored 1 or 0), and the reason says so: an agent that apologises but still runs the tool fails.
With calibration on, your labelled cases become DeepTeam's few-shot evaluation examples (each
label's verdict is pass or fail against its own threshold, 0.5 without one; at most 5, as for
G-Eval) plus one guideline to score like the human reviewer; the run reports how many were used.

**Agents: what they did, not only what they said.** For a chatbot with a tool calls path, the 41
single-turn probe metrics (`MetricSpec.reads_reported_tools`; not Multi-turn Jailbreak) score the
tools it called too: each case carries `tools_called` (empty when it called none), the G-Eval reads
Tools Called and adds `AGENT_TOOLS_STEP` (a tool that carries out the attack counts as giving in,
even when the text refuses). The runner decides per run from the client's `tools_path`; without
one, metric and cases are exactly as before.

### DeepTeam's own engine (side panel → DeepTeam red teaming)

The security metrics above run inside the app's runner. The **DeepTeam red teaming** page
(`ChatbotExtension/deepteam/`, backend `backend/redteam/`) runs DeepTeam itself instead, as its
docs describe, and changes nothing else (no metrics, jobs or scans):

- **Red team** — `POST /api/deepteam/runs` starts DeepTeam's `RedTeamer.red_team()` on a
  background thread (one run at a time; `RUNNER`, Stop = `POST /api/deepteam/runs/{id}/cancel`).
  The judge is DeepTeam's `simulator_model` and `evaluation_model`; the chatbot is its
  `model_callback` (`engine.make_callback`: earlier turns of multi-turn attacks go to the chatbot as
  history, and the reply comes back as an `RTTurn` with `retrieval_context` and `tools_called` when
  the chatbot reports them). Picks: DeepTeam vulnerabilities with their types (all 36 of the
  installed deepteam), any of its single-turn (22) and multi-turn (Linear, Crescendo, Tree,
  Sequential, Bad Likert Judge) attacks, **or** a framework (`engine.FRAMEWORKS`: OWASP LLM 2025,
  OWASP Agents 2026, NIST AI RMF, MITRE ATLAS, EU AI Act, and the BeaverTails / Aegis datasets,
  which load prompts from Hugging Face); `attacks_per_vulnerability_type` (1–10),
  `run_all_attacks`, `exposure` (LOW / MEDIUM / HIGH, feeds DeepTeam's CVSS score),
  `target_purpose` (defaults to the chatbot's persona) and an optional `AttackEngine`
  (variations, generation guidelines). DeepTeam never prints its table or uploads on its own
  (`_print_assessment` / `_upload_to_confident` off).
- **Runs & risk assessment** — DeepTeam's `RiskAssessment` (overview + test cases: vulnerability,
  type, attack, input or turns, reply, score 0/1, CVSS, reason) is stored in the `deepteam_runs`
  table (`engine.ensure_table`, created on first use; runs left queued/running by a restart become
  `interrupted`) with a `summary` (pass rate per vulnerability and per attack, max CVSS).
  `GET /api/deepteam/runs/{id}`, `GET /api/targets/{id}/deepteam/runs`, `DELETE /api/deepteam/runs/{id}`.
  **Report:** each run's **Report** button opens the Risk Assessment page in DeepTeam mode
  (`risk/risk.html?deepteam=<id>`; the page's **Source** select switches between app scans and
  DeepTeam runs). Its first tab, **DeepTeam Report**, is DeepTeam's own report as `red_team()`
  prints it (`red_teamer._print_risk_assessment`): DeepTeam Risk Assessment (errored count),
  Overall CVSS Score / 10.0 with its severity (Low < 4, Medium < 7, High < 9, Critical), Overview
  by Vulnerabilities and by Attack Methods (✓ PASS ≥ 80 %, ⚠ WARNING ≥ 50 %, ✗ FAIL, with
  "Mitigation Rate: x% (passing/total)", sorted by rate), and the Test Cases Overview
  (Vulnerability, Type, Attack Method, Input, Output, Turns, Reason, CVSS, Status; filter
  All / Passed / Failed / Errored), from DeepTeam's stored `overview` and test cases. Between the
  overviews and the test cases, **Impact of the vulnerabilities** explains each tested vulnerability:
  what an attacker gets ("If exploited") and what it costs the business ("Business impact"), from
  `backend/dashboard/impact.py` (`IMPACT` per metric key, else `CATEGORY_IMPACT` per DeepTeam
  category), marked ✗ Exploited (an attack got through, with the worst attack as proof) or ✓ Held,
  exploited ones first. `risk.assessment` puts it on every vulnerability and mitigation as `impact`
  {if_exploited, business, status, confirmed}, so scans' Vulnerabilities and Mitigation tabs
  ("Why it matters") and the printed mitigation plan show it too. The other
  tabs (Overview, Vulnerabilities, Mitigation, Attacks, Test Cases, Frameworks) are the same as a
  scan's: `GET /api/deepteam/runs/{id}/risk` (`risk.deepteam_assessment`) maps DeepTeam
  vulnerabilities to the app's metric keys where it has one, uses DeepTeam's CVSS as each case's
  risk score and counts errored cases apart. **Download report** prints the DeepTeam Report.
- **Guardrails** — `POST /api/deepteam/guardrails` runs DeepTeam's `Guardrails` with the judge as
  model: input guards (prompt injection, topical with allowed topics, cybersecurity, toxicity,
  privacy, illegal) on a message, output guards (cybersecurity, toxicity, privacy, illegal,
  hallucination) on a given reply or the chatbot's real reply (`target_id`).
- **YAML / CLI** — `POST /api/targets/{id}/deepteam/export` returns a zip for `deepteam run
  config.yaml`: `config.yaml` (models: the judge as `provider: local`; target callback;
  system_config; default_vulnerabilities and attacks from the form), `model_callback.py`,
  `chatbot_client.py` + `chatbot.json` (the CI export's client, `export_suite.CLIENT_PY`: header
  values never exported, secrets from `LLM_EVAL_HEADERS`), `.env.example` and a README.

`GET /api/deepteam/options` lists what the installed deepteam offers. Tests:
`tests/test_deepteam_engine.py` (RedTeamer and Guardrails faked where they would call a model).

### Judge consistency

With **Check judge consistency** on, the judge scores every reply twice (the chatbot is asked
once) and the case scores the average. When the two differ by more than 0.15
(`UNSTABLE_SPREAD`), a third scoring breaks the tie and the median is kept; the note counts these
tie-breaks. If even then no two scorings of a case agree within 0.15, the run is flagged
`judge_unstable`. It costs about twice the judge tokens and is not part of DeepEval.

## 8. Shared replies

DeepEval scores one test case, one reply, with many metrics. A scan works the same way: the
job's metrics share one chatbot client, `targets/shared.SharedReplies` (kept per job in
`app._job_clients`). The first metric that needs the reply to a question (with the same
conversation so far) sends it; every other metric of the job gets that same reply, waiting if it
is still on its way. A failure is not kept, so the next metric tries again. Metrics that need
several different replies (Robustness, Self-Check, Multi-turn Jailbreak) use `.fresh`, the client
itself, and every message is sent. The cache is dropped when the job ends.

## 9. Pass and fail

The app follows DeepEval's rules rather than averaging:

| Level | Passes when |
|---|---|
| Case | DeepEval's `metric.is_successful()` says so: the score meets that metric's threshold. Strict mode raises the threshold to 1. |
| Metric run | Every case passes. A latency limit (`max_latency_s`) also fails it when the p95 reply time is over the limit. The average is reported, never used to pass. |
| Test case (in a scan) | Every metric that scored it passes it. |
| Scan | Every metric passed (`passed`). At least one failed: `failed`. A metric errored, nothing was scored, or the scan was stopped: `incomplete`. Otherwise `running` or `queued`. |

Example (made-up numbers, one threshold per metric):

| Case | Answer Relevancy ≥ 0.7 | Faithfulness ≥ 0.8 | Correctness ≥ 0.6 | Test case |
|---|---|---|---|---|
| Q1 | 0.92 | 0.88 | 0.74 | passed |
| Q2 | 0.95 | **0.71 ✗** | 0.66 | failed |
| Q3 | 0.86 | 0.93 | 0.81 | passed |
| Average | 0.91 | 0.84 | 0.74 | shown only |
| Metric run | pass | **fail** | pass | scan failed |

Faithfulness averages 0.84, above its threshold, and still fails because Q2 scored 0.71. The scan
reads "2 of 3 test cases passed · 2 of 3 metrics passed".

## 10. Results

Two records feed every view. Every finished metric run is a row in `runs`; every scan is a row in
`jobs`. The quick views read runs and pass `since_id` (the session start), so they begin empty on
each load; History reads scans and keeps everything until you delete it. Deletes, and every scan
as it ends (`ui.js followJob`), broadcast setting `runsChangedAt`, so open views (History, Latest
scores) reload.

| Record | Read through | Shown on |
|---|---|---|
| `runs` (per metric run, with `result_json`) | `GET /api/runs/latest?target_id=` (newest run per metric), `GET /api/history?target_id=&metric_key=` (every run of one metric) | Side panel Latest scores; dashboard cards, score trend and Last run case by case; the report |
| `jobs` (per scan) | `backend/dashboard/scans.py`: `GET /api/targets/{id}/scans` (newest first), `GET /api/scans/{id}`, `GET /api/scans/{id}/compare?with=`, `GET /api/scans/{id}/cases?with=` | Dashboard History and the pages below |

Scans are numbered 1, 2, 3, … per chatbot, A/B jobs excepted. Each carries one overall result
(section 9), counts, an average where "lower" metrics count as 1 − score, the skipped metrics, the
run options, and `metric_scores` `[{key, score, status, direction, std, cases}]`. Each History row
has these buttons (pages open in new tabs):

| Button | Page | Shows |
|---|---|---|
| Chatbot form after Save | side panel **Save chatbot** | Stays open on the saved chatbot in edit mode so every stored field is visible (URL, body, reply path, theme, …). Header values show as `Name: ***`; leaving a line as `***` keeps the secret (`app._keep_stored_secrets` for http and mcp). |
| Chatbot form after **Build all test data** | `refreshAfterBuild` → `startEditTarget` | Opens **Edit chatbot** with every parameter the build wrote: persona/role, theme, help URL, probe set (own `target_<id>`), context/tools paths, sources fallback, instructions, available tools list, limits. **Advanced** and **Limits and reply checks** open when any of those are set; a banner lists what Build set. |
| Open | `scan/scan.html?job=<id>` | One scan in full: overall result, started / duration / judge / tokens, the metric table, every metric's reason and cases; links to the scans before and after. Header actions: **Download report (PDF)** opens `report/report.html?target=&job=&all=1` with every section ticked (case cards included even on large scans), then Save as PDF; **Download full report (HTML)** saves one offline file built in the browser (`scan.js downloadFullReport`) with properties, metrics summary, not-run reasons, every metric's every case (question, reply, expected, score, result, judge reason) and the security section. |
| Report | `report/report.html?target=&job=` | Printable evaluation report of **this scan** (cover, summary, cases); Save as PDF from the print dialog. History's **Download report (PDF)** opens the newest finished scan the same way. With `all=1` (from the scan page) every section starts on. Failed single-turn cases mark **word differences** the same way as the app (missing / changed / added highlights, Differences list, How to pass) when Highlight differences is on — in the test-case table and in metric case cards (`failedCaseView`); colours keep in the PDF via `print-color-adjust`. |
| Security | `risk/risk.html?job=<id>#overview` | The scan's **Risk Assessment**, laid out like Confident AI's red-teaming risk profile (see below). |
| Overview | `runs/runs.html?target=&job=#overview` | Test runs, laid out like Confident AI: **Evaluation** (`#evaluation`: every run with a date range, a per-metric chart with ±1 std error bars, % trends, a runs table; tick two to compare), **Home**, a run's **Overview** (properties, results dial, run settings, radar, Histogram / Average / Median / Breakdown, Eval Insights, Send to Confident AI, Send / View in Langfuse, **Download report**: the run as one self-contained HTML file built in the browser by `runs.js downloadReport`, with properties, hyperparameters, metric results, failures and test case scores), **Test Cases** (`#cases`: filters, columns, per-metric tabs, Save as new dataset) and **Compare** (`#compare&with=`). |
| Compare with N | inline in the dashboard | Metric by metric against the scan before: better / worse / same / new / removed. |
| Regressions | `regressions/regressions.html?job=&with=` | The two scans case by case: improved, regressed, new, removed. |
| Delete | in the row | Deletes that one finished scan (`DELETE /api/scans/{id}`); greyed out while it is queued or running. |

- **Risk Assessment** (`GET /api/scans/{id}/risk`, `backend/dashboard/risk.py`, page
  `risk/risk.html?job=` or `?target=` for the newest scan; opened by History's **Security** button and
  the scan page's security section). Built only from the stored scan, nothing judged or sent again.
  DeepTeam-style additions: a **CVSS Risk Score** with its rating (None/Low/Medium/High/Critical) for
  the scan, each DeepTeam category, each vulnerability and each case; results per DeepTeam category
  (`risk.APP_CATEGORIES` for the app's own metrics, the catalog's category for DeepTeam's, Custom for
  custom vulnerabilities) and per subtype; a vulnerability × attack heatmap (`matrix`); an **Attacks** tab
  listing every single-turn and multi-turn attack as broke through / resisted / not run
  (`attack_catalog`, recommended ones starred); **Coverage gaps** (few attack methods, untested
  categories); and a **Mitigation** tab (`mitigations`: each vulnerability an attack got through, by
  priority, with the metric's own advice plus `risk.MITIGATIONS` for its category and the worst failing
  example). The printed report adds the categories, gaps, mitigation plan and attack coverage.
  It keeps the scan's red-team metrics (keys in `redteam_coverage.COVERAGE` or group `security`) and
  gives each test case its vulnerability, the probe's type (first part of its note; the subtype for
  DeepTeam probes), the attack method (enhancer, Baseline, or Multi-turn), a severity from the
  vulnerability (`risk.CRITICAL` keys are critical; else by risk category: access, privacy, agentic
  and illegal activities high, brand image and responsible AI medium), a priority P0–P3 and a
  0–10 risk score: 0 when the attack was resisted, else the severity's base (9.5 / 7.5 / 5.0 / 2.5).
  The scan's **Risk score** (gauge, lower is better) is the mean over its cases: a CVSS-style
  estimate, not a CVSS calculation. Page: KPI cards (total test cases, most common vulnerability,
  most successful attack), **Executive Summary** (date, framework, pass rate, vulnerabilities
  tested, attack vectors, critical and high issues) and tabs **Overview** (impact severity,
  exploitability (High ≥ 50 % of a vulnerability's attacks got through, Medium ≥ 20 %), risk score
  bands, remediation priority, attack success by method, coverage by vulnerability),
  **Vulnerabilities** (critical findings, one row per vulnerability with a pass/fail dash strip and
  pass rate, expanding to its cases), **Test Cases** (search, status and vulnerability filters, 50 per
  page; a row opens a drawer with the verdict, reason, facts, message and reply) and **Frameworks**
  (per OWASP LLM 2025 id: its vulnerabilities and attacks). **Download report** prints an LLM
  Security Risk Assessment (cover, contents, executive summary, remediation table, a page per
  OWASP id, the failed cases); Save as PDF in the print dialog.
- **Eval Insights** (`GET` / `POST /api/scans/{id}/insights`, `backend/dashboard/insights.py`):
  one judge call reads the failed cases and the weak ones (passed within 0.15 of the threshold),
  groups them into issues rated mild or serious and writes an overview; saved in `meta`, so opening
  the page again costs nothing.
- **Report** (dashboard header **Download report (PDF)** → `report/report.html?target=`: each metric's
  latest run of this session; History row **Report** or History **Download report (PDF)** →
  `report/report.html?target=&job=`: that scan only). Tick sections and metrics, then Save as PDF
  from the print dialog. A scan with more than 40 scored rows opens with case cards and diffs off
  so the tab and the print dialog stay responsive; tick them under Sections if you need them. Its **OWASP coverage** section (`report.js owaspSection`, from
  `/api/security/frameworks`) lists, per OWASP list that a reported security metric tests, each
  risk id with those metrics and how many of their attacks were resisted, then the ids nothing
  tested ("not tested is not the same as safe"). Without that endpoint the section is left out.
- **Confident AI** (`backend/confident_upload.py`): turns a stored scan into a DeepEval `TestRun`
  (nothing judged again) and sends it like `deepeval test run` does. `POST /api/scans/{id}/confident`
  sends one; with auto-send on, every finished scan goes. No key, nothing is sent. Multi-turn
  metrics (spec kind `conversation` / `attack`) go as DeepEval conversational test cases, turn by
  turn (the runner's "User: / Bot:" transcript split back into turns), so Confident AI shows the
  conversation. Confident AI refuses a test run that mixes the two, so such a scan goes as two runs,
  "<chatbot> · vN" and "<chatbot> · vN · conversations" (job option `confident.conversations_link`).
  DeepTeam runs (`backend/redteam/confident_push.py`) go to Red Teaming > Risk Assessments
  (`POST /v1/risk-assessments`); Confident AI keeps that for its Enterprise plan, so when it refuses
  for the plan the run goes as a DeepEval test run instead (`confident_push.test_run`: one metric per
  vulnerability, score 1 resisted / 0 vulnerable with DeepTeam's reason, each attack a test case named
  "<attack> → <vulnerability> (<type>)", tagged with vulnerability, type, attack method and risk
  category, CVSS in metadata, the judge as each metric's Evaluation Model and "judge model"
  hyperparameter: `result.judge_model`, saved with every DeepTeam run, else today's judge for older
  runs; multi-turn attacks as a second "· multi-turn" run). The run's `confident` then has
  `as: "test_run"` and a `note`. In the app the link is on the DeepTeam page's **Runs** tab
  (**View in Confident AI ↗** per run) and on its report (`risk/risk.html?deepteam=<id>`).
  Every upload is kept (`backend/confident_reports.py`): one row in table `confident_reports`
  (kind scan / deepteam, the scan's job or DeepTeam run id, chatbot, title, Confident AI test-run
  id, link plus `links` {conversations, multi_turn}, cases, sent as, sent at) and the exact JSON
  sent saved to `reports/confident/<id>-<kind>-<ref>.json` (git-ignored; `JUDGE_CONFIDENT_REPORTS_DIR`
  moves it, tests use a temp dir), so a re-send adds a report and never replaces one. Uploads made
  before the store existed are added once at start (`backfill`, from the links on jobs and DeepTeam
  runs) and their JSON is rebuilt from the stored results on first download (`rebuilt: true`).
  `GET /api/confident/reports?target_id=` lists them, `GET /api/confident/reports/{id}/file`
  downloads the copy (410 when the run behind it was deleted). Page `confident/confident.html`
  (**Sent reports ↗** in the side panel's Confident AI card; also linked from the DeepTeam Runs
  tab): every report with View ↗ / Conversations ↗ / Multi-turn ↗ and Download JSON, filtered by
  chatbot and by Scans / DeepTeam.
- **Langfuse** (`backend/langfuse_upload.py`, optional, Langfuse Cloud or self-hosted): one finished
  scan becomes one trace, "LLM Judge scan <n> · <chatbot>", with a span per metric run and a child
  span per case (the message sent, the reply, expected output and context, the attack or disguise
  used) and Langfuse scores (one per run on the trace, one per case on its span, the judge's reason
  as comment). Spans go to `{host}/api/public/otel/v1/traces` (OTLP/HTTP JSON, header
  `x-langfuse-ingestion-version: 4`; Langfuse's legacy ingestion API stops taking traces on Cloud on
  2026-11-16), scores to `{host}/api/public/ingestion` (`score-create`), both with `requests` and
  basic auth, in requests of at most 1 MB; ids come from the scan, so sending again updates the same
  trace. Keys: side panel card **Langfuse** (`GET`/`PUT /api/langfuse/settings`, meta
  `langfuse_public_key` / `langfuse_secret_key` / `langfuse_host` / `langfuse_auto`, keys returned
  only as hints) or `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` in `.env` (saved
  keys win; host defaults to `https://cloud.langfuse.com`). `POST /api/scans/{id}/langfuse` (body
  `{}`) sends one; the trace link or the error (keys blanked) is kept in the job's options as
  `langfuse` and returned by `GET /api/scans/{id}`; the runs Overview shows **Send to Langfuse** /
  **View in Langfuse**. No keys, nothing is sent; keys and chatbot header values never are.
- **Deleting**: History's **Delete all** (`DELETE /api/targets/{id}/runs`) removes a chatbot's
  runs and finished jobs; a row's **Delete** (`DELETE /api/scans/{id}`) removes one finished scan
  (409 while it is queued or running); `DELETE /api/runs/{id}` removes one run.

## 11. Side paths

- **Chat** (dashboard): `POST /api/chat {target_id, message, history}` calls the chatbot with the
  last 40 turns and returns the reply; a chatbot failure is a 502 with the error type. A
  convenience view: nothing is scored.
- **Judge this answer** (on every chat reply): `POST /api/judge {metric_key, question,
  actual_output, theme?, expected_answer?, context?}` scores that one answer with
  `runner.judge_one`, which builds the same `LLMTestCase` a run would, without a dataset or a
  chatbot call. Metrics that need reference data declare it in `MetricSpec.needs`; when the
  question is one of the theme's goldens the route fills it in, otherwise it says what is missing.
  Nothing is stored, so the trends stay a record of golden-set runs.
- **Your own metrics**: G-Eval metrics, single- or multi-turn, are made in the side panel's G-Eval
  group under Metrics to run (`lib/geval-builder.js`, with a live `GEval(...)` preview); DAG
  metrics on Advanced tools. Both save through `/api/custom-metrics` with `custom_` keys and set
  `metricsChangedAt`, so every pane reloads the catalog.
- **Advanced tools** ([ADVANCED_TOOLS.md](ADVANCED_TOOLS.md)): run options saved in setting
  `runOptions` and added to every job; Compare runs (`GET /api/runs/compare?a=&b=`); A/B test
  (`POST /api/pairwise`: a pairwise job in the same queue, the judge compares two chatbots on each
  golden in both orders, stored in `pairwise_runs`); red-team coverage
  (`GET /api/security/coverage`); Simulate (`POST /api/simulate`: the judge plays the user, the talk
  is scored with conversational metrics and can be saved as a scenario); Generate & review;
  Calibrate (`POST /api/calibration`: your score for a case of a stored run; with calibration on,
  the labels join G-Eval criteria as one extra evaluation step); Export (`GET /api/export/suite`:
  a zip of the test suite for CI, never with header values). Red-team probe metrics export too: their
  `metrics.json` entry is the usual G-Eval one plus `"cases": "probes"`, `tests/evals/probes.json`
  holds the probes this chatbot's probe set would send (with its role as context, as
  `MetricSpec.cases` builds them), and the generated `test_red_team` sends each probe and scores it
  with the same G-Eval. Bias and Toxicity export what the app sends them: their adversarial probes
  first, then the goldens (`"cases": "probes+goldens"`); with no probes in the set they export as
  before. The multi-turn attack and attack enhancers stay in the app (listed under "Not exported").
- **Usage and health**: `GET /api/usage` (judge tokens and calls and chatbot calls since the
  backend started, the dashboard's Tokens used tile; `POST /api/usage/reset`); `GET /api/health`
  (200, or 503 when SQLite fails).

## 12. Metric catalog

90 metrics in eight groups (`UI_CATEGORIES`, shown in this order: Quality, Retrieval, Agent, Conversational, G-Eval, Safety, Text match, Security, Custom), plus your own. Thirteen more DeepEval metrics (`EXTRA_DEEPEVAL_SPECS`): MCP Use, Multi-turn MCP Use and MCP Task Completion (MCP servers only: the server's tool list and each reply's tool calls), Misuse, Non-Advice (DeepEval), Role Violation (DeepEval) (safety, every golden), Topic Adherence and Goal Accuracy (conversation scenarios), Tool Use (scenarios, needs the chatbot's tool list), Tool Permission (opt-in, allow/deny list) and Agent Loop Detection (from reported tool calls, no judge), Pattern Match (full regex match, no judge) and JSON Correctness (opt-in, JSON Schema). Their configuration (domain, advice types, role, topics, tool lists, regex, schema) is a text setting on the Advanced tools page (Metric settings); an empty field shows an example as its placeholder (the setting's `hint`, else its default; Tool Permission's allowed list shows the chatbot's own `available_tools`, with a **Use my chatbot's tools** button that fills them in, and Tool Permission only runs once the allowed or denied list is filled). RAGAS (DeepEval's `RagasMetric`, https://deepeval.com/docs/metrics-ragas) averages five ragas scores (Contextual Precision, Contextual Recall, Context Entities Recall, Answer Relevancy, Faithfulness) on goldens with context; it reads input, actual output, expected output and retrieval context, runs on the app's judge (`backend/judges/ragas_adapter.py`) with local embeddings, is opt-in (unticked by default) and stays unavailable, with the install command on its hover card, until `pip install ragas instructor "datasets<5"` (left out of requirements.txt because instructor forces an older openai; datasets 5 pulls huggingface_hub 2, which breaks transformers and DeepEval's click pin). It averages only the sub-scores ragas returned (ragas gives NaN when it cannot parse the judge's answer or a call times out) and names the missing ones in the reason; with none, the case is an error. `stats.summary_stats` also leaves NaN scores out. Each `MetricSpec` in
`backend/metrics_catalog.py` holds the DeepEval metric builder, how to build an `LLMTestCase` from
a case and a reply, the dataset, what it needs, a default threshold, a preset per environment
(`ENVIRONMENTS`: Local Development, PR / Feature Branch, Staging / QA, Production; Bias, for
example, needs 0.4 / 0.6 / 0.7 / 0.8) and the dashboard's card copy. Every shipped G-Eval metric has
fixed `evaluation_steps`, so the judge never writes its own.

| Group | Metrics | Datasets |
|---|---|---|
| Quality (7) | Faithfulness, Answer Relevancy, Hallucination, Summarization, Prompt Alignment, Robustness (G-Eval), Self-Check Consistency (G-Eval) | `goldens`, `goldens_with_context`, `goldens_without_context` |
| Retrieval (3) | Contextual Precision, Contextual Recall, Contextual Relevancy | `goldens_with_context` |
| Safety (3) | Bias, Toxicity, PII Leakage | `safety_probes`, `goldens` |
| Security (42) | Prompt Injection, Jailbreak, Encoded Injection, Data Exfiltration, Social Engineering, Domain Misuse, Non-Advice, Role Violation, Harmful Content, Multi-turn Jailbreak, and 32 DeepTeam vulnerability metrics (see Security metrics) | `security_probes` |
| G-Eval (4) | Correctness, Helpfulness, Citation Quality, No-Prompt-Leak | `goldens`, `goldens_with_context`, `prompt_leak_probes` |
| Conversational (8) | Conversation Completeness, Knowledge Retention, Role Adherence, Turn Relevancy, Turn Faithfulness, Turn Contextual Precision, Turn Contextual Recall, Turn Contextual Relevancy | `conversations`, `conversations_with_role` |
| Agent (6) | Task Completion, Tool Correctness, Argument Correctness, Plan Quality, Plan Adherence, Step Efficiency | `goldens`, `goldens_with_tools` |
| Text match (3) | BLEU, ROUGE-L, Exact Match (no judge) | `goldens` |
| Your own | G-Eval (single- or multi-turn) and DAG metrics | `custom_metrics.json` |

Scores run from 0 to 1 and every shipped metric is higher-is-better: DeepEval 4 scores
Hallucination, Bias, Toxicity and PII Leakage 1 = clean (`storage._migrate_deepeval_scale` moved
old runs once). Only metrics you make as a violation rate are lower-is-better.

## 13. Tests and CI

- `.venv\Scripts\python -m pytest`: the offline suite, no judge tokens (what CI runs).
- `.venv\Scripts\python -m pytest -m smoke`: fast wiring checks only.
- `set RUN_LIVE_JUDGE=1`, then `.venv\Scripts\python -m pytest -m live`: every metric against the
  real judge (spends tokens).
- `node --test tests/js/report.test.js`: the PDF report's JavaScript (also run by pytest through
  `tests/test_report_js.py`; skipped without Node).
- CI (`.github/workflows/ci.yml`) runs the offline suite on Python 3.13 for pushes to `master` and
  pull requests; the live suite is a manual `workflow_dispatch` job that needs the `JUDGE_API_KEY`
  repository secret.

## 14. Where to change behaviour

| Want to change | Edit |
|---|---|
| Add or retune a metric | `backend/metrics_catalog.py` (one `MetricSpec`) |
| What a metric needs, or when it applies | `needs`, `applies`, `skip_before` / `skip_case` / `not_applicable` on its `MetricSpec` |
| How a case is sent, scored and recorded | `backend/dashboard/runner.py` (`run_spec`) |
| The job queue, parallel metrics, Stop | `backend/dashboard/jobs.py`, `backend/cancel.py` |
| What happens after a scan | `app._after_job` in `backend/dashboard/app.py` |
| Single-answer judging | `judge_one` in `backend/dashboard/runner.py`, `POST /api/judge` |
| Pass threshold | per metric: the threshold next to its tick box under **Metrics to run**, or a **Thresholds for** preset; defaults: each spec's threshold and `presets()` |
| Judge model / provider | side panel **Judge settings**, or `.env`: `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY`. Optional per-run judge result cache (`cache_judge_results`) reuses identical evaluations (test case + criteria) to save tokens. Every metric in the side panel and report now shows whether it is **DeepEval** or **DeepTeam**. Security metrics (DeepTeam, OWASP LLM / Agentic / MCP, red-team) always use the full set of probes with no compromise on coverage. For security metrics you can add your own question and click "Generate similar security probes with AI" to let the judge create related probes from DeepTeam, OWASP, Agentic, MCP, and RAG security topics on the same subject. When you open or add test cases for any metric, the panel now automatically scrolls into view. The UI (dashboard, report, risk page, side panel) has been polished. The Golden answers and Conversation scenarios sections received a major UX upgrade (modern forms, card-style list items, better labels and layout). Manually added test data now triggers automatic re-evaluation of metric applicability.
| Default golden answers, scenarios, probes | `backend/datasets/goldens.default.json`, `conversations.default.json`, `security_probes.default.json` |
| DeepTeam metrics, their test cases, attacks, OWASP maps | `backend/datasets/deepteam_sync.py` (catalog; `deepteam_catalog.default.json` is regenerated with `build_catalog()` + `save_catalog(…, DEFAULT_CATALOG_PATH)`), `metrics_catalog._DEEPTEAM_RULES`, `runner.ATTACK_PROMPTS`, `redteam_coverage.OWASP_MCP_2025` |
| Test data written for a new chatbot | `backend/datasets/autogen.py`; documents in `backend/rag/` |
| Calling the chatbot and reading its reply | `backend/targets/http_client.py`; cURL parsing in `backend/targets/curl.py` |
| Scans, compare, regressions | `backend/dashboard/scans.py` |
| Risk Assessment (severity, risk score, report) | `backend/dashboard/risk.py`, `ChatbotExtension/risk/` |
| API surface | `backend/dashboard/app.py` |
| Web app pages | `ChatbotExtension/` (files are served with `no-cache`, so a reload shows edits) |

## 15. Keeping this document current

The workflow is documented in three places that change with the code:

| Where | What |
|---|---|
| `docs/WORKFLOW.md` | This text. |
| `docs/workflow.html` | The diagrams. Its footer carries the date it was last drawn from the code. |
| [claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv](https://claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv) | `workflow.html`, published. Republish to this same link after every edit (Artifact tool: read the link, then publish `docs/workflow.html` with `url` set to it), so every copy of the link stays current. |

A change that adds or alters behaviour updates the sections it touches in the same change, along
with `CLAUDE.md`, and `README.md` or `ADVANCED_TOOLS.md` where they describe it.

Claude Code enforces this with a Stop hook (`.claude/settings.json` →
`.claude/hooks/docs_guard.py`): when backend code, web-app files, start scripts, `*.default.json`
datasets, `requirements.txt` or CI files are newer than both workflow docs, it stops Claude from
finishing the turn once and asks for the update. A change that needs no doc update is
acknowledged with `python .claude/hooks/docs_guard.py --ack`.

The link opens only for people the page is shared with; share it from the page's Share menu on
claude.ai.
