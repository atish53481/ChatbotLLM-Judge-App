// Regressions: two scans of one chatbot, test case by test case.
// regressions.html?job=<compared scan job>&with=<baseline scan job>, or pick
// the chatbot and scans in the filter bar. Rows tint teal (improved) or pink
// (regressed); click one for every metric's verdict on both sides.

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 10;
const BADGE = { improved: "↗ Improved", regressed: "↘ Regressed", new: "New", removed: "Removed" };

const state = {
  targets: [], scans: [], titles: new Map(), data: null,
  view: "all", metric: "", search: "", page: 0, open: new Set(),
};

const title = (key) => state.titles.get(key) || key;
const scanLabel = (s) => `Scan ${s.version} · ${formatRunTime(s.started_at)} · avg ${formatScore(s.average)}`;

async function init() {
  const params = new URLSearchParams(location.search);
  try {
    const [targets, metrics] = await Promise.all([api("/api/targets"), api("/api/metrics").catch(() => [])]);
    state.targets = targets;
    state.titles = new Map(metrics.map((m) => [m.key, m.title]));
    // ?target=<id> (the dashboard's Regressions button): that chatbot's newest two scans.
    const wanted = targets.find((t) => String(t.id) === params.get("target"));
    let targetId = wanted ? wanted.id : targets[0] && targets[0].id;
    if (params.get("job")) {
      const scan = await api(`/api/scans/${encodeURIComponent(params.get("job"))}`);
      targetId = scan.target_id;
    }
    fillSelect($("reg-target"), targets.map((t) => ({ value: t.id, label: t.name })), targetId);
    await loadScans(targetId, params.get("job"), params.get("with"));
  } catch (error) {
    $("reg-showing").textContent = `Could not load: ${error.message}`;
  }
}

async function loadScans(targetId, nowJob, beforeJob) {
  state.scans = targetId ? await api(`/api/targets/${targetId}/scans`) : [];
  const done = state.scans.filter((s) => !["queued", "running"].includes(s.status));
  const options = done.map((s) => ({ value: s.job_id, label: scanLabel(s) }));
  const now = nowJob || (done[0] && done[0].job_id);
  const before = beforeJob || (done.find((s) => String(s.job_id) !== String(now)) || {}).job_id;
  fillSelect($("reg-now"), options, now);
  fillSelect($("reg-before"), options, before);
  if (done.length < 2) {
    state.data = null;
    render();
    $("reg-rows").replaceChildren(el("p", { className: "reg-empty" },
      "This chatbot needs at least two finished scans to compare. Run its metrics twice from the dashboard."));
    $("reg-showing").textContent = "";
    return;
  }
  await loadCases();
}

async function loadCases() {
  const now = $("reg-now").value;
  const before = $("reg-before").value;
  if (!now || !before) return;
  $("reg-showing").textContent = "Loading…";
  try {
    state.data = await api(`/api/scans/${now}/cases?with=${before}`);
  } catch (error) {
    $("reg-showing").textContent = `Could not compare: ${error.message}`;
    return;
  }
  state.page = 0;
  // Open the first regression: it is what a reviewer looks at first.
  state.open = new Set(state.data.cases.filter((c) => c.change === "regressed").slice(0, 1).map((c) => c.key));
  history.replaceState(null, "", `?job=${now}&with=${before}`);
  const metricKeys = [...new Set(state.data.cases.flatMap((c) =>
    [...(c.now?.metrics || []), ...(c.before?.metrics || [])].map((m) => m.key)))];
  if (!metricKeys.includes(state.metric)) state.metric = "";
  fillSelect($("reg-metric"), [{ value: "", label: "Any metric" }, ...metricKeys.map((k) => ({ value: k, label: title(k) }))], state.metric);
  const { scan, other } = state.data;
  $("reg-head-before").textContent = `Scan ${other.version} (baseline) · avg ${formatScore(other.average)}`;
  $("reg-head-now").textContent = `Scan ${scan.version} · avg ${formatScore(scan.average)}`;
  render();
}

