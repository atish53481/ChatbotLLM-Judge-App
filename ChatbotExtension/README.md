# LLM Judge — web app

Judge any chatbot's answers with DeepEval metrics in your browser. These pages are
served by the local backend at `http://127.0.0.1:8765/app/` and talk only to it.
There is no build step.

## Start

**One click:** double-click `start-llm-judge.bat` in the project root. It starts
the backend when it is not running yet and opens **http://127.0.0.1:8765/** in
your browser once it answers (close its window to stop the backend). For a
desktop button, run once:
`powershell -ExecutionPolicy Bypass -File .\create-desktop-shortcut.ps1`
— it creates an **LLM Judge** shortcut that runs the same file.

By hand: put `JUDGE_API_KEY=<your judge key>` in the project's `.env`, start the
backend with `run-backend.bat`, then open **http://127.0.0.1:8765/**. The side
panel is on the left, the dashboard on the right; the PDF report and Advanced
tools open in new tabs.

Settings (selected chatbot, ticked metrics, thresholds) are kept in the browser's
`localStorage`. When a run ends you get a browser notification (allow it when
asked) plus a mark in the tab title. A run keeps going on the backend if you close
the page; reopening it follows the run again.

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
- **Dashboard**: chat with the chatbot, see the latest score per metric, the trend
  across runs, and the last run case by case. Every chatbot reply has a **Judge this
  answer** button: it scores just that answer with the selected metric, and nothing
  is saved to the trend.
- **Fresh start and history**: each page load starts with empty scores. Every run
  stays stored in `judge.db`; the dashboard's **History** tab lists them all, with
  **Delete** per run and **Delete all**.

After changing these files, hard-refresh the page (Ctrl+Shift+R) so the browser
does not keep old copies.

## Manual smoke checklist

1. Backend running; side panel shows "Judge ready · <model>" (or the JUDGE_API_KEY hint,
   with **Run judge** disabled and a tooltip).
2. Add a chatbot from a pasted cURL; **Test** shows its reply. Golden list shows 10 answers.
3. Add a golden answer, see the count rise; edit it (Edit, change the question, Save
   changes); delete it (two clicks), see it go.
4. Run *Answer Relevancy*: a result line appears, then the latest-scores chart.
5. In the dashboard the card fills in; the chat answers through the chatbot;
   the case table lists 10 rows.
6. Run the same metric again: the trend chart shows two points; Table views show the
   same numbers.
7. Reload the page: cards show "not run"; **History** still lists both runs.
8. Switch the OS to dark mode: charts redraw with the dark palette.
9. Stop the backend: both views show "Backend not reachable…"; start it again and the
   side panel recovers within 10 seconds.

## Security notes

- The backend serves this app itself and refuses cross-origin calls (no CORS), and
  answers only on `127.0.0.1` / `localhost`.
- API keys for connected chatbots are stored in the local `judge.db` and are never
  sent back to the page (responses show `***`).
- Captured cookies and tokens are stored like API keys: in `judge.db`, never sent back.

## Limits

- Requests that need a fresh signature or token per message (some anti-bot
  protections) cannot be replayed; use the site's API instead if it has one.
