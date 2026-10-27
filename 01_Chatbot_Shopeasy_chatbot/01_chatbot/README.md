# Subsystem A — ShopSphere E-commerce Chatbot

React (Vite) frontend + FastAPI backend + Groq LLM. The "app under test" for the DeepEval framework.

## Ports
| Service | Port |
|--------|------|
| FastAPI backend | 8202 |
| Vite dev server | 5173 (proxies `/chat` and `/health` to 8202) |

## Run (Windows)

One time:

1. Run `run-backend.bat` in the project root once, so `.venv` exists (this bot uses it).
2. Copy `.env.sample` to `.env` here and set `GROQ_API_KEY` and `CHATBOT_MODEL`.
   The backend reads `.env` itself, so the key is never put on a command line.
3. In `frontend`, run `npm install`.

Then double-click `start.bat` (once). It opens a "Backend (8202)" and a
"Frontend (5173)" window and then <http://localhost:5173>. If the bot is already
running, it just opens the page: close both windows first to restart it.

Use `http://localhost:5173`, not `127.0.0.1`: the Vite dev server listens on IPv6
(`::1`) only.

## API
- `GET /health` — status + active model
- `POST /chat` — `{message, history?}` → `{reply, model, mode, usage, sources}`; `sources` lists the knowledge-base chunks retrieved for the question (`{id, title, text}`, top `CHATBOT_TOP_K`, default 3). Only those reach the model, so the LLM Judge can score retrieval: set the chatbot's "Retrieved context path" to `sources`.
- Tools: the model may call `lookup_order(order_id)` (mock orders `ORD-1001` shipped, `ORD-1002` processing, `ORD-1003` delivered), `get_product(sku)` and `search_knowledge_base(query)`. Every call comes back in `tools_called` (`{name, args, output}`), so the LLM Judge's agent metrics (Tool Correctness, Argument Correctness, Plan Quality, Plan Adherence, Step Efficiency) can run: set the chatbot's "Tool calls path" to `tools_called` (a scan also finds it by itself). Chunks a tool returned are added to `sources`. `CHATBOT_TOOLS=0` turns tools off.
