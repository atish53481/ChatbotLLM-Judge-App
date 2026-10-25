// Test runs: Evaluation (every run), Home, a run's Overview, its Test Cases
// and Compare Test Results, behind a left nav.
// runs.html#evaluation | #home | #overview | #cases | #compare, with
// ?target=<id>&job=<scan job id>&with=<baseline scan job id>.
// A "test run" is a scan (one Run of a chatbot, all its metrics); a "test
// case" is one golden (or probe) with every metric's verdict on it.

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

const state = {
  targets: [], target: null, scans: [], scan: null, baseline: null, cache: new Map(), titles: new Map(), series: new Map(),
  view: "evaluation", filter: "all", open: new Set(), json: false,
  // Evaluation
  range: "all", selected: new Set(), hidden: new Set(),
  // Overview
  analysis: "histogram", interval: 0.1, radarCat: "", categories: new Map(),
  // Test Cases
  query: "", metricFilter: "", caseTab: new Map(), saving: false,
  // Compare
  cmp: { match: "id", filter: "all", query: "", open: new Set(), current: true, baseline: true },
};

// Per-viewer conveniences (column picks); the page works without storage.
const store = {
  get(key, fallback) {
    try { const raw = localStorage.getItem(`llmJudge:runs:${key}`); return raw ? JSON.parse(raw) : fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`llmJudge:runs:${key}`, JSON.stringify(value)); } catch { /* storage off */ }
  },
};

const RUN_COLUMNS = { time: "Time", run: "Test run", dataset: "Dataset", scores: "Metric scores", result: "Test result", status: "Status" };
const CASE_COLUMNS = { name: "Name", status: "Status", input: "Input", actual_output: "Actual output",
  expected_output: "Expected output", context: "Context", score: "Score", latency: "Latency" };
state.runCols = new Set(store.get("runCols", Object.keys(RUN_COLUMNS)));
state.caseCols = new Set(store.get("caseCols", ["name", "status", "input", "actual_output", "context"]));

const title = (key) => state.titles.get(key) || key;
const goodness = (score, direction) => (score == null ? null : direction === "lower" ? 1 - score : score);
const pct = (x) => (x == null ? "–" : `${Math.round(x * 1000) / 10}%`);
const finished = (s) => !["queued", "running"].includes(s.status);
const themeName = () => (state.target && state.target.config && state.target.config.theme) || "generic";
const SERIES = 8;  // categorical slots (runs.css --s1..--s8); later metrics fold into the table

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

