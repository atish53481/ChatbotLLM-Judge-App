// Test runs: Home, a run's Overview and its Test Cases, behind a left nav.
// runs.html#home | #overview | #cases, with ?target=<id>&job=<scan job id>.
// A "test run" is a scan (one Run of a chatbot, all its metrics); a "test
// case" is one golden (or probe) with every metric's verdict on it.

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

const state = { targets: [], target: null, scans: [], scan: null, titles: new Map(), view: "home", filter: "all", open: new Set(), json: false };

const title = (key) => state.titles.get(key) || key;
const goodness = (score, direction) => (score == null ? null : direction === "lower" ? 1 - score : score);
const pct = (x) => (x == null ? "–" : `${Math.round(x * 1000) / 10}%`);
const finished = (s) => !["queued", "running"].includes(s.status);

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function duration(start, end) {
  const s = (new Date(end) - new Date(start)) / 1000;
  if (!Number.isFinite(s) || s < 0) return "–";
  return s < 90 ? `${s.toFixed(1)}s` : `${Math.round(s / 60)} min`;
}

// --- Data ------------------------------------------------------------------
function casesOf(scan) {
  const cases = new Map();
  for (const result of scan.results || []) {
    for (const row of result.rows || []) {
      const key = row.golden_id ? `id:${row.golden_id}` : `q:${row.input || row.question || ""}`;
      if (!cases.has(key)) {
        cases.set(key, {
          key, name: `test_case_${cases.size}`, input: row.input || row.question || "",
          actual_output: row.actual_output || "", expected_output: row.expected_output || "",
          context: row.context || [], tools_called: row.tools_called || [], latency_s: row.latency_s, metrics: [],
        });
      }
      cases.get(key).metrics.push({ key: result.key, score: row.score, passed: Boolean(row.passed),
        reason: row.reason || "", direction: result.direction || "higher", actual_output: row.actual_output || "" });
    }
  }
  return [...cases.values()].map((c) => {
    const scores = c.metrics.map((m) => goodness(m.score, m.direction)).filter((s) => s != null);
    const failed = c.metrics.find((m) => !m.passed);
    return { ...c, passed: !failed, score: scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null,
      // Show the reply the failing metric judged, else the first one.
      actual_output: (failed && failed.actual_output) || c.actual_output };
  });
}

// --- Charts ----------------------------------------------------------------
function donut(fraction, value, sub) {
  const r = 70, c = 2 * Math.PI * r, f = Math.max(0, Math.min(1, fraction || 0));
  return el("div", { className: "ring" }, svg("svg", { viewBox: "0 0 200 200", role: "img", "aria-label": `${value} ${sub}` },
    svg("circle", { cx: 100, cy: 100, r, fill: "none", stroke: f < 1 ? "var(--fail)" : "var(--pass)", "stroke-width": 14 }),
    svg("circle", { cx: 100, cy: 100, r, fill: "none", stroke: "var(--pass)", "stroke-width": 14,
      "stroke-dasharray": `${c * f} ${c}`, transform: "rotate(-90 100 100)" }),
    svg("text", { x: 100, y: 102, "text-anchor": "middle", class: "ring-value" }, value),
    svg("text", { x: 100, y: 126, "text-anchor": "middle", class: "ring-sub" }, sub)));
}

function gauge(fraction, value, sub) {
  const f = Math.max(0, Math.min(1, fraction || 0));
  const arc = (to) => {
    const a = Math.PI * (1 - to), x = 100 + 80 * Math.cos(a), y = 100 - 80 * Math.sin(a);
    return `M 20 100 A 80 80 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)}`;
  };
  return el("div", { className: "ring" }, svg("svg", { viewBox: "0 0 200 130", role: "img", "aria-label": `${value} ${sub}` },
    svg("path", { d: arc(1), fill: "none", stroke: "var(--border)", "stroke-width": 14, "stroke-linecap": "round" }),
    f > 0 ? svg("path", { d: arc(f), fill: "none", stroke: "var(--pass)", "stroke-width": 14, "stroke-linecap": "round" }) : "",
    svg("text", { x: 100, y: 96, "text-anchor": "middle", class: "ring-value" }, value),
    svg("text", { x: 100, y: 122, "text-anchor": "middle", class: "ring-sub" }, sub)));
}

