// Client for the local LLM Judge backend. The address is fixed on purpose
// (see README): the extension talks to nothing else. Served as the web app by
// the backend itself, the pages call their own origin instead.
const BACKEND_URL = /^https?:$/.test((typeof location !== "undefined" && location.protocol) || "")
  ? location.origin
  : "http://127.0.0.1:8765";

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`${BACKEND_URL}${path}`, init);
  } catch {
    throw new Error(`Backend not reachable at ${new URL(BACKEND_URL).host}. Start it with run-backend.bat.`);
  }
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail !== undefined ? data.detail : text;
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw new Error(`${response.status}: ${message || response.statusText}`);
  }
  return data;
}