function median(values) {
  if (!values.length) return null;
  const v = [...values].sort((a, b) => a - b), mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

// A metric keeps its colour across every chart and filter: slots go to metrics
// in the order they first ran for this chatbot, never by rank.
function seriesIndex() {
  const order = [];
  for (const scan of [...state.scans].reverse()) {
    for (const m of scan.metric_scores || []) if (!order.includes(m.key)) order.push(m.key);
  }
  return new Map(order.map((key, i) => [key, i]));
}
const seriesColor = (key) => {
  const i = state.series.get(key);
  return i != null && i < SERIES ? `var(--s${i + 1})` : "var(--text-muted)";
};

// --- Tooltip ---------------------------------------------------------------
function showTip(event, lines) {
  const tip = $("tip");
  tip.replaceChildren(...lines.filter(Boolean).map((line, i) => el(i ? "div" : "strong", {}, line)));
  tip.hidden = false;
  const x = Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(event.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { $("tip").hidden = true; }
function tipped(node, lines) {
  node.addEventListener("mousemove", (e) => showTip(e, typeof lines === "function" ? lines() : lines));
  node.addEventListener("mouseleave", hideTip);
  return node;
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
      const c = cases.get(key);
      if (!c.tools_called.length && row.tools_called) c.tools_called = row.tools_called;
      c.metrics.push({ key: result.key, score: row.score, passed: Boolean(row.passed), threshold: result.threshold,
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

const rowScores = (result) => (result.rows || []).map((r) => r.score).filter((s) => typeof s === "number");

async function loadScan(jobId) {
  if (!state.cache.has(jobId)) state.cache.set(jobId, await api(`/api/scans/${jobId}`));
  return state.cache.get(jobId);
}

// --- Charts ----------------------------------------------------------------
function donut(fraction, value, sub) {
  const r = 70, c = 2 * Math.PI * r, f = Math.max(0, Math.min(1, fraction || 0));
  return el("div", { className: "dial" }, svg("svg", { viewBox: "0 0 200 200", role: "img", "aria-label": `${value} ${sub}` },
    svg("circle", { cx: 100, cy: 100, r, fill: "none", stroke: f < 1 ? "var(--fail)" : "var(--pass)", "stroke-width": 14 }),
    svg("circle", { cx: 100, cy: 100, r, fill: "none", stroke: "var(--pass)", "stroke-width": 14,
      "stroke-dasharray": `${c * f} ${c}`, transform: "rotate(-90 100 100)" }),
    svg("text", { x: 100, y: 102, "text-anchor": "middle", class: "ring-value" }, value),
    svg("text", { x: 100, y: 126, "text-anchor": "middle", class: "ring-sub" }, sub)));
}

// Results Summary: a half dial of separate segments, the passing share lit.
function segmentedGauge(passed, total) {
  const n = 24, lit = Math.round((total ? passed / total : 0) * n);
  const segs = [];
  for (let i = 0; i < n; i++) {
    const a = Math.PI * (1 - (i + 0.5) / n), cos = Math.cos(a), sin = Math.sin(a);
    segs.push(svg("line", { x1: 100 + 62 * cos, y1: 104 - 62 * sin, x2: 100 + 82 * cos, y2: 104 - 82 * sin,
      stroke: i < lit ? "var(--pass)" : "var(--track)", "stroke-width": 7, "stroke-linecap": "round" }));
  }
  return el("div", { className: "dial" }, svg("svg", { viewBox: "0 0 200 124", role: "img",
    "aria-label": `${passed} of ${total} test cases passing` },
  ...segs,
  svg("text", { x: 100, y: 92, "text-anchor": "middle", class: "ring-value" }, `${passed}/${total}`),
  svg("text", { x: 100, y: 110, "text-anchor": "middle", class: "ring-sub" }, "passing test cases"),
  svg("text", { x: 18, y: 122, "text-anchor": "middle", class: "ring-sub" }, "0%"),
  svg("text", { x: 182, y: 122, "text-anchor": "middle", class: "ring-sub" }, "100%")));
}

const MAX_STACK = 12;

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
    // A stack shows at most MAX_STACK dots; the rest are counted above it.
    if (level >= MAX_STACK) continue;
    strip.append(el("span", { className: "strip-dot", title: `${c.name}: ${formatScore(c.score)}`,
      style: `left:${bin * 100}%;bottom:${24 + level * 11}px;background:${c.passed ? "var(--pass)" : "var(--fail)"}` }));
  }
  for (const [bin, count] of stacks) {
    if (count > MAX_STACK) {
      strip.append(el("span", { className: "strip-more", style: `left:${bin * 100}%;bottom:${30 + MAX_STACK * 11}px` }, `+${count - MAX_STACK}`));
    }
  }
  const tallest = Math.min(MAX_STACK, Math.max(0, ...stacks.values()));
  strip.style.height = `${Math.max(92, 52 + tallest * 11)}px`;
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

// Metric Data: each metric's score per run with ±1 std error bars over its cases.
function metricDataChart(runs, keys) {
  if (!runs.length) return el("p", { className: "empty" }, "No test runs in this date range.");
  const W = 760, H = 260, L = 36, R = 16, T = 14, B = 34;
  const x = (i) => (runs.length === 1 ? (L + W - R) / 2 : L + (i * (W - L - R)) / (runs.length - 1));
  const y = (v) => T + (1 - Math.max(0, Math.min(1, v))) * (H - T - B);
  const plot = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Metric scores per test run with ±1 standard deviation" },
    ...[0, 0.25, 0.5, 0.75, 1].map((v) => svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" })),
    ...[0, 0.25, 0.5, 0.75, 1].map((v) => svg("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end", class: "axis-text" }, String(v))));
  const step = Math.max(1, Math.ceil(runs.length / 8));
  runs.forEach((r, i) => {
    if (i % step === 0 || i === runs.length - 1) {
      plot.append(svg("text", { x: x(i), y: H - 12, "text-anchor": "middle", class: "axis-text" }, `v${r.version}`));
    }
  });
  for (const key of keys) {
    const pts = runs.map((r, i) => ({ i, m: (r.metric_scores || []).find((m) => m.key === key) }))
      .filter((p) => p.m && p.m.score != null);
    if (!pts.length) continue;
    const color = seriesColor(key);
    if (pts.length > 1) {
      plot.append(svg("path", { d: pts.map((p, j) => `${j ? "L" : "M"} ${x(p.i).toFixed(1)} ${y(p.m.score).toFixed(1)}`).join(" "),
        fill: "none", stroke: color, "stroke-width": 2 }));
    }
    for (const p of pts) {
      if (p.m.std) {
        const top = y(p.m.score + p.m.std), bottom = y(p.m.score - p.m.std), cx = x(p.i);
        plot.append(svg("path", { d: `M ${cx} ${top} V ${bottom} M ${cx - 4} ${top} H ${cx + 4} M ${cx - 4} ${bottom} H ${cx + 4}`,
          stroke: color, "stroke-width": 1.5, fill: "none", opacity: 0.7 }));
      }
      plot.append(svg("circle", { cx: x(p.i), cy: y(p.m.score), r: 4.5, fill: color, stroke: "var(--card)", "stroke-width": 2 }));
    }
  }
  // Hover: one column per run lists every plotted metric.
  const col = runs.length > 1 ? (W - L - R) / (runs.length - 1) : W - L - R;
  runs.forEach((r, i) => {
    const hit = svg("rect", { x: x(i) - col / 2, y: T, width: col, height: H - T - B, fill: "transparent" });
    tipped(hit, () => [`v${r.version} · ${formatRunTime(r.started_at)}`,
      ...keys.map((k) => (r.metric_scores || []).find((m) => m.key === k)).filter((m) => m && m.score != null)
        .map((m) => `${title(m.key)}: ${formatScore(m.score)}${m.std ? ` ± ${formatScore(m.std)}` : ""}`)]);
    plot.append(hit);
  });
  return el("div", { className: "chart" }, plot);
}

// Metric Trends: change over the last 7 runs that scored each metric.
function metricTrends(runs, keys) {
  const rows = keys.map((key) => {
    const pts = runs.map((r) => (r.metric_scores || []).find((m) => m.key === key)).filter((m) => m && m.score != null).slice(-7);
    if (pts.length < 2) return null;
    const first = goodness(pts[0].score, pts[0].direction), last = goodness(pts[pts.length - 1].score, pts[pts.length - 1].direction);
    const change = first > 0 ? ((last - first) / first) * 100 : (last - first) * 100;
    return { key, change, runs: pts.length };
  }).filter(Boolean);
  if (!rows.length) return el("p", { className: "empty small" }, "Needs two runs of a metric in this range.");
  // Biggest moves first; metrics that did not move fold into one line.
  const moved = rows.filter((t) => Math.abs(t.change) > 0.005).sort((a, b) => Math.abs(b.change) - Math.abs(a.change));
  const still = rows.length - moved.length;
  return el("div", { className: "trends-wrap" }, moved.length ? el("ul", { className: "trends" }, ...moved.map((t) => {
    const better = t.change > 0.005, worse = t.change < -0.005;
    return el("li", {}, el("i", { className: "swatch", style: `background:${seriesColor(t.key)}` }),
      el("span", { className: "trend-name" }, title(t.key)),
      el("span", { className: `trend-change ${better ? "up" : worse ? "down" : "muted"}`,
        title: `Over the last ${t.runs} runs (lower-is-better metrics counted as 1 − score)` },
      `${better ? "↗" : worse ? "↘" : "→"} ${t.change >= 0 ? "+" : ""}${t.change.toFixed(2)}%`));
  })) : "",
  still ? el("p", { className: "muted small" }, `→ ${still} metric${still === 1 ? "" : "s"} unchanged`) : "");
}

function legend(keys, toggle) {
  return el("div", { className: "legend legend-wrap" }, ...keys.map((k) => el("button", {
    type: "button", className: "legend-item", "aria-pressed": String(!state.hidden.has(k)),
    onclick: () => toggle(k) }, el("i", { style: `background:${seriesColor(k)}` }), title(k))));
}

// Radar of each metric's score (lower-is-better metrics as 1 − score). Every
// metric is plotted; past RADAR_LABELS the names move to the hover tooltip.
const RADAR_LABELS = 12;
// metrics_catalog.UI_CATEGORY_LABELS
const CATEGORY_LABELS = { quality: "Quality", retrieval: "Retrieval", safety: "Safety", security: "Security", geval: "G-Eval",
  conversational: "Conversational", agent: "Agent", reference: "Text match", custom: "Custom" };

function radar(results) {
  const pts = results.filter((r) => r.score != null);
  if (!pts.length) return el("p", { className: "empty" }, "No metric scored in this category.");
  const labelled = pts.length <= RADAR_LABELS;
  const S = 340, c = S / 2, R = 100, n = pts.length;
  const at = (i, v) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
    return [c + R * v * Math.cos(a), c + R * v * Math.sin(a)];
  };
  // Side margin of the viewBox leaves room for the axis labels.
  const plot = svg("svg", { viewBox: `-70 0 ${S + 140} ${S}`, role: "img", "aria-label": "Metric scores overview" },
    ...[0.25, 0.5, 0.75, 1].map((v) => svg("circle", { cx: c, cy: c, r: R * v, class: "radar-ring" })),
    ...pts.map((_, i) => { const [x2, y2] = at(i, 1); return svg("line", { x1: c, y1: c, x2, y2, class: "radar-ring" }); }));
  if (n >= 3) {
    plot.append(svg("polygon", { points: pts.map((r, i) => at(i, goodness(r.score, r.direction)).join(",")).join(" "),
      class: "radar-area" }));
  }
  pts.forEach((r, i) => {
    const [x, y] = at(i, goodness(r.score, r.direction)), [lx, ly] = at(i, 1.16);
    plot.append(tipped(svg("circle", { cx: x, cy: y, r: labelled ? 5 : 4, fill: r.status === "pass" ? "var(--pass)" : "var(--fail)",
      stroke: "var(--card)", "stroke-width": 2 }), [title(r.key), `Score ${formatScore(r.score)} · ${r.status}`]));
    if (!labelled) return;
    const name = title(r.key);
    plot.append(svg("text", { x: lx, y: ly + 4, "text-anchor": Math.abs(lx - c) < 8 ? "middle" : lx > c ? "start" : "end",
      class: "axis-text" }, name.length > 20 ? `${name.slice(0, 19)}…` : name));
  });
  return el("div", { className: "radar" }, plot);
}

function bins(scores, interval) {
  const n = Math.round(1 / interval), counts = new Array(n).fill(0);
  for (const s of scores) counts[Math.max(0, Math.min(n - 1, Math.floor(s / interval + 1e-9)))]++;
  return counts;
}

// Small histogram of one metric's case scores; series = [{label, color, scores}].
function histogram(series, interval, name) {
  const n = Math.round(1 / interval), W = 300, H = 150, L = 26, B = 22, T = 14;
  const counts = series.map((s) => bins(s.scores, interval));
  const max = Math.max(1, ...counts.flat());
  const bw = (W - L - 6) / n, gap = 2, each = Math.max(1, (bw - gap) / Math.max(1, series.length) - (series.length > 1 ? 1 : 0));
  const y = (v) => H - B - (v / max) * (H - B - T);
  const plot = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${name}: score distribution` },
    svg("line", { x1: L, x2: W - 6, y1: H - B, y2: H - B, class: "grid-line" }),
    svg("text", { x: L - 6, y: y(max) + 4, "text-anchor": "end", class: "axis-text" }, String(max)),
    svg("text", { x: L - 6, y: H - B + 4, "text-anchor": "end", class: "axis-text" }, "0"),
    svg("text", { x: L, y: H - 6, class: "axis-text" }, "0.0"),
    svg("text", { x: W - 6, y: H - 6, "text-anchor": "end", class: "axis-text" }, "1.0"));
  counts.forEach((cs, si) => cs.forEach((count, b) => {
    if (!count) return;
    const x = L + b * bw + gap / 2 + si * (each + 1), top = y(count), r = Math.min(3, each / 2);
    const lo = (b * interval).toFixed(2), hi = ((b + 1) * interval).toFixed(2);
    plot.append(tipped(svg("path", { d: `M ${x} ${H - B} V ${top + r} Q ${x} ${top} ${x + r} ${top} H ${x + each - r} Q ${x + each} ${top} ${x + each} ${top + r} V ${H - B} Z`,
      fill: series[si].color }), [`${name}${series.length > 1 ? ` · ${series[si].label}` : ""}`,
      `${lo}–${hi}: ${count} test case${count === 1 ? "" : "s"}`]));
  }));
  return el("figure", { className: "hist" }, plot, el("figcaption", {}, name));
}

// Horizontal bars of one value per metric (average / median) with its threshold.
function metricBars(results, valueOf, label) {
  const rows = results.map((r) => ({ r, v: valueOf(r) })).filter((x) => x.v != null);
  if (!rows.length) return el("p", { className: "empty" }, "No scores to show.");
  return el("div", { className: "bars" }, ...rows.map(({ r, v }) => tipped(el("div", { className: "bar-row" },
    el("span", { className: "bar-name" }, title(r.key)),
    el("span", { className: "bar-track" },
      el("span", { className: "bar-fill", style: `width:${v * 100}%;background:var(--s1)` }),
      r.threshold != null ? el("span", { className: "bar-threshold", style: `left:${r.threshold * 100}%` }) : ""),
    el("span", { className: "bar-value" }, formatScore(v))),
  [title(r.key), `${label}: ${formatScore(v)}`, r.threshold != null ? `Threshold: ${formatScore(r.threshold)} (${r.direction === "lower" ? "max" : "min"})` : ""])));
}

function breakdownBars(results) {
  return el("div", { className: "bars" }, ...results.map((r) => {
    const rows = r.rows || [], ok = rows.filter((x) => x.passed).length, bad = rows.length - ok;
    return tipped(el("div", { className: "bar-row" },
      el("span", { className: "bar-name" }, title(r.key)),
      el("span", { className: "bar-track stack" },
        ok ? el("span", { className: "bar-fill", style: `width:${(ok / Math.max(1, rows.length)) * 100}%;background:var(--pass)` }) : "",
        bad ? el("span", { className: "bar-fill", style: `width:${(bad / Math.max(1, rows.length)) * 100}%;background:var(--fail)` }) : ""),
      el("span", { className: "bar-value" }, `✓${ok} ✗${bad}`)),
    [title(r.key), `${ok} passed · ${bad} failed`]);
  }));
}

// --- Shared pieces -----------------------------------------------------------
function head(h1, sub, ...right) {
  return el("div", { className: "page-head" }, el("div", {}, el("h1", {}, h1), el("p", {}, sub)), el("div", { className: "head-actions" }, ...right));
}

function noScan() {
  return el("div", { className: "card empty" }, "No finished test runs for this chatbot yet. Run its metrics from the dashboard, then come back.");
}

function seg(options, current, onPick, label) {
  return el("div", { className: "seg", role: "group", "aria-label": label }, ...options.map(([value, text]) =>
    el("button", { type: "button", "aria-pressed": String(current === value), onclick: () => onPick(value) }, text)));
}

function choice(options, current, onPick, label) {
  return el("select", { "aria-label": label, onchange: (e) => onPick(Number(e.target.value)) },
    ...options.map((v) => { const o = el("option", { value: v }, String(v)); o.selected = v === current; return o; }));
}

function columnPicker(columns, chosen, storeKey) {
  return el("details", { className: "picker" }, el("summary", { className: "btn" }, `✎ Edit columns · ${chosen.size}`),
    el("div", { className: "picker-menu" }, ...Object.entries(columns).map(([key, label]) => el("label", {},
      el("input", { type: "checkbox", checked: chosen.has(key), onchange: (e) => {
        if (e.target.checked) chosen.add(key); else if (chosen.size > 1) chosen.delete(key); else e.target.checked = true;
        store.set(storeKey, [...chosen]);
        render();
      } }), label))));
}

function contextCards(context, max = 3) {
  if (!context || !context.length) return "None";
  return el("div", { className: "ctx-cards" }, ...context.slice(0, max).map((c) => el("div", { className: "ctx-card" }, String(c))),
    context.length > max ? el("p", { className: "muted small" }, `…and ${context.length - max} more retrieval context(s)`) : "");
}

function toolList(tools) {
  if (!tools || !tools.length) return "None";
  return el("div", { className: "tools" }, ...tools.map((t) => el("details", { className: "tool" },
    el("summary", {}, t.name || "tool"),
    el("pre", { className: "json" }, JSON.stringify({ input: t.input_parameters, output: t.output, reasoning: t.reasoning }, null, 2)))));
}

function metricChip(m) {
  return el("span", { className: `chip ${m.passed ? "pass" : "fail"}`, title: m.reason },
    `${m.passed ? "✓" : "✗"} ${title(m.key)} (${formatScore(m.score)})`);
}

// Re-rendering replaces the search box; keep typing in it.
function searchBox(value, placeholder, onInput) {
  return el("input", { type: "search", className: "search", placeholder, value, "aria-label": placeholder,
    oninput: (e) => {
      onInput(e.target.value);
      render();
      const box = document.querySelector(".search");
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    } });
}

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

// --- Evaluation --------------------------------------------------------------
const RANGES = [["all", "All"], ["today", "Today"], ["7", "7D"], ["30", "30D"], ["90", "3M"], ["365", "12M"]];

function inRange(scan) {
  if (state.range === "all") return true;
  const t = new Date(scan.started_at).getTime();
  if (state.range === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return t >= d.getTime(); }
  return Date.now() - t <= Number(state.range) * 864e5;
}

function evaluationView() {
  if (!state.target) return [head("Evaluation", "")];
  const runs = state.scans.filter(finished).filter(inRange);
  const oldestFirst = [...runs].reverse();
  const keys = [...new Set(oldestFirst.flatMap((r) => (r.metric_scores || []).map((m) => m.key)))]
    .sort((a, b) => (state.series.get(a) ?? 99) - (state.series.get(b) ?? 99));
  const charted = keys.filter((k) => (state.series.get(k) ?? 99) < SERIES);
  const shown = charted.filter((k) => !state.hidden.has(k));
  const toggle = (k) => { if (state.hidden.has(k)) state.hidden.delete(k); else state.hidden.add(k); render(); };
  const picked = [...state.selected];
  const compareBtn = el("button", { type: "button", className: "btn", disabled: picked.length !== 2,
    title: "Tick two test runs to compare them", onclick: () => {
      const [a, b] = picked.map((id) => state.scans.find((s) => s.job_id === id)).sort((p, q) => q.version - p.version);
      openCompare(a.job_id, b.job_id);
    } }, `⇆ Compare selected (${picked.length}/2)`);
  const del = confirmButton("🗑 Delete all test runs", "Delete every test run of this chatbot", async () => {
    await api(`/api/targets/${state.target.id}/runs`, { method: "DELETE" });
    settings.set("runsChangedAt", Date.now());
    await selectTarget(state.target.id);
  });
  del.className = "btn btn-danger";
  // Two clicks (confirmButton) delete each ticked run and the scores it recorded.
  const delPicked = confirmButton(`🗑 Delete selected (${picked.length})`, "Delete the ticked test runs", async () => {
    for (const id of picked) await api(`/api/scans/${id}`, { method: "DELETE" });
    settings.set("runsChangedAt", Date.now());
    await selectTarget(state.target.id);
  });
  delPicked.className = "btn btn-danger";
  delPicked.disabled = !picked.length;
  return [
    head("Evaluation", `${state.target.name} · every test run of this chatbot`,
      el("a", { className: "btn btn-primary", href: "../index.html" }, "▶ Evaluate")),
    el("div", { className: "toolbar" }, seg(RANGES, state.range, (v) => { state.range = v; render(); }, "Date range")),
    el("div", { className: "grid grid-eval" },
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Metric Data"),
        el("p", { className: "section-sub" }, `Scores from ${runs.length} test run${runs.length === 1 ? "" : "s"} (error bar = ±1 std over the test cases)`),
        charted.length ? legend(charted, toggle) : "",
        metricDataChart(oldestFirst, shown),
        keys.length > SERIES ? el("p", { className: "muted small" }, `${keys.length - SERIES} more metrics are listed in the table below.`) : ""),
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Metric Trends"),
        el("p", { className: "section-sub" }, "% change in metric scores over the 7 most recent test runs"),
        metricTrends(oldestFirst, keys))),
    el("div", { className: "toolbar table-bar" },
      el("p", { className: "muted small" }, `Showing ${runs.length} of ${state.scans.filter(finished).length} test run(s)`),
      el("div", { className: "head-actions" }, compareBtn, delPicked, del, columnPicker(RUN_COLUMNS, state.runCols, "runCols"))),
    el("div", { className: "card", style: "padding:0" }, el("div", { className: "table-wrap" }, el("table", { className: "data" },
      el("thead", {}, el("tr", {}, el("th", { style: "width:36px" }, ""),
        ...Object.entries(RUN_COLUMNS).filter(([k]) => state.runCols.has(k)).map(([, label]) => el("th", {}, label)))),
      el("tbody", {}, ...(runs.length ? runs.map(runRow)
        : [el("tr", {}, el("td", { colspan: state.runCols.size + 1, className: "empty" }, "No test runs in this date range."))]))))),
  ];
}

function runRow(s) {
  const cells = {
    time: () => el("td", { className: "nowrap" }, formatRunTime(s.started_at)),
    run: () => el("td", {}, el("span", { className: "run-id" }, `v${s.version} · job-${s.job_id}`)),
    dataset: () => el("td", {}, el("span", { className: "badge badge-data" }, themeName())),
    // Failing metrics first, so "+N more" never hides a failure.
    scores: () => el("td", {}, el("div", { className: "score-pills" }, ...[...(s.metric_scores || [])]
      .sort((a, b) => (a.status === "fail" ? 0 : 1) - (b.status === "fail" ? 0 : 1)).slice(0, 6).map((m) =>
      el("span", { className: `score-pill ${m.status === "pass" ? "pass" : m.status === "fail" ? "fail" : "err"}`, title: m.status },
        `${m.status === "pass" ? "✓" : m.status === "fail" ? "✗" : "!"} ${title(m.key)}: ${formatScore(m.score)}`)),
    (s.metric_scores || []).length > 6 ? el("span", { className: "muted small" }, `+${s.metric_scores.length - 6} more`) : "")),
    result: () => el("td", { className: "nowrap" }, el("div", { className: "pass-bar", title: `${s.test_cases_passed} of ${s.test_cases} passed` },
      el("span", { style: `width:${s.test_cases ? (s.test_cases_passed / s.test_cases) * 100 : 0}%` })),
    el("span", { className: "small" }, `${s.test_cases_passed} / ${s.test_cases} test cases`)),
    status: () => el("td", {}, el("span", { className: `pill ${s.result === "passed" ? "pill-pass" : s.result === "failed" ? "pill-fail" : "pill-muted"}` }, s.result.toUpperCase())),
  };
  const box = el("input", { type: "checkbox", "aria-label": `Select test run v${s.version}`, checked: state.selected.has(s.job_id),
    onclick: (e) => e.stopPropagation(),
    onchange: (e) => { if (e.target.checked) state.selected.add(s.job_id); else state.selected.delete(s.job_id); render(); } });
  return el("tr", { className: "case-row", onclick: () => { selectScan(s.job_id, false).then(() => go("overview")); } },
    el("td", {}, box), ...Object.keys(RUN_COLUMNS).filter((k) => state.runCols.has(k)).map((k) => cells[k]()));
}

// --- Home --------------------------------------------------------------------
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
        el("p", { className: "muted small" }, scanTally(scan))),
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

// --- Overview ----------------------------------------------------------------
const INTERVALS = [0.05, 0.1, 0.2, 0.25];

function overviewView() {
  const scan = state.scan;
  if (!scan) return [head("Overview", "General information about the test run."), noScan()];
  const cases = casesOf(scan);
  const passing = cases.filter((c) => c.passed).length;
  const results = [...scan.results].sort((a, b) => (a.status === "pass") - (b.status === "pass"));
  const failures = cases.flatMap((c) => c.metrics.filter((m) => !m.passed).map((m) => ({ c, m }))).slice(0, 8);
  const opts = scan.run_options || {}, de = opts.deepeval || {};
  const cost = scan.results.reduce((sum, r) => sum + (typeof r.judge_cost_usd === "number" ? r.judge_cost_usd : 0), 0);
  const kv = (name, value, mono) => el("div", { className: "kv" }, el("span", {}, name), el("b", { className: mono ? "mono" : "" }, value));
  const yes = (v) => (v === undefined ? "default" : v ? "on" : "off");
  const copy = el("button", { type: "button", className: "btn", onclick: async (e) => {
    try { await navigator.clipboard.writeText(location.href); e.target.textContent = "✓ Link copied"; } catch { e.target.textContent = "Copy failed"; }
  } }, "⧉ Copy link");
  const analysisBody = {
    histogram: () => el("div", { className: "hist-grid" }, ...scan.results.filter((r) => rowScores(r).length).map((r) =>
      histogram([{ label: "scores", color: "var(--s1)", scores: rowScores(r) }], state.interval, title(r.key)))),
    average: () => metricBars(scan.results, (r) => r.score, "Average"),
    median: () => metricBars(scan.results, (r) => median(rowScores(r)), "Median"),
    breakdown: () => breakdownBars(scan.results),
  }[state.analysis];
  // Radar: every scored metric, or one dashboard category of them.
  const scored = scan.results.filter((r) => r.score != null);
  const cats = [...new Set(scored.map((r) => state.categories.get(r.key)).filter(Boolean))];
  if (state.radarCat && !cats.includes(state.radarCat)) state.radarCat = "";
  const radarResults = state.radarCat ? scored.filter((r) => state.categories.get(r.key) === state.radarCat) : scored;
  return [
    head("Overview", `${state.target.name} · test run v${scan.version}`, copy,
      el("a", { className: "btn", href: "#cases" }, "View test cases →")),
    el("div", { className: "grid grid-3" },
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Test Run Properties"),
        el("p", { className: "section-sub" }, "General info on the evaluation run"),
        kv("Test run ID", `job-${scan.job_id}`, true),
        kv("Dataset", el("span", { className: "badge badge-data" }, themeName())),
        kv("Identifier", `${state.target.name} · v${scan.version}`),
        kv("Executed at", formatRunTime(scan.started_at)),
        kv("Evaluation cost (USD)", cost ? `$${cost.toFixed(4)}` : "N/A"),
        kv("Judge tokens", (scan.judge_tokens || 0).toLocaleString()),
        kv("Duration", duration(scan.started_at, scan.finished_at))),
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Results Summary"),
        el("p", { className: "section-sub" }, "Number of passing test cases"),
        segmentedGauge(passing, cases.length),
        el("p", { className: "muted small center" }, scanTally(scan))),
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Hyperparameters"),
        el("p", { className: "section-sub" }, "How this test run was configured"),
        kv("Judge model", scan.judge_model || "–"),
        kv("Chatbot", state.target.name),
        kv("Cases per run", opts.limit ? String(opts.limit) : "all"),
        kv("Parallel metrics", opts.metric_concurrency ? String(opts.metric_concurrency) : "default"),
        kv("Strict mode", yes(de.strict_mode)),
        kv("Include reason", yes(de.include_reason)),
        kv("Consistency check", yes(opts.check_consistency)),
        opts.enhancements && opts.enhancements.length ? kv("Attack enhancements", opts.enhancements.join(", ")) : "",
        opts.max_latency_s ? kv("Latency limit", `${opts.max_latency_s}s`) : "")),
    el("div", { className: "grid grid-main", style: "margin-top:16px" },
      el("div", { className: "card" }, el("p", { className: "card-title" }, "Metric Scores Overview"),
        el("div", { className: "card-head" },
          el("p", { className: "section-sub" }, `Average score of each metric (lower-is-better as 1 − score)${radarResults.length > RADAR_LABELS ? "; hover a dot for its name" : ""}`),
          cats.length > 1 ? el("select", { "aria-label": "Metric category", onchange: (e) => { state.radarCat = e.target.value; render(); } },
            ...[["", `All metrics (${scored.length})`], ...cats.map((c) => [c, `${CATEGORY_LABELS[c] || c} (${scored.filter((r) => state.categories.get(r.key) === c).length})`])]
              .map(([value, label]) => { const o = el("option", { value }, label); o.selected = value === state.radarCat; return o; })) : ""),
        radar(radarResults)),
      el("div", { className: "card" },
        el("div", { className: "card-head" },
          el("div", {}, el("p", { className: "card-title" }, "Metrics Analysis"),
            el("p", { className: "section-sub" }, "Metric scores for the test cases")),
          el("div", { className: "head-actions" },
            state.analysis === "histogram" ? el("label", { className: "small muted" }, "Interval ",
              choice(INTERVALS, state.interval, (v) => { state.interval = v; render(); }, "Histogram interval")) : "",
            seg([["histogram", "Histogram"], ["average", "Average"], ["median", "Median"], ["breakdown", "Breakdown"]],
              state.analysis, (v) => { state.analysis = v; render(); }, "Analysis"))),
        analysisBody())),
    el("div", { className: "card", style: "margin-top:16px" },
      el("div", { className: "card-head" },
        el("div", {}, el("h2", { className: "section" }, "Test case scores"),
          el("p", { className: "section-sub" }, "One dot per test case, averaged over its metrics")),
        el("div", { className: "legend" }, el("span", {}, el("i", { style: "background:var(--pass)" }), "Passing"),
          el("span", {}, el("i", { style: "background:var(--fail)" }), "Failing"))),
      scoreStrip(cases)),
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

// --- Test Cases --------------------------------------------------------------
function casesView() {
  const scan = state.scan;
  if (!scan) return [head("Test Cases", ""), noScan()];
  const cases = casesOf(scan);
  const passed = cases.filter((c) => c.passed).length;
  const q = state.query.trim().toLowerCase();
  const shown = cases
    .filter((c) => state.filter === "all" || (state.filter === "passed") === c.passed)
    .filter((c) => !state.metricFilter || c.metrics.some((m) => m.key === state.metricFilter && !m.passed))
    .filter((c) => !q || [c.name, c.input, c.actual_output, c.expected_output].some((t) => t.toLowerCase().includes(q)));
  const keys = [...new Set(cases.flatMap((c) => c.metrics.map((m) => m.key)))];
  return [
    head("Test Cases", `${state.target.name} · v${scan.version} · ${formatRunTime(scan.started_at)}`),
    el("div", { className: "toolbar" },
      seg([["all", "All"], ["passed", "Passed"], ["failed", "Failed"]], state.filter, (v) => { state.filter = v; render(); }, "Show"),
      el("div", { className: "summary-pills" },
        el("span", {}, el("span", { className: "pill pill-pass" }, "PASSED"),
          el("b", {}, pct(cases.length ? passed / cases.length : null)), `${passed}/${cases.length} test cases`),
        el("span", {}, el("span", { className: "pill pill-fail" }, "FAILED"),
          el("b", {}, pct(cases.length ? (cases.length - passed) / cases.length : null)), `${cases.length - passed}/${cases.length} test cases`))),
    el("div", { className: "card filters" }, el("p", { className: "card-title" }, "Filters"),
      el("div", { className: "filter-row" }, searchBox(state.query, "Search input, output, name…", (v) => { state.query = v; }),
        el("label", { className: "small muted" }, "Failed on ",
          el("select", { onchange: (e) => { state.metricFilter = e.target.value; render(); } },
            el("option", { value: "" }, "any metric"),
            ...keys.map((k) => { const o = el("option", { value: k }, title(k)); o.selected = k === state.metricFilter; return o; }))),
        el("div", { className: "head-actions" },
          el("button", { type: "button", className: "btn", "aria-expanded": String(state.saving), onclick: () => { state.saving = !state.saving; render(); } },
            "⊕ Save as new dataset"),
          columnPicker(CASE_COLUMNS, state.caseCols, "caseCols"),
          el("button", { type: "button", className: "btn", onclick: () => downloadCsv(cases) }, "⭳ Download all as CSV"))),
      state.saving ? saveDatasetRow(shown, scan) : ""),
    el("p", { className: "muted small" }, `Showing ${shown.length} of ${cases.length} test case(s)`),
    el("div", { className: "card", style: "padding:0" }, el("div", { className: "table-wrap" }, el("table", { className: "data" },
      el("thead", {}, el("tr", {}, ...Object.entries(CASE_COLUMNS).filter(([k]) => state.caseCols.has(k)).map(([, l]) => el("th", {}, l)))),
      el("tbody", {}, ...(shown.length ? shown.flatMap(caseRows)
        : [el("tr", {}, el("td", { colspan: state.caseCols.size, className: "empty" }, "No test cases here."))]))))),
  ];
}

// Save as new dataset: the cases shown become goldens of a new golden set
// (theme). The expected answer is the case's own, else the reply it got.
function saveDatasetRow(shown, scan) {
  const name = el("input", { type: "text", className: "search", "aria-label": "New golden set name",
    value: `${themeName()}-v${scan.version}`, maxlength: 60 });
  const status = el("span", { className: "small muted", role: "status" }, "");
  const usable = shown.filter((c) => c.input.trim() && (c.expected_output || c.actual_output).trim());
  const save = el("button", { type: "button", className: "btn btn-primary", disabled: !usable.length, onclick: async () => {
    const theme = name.value.trim().replace(/\s+/g, "_");
    if (!theme) { status.textContent = "Give the golden set a name."; return; }
    save.disabled = true;
    let done = 0;
    try {
      for (const c of usable) {
        await api("/api/goldens", { method: "POST", body: { theme, question: c.input,
          expected_answer: c.expected_output || c.actual_output, context: c.context.map(String) } });
        status.textContent = `Saved ${++done} of ${usable.length}…`;
      }
      status.textContent = `✓ Saved ${done} golden${done === 1 ? "" : "s"} to golden set '${theme}'. Pick it as a chatbot's golden set in the side panel.`;
    } catch (error) {
      status.textContent = `Stopped after ${done}: ${error.message}`;
    } finally {
      save.disabled = false;
    }
  } }, `Save ${usable.length} test case${usable.length === 1 ? "" : "s"}`);
  return el("div", { className: "filter-row save-row" }, el("label", { className: "small muted" }, "Golden set name ", name), save, status,
    usable.length < shown.length ? el("span", { className: "small muted" }, `${shown.length - usable.length} without input or answer are left out`) : "");
}