function scoreStrip(cases) {
  const strip = el("div", { className: "strip", role: "img", "aria-label": "Test case scores from 0 to 1" },
    el("div", { className: "strip-axis" }),
    ...[0, 0.2, 0.4, 0.6, 0.8, 1].map((t) => el("span", { className: "strip-tick", style: `left:${t * 100}%` }, String(t))));
  const stacks = new Map();
  for (const c of cases) {
    if (c.score == null) continue;
    const bin = Math.round(c.score * 50) / 50;
    const level = stacks.get(bin) || 0;
    stacks.set(bin, level + 1);
    strip.append(el("span", { className: "strip-dot", title: `${c.name}: ${formatScore(c.score)}`,
      style: `left:${bin * 100}%;bottom:${24 + level * 11}px;background:${c.passed ? "var(--pass)" : "var(--fail)"}` }));
  }
  return strip;
}

function trend(scans) {
  const points = scans.filter((s) => s.average != null).slice().reverse();
  if (points.length < 2) return el("p", { className: "empty" }, "Run the chatbot's metrics a few times to see a trend.");
  const W = 800, H = 200, pad = 32;
  const x = (i) => pad + (i * (W - 2 * pad)) / (points.length - 1);
  const y = (v) => H - pad - v * (H - 2 * pad);
  const d = points.map((p, i) => `${i ? "L" : "M"} ${x(i).toFixed(1)} ${y(p.average).toFixed(1)}`).join(" ");
  return el("div", { className: "trend" }, svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Average score per test run" },
    ...[0, 0.5, 1].map((v) => svg("line", { x1: pad, x2: W - pad, y1: y(v), y2: y(v), class: "grid-line" })),
    ...[0, 0.5, 1].map((v) => svg("text", { x: 4, y: y(v) + 4 }, v.toFixed(1))),
    svg("path", { d: `${d} L ${x(points.length - 1)} ${H - pad} L ${x(0)} ${H - pad} Z`, class: "area" }),
    svg("path", { d, class: "line" }),
    ...points.map((p, i) => svg("circle", { cx: x(i), cy: y(p.average), r: 4 }, svg("title", {}, `Scan ${p.version}: ${formatScore(p.average)}`))),
    ...points.map((p, i) => svg("text", { x: x(i), y: H - 8, "text-anchor": "middle" }, `v${p.version}`))));
}

// --- Views -----------------------------------------------------------------
function head(h1, sub, ...right) {
  return el("div", { className: "page-head" }, el("div", {}, el("h1", {}, h1), el("p", {}, sub)), el("div", {}, ...right));
}

function noScan() {
  return el("div", { className: "card empty" }, "No finished test runs for this chatbot yet. Run its metrics from the dashboard, then come back.");
}

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function homeView() {
  const scan = state.scan;
  const done = state.scans.filter(finished);
  const i = done.findIndex((s) => scan && s.job_id === scan.job_id);
  const previous = i >= 0 ? done[i + 1] : null;
  if (!scan) return [head("Home", state.target ? state.target.name : ""), noScan()];
  const cases = casesOf(scan);
  const passing = cases.filter((c) => c.passed).length;
  const healthy = scan.result === "passed";
  const delta = previous && previous.average != null && scan.average != null ? scan.average - previous.average : null;
  const failing = cases.flatMap((c) => c.metrics.filter((m) => !m.passed).map((m) => ({ c, m })))
    .sort((a, b) => (goodness(a.m.score, a.m.direction) ?? 1) - (goodness(b.m.score, b.m.direction) ?? 1)).slice(0, 6);
  return [
    head(greeting(), `${state.target.name} · test run v${scan.version}, ${formatRunTime(scan.started_at)}`),
    el("div", { className: "grid grid-3" },
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Overall LLM performance"),
        el("p", { className: "big" }, el("span", { className: "dot", style: `background:${healthy ? "var(--pass)" : "var(--fail)"}` }),
          healthy ? "Healthy" : scan.result === "failed" ? "Needs attention" : "Incomplete"),
        el("p", { className: "muted small" }, `${scan.passed} of ${scan.metrics} metrics passed · ${cases.length} test cases`)),
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Average score"),
        el("p", { className: "big" }, formatScore(scan.average)),
        el("p", { className: `small ${delta == null ? "muted" : delta >= 0 ? "up" : "down"}` },
          delta == null ? "No earlier run to compare with"
            : `${delta >= 0 ? "▲ Up" : "▼ Down"} ${Math.abs(delta * 100).toFixed(1)} points since v${previous.version}`)),
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Judge model"),
        el("p", { className: "big" }, scan.judge_model || "–"),
        el("p", { className: "muted small" }, `${(scan.judge_tokens || 0).toLocaleString()} judge tokens in this run`))),
    el("div", { className: "grid grid-main", style: "margin-top:16px" },
      el("div", { className: "card" }, el("p", { className: "card-title" }, `Latest test run · ${formatRunTime(scan.started_at)}`),
        donut(cases.length ? passing / cases.length : 0, pct(cases.length ? passing / cases.length : null), `${passing}/${cases.length} passed`),
        el("p", { style: "text-align:center;margin:8px 0 0;font-size:18px" },
          `${cases.length - passing} failing test case${cases.length - passing === 1 ? "" : "s"}`)),
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Top failing LLM outputs ", el("span", { className: "muted small" }, "(lowest scores first)")),
        failing.length ? el("div", { className: "table-wrap" }, el("table", { className: "data" },
          el("thead", {}, el("tr", {}, el("th", {}, "Metric"), el("th", {}, "Input"), el("th", {}, "LLM output"))),
          el("tbody", {}, ...failing.map(({ c, m }) => el("tr", { className: "case-row", onclick: () => go("cases", c.key) },
            el("td", {}, el("span", { className: "badge badge-metric" }, title(m.key))),
            el("td", {}, c.input.slice(0, 160)),
            el("td", {}, (m.actual_output || c.actual_output).slice(0, 220)))))))
          : el("p", { className: "empty" }, "Every test case passed every metric."))),
    el("div", { className: "card", style: "margin-top:16px" },
      el("h2", { className: "section" }, "Output quality across test runs"),
      el("p", { className: "section-sub" }, "Average score of each run (lower-is-better metrics counted as 1 − score)"),
      trend(state.scans)),
  ];
}

