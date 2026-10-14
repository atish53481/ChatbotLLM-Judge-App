# LLM Judge — Chrome extension

Judge any chatbot's answers with DeepEval metrics from the Chrome side panel.
The extension talks only to the local backend at `http://127.0.0.1:8765`.

## Install

1. Start the backend from the project root: `run-backend.bat`
   (first put `JUDGE_API_KEY=<your judge key>` in the project's `.env`).
2. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**
   and pick this `ChatbotExtension` folder.
3. Click the **LLM Judge** toolbar button. The side panel opens.

## Use

- **Chatbot**: *Add a chatbot* connects a chatbot from a real website. On the site,
  press F12 → Network, send a message, right-click the chat request → Copy →
  **Copy as cURL (bash)** and paste it. The method, URL, headers, body, the message you
  sent (becomes `{{message}}`, earlier turns emptied) and the reply path (found by
  asking the chatbot once) all fill in by themselves; check the reply shown and save.
  If the wrong message was picked, correct it and press **Re-detect**.
  When the site's cookies or tokens expire (401/403), paste a fresh request.
- **Golden answers**: each chatbot uses a golden set (theme). Add, edit or delete
  question / expected-answer pairs (optional context lines and categories) in the side
  panel — **Edit** loads a row back into the form, **Save changes** writes it back.
- **Judge**: tick the metrics to run under **Metrics to run** (a group tick selects the
  whole group; **All** / **None**), set each one's threshold there or pick a preset under
  **Thresholds for**, and press **Run judge (N metrics)**. The latest scores
  chart updates in the side panel and in the dashboard.
- **Dashboard** (*Open dashboard*): chat with the chatbot, see the latest score per
  metric (bar, radar, or table), the trend across runs, and the last run case by case.
  Every chatbot reply has a **Judge this answer** button: it scores just that answer
  with the selected metric, and nothing is saved to the trend.

## Manual smoke checklist

1. Backend running; side panel shows "Judge ready · <model>" (or the JUDGE_API_KEY hint,
   with **Run judge** disabled and a tooltip).
2. Add a chatbot from a pasted cURL; **Test** shows its reply. Golden list shows 10 answers.
3. Add a golden answer, see the count rise; edit it (Edit, change the question, Save
   changes); delete it (two clicks), see it go.
4. Run *Answer Relevancy*: a result line appears, then the latest-scores chart.
5. Open the dashboard: the KPI tiles fill in; the chat answers through the chatbot;
   the case table lists 10 rows.
6. Run the same metric again: the trend chart shows two points; Table views show the
   same numbers.
7. Switch the OS to dark mode: charts redraw with the dark palette.
8. Stop the backend: both views show "Backend not reachable…"; start it again and the
   side panel recovers within 10 seconds.

## Security notes

- The backend accepts requests only from this extension's origin and only on
  `127.0.0.1` / `localhost`.
- API keys for connected chatbots are stored in the local `judge.db` and are never
  sent back to the extension (responses show `***`).
- Captured cookies and tokens are stored like API keys: in `judge.db`, never sent back.

## Limits

- Requests that need a fresh signature or token per message (some anti-bot
  protections) cannot be replayed; use the site's API instead if it has one.
