# Chrome Sidebar LLM Judge

Judge any chatbot's answers with [DeepEval](https://github.com/confident-ai/deepeval)
metrics from a Chrome side panel. A local FastAPI backend scores the answers with a
separate judge model; the extension shows the scores, the trend across runs, and a
chat window for the chatbot being judged.

## Quick start (Windows)

1. Copy `.env.example` to `.env` and set `JUDGE_API_KEY` (a Groq key works with the
   defaults; for a Command Code key also set
   `JUDGE_BASE_URL=https://api.commandcode.ai/provider/v1`).
2. Double-click `run-backend.bat`. The first start creates `.venv` and installs the
   packages (a few minutes); later starts take seconds. The backend listens on
   `http://127.0.0.1:8765`.
3. Load the extension: see [ChatbotExtension/README.md](ChatbotExtension/README.md).

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
- `ChatbotExtension/` — Chrome extension (side panel, dashboard tab).
- `docs/superpowers/` — design spec and implementation plan.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `JUDGE_API_KEY` | required | key for the judge model's OpenAI-compatible API |
| `JUDGE_MODEL` | `openai/gpt-oss-120b` | judge model name |
| `JUDGE_BASE_URL` | `https://api.groq.com/openai/v1` | judge API base URL |
| `JUDGE_DB_PATH` | `judge.db` | SQLite file for chatbots and run history |
| `JUDGE_EXTENSION_ID` | any extension | pin CORS to one installed extension id |
| `JUDGE_LOG_LEVEL` | `INFO` | backend log level (logs paths and status codes, never bodies) |
| `JUDGE_MAX_BODY_MB` | `25` | largest request body accepted (document uploads) |

The judge is never the chatbot being judged.

The judge's API key, model and base URL can also be set in the side panel under
**Judge settings** (with **Test** before saving). Values saved there are stored in
`judge.db`, override `.env`, and the key is never shown again (only its last 4
characters); **Remove saved key** goes back to the `.env` key.

## Metrics

31 DeepEval metrics in six groups, plus your own (`backend/metrics_catalog.py`):