function overviewView() {
  const scan = state.scan;
  if (!scan) return [head("Overview", "General information about the test run."), noScan()];
  const cases = casesOf(scan);
  const passing = cases.filter((c) => c.passed).length;
  const results = [...scan.results].sort((a, b) => (a.status === "pass") - (b.status === "pass"));
  const failures = cases.flatMap((c) => c.metrics.filter((m) => !m.passed).map((m) => ({ c, m }))).slice(0, 8);
  const prop = (name, value, mono) => el("div", {}, el("span", {}, name), el("b", { className: mono ? "mono" : "" }, value));
  return [
    head("Overview", "General information about the test run.", el("a", { className: "btn", href: "#cases" }, "View test cases →")),
    el("div", { className: "props" },
      prop("ID", `job-${scan.job_id}`, true), prop("Name", `${state.target.name} · v${scan.version}`),
      prop("Duration", duration(scan.started_at, scan.finished_at)), prop("Judge", scan.judge_model || "–"),
      prop("Judge tokens", (scan.judge_tokens || 0).toLocaleString()),
      prop("Dataset", (state.target.config && state.target.config.theme) || "generic"),
      prop("Result", scan.result)),
    el("div", { className: "grid grid-main" },
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Results summary"),
        gauge(cases.length ? passing / cases.length : 0, pct(cases.length ? passing / cases.length : null),
          `${passing} of ${cases.length} test cases passing`)),
      el("div", { className: "card" },
        el("div", { className: "page-head", style: "margin:0" }, el("p", { className: "card-title" }, "Test case scores"),
          el("div", { className: "legend" }, el("span", {}, el("i", { style: "background:var(--pass)" }), "Passing"),
            el("span", {}, el("i", { style: "background:var(--fail)" }), "Failing"))),
        scoreStrip(cases))),
    el("div", { className: "card", style: "margin-top:16px" },
      el("h2", { className: "section" }, "Metrics"),
      el("p", { className: "section-sub" }, "Failing metrics first; each score is the run's average over its test cases"),
      el("div", { className: "metric-cards" }, ...results.map((r) => {
        const rows = r.rows || [];
        const ok = rows.filter((x) => x.passed).length;
        return el("div", { className: `metric-card ${r.status === "pass" ? "pass" : "fail"}` },
          el("h3", {}, title(r.key)),
          el("p", { className: "score" }, r.score == null ? "–" : formatScore(r.score)),
          el("p", {}, r.error ? r.error.slice(0, 90) : `${ok} passed · ${rows.length - ok} failed`));
      }))),
    el("div", { className: "card", style: "margin-top:16px" },
      el("h2", { className: "section" }, "Failures"),
      el("p", { className: "section-sub" }, "Failing test cases and what the judge said"),
      failures.length ? el("ul", { className: "fail-list" }, ...failures.map(({ c, m }) => el("li", {},
        el("span", { className: "badge badge-metric" }, title(m.key)), " ",
        el("a", { href: "#cases", onclick: () => state.open.add(c.key) }, c.name), " · ", el("span", {}, c.input.slice(0, 140)),
        el("p", { className: "why" }, m.reason || "No reason given."))))
        : el("p", { className: "empty" }, "No failing test cases.")),
  ];
}