function matching() {
  if (!state.data) return [];
  const search = state.search.toLowerCase();
  return state.data.cases
    .map((c, i) => ({ ...c, index: i }))
    .filter((c) => state.view === "all" || c.change === state.view
      || (state.view === "added" && (c.change === "new" || c.change === "removed")))
    .filter((c) => !search || c.input.toLowerCase().includes(search))
    .filter((c) => !state.metric || [c.now, c.before].some((side) => side?.metrics.some((m) => m.key === state.metric)));
}

function render() {
  const counts = state.data ? state.data.counts : {};
  $("count-all").textContent = state.data ? `· ${state.data.cases.length}` : "";
  for (const view of ["regressed", "improved", "same"]) $(`count-${view}`).textContent = state.data ? `· ${counts[view] || 0}` : "";
  // Every case sits in exactly one tab, so the tab counts add up to All.
  $("count-added").textContent = state.data ? `· ${(counts.new || 0) + (counts.removed || 0)}` : "";
  document.querySelectorAll(".reg-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === state.view)));

  const rows = matching();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  const shown = rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  $("reg-showing").replaceChildren(rows.length
    ? el("span", {}, "Showing ", el("strong", {}, `${state.page * PAGE_SIZE + 1} to ${state.page * PAGE_SIZE + shown.length}`),
      ` of ${rows.length} matching test case${rows.length === 1 ? "" : "s"}`)
    : "No test cases match.");
  $("reg-rows").replaceChildren(...(shown.length ? shown.map(caseRow)
    : [el("p", { className: "reg-empty" }, state.view === "regressed" ? "No regressions between these scans."
      : state.view === "improved" ? "No improvements between these scans."
        : state.view === "added" ? "Both scans have the same test cases." : "Nothing to show for these filters.")]));
  $("reg-page").textContent = `Page ${state.page + 1} of ${pages}`;
  $("reg-prev").disabled = state.page === 0;
  $("reg-next").disabled = state.page >= pages - 1;
}

// Moves of the average score this big are shown even when no verdict flipped.
const SCORE_MOVE = 0.1;

// The case's average score on one side (lower-is-better metrics as 1 − score).
function sideScore(side) {
  const scores = (side?.metrics || []).filter((m) => m.score != null)
    .map((m) => (m.direction === "lower" ? 1 - m.score : m.score));
  return scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
}

function scoreMove(c) {
  const before = sideScore(c.before), now = sideScore(c.now);
  if (before == null || now == null || Math.abs(now - before) < SCORE_MOVE) return null;
  const up = now > before;
  return el("span", {
    className: `reg-move ${up ? "up" : "down"}`,
    title: `Average score ${formatScore(before)} → ${formatScore(now)}, though no metric flipped pass/fail`,
  }, `${up ? "▲" : "▼"} ${formatScore(Math.abs(now - before))}`);
}

function sideHead(c, side, withBadge) {
  const data = c[side];
  if (!data) {
    // Why a case is missing: it was not scored in that scan.
    const why = side === "before" ? "Not in the baseline scan (its metric did not run or failed there)"
      : "Not in this scan (its metric did not run or failed here)";
    return el("div", { className: "reg-side" }, el("span", { className: "reg-absent" }, why));
  }
  return el("div", { className: "reg-side" },
    el("span", { className: "reg-case-id" }, `test_case_${c.index}`),
    el("span", { className: "reg-case-q", title: c.input }, c.input),
    withBadge && BADGE[c.change] ? el("span", { className: `reg-badge ${c.change}` }, BADGE[c.change]) : null,
    withBadge && c.change === "same" ? scoreMove(c) : null);
}

function caseRow(c) {
  const open = state.open.has(c.key);
  const head = el("button", {
    type: "button", className: "reg-row-head", "aria-expanded": String(open),
    onclick: () => { if (open) state.open.delete(c.key); else state.open.add(c.key); render(); },
  },
  el("span", { className: "reg-chevron", "aria-hidden": "true" }, "›"),
  sideHead(c, "before", false),
  sideHead(c, "now", true));
  return el("div", { className: `reg-row ${c.change}${open ? " open" : ""}` }, head, open ? detail(c) : null);
}

function metricList(side, other) {
  if (!side) return "–";
  const was = new Map((other?.metrics || []).map((m) => [m.key, m.passed]));
  return el("ul", { className: "reg-metrics" }, ...side.metrics.map((m) => {
    const changed = was.has(m.key) && was.get(m.key) !== m.passed;
    return el("li", { className: changed ? `changed${m.passed ? " up" : ""}` : "", title: m.reason || "" },
      el("span", { className: m.passed ? "ok" : "ko", "aria-label": m.passed ? "passed" : "failed" }, m.passed ? "✓" : "✕"),
      el("span", {}, title(m.key)),
      el("span", { className: "score" }, `(${formatScore(m.score)})`));
  }));
}

function toolList(side) {
  if (!side || !side.tools_called.length) return "–";
  return el("div", { className: "reg-tools" }, ...side.tools_called.map((t) => el("details", {},
    el("summary", {}, t.name),
    el("pre", {}, JSON.stringify({ input: t.input_parameters || {}, ...(t.output ? { output: t.output } : {}) }, null, 2)))));
}

// Each metric asks the chatbot on its own, so replies can differ: the one
// shown first is the reply behind the metric that changed; the rest fold away.
function replies(side) {
  if (!side) return "–";
  const others = side.metrics.filter((m) => m.actual_output && m.actual_output !== side.actual_output);
  if (!others.length) return side.actual_output || "–";
  return el("div", {},
    side.actual_output || "–",
    el("details", { className: "reg-replies" },
      el("summary", {}, `Replies by metric (${side.metrics.length})`),
      ...side.metrics.map((m) => el("p", {}, el("strong", {}, `${title(m.key)}: `), m.actual_output || "–"))));
}

function reasons(side) {
  if (!side) return "–";
  const failed = side.metrics.filter((m) => !m.passed && m.reason);
  return failed.length ? failed.map((m) => `${title(m.key)}: ${m.reason}`).join("\n\n") : "All metrics passed.";
}

function detail(c) {
  const { before, now } = c;
  const line = (label, show, always = true) => {
    if (!always) return [];
    return [el("div", { className: "reg-label" }, label),
      el("div", { className: "reg-cell", "data-side": "Baseline" }, show(before, now)),
      el("div", { className: "reg-cell", "data-side": "Compared" }, show(now, before))];
  };
  const text = (field) => (side) => (side ? side[field] || "–" : "–");
  const hasTools = [before, now].some((s) => s && s.tools_called.length);
  return el("div", { className: "reg-detail" },
    ...line("Metrics", metricList),
    ...line("Input", text("input")),
    ...line("Actual output", replies),
    ...line("Expected output", text("expected_output")),
    ...line("Tools called", toolList, hasTools),
    ...line("Why it failed", reasons));
}

document.querySelectorAll(".reg-tabs button").forEach((b) => b.addEventListener("click", () => {
  state.view = b.dataset.view;
  state.page = 0;
  render();
}));
$("reg-target").addEventListener("change", () => loadScans($("reg-target").value));
$("reg-now").addEventListener("change", loadCases);
$("reg-before").addEventListener("change", loadCases);
$("reg-metric").addEventListener("change", () => { state.metric = $("reg-metric").value; state.page = 0; render(); });
$("reg-search").addEventListener("input", () => { state.search = $("reg-search").value.trim(); state.page = 0; render(); });
$("reg-prev").addEventListener("click", () => { state.page -= 1; render(); });
$("reg-next").addEventListener("click", () => { state.page += 1; render(); });

init();
