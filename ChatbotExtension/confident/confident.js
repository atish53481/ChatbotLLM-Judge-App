// Confident AI reports page: every upload the app made (GET /api/confident/reports),
// with its portal link(s) and the saved JSON copy (GET /api/confident/reports/{id}/file).
const $ = (id) => document.getElementById(id);
const state = { reports: [], kind: "", target: "" };

function node(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "className") el.className = v;
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v);
  }
  for (const child of children) if (child != null) el.append(child);
  return el;
}

function setStatus(text, isError = false) {
  $("status").textContent = text || "";
  $("status").classList.toggle("status-error", Boolean(isError));
}

function when(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso || "" : d.toLocaleString();
}

const TYPE = {
  "scan:test_run": "Scan · test run",
  "deepteam:test_run": "DeepTeam · test run",
  "deepteam:risk_assessment": "DeepTeam · risk assessment",
};

function linkButton(href, label) {
  return node("a", { className: "small-button", href, target: "_blank", rel: "noopener" }, label);
}

function row(r) {
  const open = node("div", { className: "cf-links" });
  if (r.link) open.append(linkButton(r.link, "View ↗"));
  if (r.links && r.links.conversations) open.append(linkButton(r.links.conversations, "Conversations ↗"));
  if (r.links && r.links.multi_turn) open.append(linkButton(r.links.multi_turn, "Multi-turn ↗"));
  const download = node("a", { className: "small-button", href: `/api/confident/reports/${r.id}/file`,
    download: "", title: r.saved ? "The JSON sent to Confident AI" : "Rebuilt from the stored run on first download" },
  "Download JSON");
  const copy = node("div", {}, download,
    r.saved ? null : node("span", { className: "cf-note muted", text: "sent before copies were kept; rebuilt on download" }));
  const source = r.kind === "scan" ? `/app/scan/scan.html?job=${r.ref_id}` : `/app/deepteam/deepteam.html`;
  return node("tr", {},
    node("td", { text: when(r.sent_at) }),
    node("td", { text: r.chatbot || (r.target_id != null ? `#${r.target_id}` : "—") }),
    node("td", {}, node("a", { href: source, target: "_blank", rel: "noopener", text: r.title })),
    node("td", {}, node("span", { className: "cf-pill", text: TYPE[`${r.kind}:${r.sent_as}`] || `${r.kind} · ${r.sent_as}` })),
    node("td", { className: "num", text: r.test_cases != null ? String(r.test_cases) : "—" }),
    node("td", {}, open),
    node("td", {}, copy));
}

function render() {
  const shown = state.reports.filter((r) => !state.kind || r.kind === state.kind);
  $("rows").replaceChildren(...shown.map(row));
  $("empty").classList.toggle("hidden", state.reports.length > 0);
  $("count").textContent = `${shown.length} report${shown.length === 1 ? "" : "s"}`;
}

async function load() {
  try {
    setStatus("Loading…");
    const query = state.target ? `?target_id=${encodeURIComponent(state.target)}` : "";
    state.reports = await api(`/api/confident/reports${query}`);
    setStatus("");
    render();
  } catch (error) {
    setStatus(error.message || String(error), true);
  }
}

async function loadTargets() {
  try {
    const targets = await api("/api/targets");
    for (const t of targets) $("target-select").append(node("option", { value: String(t.id), text: t.name }));
    const wanted = new URLSearchParams(location.search).get("target");
    if (wanted && targets.some((t) => String(t.id) === wanted)) {
      $("target-select").value = wanted;
      state.target = wanted;
    }
  } catch (_) { /* the list still works for all chatbots */ }
}

for (const button of document.querySelectorAll(".cf-filters button")) {
  button.addEventListener("click", () => {
    state.kind = button.dataset.kind;
    for (const b of document.querySelectorAll(".cf-filters button")) b.setAttribute("aria-pressed", String(b === button));
    render();
  });
}
$("target-select").addEventListener("change", () => { state.target = $("target-select").value; load(); });

loadTargets().then(load);