function casesView() {
  const scan = state.scan;
  if (!scan) return [head("Test Cases", ""), noScan()];
  const cases = casesOf(scan);
  const passed = cases.filter((c) => c.passed).length;
  const shown = cases.filter((c) => state.filter === "all" || (state.filter === "passed") === c.passed);
  const seg = (value, label) => el("button", { type: "button", "aria-pressed": String(state.filter === value),
    onclick: () => { state.filter = value; render(); } }, label);
  return [
    head("Test Cases", `${state.target.name} · v${scan.version} · ${formatRunTime(scan.started_at)}`),
    el("div", { className: "toolbar" },
      el("div", { className: "seg", role: "group", "aria-label": "Show" }, seg("all", "All"), seg("passed", "Passed"), seg("failed", "Failed")),
      el("div", { className: "summary-pills" },
        el("span", {}, el("span", { className: "pill pill-pass" }, "PASSED"),
          el("b", {}, pct(cases.length ? passed / cases.length : null)), `${passed}/${cases.length} test cases`),
        el("span", {}, el("span", { className: "pill pill-fail" }, "FAILED"),
          el("b", {}, pct(cases.length ? (cases.length - passed) / cases.length : null)), `${cases.length - passed}/${cases.length} test cases`))),
    el("div", { className: "toolbar", style: "justify-content:flex-end" },
      el("button", { type: "button", className: "btn", onclick: () => downloadCsv(cases) }, "⭳ Download as CSV")),
    el("div", { className: "card", style: "padding:0" }, el("div", { className: "table-wrap" }, el("table", { className: "data" },
      el("thead", {}, el("tr", {}, el("th", { style: "width:18%" }, "Name"), el("th", { style: "width:12%" }, "Status"),
        el("th", { style: "width:35%" }, "Input"), el("th", {}, "Actual output"))),
      el("tbody", {}, ...(shown.length ? shown.flatMap(caseRows)
        : [el("tr", {}, el("td", { colspan: 4, className: "empty" }, "No test cases here."))]))))),
  ];
}

function caseRows(c) {
  const open = state.open.has(c.key);
  const row = el("tr", { className: "case-row", "aria-expanded": String(open),
    onclick: () => { if (open) state.open.delete(c.key); else state.open.add(c.key); render(); } },
  el("td", {}, `${open ? "▾" : "▸"} `, el("strong", {}, c.name)),
  el("td", {}, el("span", { className: `pill ${c.passed ? "pill-pass" : "pill-fail"}` }, c.passed ? "PASSED" : "FAILED")),
  el("td", { className: "clip", title: c.input }, c.input),
  el("td", { className: "clip", title: c.actual_output }, c.actual_output));
  if (!open) return [row];
  const prop = (name, value) => el("div", { className: "prop" }, el("span", {}, name), value || "None");
  const json = { name: c.name, status: c.passed ? "passed" : "failed", input: c.input, actual_output: c.actual_output,
    expected_output: c.expected_output, context: c.context, tools_called: c.tools_called,
    metrics: c.metrics.map(({ key, score, passed, reason }) => ({ metric: title(key), score, passed, reason })) };
  const detail = el("td", { className: "detail-cell", colspan: 4 },
    el("div", { className: "detail-bar" },
      el("div", { className: "chips" }, ...c.metrics.map((m) => el("span", { className: `chip ${m.passed ? "pass" : "fail"}`, title: m.reason },
        `${title(m.key).toUpperCase()}: ${formatScore(m.score)}`))),
      el("label", { className: "switch" }, el("input", { type: "checkbox", checked: state.json,
        onchange: (e) => { state.json = e.target.checked; render(); } }), "View JSON")),
    state.json ? el("pre", { className: "json" }, JSON.stringify(json, null, 2))
      : el("div", { className: "props-grid" },
        prop("Name", c.name), prop("Status", c.passed ? "Passed" : "Failed"),
        prop("Chatbot latency", typeof c.latency_s === "number" ? `${c.latency_s.toFixed(2)}s` : "–"),
        prop("Input", c.input), prop("Actual output", c.actual_output), prop("Expected output", c.expected_output),
        c.context.length ? prop("Context", c.context.join("\n")) : "",
        c.tools_called.length ? prop("Tools called", c.tools_called.map((t) => t.name).join(", ")) : "",
        ...c.metrics.filter((m) => !m.passed && m.reason).map((m) => prop(`Why ${title(m.key)} failed`, m.reason))));
  return [row, el("tr", {}, detail)];
}