function caseRows(c) {
  const open = state.open.has(c.key);
  const cells = {
    name: () => el("td", {}, `${open ? "▾" : "▸"} `, el("strong", {}, c.name)),
    status: () => el("td", {}, el("span", { className: `pill ${c.passed ? "pill-pass" : "pill-fail"}` }, c.passed ? "PASSED" : "FAILED")),
    input: () => el("td", { className: "clip", title: c.input }, c.input),
    actual_output: () => el("td", { className: "clip", title: c.actual_output }, c.actual_output),
    expected_output: () => el("td", { className: "clip", title: c.expected_output }, c.expected_output || "None"),
    context: () => el("td", { className: "clip" }, c.context.length ? `${c.context.length} chunk(s)` : "None"),
    score: () => el("td", {}, formatScore(c.score)),
    latency: () => el("td", {}, typeof c.latency_s === "number" ? `${c.latency_s.toFixed(2)}s` : "–"),
  };
  const row = el("tr", { className: "case-row", "aria-expanded": String(open),
    onclick: () => { if (open) state.open.delete(c.key); else state.open.add(c.key); render(); } },
  ...Object.keys(CASE_COLUMNS).filter((k) => state.caseCols.has(k)).map((k) => cells[k]()));
  if (!open) return [row];
  const tab = state.caseTab.get(c.key) || "all";
  const prop = (name, value) => el("div", { className: "prop" }, el("span", {}, name), value || "None");
  const json = { name: c.name, status: c.passed ? "passed" : "failed", input: c.input, actual_output: c.actual_output,
    expected_output: c.expected_output, context: c.context, tools_called: c.tools_called,
    metrics: c.metrics.map(({ key, score, passed, reason }) => ({ metric: title(key), score, passed, reason })) };
  const pick = (value) => { state.caseTab.set(c.key, value); render(); };
  const m = c.metrics.find((x) => x.key === tab);
  const body = state.json ? el("pre", { className: "json" }, JSON.stringify(json, null, 2))
    : m ? el("div", { className: "props-grid" },
      prop("Metric", title(m.key)), prop("Status", m.passed ? "Passed" : "Failed"),
      prop("Score", `${formatScore(m.score)}${m.threshold != null ? ` (threshold ${formatScore(m.threshold)}, ${m.direction === "lower" ? "max" : "min"})` : ""}`),
      prop("Reason", m.reason), prop("Actual output it judged", m.actual_output || c.actual_output))
      : el("div", { className: "props-grid" },
        prop("Name", c.name), prop("Status", c.passed ? "Passed" : "Failed"),
        prop("Run duration", typeof c.latency_s === "number" ? `${c.latency_s.toFixed(2)}s` : "–"),
        prop("Input", c.input), prop("Actual output", c.actual_output), prop("Expected output", c.expected_output),
        el("div", { className: "prop" }, el("span", {}, "Retrieval context"), contextCards(c.context)),
        el("div", { className: "prop" }, el("span", {}, "Tools called"), toolList(c.tools_called)));
  const detail = el("td", { className: "detail-cell", colspan: state.caseCols.size },
    el("div", { className: "detail-bar" },
      el("div", { className: "tabs", role: "tablist" },
        el("button", { type: "button", role: "tab", "aria-selected": String(!m), onclick: () => pick("all") }, "All Properties"),
        ...c.metrics.map((x) => el("button", { type: "button", role: "tab", "aria-selected": String(tab === x.key),
          className: x.passed ? "pass" : "fail", onclick: () => pick(x.key) }, `${x.passed ? "✓" : "✗"} ${title(x.key)} (${formatScore(x.score)})`))),
      el("label", { className: "switch" }, el("input", { type: "checkbox", checked: state.json,
        onchange: (e) => { state.json = e.target.checked; render(); } }), "View JSON")),
    body);
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

// --- Compare Test Results ----------------------------------------------------
function matchKey(c) { return state.cmp.match === "input" ? `q:${c.input.trim().toLowerCase()}` : c.key; }

// A case regressed when a metric that passed it in the baseline fails it now,
// improved when one that failed passes now (as /api/scans/{id}/cases does).
function caseChange(now, before) {
  if (!before) return "new";
  if (!now) return "removed";
  const was = new Map(before.metrics.map((m) => [m.key, m.passed]));
  const flips = now.metrics.filter((m) => was.has(m.key)).map((m) => [was.get(m.key), m.passed]);
  if (flips.some(([o, n]) => o && !n)) return "regressed";
  if (flips.some(([o, n]) => !o && n)) return "improved";
  return "same";
}

function compareView() {
  const scan = state.scan, base = state.baseline;
  if (!scan) return [head("Compare Test Results", ""), noScan()];
  const others = state.scans.filter(finished).filter((s) => s.job_id !== scan.job_id);
  const picker = el("select", { "aria-label": "Baseline test run", onchange: (e) => setBaseline(Number(e.target.value) || null) },
    el("option", { value: "" }, "Select test run to compare against"),
    ...others.map((s) => { const o = el("option", { value: s.job_id }, `v${s.version} · ${formatRunTime(s.started_at)}`); o.selected = Boolean(base) && base.job_id === s.job_id; return o; }));
  const top = el("div", { className: "toolbar cmp-pick" },
    el("div", { className: "head-actions" },
      el("span", { className: "run-id" }, `v${scan.version} · job-${scan.job_id}`),
      el("button", { type: "button", className: "btn", title: "Swap current and baseline", "aria-label": "Swap current and baseline", disabled: !base,
        onclick: () => openCompare(base.job_id, scan.job_id) }, "⇄"),
      picker,
      base ? el("a", { className: "small", href: `?target=${state.target.id}&job=${base.job_id}#overview` }, "view test run ↗") : ""));
  if (!base) return [head("Compare Test Results", `${state.target.name}: pick a baseline test run`), top,
    el("div", { className: "card empty" }, others.length ? "Select a test run to compare against." : "This chatbot has only one finished test run.")];

  const now = casesOf(scan), before = casesOf(base);
  const nowBy = new Map(now.map((c) => [matchKey(c), c])), beforeBy = new Map(before.map((c) => [matchKey(c), c]));
  const keys = [...nowBy.keys(), ...[...beforeBy.keys()].filter((k) => !nowBy.has(k))];
  const pairs = keys.map((k) => ({ key: k, a: nowBy.get(k), b: beforeBy.get(k), change: caseChange(nowBy.get(k), beforeBy.get(k)) }));
  const counts = Object.fromEntries(["regressed", "improved", "same", "new", "removed"].map((c) => [c, pairs.filter((p) => p.change === c).length]));
  const q = state.cmp.query.trim().toLowerCase();
  const shown = pairs.filter((p) => state.cmp.filter === "all" || p.change === state.cmp.filter)
    .filter((p) => !q || (p.a || p.b).input.toLowerCase().includes(q));
  const side = (s, cs, label) => {
    const ok = cs.filter((c) => c.passed).length;
    return el("div", { className: "cmp-side" }, el("p", { className: "card-title center" }, `${label} · v${s.version}`),
      donut(cs.length ? ok / cs.length : 0, pct(cs.length ? ok / cs.length : null), `${ok} / ${cs.length}`),
      el("p", { className: "center" }, `${cs.length - ok} failing test case(s)`));
  };
  const metricKeys = [...new Set([...scan.results, ...base.results].map((r) => r.key))];
  const series = (key) => [
    state.cmp.current ? { label: `Current v${scan.version}`, color: "var(--cur)", scores: rowScores(scan.results.find((r) => r.key === key) || {}) } : null,
    state.cmp.baseline ? { label: `Baseline v${base.version}`, color: "var(--base)", scores: rowScores(base.results.find((r) => r.key === key) || {}) } : null,
  ].filter(Boolean);
  return [
    head("Compare Test Results", `${state.target.name} · v${scan.version} against v${base.version}`),
    top,
    el("details", { className: "card fold", open: true }, el("summary", {}, "Overview comparer"),
      el("div", { className: "cmp-grid" }, side(scan, now, "Current test run"), side(base, before, "Baseline test run")),
      el("div", { className: "toolbar center-bar" },
        el("label", { className: "small muted" }, "Distribution interval ",
          choice(INTERVALS, state.interval, (v) => { state.interval = v; render(); }, "Distribution interval")),
        el("label", { className: "small" }, el("input", { type: "checkbox", checked: state.cmp.current,
          onchange: (e) => { state.cmp.current = e.target.checked; render(); } }), el("i", { className: "swatch", style: "background:var(--cur)" }), "Current test run"),
        el("label", { className: "small" }, el("input", { type: "checkbox", checked: state.cmp.baseline,
          onchange: (e) => { state.cmp.baseline = e.target.checked; render(); } }), el("i", { className: "swatch", style: "background:var(--base)" }), "Baseline test run")),
      el("div", { className: "hist-grid" }, ...metricKeys.map((k) => histogram(series(k), state.interval, title(k))))),
    el("div", { className: "card filters" }, el("p", { className: "card-title" }, "Test case filters"),
      el("div", { className: "filter-row" },
        seg([["all", `All ${pairs.length}`], ["regressed", `Regressed ${counts.regressed}`], ["improved", `Improved ${counts.improved}`],
          ["same", `Same ${counts.same}`], ["new", `New ${counts.new}`], ["removed", `Removed ${counts.removed}`]],
        state.cmp.filter, (v) => { state.cmp.filter = v; render(); }, "Show"),
        searchBox(state.cmp.query, "Search input…", (v) => { state.cmp.query = v; }))),
    el("div", { className: "toolbar" },
      el("p", { className: "muted small" }, `Showing ${shown.length} of ${pairs.length} matching test case(s)`),
      el("div", { className: "head-actions small muted" }, "Match by ",
        seg([["id", "Golden"], ["input", "Input"]], state.cmp.match, (v) => { state.cmp.match = v; state.cmp.open.clear(); render(); }, "Match by"))),
    el("div", { className: "cmp-list" }, ...(shown.length ? shown.map(comparePair) : [el("div", { className: "card empty" }, "No test cases here.")])),
  ];
}

function comparePair(p) {
  const open = state.cmp.open.has(p.key);
  const failing = (p.a && !p.a.passed) || (!p.a && p.b && !p.b.passed);
  const name = (c) => (c ? c.name : "—");
  const toggle = () => { if (open) state.cmp.open.delete(p.key); else state.cmp.open.add(p.key); render(); };
  const headRow = el("button", { type: "button", className: `cmp-head ${failing ? "fail" : "pass"}`, "aria-expanded": String(open), onclick: toggle },
    el("span", { className: "caret" }, open ? "▾" : "▸"),
    el("span", { className: `pill pill-${p.change}` }, p.change.toUpperCase()),
    el("span", { className: "cmp-name" }, `⚗ ${name(p.a)}`), el("span", { className: "cmp-name" }, `⚗ ${name(p.b)}`));
  if (!open) return el("div", { className: "cmp-item" }, headRow);
  const cell = (c, f) => el("div", { className: "cmp-cell" }, c ? f(c) : el("span", { className: "muted" }, "Not in this run"));
  const line = (label, f) => el("div", { className: "cmp-line" }, el("span", { className: "cmp-label" }, label), cell(p.a, f), cell(p.b, f));
  return el("div", { className: "cmp-item" }, headRow, el("div", { className: "cmp-body" },
    line("Metrics", (c) => el("div", { className: "chips col" }, ...[...c.metrics].sort((x, y) => x.passed - y.passed).map(metricChip))),
    line("Input", (c) => c.input),
    line("Actual output", (c) => c.actual_output || "None"),
    line("Expected output", (c) => c.expected_output || "None"),
    line("Retrieval context", (c) => contextCards(c.context)),
    line("Tools called", (c) => toolList(c.tools_called))));
}

async function setBaseline(jobId) {
  state.baseline = jobId ? await loadScan(jobId) : null;
  state.cmp.open.clear();
  syncUrl();
  render();
}

async function openCompare(jobId, withId) {
  state.baseline = await loadScan(withId);
  await selectScan(jobId, false);
  state.cmp.open.clear();
  syncUrl();
  go("compare");
}

// --- Navigation ------------------------------------------------------------
const VIEWS = { evaluation: evaluationView, home: homeView, overview: overviewView, cases: casesView, compare: compareView };
const VIEW_TITLES = { evaluation: "Evaluation", home: "Home", overview: "Overview", cases: "Test Cases", compare: "Compare Test Results" };

function go(view, openKey) {
  if (openKey) state.open.add(openKey);
  if (location.hash.slice(1) === view) render();
  else location.hash = view;
}

function render() {
  hideTip();
  state.view = VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "evaluation";
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

function syncUrl() {
  const params = new URLSearchParams({ target: state.target.id });
  if (state.scan) params.set("job", state.scan.job_id);
  if (state.baseline) params.set("with", state.baseline.job_id);
  history.replaceState(null, "", `?${params}${location.hash}`);
}

async function selectScan(jobId, draw = true) {
  state.scan = jobId ? await loadScan(jobId) : null;
  state.open.clear();
  state.caseTab.clear();
  if (state.scan) $("scan").value = String(jobId);
  // Default baseline: the finished run before this one.
  if (!state.baseline || !state.scan || state.baseline.job_id === state.scan.job_id) {
    const done = state.scans.filter(finished);
    const i = done.findIndex((s) => state.scan && s.job_id === state.scan.job_id);
    const older = i >= 0 ? done[i + 1] : null;
    state.baseline = older ? await loadScan(older.job_id) : null;
  }
  syncUrl();
  if (draw) render();
}

async function selectTarget(targetId, jobId, withId) {
  state.target = state.targets.find((t) => String(t.id) === String(targetId)) || state.targets[0] || null;
  if (!state.target) {
    $("view").replaceChildren(el("p", { className: "empty" }, "No chatbots yet: add one in the side panel."));
    return;
  }
  state.cache.clear();
  state.selected.clear();
  state.baseline = null;
  state.scans = await api(`/api/targets/${state.target.id}/scans`);
  state.series = seriesIndex();
  const done = state.scans.filter(finished);
  fillSelect($("scan"), done.map((s) => ({ value: s.job_id, label: `v${s.version} · ${formatRunTime(s.started_at)}` })),
    jobId || (done[0] && done[0].job_id));
  if (withId && done.some((s) => String(s.job_id) === String(withId))) state.baseline = await loadScan(Number(withId));
  await selectScan(Number($("scan").value) || null);
}

async function init() {
  const params = new URLSearchParams(location.search);
  try {
    const [targets, metrics] = await Promise.all([api("/api/targets"), api("/api/metrics").catch(() => [])]);
    state.targets = targets;
    state.titles = new Map(metrics.map((m) => [m.key, m.title]));
    state.categories = new Map(metrics.map((m) => [m.key, m.ui_category || m.group]));
    const targetId = params.get("target") || (targets[0] && targets[0].id);
    fillSelect($("target"), targets.map((t) => ({ value: t.id, label: t.name })), targetId);
    await selectTarget(targetId, params.get("job"), params.get("with"));
  } catch (error) {
    $("view").replaceChildren(el("p", { className: "empty" }, `Could not load: ${error.message}`));
  }
}

$("target").addEventListener("change", () => selectTarget($("target").value));
$("scan").addEventListener("change", () => selectScan(Number($("scan").value)));
window.addEventListener("hashchange", render);
init();