| Group | Metrics (default threshold) |
|---|---|
| Chatbot · safety | Bias ≤ 0.5, Toxicity ≤ 0.5, PII Leakage ≤ 0.1, No-Prompt-Leak (G-Eval) ≥ 0.7 |
| Chatbot · conversational (multi-turn) | Conversation Completeness ≥ 0.7, Knowledge Retention ≥ 0.7, Role Adherence ≥ 0.7, Turn Relevancy ≥ 0.7 (window size setting) |
| RAG · retrieval | Contextual Precision ≥ 0.7, Contextual Recall ≥ 0.7, Contextual Relevancy ≥ 0.7 |
| RAG · quality + Chatbot | Faithfulness ≥ 0.8, Answer Relevancy ≥ 0.7, Hallucination ≤ 0.5, Correctness (G-Eval) ≥ 0.7, Summarization ≥ 0.6, Prompt Alignment ≥ 0.7 (needs the chatbot's instructions), Robustness (G-Eval) ≥ 0.7, Self-Check Consistency (G-Eval) ≥ 0.7 |
| RAG · G-Eval | Citation Quality ≥ 0.7, Helpfulness ≥ 0.7 |
| Security · red team (G-Eval) | Prompt Injection, Jailbreak, Encoded Injection, Data Exfiltration, Social Engineering, Domain Misuse, Non-Advice, Role Violation, Harmful Content, Multi-turn Jailbreak — all ≥ 0.7 |
| Custom · your own metrics | G-Eval rubrics and DAG decision trees you write in **Advanced tools** |

Scores run from 0 to 1. **≥** metrics are higher-is-better. **≤** metrics (Hallucination,
Bias, Toxicity, PII Leakage) are shown as a violation rate, lower is better; DeepEval 4
scores them 1 = clean, so the app shows 1 − score and passes it the matching minimum.

In the side panel, **Metrics to run** lists every metric with a tick box (a group tick
selects the whole group) and its threshold; **Run judge** runs the ticked ones. Every
metric's threshold is set there too: **Thresholds for** picks a preset
(Local Development / PR / Staging / Production, from the threshold strategy) and
**Thresholds per metric** edits any one of them (Custom). A run passes when its average
score meets the threshold (≥ or ≤); each case still shows its own pass/fail.

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

Security metrics send the red-team probes from that file — pick **Generic** (any assistant, the default) or **E-commerce** (orders, refunds) under the target's **Security probes**; set the target's **Chatbot role** in the side panel so Domain Misuse, Non-Advice and Role Violation know what the bot is meant to be.

The dashboard header picks the target, **Cases per run** (1 / 3 / 5 / all) and runs every visible card with **Run all visible**. Tiles show chatbot, RAG-context and judge status, **Tokens used** (judge tokens and calls since the backend started), the **Average score** of the shown cards (lower-is-better metrics counted as 1 − score), and pass / fail / pending.

The dashboard shows, per metric, what it **scores on** (question, actual result, expected
result, context, retrieved context, conversation) and the G-Eval criteria; **Details** lists
every case with its question, actual result, expected result, context, score and the
judge's reason (why).

A **Run judge** sweep asks every golden question and scores the answers. To judge a
single answer instead, chat with the chatbot in the dashboard and press **Judge this
answer** on its reply — ad-hoc scores are not saved, so they never enter the trend.

## Advanced tools

**Advanced tools** (side panel button, dashboard header) opens a page with:

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
- **Red-team coverage**: latest results by risk category (Responsible AI, Illegal
  Activities, Brand Image, Data Privacy, Unauthorized Access) and OWASP LLM Top 10 id.
- **Simulate a user**: the judge plays a user with a goal against the real chatbot, then
  conversational metrics score it; save the conversation as a fixed scenario.
- **Generate & review**: golden generation from a help page with DeepEval evolutions,
  quality filtering and styling, saved as drafts that runs skip until you accept them.
- **Calibrate the judge**: score cases yourself; see how often the judge agrees, and feed
  your scores to G-Eval metrics as examples.
- **Export to CI**: a zip with a pytest suite and a GitHub Actions workflow that runs the
  chosen metrics on every pull request. Header values (cookies, keys) are never exported.

## Chatbot limits and reply checks

Each chatbot's form has **Limits and reply checks**, so one run cannot quietly
record scores that mean nothing:

- **Service messages are errors, not answers.** A short reply such as "daily limit
  reached", "please log in", "session expired" or an empty reply stops the run with
  an error and records nothing. Untick *Treat service messages as errors* only if
  the chatbot's real answers look like that.
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

A Chrome notification also says how the run went ("Shop bot: 18 passed · 2 failed
(Faithfulness, Bias)"; for a single metric that could not run, why), and the
toolbar icon shows a badge: a green ✓ when everything passed, the number of failed
metrics in red, or an amber ! when a metric could not run. Clicking the
notification opens (or focuses) the dashboard; opening the side panel or dashboard
clears the badge. A run you stop yourself is not announced. Runs are backend
jobs: closing the side panel or dashboard does not stop them, reopening either
shows the live progress, and the notification arrives when the job ends (within 30
seconds if no page is open). If no notification appears, Windows may be hiding them:
allow Google Chrome under Settings → System → Notifications and check Focus assist /
Do not disturb.

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
- **Metric details**: each metric's meaning and verdict and every case (failed first) with
  the question, actual and expected answer (differing words in bold for single-turn cases)
  and the judge's reason; then how to read the scores.

Before saving, tick on the page which sections and which metrics (All / Failed only /
None, or one by one) go into the PDF; the choice is remembered in this browser and is
not printed. Then click **Save as PDF** and pick *Save as PDF* as the printer. Case
details come from the runs this browser made; a metric run elsewhere shows its score
without the cases. Every metric is handled from the catalog's data, so new metrics
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
last until the side panel is loaded again: every fresh load (reopening the panel or
reloading the extension) restores the shipped sets from
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