function downloadCsv(cases) {
  const keys = [...new Set(cases.flatMap((c) => c.metrics.map((m) => m.key)))];
  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [["name", "status", "input", "actual_output", "expected_output", ...keys.map(title)].map(cell).join(",")];
  for (const c of cases) {
    const by = new Map(c.metrics.map((m) => [m.key, m.score]));
    lines.push([c.name, c.passed ? "passed" : "failed", c.input, c.actual_output, c.expected_output,
      ...keys.map((k) => by.get(k))].map(cell).join(","));
  }
  const a = el("a", { href: URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" })),
    download: `${state.target.name}-v${state.scan.version}.csv`.replace(/[^\w.-]+/g, "_") });
  document.body.append(a);
  a.click();
  a.remove();
}

// --- Navigation ------------------------------------------------------------
const VIEWS = { home: homeView, overview: overviewView, cases: casesView };
const VIEW_TITLES = { home: "Home", overview: "Overview", cases: "Test Cases" };

function go(view, openKey) {
  if (openKey) state.open.add(openKey);
  if (location.hash.slice(1) === view) render();
  else location.hash = view;
}

function render() {
  state.view = VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "home";
  document.querySelectorAll(".nav nav a[data-view]").forEach((a) => {
    if (a.dataset.view === state.view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.title = `${VIEW_TITLES[state.view]} · Test runs`;
  $("view").replaceChildren(...VIEWS[state.view]());
  const done = state.scans.filter(finished);
  const i = done.findIndex((s) => state.scan && s.job_id === state.scan.job_id);
  const older = i >= 0 ? done[i + 1] : null;
  $("nav-regressions").href = older
    ? `../regressions/regressions.html?job=${state.scan.job_id}&with=${older.job_id}` : "../regressions/regressions.html";
}

async function selectScan(jobId) {
  state.scan = jobId ? await api(`/api/scans/${jobId}`) : null;
  state.open.clear();
  const params = new URLSearchParams({ target: state.target.id, ...(jobId ? { job: jobId } : {}) });
  history.replaceState(null, "", `?${params}${location.hash}`);
  render();
}

async function selectTarget(targetId, jobId) {
  state.target = state.targets.find((t) => String(t.id) === String(targetId)) || state.targets[0] || null;
  if (!state.target) {
    $("view").replaceChildren(el("p", { className: "empty" }, "No chatbots yet: add one in the side panel."));
    return;
  }
  state.scans = await api(`/api/targets/${state.target.id}/scans`);
  const done = state.scans.filter(finished);
  fillSelect($("scan"), done.map((s) => ({ value: s.job_id, label: `v${s.version} · ${formatRunTime(s.started_at)}` })),
    jobId || (done[0] && done[0].job_id));
  await selectScan($("scan").value || null);
}

async function init() {
  const params = new URLSearchParams(location.search);
  try {
    const [targets, metrics] = await Promise.all([api("/api/targets"), api("/api/metrics").catch(() => [])]);
    state.targets = targets;
    state.titles = new Map(metrics.map((m) => [m.key, m.title]));
    const targetId = params.get("target") || (targets[0] && targets[0].id);
    fillSelect($("target"), targets.map((t) => ({ value: t.id, label: t.name })), targetId);
    await selectTarget(targetId, params.get("job"));
  } catch (error) {
    $("view").replaceChildren(el("p", { className: "empty" }, `Could not load: ${error.message}`));
  }
}

$("target").addEventListener("change", () => selectTarget($("target").value));
$("scan").addEventListener("change", () => selectScan($("scan").value));
window.addEventListener("hashchange", render);
init();
