# Advanced tools: how to use them

Open the page from the **Advanced tools** button at the bottom of the side panel or in the
dashboard header (web app: http://127.0.0.1:8765/app/advanced/advanced.html). Pick the
chatbot in the top-right **Chatbot** list; every section works on that chatbot. Each
section says what it costs: most features call your judge model, a few call the chatbot
several times per question.

## Run options (affects every run you start afterwards)

Nothing here is on until you save it, and with everything off runs behave as before.

1. **Metric settings**: Turn Relevancy window (how many earlier turns the judge reads),
   Self-Check replies per question, Multi-turn Jailbreak attack turns.
2. **Chatbot instructions**: paste the chatbot's rules, one per line (e.g. "Answer in
   English", "Never promise delivery dates") and press **Save instructions**. Prompt
   Alignment checks each one; without them that metric shows as unavailable.
3. **Red-team attack enhancers**: tick disguises (Base64, ROT13, leetspeak, role-play,
   injection wrapper, math puzzle, many-shot, other language). Every security probe is then
   also sent in each disguise. Each one adds a chatbot call per probe; Multilingual also asks
   the judge to translate.
4. **Performance and cost**: a latency limit in seconds (a run fails when 95% of replies are
   slower), the judge's price per million tokens (each run then shows its cost), and
   **Give the judge my calibration labels** (see Calibrate the judge).
5. Press **Save run options**, then run metrics from the side panel or dashboard as usual.
   **Details** now also shows score spread, chatbot latency, cost and, for security metrics,
   which disguise got through.

## Custom metrics

- **G-Eval**: name it, write the criteria ("Is the reply polite and free of sarcasm?"),
  tick what the judge reads (question, answer, expected answer, context), set the
  threshold. Tick **Strict** for a plain pass/fail.
- **DAG**: rules as a decision tree that scores the same way every time. Press
  **Insert an example**, edit the JSON (judgement nodes ask yes/no or multiple-choice
  questions, verdict nodes give a score 0-10), save.
- Saved metrics appear under **Custom** in the side panel tick list and the dashboard
  chips; run them like any metric. **Restore the examples** brings back the two samples.

## Compare runs

Pick a metric, a **Before** and an **After** run, press **Compare**. You get the score
change and a table: **regressed** (passed before, fails now) first, then fixed, added,
removed. Use it after changing the chatbot's prompt or model. A metric needs two runs.

## A/B test

Choose chatbot **B** (A is the selected chatbot), the number of cases, optionally what
"better" means, then **Start A/B test**. Both chatbots answer A's golden questions; the judge
picks the better reply twice with the order swapped, and only a consistent choice counts
(otherwise a tie). A note warns when the winner is just much longer. Past comparisons are
listed below.

## Red-team coverage

Read-only: the latest result of every security and safety metric by risk category and
OWASP LLM Top 10 id. "not run" means untested, not safe; run the Security metrics from the
dashboard to fill it.

## Simulate a user

Pick a saved scenario or describe one, say what the user wants by the end, who the user
is, and the max turns. Tick metrics to score with (e.g. Conversation Completeness) and
press **Run simulation**. The judge plays the user and reacts to the chatbot's real
replies. Type a name in **Save as scenario** to keep the conversation as a fixed scenario
for the regular conversational runs.

## Generate & review

Enter a help-page URL or choose a file, optionally tick **evolutions** (make questions
harder: reasoning, comparisons, hypotheticals...), a quality bar, retries, and the
scenario / task / formats so questions sound like your users. Keep **Save as drafts** ticked:
the new golden questions wait under **Drafts** and runs skip them until you **Accept**
(or **Reject**) them, one by one or all at once.

## Calibrate the judge

Run a metric first, then pick it here. Each case of its latest run is listed with the
judge's score: enter your own score (0 to 1) and a reason, **Save**. The top line shows how
often you and the judge agree. With **Give the judge my calibration labels** on (Run
options), G-Eval and custom G-Eval metrics get your scored cases as examples.

## Export to CI

Choose which environment's thresholds to use and tick the metrics, then **Download suite
(.zip)**. Unzip it at the root of your chatbot's repository: `tests/evals/` holds the
questions, metrics and a pytest file, `.github/workflows/llm-evals.yml` runs it on every
pull request. Add the repository secrets `JUDGE_API_KEY` and `LLM_EVAL_HEADERS` (the
chatbot's headers as JSON, e.g. `{"Authorization": "Bearer ..."}`); header values are
never in the zip. `tests/evals/README.md` lists what was left out and why.
