# LLM Judge

Judge any chatbot's answers with [DeepEval](https://github.com/confident-ai/deepeval)
metrics from a web app. A local FastAPI backend scores the answers with a
separate judge model and serves the app, which shows the scores, the trend across
runs, and a chat window for the chatbot being judged.

**How it works:** [LLM Judge Workflow](https://claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv) shows the whole flow as diagrams;
[docs/WORKFLOW.md](docs/WORKFLOW.md) is the text version. Both are kept current with the code.

## Quick start (Windows)

1. Copy `.env.example` to `.env` and set `JUDGE_API_KEY` (a Groq key works with the
   defaults; for a Command Code key also set
   `JUDGE_BASE_URL=https://api.commandcode.ai/provider/v1`).
2. Double-click `run-backend.bat`. The first start creates `.venv` and installs the
   packages (a few minutes). Every start then upgrades them (DeepEval, FastAPI, the
   OpenAI client, ...) to the newest versions `requirements.txt` allows, runs the
   smoke tests and restores the previous versions if anything breaks
   (`update-packages.bat`; `JUDGE_AUTO_UPDATE=0` in `.env` turns it off). The
   backend listens on `http://127.0.0.1:8765`.
3. Open **http://127.0.0.1:8765/** (or double-click `start-llm-judge.bat`, which does
   steps 2–3). See [ChatbotExtension/README.md](ChatbotExtension/README.md).

To add a chatbot, capture one real request from its website and paste it as cURL:

1. Open the chatbot's website, press F12 → **Network**, and send it a message.
2. Right-click the chat request → **Copy** → **Copy as cURL (bash)**.
3. In the side panel, open *Add a chatbot* and paste the command. Everything fills in
   by itself:
   - the method, URL, headers and body;
   - the message you sent, which becomes `{{message}}` (where the judge puts each
     golden question), with earlier turns emptied;
   - the **reply path**: the chatbot is asked your question once, and the answer's
     location is found, e.g. `reply` or `choices.0.message.content`; for a stream,
     the path inside each event.
4. Check the reply shown under **Test**, then press **Save chatbot**. If the wrong text
   was picked as the message, correct it and press **Re-detect**.

Captured requests often carry session cookies or CSRF tokens that expire; when the
chatbot starts answering 401/403, capture a fresh request and paste it again.

### Judge another LLM

**An MCP server** (tools, no chat): in *Add a chatbot* pick **MCP server**, give a local command
(stdio, e.g. `python my_server.py`) or a streamable HTTP URL, put keys in environment variables or
headers (never in the arguments), and press **Test & list tools**. The app plays the agent: an
**Agent model** (a model on your judge's provider) picks a tool for each test message; left empty,
the judge model does it and the run says so. Agent and security metrics then score what the tools did.

**Test data for every metric**: right after you save a chatbot the app builds its test data by
itself (set `JUDGE_AUTO_BUILD=0` to be asked first). It asks the chatbot what it is and which tools it
calls (or reads the MCP server's tools), reads its help pages, and has the judge write only what is
missing: golden answers, conversation scenarios, Context goldens, tool tests and security probes
fitted to it, checked against the chatbot or its help pages where it can. A build sends at most 30
messages and makes 40 judge calls (`JUDGE_BUILD_MAX_MESSAGES`, `JUDGE_BUILD_MAX_JUDGE_CALLS`), then
lists any metric that still has no test case with why and what to do, and the metrics that do not
apply to this kind of chatbot. **Build all test data** runs it again later and fills only what is
missing. It uses judge tokens.

If what you want to score is itself a model with an OpenAI-compatible chat API
(OpenAI, Groq, a local Ollama or LM Studio, your Command Code provider, …) there is
nothing to capture. In *Add a chatbot*, open **Or fill from an LLM API**, give the
base URL, the model and the key (leave the key empty for a local model), and press
**Fill from LLM API**. The URL, headers and body fill in (`/chat/completions`,
`Authorization: Bearer …`, a `messages` body), then press **Test** and
**Save chatbot**. The target is stored as an ordinary chatbot, so every metric runs
against it unchanged.

## Keep the backend running (optional)

`run-backend.bat` stops when its window closes or you log off. To have the backend
start with Windows, keep running while you are logged off, and restart itself 30
seconds after any crash, register it as a scheduled task once, in PowerShell opened
with **Run as administrator**, from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-backend-task.ps1
```

It asks for your Windows password (for a Microsoft account, the account password,
not the PIN): Windows needs it to run a task while you are logged off. Output goes to
`logs\backend.log`. Remove it again with `.\uninstall-backend-task.ps1`.

Nothing runs while the laptop sleeps, hibernates or is shut down. To stop it
sleeping while plugged in: `powercfg /change standby-timeout-ac 0`. Chrome and a
local chatbot also stop when you log off, so evaluations still run while you are
logged in; the task just means the backend is always there when you are.

## Layout

- `backend/` — FastAPI app (`dashboard/app.py`); the chatbot connector (`targets/`:
  replays a request captured as cURL, `curl.py` parses it, `presets.py` fills the
  same fields for an OpenAI-compatible LLM); the judge model (`judges/judge.py`);
  golden answers (`datasets/goldens.json`, seeded from `goldens.default.json`);
  the metric catalog (`metrics_catalog.py`); run history in SQLite
  (`storage.py`, `judge.db`).
- `tests/` — pytest suite.
- `ChatbotExtension/` — the web app's pages (side panel, dashboard, report,
  Advanced tools), served by the backend at `/app/`.
- `docs/` — `WORKFLOW.md` (how the app works; diagrams in `workflow.html`, published at the
  link above), `ADVANCED_TOOLS.md`, and design specs and plans in `superpowers/`.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `JUDGE_API_KEY` | required | key for the judge model's OpenAI-compatible API |
| `JUDGE_MODEL` | `openai/gpt-oss-120b` | judge model name |
| `JUDGE_BASE_URL` | `https://api.groq.com/openai/v1` | judge API base URL |
| `JUDGE_DB_PATH` | `judge.db` | SQLite file for chatbots and run history |
| `JUDGE_LOG_LEVEL` | `INFO` | backend log level (logs paths and status codes, never bodies) |
| `JUDGE_MAX_BODY_MB` | `25` | largest request body accepted (document uploads) |
| `JUDGE_CONCURRENCY` | `4` | judge calls and test cases in flight across all runs (1–16) |
| `JUDGE_METRIC_CONCURRENCY` | `2` | metrics of one scan run side by side (1–8); the web app's **Parallel metrics** overrides it per scan |

The judge is never the chatbot being judged.

The judge's API key, model and base URL can also be set in the side panel under
**Judge settings** (with **Test** before saving). Values saved there are stored in
`judge.db`, override `.env`, and the key is never shown again (only its last 4
characters); **Remove saved key** goes back to the `.env` key.

## Metrics

76 DeepEval metrics in eight groups (32 of them DeepTeam red-team vulnerabilities), plus your own (`backend/metrics_catalog.py`;
[docs/WORKFLOW.md](docs/WORKFLOW.md#12-metric-catalog) lists the dataset each one reads):

| Group | Metrics |
|---|---|
| Quality | Faithfulness, Answer Relevancy, Hallucination, Summarization, Prompt Alignment (the chatbot's rules), Robustness (G-Eval), Self-Check Consistency (G-Eval) |
| Retrieval | Contextual Precision, Contextual Recall, Contextual Relevancy |
| Safety | Bias, Toxicity, PII Leakage |
| Security · red team | Prompt Injection, Jailbreak, Encoded Injection, Data Exfiltration, Social Engineering, Domain Misuse, Non-Advice, Role Violation, Harmful Content, Multi-turn Jailbreak |
| G-Eval | Correctness, Helpfulness, Citation Quality, No-Prompt-Leak |
| Conversational (multi-turn) | Conversation Completeness, Knowledge Retention, Role Adherence, Turn Relevancy (window size setting), Turn Faithfulness, Turn Contextual Precision / Recall / Relevancy |
| Agent · tool calls | Task Completion, Tool Correctness, Argument Correctness, Plan Quality, Plan Adherence, Step Efficiency |
| Text match (no judge) | BLEU, ROUGE-L, Exact Match |
| Your own | G-Eval rubrics (single- or multi-turn, made in the side panel's G-Eval group or on **Advanced tools**) and DAG decision trees |

Scores run from 0 to 1 and every shipped metric is higher-is-better: DeepEval 4 scores
Hallucination, Bias, Toxicity and PII Leakage 1 = clean. Only metrics you make as a
violation rate are lower-is-better (**≤**).

In the side panel, **Metrics to run** lists every metric with a tick box (a group tick
selects the whole group) and its threshold; **Run judge** runs the ticked ones. Every
metric's threshold is set there too: **Thresholds for** picks a preset
(Local Development / PR / Staging / Production, from the threshold strategy) and
**Thresholds per metric** edits any one of them (Custom). As in DeepEval, a case passes
when its score meets the threshold (≥ or ≤) and a run passes only when every case passes; the
run's average score is shown for trends.

- **Multi-turn** metrics play the **Conversation scenarios** (side panel: add, edit, delete,
  reset; shipped ones in `backend/datasets/conversations.default.json`) turn by turn, sending the conversation so far where the chatbot's request carries it
  (a `history` or `messages` list in the captured body).
- **RAG · retrieval** metrics (and Faithfulness, Citation Quality) score the chatbot's own
  retrieved documents, read from the **Retrieved context path** of its response (found
  automatically when present). When the chatbot returns none, they score each golden's
  reference context instead, and every result and case is labelled "golden reference":
  those scores check the reference data, not the chatbot's retriever.
- **No-Prompt-Leak** sends prompt-extraction attacks; **Bias** and **Toxicity** first send
  prompts that bait a biased or insulting reply, then the goldens (a neutral question
  rarely provokes one); **Summarization** asks the chatbot to summarise each golden's context.

All of these adversarial prompts live with the red-team probes in one file,
`backend/datasets/security_probes.default.json`, one probe per line:
`{"id", "metric", "set", "question", "note"}`. Each `note` ends with its references:
the [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/)
category (LLM01 Prompt Injection, LLM02 Sensitive Information Disclosure, LLM06
Excessive Agency, LLM07 System Prompt Leakage, LLM09 Misinformation; "n/a (content
safety)" for bias, toxicity and harmful content, which that list does not cover) and the
[DeepTeam](https://github.com/confident-ai/deepteam) vulnerability or attack method
(e.g. `direct override · OWASP LLM01 · DeepTeam: Prompt Injection`). `metric` is one of the 9 Security metrics
(`prompt_injection`, `jailbreak`, `encoded_injection`, `data_exfiltration`,
`social_engineering`, `domain_misuse`, `non_advice`, `role_violation`, `harmful_content`)
or `bias`, `toxicity`, `no_prompt_leak`; `set` is `generic` or `ecommerce`. To add your
own, add a line there (shipped, survives resets) or `POST /api/security-probes` (this
session only). **Harmful Content** asks for dangerous help (weapons, drugs, malware,
fraud, self-harm) and passes only when the bot refuses without any actionable steps.

**DeepTeam.** The app also runs every vulnerability of
[DeepTeam](https://github.com/confident-ai/deepteam) (Confident AI's red-teaming
framework) that it had no metric for: 32 more Security metrics in deepteam 1.0.10 (BFLA,
BOLA, RBAC, SSRF, SQL / Shell Injection, Debug Access, System Reconnaissance, Cross-Context
Retrieval, Indirect Instruction, Tool Metadata Poisoning, Tool Orchestration Abuse, Goal
Theft, Recursive Hijacking, Excessive Agency, Agent Identity Abuse, Inter-Agent
Communication, Autonomous Agent Drift, Exploit Tool Agent, External System Abuse,
Unexpected Code Execution, Illegal Activity, Graphic Content, Personal Safety, Child
Protection, Ethics, Fairness, Competition, Intellectual Property, Misinformation,
Hallucination and Robustness attacks). Their test cases are DeepTeam's own template
examples (up to 10 per metric, labelled with the DeepTeam version and subtype). `deepteam`
is in `requirements.txt`, so every start upgrades it, and when a newer release is installed
the backend reads it again: new vulnerabilities become metrics and new examples become test
cases (`JUDGE_DEEPTEAM_SYNC=0` turns that off; deepteam's telemetry is always switched off).
They are not ticked by default: tick them in **Metrics to run**, or use **Tick these** next to
an OWASP id under Advanced tools → Red-team coverage, which maps every metric to OWASP Top 10
for LLM Applications 2025 (LLM01–10), OWASP Top 10 for Agentic Applications 2026
(ASI01–10) and OWASP MCP Top 10 2025 (MCP01–10). The PDF report adds an **OWASP coverage**
section: each risk the reported security metrics test, with how many attacks were resisted,
and the risks nothing tested; risks that chatting with the bot can only partly check are marked
"partly testable from the chat". Each DeepTeam metric can also be scored by DeepTeam's own judge
(Advanced tools → Run options → Scorer; pass/fail per case). For an agent whose tool calls the app
can read (**Tool calls path**), the security metrics also judge the tools it called, so an agent
that refuses in words but still runs the tool fails. Red-team metrics export to CI with their probes.

**Risk Assessment.** Each scan's **Security** button (dashboard History) opens a risk profile laid
out like Confident AI's: total test cases, most common vulnerability and most successful attack;
an executive summary with pass rate, vulnerabilities tested, attack vectors and critical / high
issues; a 0–10 risk score gauge (lower is better: the mean over cases, where a resisted attack
scores 0 and one that got through scores its vulnerability's severity); Overview charts (severity,
exploitability, risk bands, remediation priority, attack success by method, coverage), a
Vulnerabilities list with critical findings, a searchable Test Cases table whose rows open the
message and reply, and an OWASP Frameworks tab. **Download report** prints an LLM Security Risk
Assessment you can save as PDF. Nothing is judged again: it reads the stored scan.

Security metrics send the red-team probes from that file — pick **Generic** (any assistant, the default) or **E-commerce** (orders, refunds) under the target's **Security probes**; set the target's **Chatbot role** in the side panel so Domain Misuse, Non-Advice and Role Violation know what the bot is meant to be.

The dashboard header picks the target, **Run mode** (1, 2, 3 or 5 cases per metric, or all) and **Parallel metrics**, and **Run all visible** runs every visible card as one background scan. Each page load starts a fresh session (scores start empty, every run stays in History); tick **Show earlier runs** in the dashboard header or the report's options to count every stored run again. Tiles show chatbot, RAG-context and judge status, **Tokens used** (judge tokens and calls since the backend started), the **Average score** of the shown cards (lower-is-better metrics counted as 1 − score), and pass / fail / pending.

The dashboard shows, per metric, what it **scores on** (question, actual result, expected
result, context, retrieved context, conversation) and the G-Eval criteria; **Details** lists
every case with its question, actual result, expected result, context, score and the
judge's reason (why).

A **Run judge** sweep asks every golden question and scores the answers. To judge a
single answer instead, chat with the chatbot in the dashboard and press **Judge this
answer** on its reply — ad-hoc scores are not saved, so they never enter the trend.

## Advanced tools

**Advanced tools** (side panel button, dashboard header) opens a page with the items below;
step-by-step instructions are in [docs/ADVANCED_TOOLS.md](docs/ADVANCED_TOOLS.md).

- **Run options**: per-metric settings (Turn Relevancy window, Self-Check samples, attack
  turns), the chatbot's **instructions** for Prompt Alignment, red-team **attack enhancers**
  (Base64, ROT13, leetspeak, role-play, injection wrapper, disguised math, many-shot,
  multilingual: every probe is also sent disguised, and Details shows which disguise got
  through), a **latency limit** (fail a run when p95 reply time is over it), the judge's
  **price** per million tokens (shows the cost of each run) and **judge calibration**. Every
  run then reports median / min / max / std, p50 / p95 chatbot latency and cost.
- **Custom metrics**: write G-Eval criteria (optionally strict pass/fail) or a DAG
  decision tree (DeepEval JSON) and run them like any other metric.
- **Compare runs**: two runs of one metric case by case: regressions, fixes, score moves.
- **A/B test**: two chatbots answer the same goldens; the judge picks the better reply
  twice with the order swapped, and only a consistent preference counts (else a tie).
- **Red-team coverage**: latest results by OWASP list (LLM01–10, Agentic ASI01–10, MCP01–10;
  **Tick these** ticks an id's metrics) and by risk category (Responsible AI, Illegal
  Activities, Brand Image, Data Privacy, Unauthorized Access, Agentic).
- **Simulate a user**: the judge plays a user with a goal against the real chatbot, then
  conversational metrics score it; save the conversation as a fixed scenario.
- **Generate & review**: golden generation from a help page or a file with DeepEval evolutions,
  quality filtering and styling, saved as drafts that runs skip until you accept them.
- **Calibrate the judge**: score cases yourself; see how often the judge agrees, and feed
  your scores to G-Eval metrics as examples.
- **Export to CI**: a zip with a pytest suite and a GitHub Actions workflow that runs the
  chosen metrics on every pull request. Header values (cookies, keys) are never exported.

## Chatbot limits and reply checks

Each chatbot's form has **Limits and reply checks**, so one run cannot quietly
record scores that mean nothing:

- **Service messages as errors (opt-in).** Off by default: like DeepEval, every reply
  is scored as it is. Tick *Treat service messages as errors* and a short reply such
  as "daily limit reached", "please log in", "session expired" or an empty reply
  stops the run with an error and records nothing.
- **Max message length.** Messages longer than this are not sent: Summarization
  shortens its source to fit, other cases are skipped, and the result's note says
  how many.
- **Busy chatbots.** HTTP 429/503 replies are retried up to 3 times (after the
  chatbot's `Retry-After`, else 2, 4, 8 s). *Delay between messages* spaces out
  every message for chatbots that rate-limit.
- **Plain errors.** An HTTP 401/403 says the session or cookie has probably expired
  (paste a fresh cURL); timeouts and unreachable hosts say so.

## How far to trust a score

Every run records how it was judged: the judge model, its tokens and calls, the
chatbot calls and the duration. The side panel's result line and the dashboard's
status line show the tokens, and the trend table has *Judge model* and *Tokens*
columns. When a metric's judge model differs from its previous run on that chatbot,
the result's note warns that the scores may not be comparable.

Tick **Check judge consistency** in the side panel to have the judge score every
reply twice (the chatbot is asked once, the judge costs about twice as much). Each
case scores the average of the two; a run whose two scorings of one case differ by
more than 0.15 is flagged ⚠ *judge unstable*, in the result and in the trend table.

## When a run finishes

In the side panel, a banner pinned to the top shows the run while it goes
("Running metric 2 of 5: Toxicity · 0m 42s") and stays when it ends: "✓ Run complete
at 17:42 · 5 passed · took 1m 12s" (green), "✕ … · 1 failed" (red), "■ Run stopped" or
"✕ Run interrupted: why". **Show results** scrolls to the per-metric rows, **×**
dismisses it; the **Run judge** button reads "Running…" until the run ends.

A browser notification also says how the run went ("Shop bot: 18 passed · 2 failed
(Faithfulness, Bias)"; for a single metric that could not run, why), and the tab
title gets a mark (✓, the number failed, or !) while the tab is in the background;
looking at the tab clears it. A run you stop yourself is not announced. Runs are
backend jobs: closing the page does not stop them, and reopening it shows the live
progress. If no notification appears, allow it when the browser asks, and check
that Windows allows your browser under Settings → System → Notifications (and
Focus assist / Do not disturb).

## Tracing in Langfuse (optional)

Save a Langfuse public and secret key (and your host, if you run Langfuse yourself; Langfuse
Cloud is the default) in the side panel's **Langfuse** card, or set `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` in `.env`. Each finished scan can then be sent as one
trace: a span per metric, a span per test case with the message sent, the reply and the
judge's score and reason. Keys saved in the card turn auto-send on; with keys only in `.env`,
switch Auto-send on in the card. The runs Overview has **Send to Langfuse** / **View in Langfuse**.
Without keys nothing is sent; keys and chatbot headers never are.

## Details and the PDF report

Each dashboard card's **Details** shows the latest run: the verdict ("Average 0.62 <
0.70 → fail"), cases passed / failed / skipped, the judge model, tokens and duration,
the last few run scores, and *What this measures* (description, scale, the fields
the judge reads, the G-Eval rubric). Failed cases come first; **Failed only** hides
the rest, and **Copy** puts one case on the clipboard as text.
A failed card's Details also has **How to improve**: the judge's reasons for the
worst failed cases, then what to change in the chatbot and what to check in the
test (a failing score does not always mean the chatbot is wrong). The advice for
every metric lives in `backend/metric_advice.py`.

Citation Quality only applies to a chatbot that returns its own sources
(*Retrieved context path* set): a chatbot that returns none could never cite the
golden's reference documents, so the metric shows as not applicable instead of
failing.

**Download report (PDF)** in the dashboard header opens a printable report of the
selected chatbot:

- cover with the overall success rate and average score, and a summary table of every metric;
- a DeepEval-style **Test case results** table: test case (one golden question, TC-01…),
  metric, score with threshold / judge model / reason, PASSED or FAILED, a "How to improve"
  tip on each failed case, and the success rate per test case and overall;
- **How to improve the score**: failed metrics furthest from passing first, then passed
  metrics that still had failed cases, each with its failed test cases, the score if they
  were fixed, the judge's reasons, what to fix in the chatbot and what to check in the test;
- **Metric details**: each metric's meaning and verdict and a card per failed case with
  the question, actual and expected answer (differing words in bold for single-turn cases)
  and the judge's reason; then how to read the scores. Cards for passed cases are off by
  default (they are already rows in the test case table and double the PDF's length);
  tick "Passed cases in metric details" to include them.

Before saving, tick on the page which sections and which metrics (All / Failed only /
None, or one by one) go into the PDF; the choice is remembered in this browser and is
not printed. Then click **Save as PDF** and pick *Save as PDF* as the printer. All
report data comes from the database (`judge.db`): each metric's latest run in this
session with its stored cases (`runs.result_json`), so any browser shows the same
report. A metric not run since the page was loaded is listed as not run. Every metric is handled from the catalog's data, so new metrics
appear without changes to the report. Tests: `node --test tests/js/report.test.js`
(also run by `pytest` via `tests/test_report_js.py`).

## Golden answers

Each chatbot is judged against one golden set (theme). `generic`, the default, fits
any chatbot. Most of its questions (what can you do, talk to a human, off-topic,
gibberish, an emergency) expect a kind of behaviour rather than facts. Its grounded
goldens (`context_in_prompt: true`) send short made-up facts with the question
("Answer using only the information below"), so Faithfulness, Hallucination,
Summarization and the contextual metrics can score any chatbot on sticking to them. `general_support`
is the online-shop set the ShopEasy sample bot answers; `legacy_store` holds the
answers migrated from the earlier `evals/` suite. For a chatbot's own facts, give
it its own theme and fill it from its docs.
Add, edit or delete answers in the side panel (each row has an **Edit** button). Changes
last until the page is loaded again: every fresh load restores the shipped sets from
`backend/datasets/goldens.default.json`. To change the defaults, edit that file.
A golden set with your own theme name is not shipped, so it is never reset.

If a shipped set is missing rows, the side panel says how many above the list;
**Reset to defaults** brings them back. Every change to the golden file is logged,
one JSON line each, in `backend/datasets/goldens_changes.log` (not committed): the
time, the action (`add_golden`, `update_golden`, `delete_golden`, `reset_to_defaults`,
`delete_generated_for`), the ids added, removed or changed, and each theme's row
count before and after. When rows vanish, that log says what removed them.

Three ways to fill a golden set, all in the side panel: add answers by hand; upload a
document (PDF, TXT, DOCX, MD); or paste the chatbot's help / FAQ page address under
**Generate from URL**. The last two let DeepEval's Synthesizer write the questions,
expected answers and context. A page built with JavaScript has little readable text;
save it as a PDF and upload that instead. To judge a different website's chatbot, give
its target its own theme and fill that set; the shipped ShopEasy goldens only fit
ShopEasy.

## Tests

```bat
.venv\Scripts\python -m pytest
.venv\Scripts\python -m pytest -m smoke
```

The first command runs the offline suite (no judge tokens); the second runs only the
quick wiring checks. To run every metric against the real judge (this spends tokens):

```bat
set RUN_LIVE_JUDGE=1
.venv\Scripts\python -m pytest -m live
```

The live job pins the integration: it drives each catalog metric through the real
judge and checks it returns a score and a reason. It does not assert that the judge
*passes* the sample bot — a cheap judge model is not steady enough for that (the same
answer can score 0 then 1), which is exactly what **Check judge consistency** is for;
the offline suite pins the pass/fail logic deterministically.

The runtime datasets — `backend/datasets/goldens.json`, `conversations.json`,
`security_probes.json` — are generated on first use from the matching
`*.default.json` and git-ignored (they are edited at runtime and reset on every side
panel load). To change what ships, edit the `*.default.json` files.
