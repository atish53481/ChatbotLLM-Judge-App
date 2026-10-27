// Helpers shared by the side panel and the dashboard. Everything that comes
// from a chatbot or the backend is inserted as text, never as HTML.

const ALL_METRICS = "__all__";
const DEFAULT_THEME = "generic";
// Thresholds per metric live in settings ("thresholds": {key: value});
// a metric with none set uses its catalog default. "thresholdEnv" names the
// preset they came from (default / local / pr / staging / production / custom).
const THRESHOLD_ENVS = [
  { key: "default", label: "Default (per metric)" },
  { key: "local", label: "Local Development" },
  { key: "pr", label: "PR / Feature Branch" },
  { key: "staging", label: "Staging / QA" },
  { key: "production", label: "Production" },
  { key: "custom", label: "Custom" },
];
const GROUP_PREFIX = "group:";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function fillSelect(select, items, selectedValue) {
  select.replaceChildren(
    ...items.map(({ value, label }) => {
      const option = el("option", { value }, label);
      option.selected = String(value) === String(selectedValue);
      return option;
    }),
  );
}

// Turns a button into a two-click delete (no native dialogs in the side panel).
function armConfirm(button, onConfirm) {
  const label = button.textContent;
  let timer = null;
  button.addEventListener("click", async () => {
    if (!timer) {
      button.textContent = "Confirm";
      timer = setTimeout(() => {
        timer = null;
        button.textContent = label;
      }, 3000);
      return;
    }
    clearTimeout(timer);
    timer = null;
    button.textContent = label;
    button.disabled = true;
    try {
      await onConfirm();
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function confirmButton(label, ariaLabel, onConfirm) {
  return armConfirm(el("button", { type: "button", "aria-label": ariaLabel }, label), onConfirm);
}

function formatScore(score) {
  return typeof score === "number" ? score.toFixed(2) : "n/a";
}

function formatTokens(tokens) {
  if (typeof tokens !== "number") return "";
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
}

// "2 skipped (too long) · 14.2k tokens · ⚠ judge unstable (0.40)": how a finished run was judged.
function judgingSummary(result) {
  const parts = [];
  if (result.cases_skipped) parts.push(`${result.cases_skipped} skipped (too long)`);
  if (result.judge && typeof result.judge.tokens === "number") parts.push(`${formatTokens(result.judge.tokens)} tokens`);
  if (result.judge_unstable) parts.push(`⚠ judge unstable (${formatScore(result.judge_spread)})`);
  return parts.join(" · ");
}

function formatRunTime(ts) {
  // Backend timestamps are ISO-8601 UTC, e.g. "2026-09-16T14:32:07Z".
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return String(ts);
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function resultLabel(passed) {
  return passed ? "✓ pass" : "✕ fail";
}

function themeOf(target) {
  return (target && target.config && target.config.theme) || DEFAULT_THEME;
}

function targetLabel(target) {
  return target.name;
}

// "Metric 2/7 · 3/10 · judging: What is your refund window? · 0:42" for a run in flight.
function progressText(progress) {
  if (!progress) return "starting…";
  if (progress.queued) return "waiting for the run before it to finish…";
  const parts = [];
  if (progress.metricCount > 1) parts.push(`metric ${progress.metricIndex + 1}/${progress.metricCount}`);
  if (progress.total) {
    const doing = progress.phase === "judge" ? "judging" : "asking chatbot";
    parts.push(`${progress.done}/${progress.total} · ${doing}: ${progress.question}`);
  } else {
    parts.push("starting…");
  }
  const seconds = Math.round((Date.now() - progress.startedAt) / 1000);
  parts.push(`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`);
  return parts.join(" · ");
}

// 0..1 through the current metric: each golden counts half for the chatbot, half for the judge.
function progressFraction(progress) {
  if (!progress || !progress.total) return 0;
  return (progress.done + (progress.phase === "judge" ? 0.5 : 0)) / progress.total;
}

// Metrics in catalog order, grouped: [{group, label, metrics}].
// Grouped by the dashboard categories (Quality, Retrieval, Safety, Security,
// G-Eval, Conversational), so the side panel and the dashboard match; the
// backend already sends the metrics in that order.
function metricGroups(metrics) {
  const groups = [];
  for (const metric of metrics) {
    let group = groups.find((g) => g.group === metric.ui_category);
    if (!group) {
      group = { group: metric.ui_category, label: metric.ui_category_label || metric.ui_category, metrics: [] };
      groups.push(group);
    }
    group.metrics.push(metric);
  }
  return groups;
}

// The metric picker: "All metrics", then per group "All · <group>" and its metrics.
function fillMetricSelect(select, metrics, selectedValue) {
  const options = [el("option", { value: ALL_METRICS }, "All metrics")];
  for (const group of metricGroups(metrics)) {
    options.push(el(
      "optgroup",
      { label: group.label },
      el("option", { value: GROUP_PREFIX + group.group }, `All · ${group.label}`),
      ...group.metrics.map((m) => el("option", { value: m.key }, m.title)),
    ));
  }
  select.replaceChildren(...options);
  const values = [...select.options].map((o) => o.value);
  select.value = values.includes(String(selectedValue)) ? selectedValue : ALL_METRICS;
}

// The metrics a picker choice stands for.
function metricsFor(choice, metrics) {
  if (choice === ALL_METRICS) return metrics;
  if (choice.startsWith(GROUP_PREFIX)) return metrics.filter((m) => m.ui_category === choice.slice(GROUP_PREFIX.length));
  return metrics.filter((m) => m.key === choice);
}

function choiceLabel(choice, metrics) {
  if (choice === ALL_METRICS) return "all metrics";
  if (choice.startsWith(GROUP_PREFIX)) {
    const group = metrics.find((m) => m.ui_category === choice.slice(GROUP_PREFIX.length));
    return group ? group.ui_category_label : choice;
  }
  const metric = metrics.find((m) => m.key === choice);
  return metric ? metric.title : choice;
}

// What a metric's judge reads, in the words the dashboard uses (DeepEval field -> label).
const FIELD_LABELS = {
  input: "Question",
  actual_output: "Actual result",
  expected_output: "Expected result",
  context: "Context (known facts)",
  retrieval_context: "Retrieved context",
  turns: "Conversation",
  scenario: "Scenario",
  expected_outcome: "Expected outcome",
};

function fieldLabel(field) {
  return FIELD_LABELS[field] || field;
}

// "≥" for higher-is-better metrics, "≤" for violation rates (lower is better).
function comparator(direction) {
  return direction === "lower" ? "≤" : "≥";
}

// The catalog with each metric's threshold replaced by the user's (for charts).
function withUserThresholds(metrics, thresholds) {
  return metrics.map((m) => ({ ...m, threshold: thresholdFor(m, thresholds) }));
}

function thresholdFor(metric, thresholds) {
  const value = thresholds && thresholds[metric.key];
  return typeof value === "number" ? value : metric.threshold;
}

// The pages run as a web app served by the backend (http://127.0.0.1:8765/):
// settings live in localStorage, finished runs show as browser notifications
// plus a mark in the tab title.
const STORAGE_PREFIX = "llmJudge:";
const webListeners = [];

function parseStored(raw) {
  try {
    return raw === null ? undefined : JSON.parse(raw);
  } catch {
    return undefined;
  }
}

// Settings kept per browser tab, not shared by every tab: the fresh-session
// mark. Loading the side panel in a second tab (or reloading another one) must
// not blank the scores this tab is showing. The panel and dashboard frames of
// one tab still share it (and hear its changes) through sessionStorage.
const TAB_KEYS = new Set(["freshSinceRunId"]);

function storeFor(key) {
  return TAB_KEYS.has(key) ? sessionStorage : localStorage;
}

function webRead(key) {
  return parseStored(storeFor(key).getItem(STORAGE_PREFIX + key));
}

// Every listener hears every change, the writing page's own included; other
// tabs and frames hear it via the "storage" event.
function webWrite(key, value) {
  const oldValue = webRead(key);
  storeFor(key).setItem(STORAGE_PREFIX + key, JSON.stringify(value));
  const changes = { [key]: { oldValue, newValue: value } };
  setTimeout(() => webListeners.forEach((callback) => callback(changes)), 0);
}

// The selected chatbot, the selected metric and the latest run are shared by
// the side panel and the dashboard.
const settings = {
  async get(key, fallback) {
    return webRead(key) ?? fallback;
  },
  async set(key, value) {
    return webWrite(key, value);
  },
  onChange(callback) {
    webListeners.push(callback);
  },
};

// Once per browser: thresholds saved for these four metrics when they were shown
// as violation rates (a maximum) move to DeepEval's own scale (1 = clean, a minimum).
(function migrateThresholdScale() {
  try {
    if (webRead("thresholdScale") === "deepeval") return;
    const saved = webRead("thresholds");
    if (saved && typeof saved === "object") {
      for (const key of ["hallucination", "bias", "toxicity", "pii_leakage"]) {
        if (typeof saved[key] === "number") saved[key] = Math.round((1 - saved[key]) * 1e6) / 1e6;
      }
      webWrite("thresholds", saved);
    }
    webWrite("thresholdScale", "deepeval");
  } catch {
    // No storage (private window, blocked site data): nothing saved to move.
  }
})();

// Each fresh load of the panel starts a session (POST /api/session/start):
// scores, trend and cases show only runs with an id above this, while every
// run stays in SQLite and the dashboard's History view lists them all.
async function sessionSince() {
  return Number(await settings.get("freshSinceRunId", 0)) || 0;
}

// A page of this app (path relative to the app root, e.g. "report/report.html").
function pageUrl(path) {
  return new URL(`../${path}`, location.href).href;
}

function openDashboard() {
  window.open(pageUrl("dashboard/dashboard.html"), "llm-judge-dashboard");
}

// A finished run puts a mark in the tab title; seeing the tab clears it.
const BASE_TITLE = document.title;
function clearBadge() {
  document.title = BASE_TITLE;
}

// Announces a finished job this browser started, once, even with the side
// panel and the dashboard both following it.
async function announceWebJob(job) {
  const watchedJobs = webRead("watchedJobs") || {};
  const info = watchedJobs[job.id];
  if (!info) return;
  delete watchedJobs[job.id];
  webWrite("watchedJobs", watchedJobs);
  const summary = jobAnnouncement(job, info.target, info.metrics);
  if (!summary) return;
  if (document.hidden) document.title = `(${summary.badge}) ${BASE_TITLE}`;
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    const note = new Notification(summary.title, { body: summary.message, icon: pageUrl("icons/icon-128.png") });
    note.onclick = () => { window.focus(); note.close(); };
  }
}

// Changes written by the other pane or another tab.
window.addEventListener("storage", (event) => {
  if (!event.key || !event.key.startsWith(STORAGE_PREFIX)) return;
  const key = event.key.slice(STORAGE_PREFIX.length);
  const changes = { [key]: { oldValue: parseStored(event.oldValue), newValue: parseStored(event.newValue) } };
  webListeners.forEach((callback) => callback(changes));
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) clearBadge(); });

async function fetchTargets() {
  return api("/api/targets");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function formatElapsed(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

// What the side panel's run banner says: progress while a run goes, then how
// it ended. status is the job's ("running", "queued", "done", "cancelled",
// "error", "interrupted") or "empty" when nothing could be sent; results are
// the finished metrics; count how many were sent; current the running metric's
// title; at the finish time to show. tone picks the banner colour.
function runSummary({ status, results = [], count = 0, elapsedMs = 0, error = null, current = "", at = "" }) {
  const took = formatElapsed(elapsedMs);
  const finished = results.filter((r) => r.status !== "cancelled");
  const passed = finished.filter((r) => r.status === "pass").length;
  const failed = finished.filter((r) => r.status === "fail").length;
  const errors = finished.filter((r) => r.status === "error").length;
  const tally = [
    passed ? `${passed} passed` : "", failed ? `${failed} failed` : "", errors ? `${errors} could not run` : "",
  ].filter(Boolean).join(" · ");
  const when = at ? ` at ${at}` : "";
  if (status === "queued") return { tone: "running", text: `Waiting for the run before this one to finish… ${took}` };
  if (status === "running") {
    const n = Math.min(finished.length + 1, count || finished.length + 1);
    return { tone: "running", text: `Running metric ${n} of ${count || n}${current ? `: ${current}` : ""} · ${took}` };
  }
  if (status === "empty") {
    return { tone: "error", text: "Nothing ran: the ticked metrics have no cases for this chatbot." };
  }
  if (status === "done") {
    const tone = failed ? "fail" : errors ? "error" : "pass";
    const mark = { pass: "✓", fail: "✕", error: "!" }[tone];
    return { tone, text: `${mark} Run complete${when} · ${tally || "no results"} · took ${took}` };
  }
  if (status === "cancelled") {
    return { tone: "error", text: `■ Run stopped${when} · ${finished.length} of ${count} finished${tally ? ` (${tally})` : ""}` };
  }
  return { tone: "error", text: `✕ Run ${status || "failed"}${when}${error ? `: ${error}` : ""}` };
}

// A backend job's live progress in the shape progressText/progressFraction read.
function jobProgress(current) {
  return {
    metricIndex: current.index, metricCount: current.count, startedAt: Date.parse(current.started_at),
    done: current.done, total: current.total, phase: current.phase, question: current.question,
  };
}

// Runs the metrics as a backend job (POST /api/jobs): the run goes on if this
// page closes, and the service worker announces it when it ends. control is
// how a Stop button reaches the job (stopRun); control.limit is the dashboard's
// cases per run and control.metrics the catalog, for the notification's titles.
async function runMetrics(target, metricKeys, onProgress, control = {}) {
  if (!metricKeys.length) return [];  // nothing this chatbot can run
  const thresholds = await settings.get("thresholds", {});
  const checkConsistency = await settings.get("checkConsistency", false);
  // Run mode (dashboard header), for runs started on any page: Low = 1 case per
  // metric, High = every case (null). The dashboard passes its own as control.limit.
  const limit = control.limit || (await settings.get("casesPerRun", null));
  const parallel = await settings.get("metricConcurrency", null);
  const picked = Object.fromEntries(metricKeys
    .filter((key) => typeof thresholds[key] === "number")
    .map((key) => [key, thresholds[key]]));
  const job = await api("/api/jobs", {
    method: "POST",
    body: {
      target_id: target.id, metric_keys: metricKeys, thresholds: picked,
      limit: limit || undefined, check_consistency: checkConsistency,
      metric_concurrency: parallel || undefined,
      ...(await runOptionsBody(metricKeys)),
    },
  });
  watchJob(target, job.job_id, control.metrics || []);
  // Lets the other page (side panel or dashboard) pick up and show this run.
  await settings.set("startedJob", { targetId: target.id, jobId: job.job_id, at: Date.now() });
  return followJob(target, job.job_id, onProgress, control);
}

// A "Parallel metrics" <select>: Default (the server's JUDGE_METRIC_CONCURRENCY)
// or any count from the server's min to max (GET /api/run-limits). Saved as
// setting "metricConcurrency" (null = default), shared by the side panel and dashboard.
async function bindParallelSelect(select) {
  let limits = { metric_concurrency: { default: 2, min: 1, max: 8 }, judge_concurrency: 4 };
  try {
    limits = await api("/api/run-limits");
  } catch {
    // Older backend: the usual bounds.
  }
  const { default: fallback, min, max } = limits.metric_concurrency;
  const options = [el("option", { value: "" }, `Default · ${fallback}`)];
  for (let n = min; n <= max; n += 1) {
    const label = n === min ? `${n} · one at a time (min)` : n === max ? `${n} (max)` : String(n);
    options.push(el("option", { value: String(n) }, label));
  }
  select.replaceChildren(...options);
  select.title = `How many metrics of a run are scored at the same time (${min} to ${max}). `
    + `The judge takes at most ${limits.judge_concurrency} calls at once, so more metrics than that wait for it.`;
  const show = (value) => { select.value = value ? String(value) : ""; };
  show(await settings.get("metricConcurrency", null));
  select.addEventListener("change", () =>
    settings.set("metricConcurrency", select.value ? Number(select.value) : null));
  settings.onChange((changes) => {
    if (changes.metricConcurrency) show(changes.metricConcurrency.newValue);
  });
}

// The optional run settings saved on the Advanced page ("runOptions"):
// per-metric settings, red-team enhancers, a latency limit and judge
// calibration. Nothing saved sends nothing, so runs behave as before.
async function runOptionsBody(metricKeys) {
  const options = await settings.get("runOptions", {});
  const body = {};
  const perMetric = options.metricSettings || {};
  const picked = Object.fromEntries(metricKeys.filter((key) => perMetric[key]).map((key) => [key, perMetric[key]]));
  if (Object.keys(picked).length) body.settings = picked;
  if (options.enhancements && options.enhancements.length) {
    body.enhancements = options.enhancements;
    body.enhancement_options = options.enhancementOptions || {};
  }
  if (typeof options.maxLatencyS === "number" && options.maxLatencyS > 0) body.max_latency_s = options.maxLatencyS;
  if (options.useCalibration) body.use_calibration = true;
  if (options.deepeval) body.deepeval = options.deepeval;
  return body;
}

// A scan's tally the DeepEval way: test cases passed (a case passes only when
// every metric passes it), then metrics passed, then metrics left out.
function scanTally(scan) {
  const parts = [];
  if (scan.test_cases) parts.push(`${scan.test_cases_passed} of ${scan.test_cases} test cases passed`);
  parts.push(`${scan.passed} of ${scan.metrics} metrics passed`);
  if (scan.skipped && scan.skipped.length) parts.push(`${scan.skipped.length} not run`);
  return parts.join(" · ");
}

function openAdvanced() {
  window.open(pageUrl("advanced/advanced.html"), "llm-judge-advanced");
}

function watchJob(target, jobId, metrics) {
  const info = { target: { id: target.id, name: target.name }, metrics: metrics.map((m) => ({ key: m.key, title: m.title })) };
  webWrite("watchedJobs", { ...(webRead("watchedJobs") || {}), [jobId]: info });
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}

// Polls a job until it ends, reporting each metric once: onProgress(key, null,
// progress) while it runs, onProgress(key, result) when it finishes. A backend
// that stops answering is retried (control.onOffline tells the page).
async function followJob(target, jobId, onProgress, control = {}) {
  control.jobId = jobId;
  const reported = new Set();
  let offline = false;
  for (;;) {
    let job;
    try {
      job = await api(`/api/jobs/${jobId}`);
    } catch (error) {
      // Tell the page once when the backend stops answering, not on every retry.
      if (!offline && control.onOffline) control.onOffline(true, error);
      offline = true;
      await sleep(2000);
      continue;
    }
    if (offline && control.onOffline) control.onOffline(false);
    offline = false;
    for (const result of job.results) {
      if (reported.has(result.key)) continue;
      reported.add(result.key);
      onProgress(result.key, result);
      if (result.status !== "cancelled") {
        await settings.set("lastRun", { targetId: target.id, metricKey: result.key, finishedAt: Date.now(), result });
      }
    }
    // Metrics of one job run side by side: report each one still running.
    const running = (job.running || (job.current ? [job.current] : []))
      .filter((current) => !reported.has(current.metric_key));
    if (running.length) {
      for (const current of running) onProgress(current.metric_key, null, jobProgress(current));
    } else if (job.status === "queued" && job.metric_keys && job.metric_keys.length) {
      // Waiting behind another run: say so instead of looking hung.
      onProgress(job.metric_keys[0], null, {
        queued: true, metricIndex: 0, metricCount: job.metric_keys.length, startedAt: Date.now(),
      });
    }
    if (job.status !== "queued" && job.status !== "running") {
      announceWebJob(job).catch(() => {});
      control.jobStatus = job.status;
      control.jobError = job.error || null;
      return job.results;
    }
    await sleep(1000);
  }
}

// The job still running for this chatbot, if any, so a reopened page can follow it.
async function activeJob(target) {
  try {
    const [job] = await api(`/api/jobs?target_id=${target.id}&active=1`);
    return job || null;
  } catch {
    return null;
  }
}

// Stop halts the whole app, not just this run: every running and queued job of
// every chatbot ends at its next chatbot or judge call.
async function stopRun(control) {
  control.stopped = true;
  await api("/api/jobs/cancel-all", { method: "POST", body: {} });
}

// Case details for Details and the report from a /api/runs/latest row's stored
// result (runs.result_json in the database): the only source for them.
function caseDetailsFromRun(run) {
  if (!run || !run.result) return null;
  return { ...caseDetailsOf(run.result), finishedAt: Date.parse(run.ts) };
}

// Opening the side panel, the dashboard or a report means the user has seen the
// outcome: clear the tab-title mark a finished run left.
clearBadge();

// What the dashboard's Details panel and the PDF report show for one metric's
// last run. Runs saved before these fields existed simply lack them.
function caseDetailsOf(result) {
  return {
    finishedAt: Date.now(),
    status: result.status,
    error: result.error || null,
    rows: result.rows || [],
    score: typeof result.score === "number" ? result.score : null,
    threshold: result.threshold,
    direction: result.direction,
    note: result.note || null,
    casesRun: result.cases_run ?? null,
    casesTotal: result.cases_total ?? null,
    casesSkipped: result.cases_skipped ?? null,
    judge: result.judge || null,
    durationS: result.duration_s ?? null,
    judgeSpread: result.judge_spread ?? null,
    judgeUnstable: Boolean(result.judge_unstable),
    stats: result.stats || null,
    latency: result.latency || null,
    judgeCostUsd: result.judge_cost_usd ?? null,
    byEnhancement: result.by_enhancement || null,
    calibrationExamples: result.calibration_examples || 0,
  };
}

// Failed cases first (they need attention), each group in run order.
function casesFailedFirst(rows) {
  return [...rows.filter((r) => !r.passed), ...rows.filter((r) => r.passed)];
}

// "Average 0.62 < 0.70 → fail": how a run's score met (or missed) its threshold.
function runVerdict(details) {
  if (typeof details.score !== "number") return "";
  const lower = details.direction === "lower";
  const passed = details.status === "pass";
  const rows = details.rows || [];
  const ok = rows.filter((r) => r.passed).length;
  // DeepEval's rule: the run passes only when every case passes; the average is shown alongside.
  const cases = rows.length ? `${ok} of ${rows.length} cases passed (each needs ${lower ? "≤" : "≥"} ${formatScore(details.threshold)})` : "";
  return [cases, `average ${formatScore(details.score)}`].filter(Boolean).join(" · ") + ` → ${passed ? "pass" : "fail"}`;
}

// Label / text pairs describing how a run was judged (missing fields left out).
function runFacts(details) {
  const facts = [];
  const rows = details.rows || [];
  if (rows.length) {
    const failed = rows.filter((r) => !r.passed).length;
    const skipped = details.casesSkipped ? ` · ${details.casesSkipped} skipped (too long)` : "";
    facts.push(["Cases", `${rows.length - failed} passed · ${failed} failed${skipped}`]);
  }
  if (details.judge && details.judge.model) facts.push(["Judge", details.judge.model]);
  if (details.judge && typeof details.judge.tokens === "number") {
    facts.push(["Judge tokens", `${formatTokens(details.judge.tokens)} (${details.judge.calls} calls)`]);
  }
  if (typeof details.durationS === "number") facts.push(["Duration", `${details.durationS.toFixed(1)} s`]);
  if (typeof details.judgeSpread === "number") {
    facts.push(["Judge consistency", `${details.judgeUnstable ? "⚠ unstable" : "steady"}: widest gap between two scorings ${formatScore(details.judgeSpread)}`]);
  }
  const stats = details.stats;
  if (stats && stats.n > 1) {
    facts.push(["Score spread", `median ${formatScore(stats.median)} · min ${formatScore(stats.min)} · max ${formatScore(stats.max)} · std ${formatScore(stats.std)}`]);
  }
  if (details.latency) {
    facts.push(["Chatbot latency", `p50 ${details.latency.p50.toFixed(2)} s · p95 ${details.latency.p95.toFixed(2)} s`]);
  }
  if (typeof details.judgeCostUsd === "number") facts.push(["Judge cost", `$${details.judgeCostUsd.toFixed(4)}`]);
  if (details.calibrationExamples) facts.push(["Calibration", `${details.calibrationExamples} human-scored examples given to the judge`]);
  if (details.byEnhancement) {
    const parts = Object.values(details.byEnhancement).map((b) => `${b.label} ${Math.round(b.pass_rate * 100)}%`);
    facts.push(["Resisted by attack", parts.join(" · ")]);
  }
  return facts;
}

// Label / text pairs explaining what a metric measures and how it is scored.
function metricMeaning(metric) {
  const pairs = [];
  if (metric.description) pairs.push(["Measures", metric.description]);
  if (metric.scale_hint) {
    const direction = metric.direction === "lower"
      ? "Lower is better: shown as 1 − DeepEval's score, and the threshold is a maximum."
      : "Higher is better: the threshold is a minimum.";
    pairs.push(["Scale", `${metric.scale_hint}. ${direction}`]);
  }
  if (metric.scores_on && metric.scores_on.length) {
    pairs.push(["The judge reads", metric.scores_on.map(fieldLabel).join(", ")]);
  }
  if (metric.criteria) pairs.push(["Rubric", metric.criteria]);
  if (metric.evaluation_steps && metric.evaluation_steps.length) {
    pairs.push(["Evaluation steps", metric.evaluation_steps.map((s, i) => `${i + 1}. ${s}`).join(" ")]);
  }
  pairs.push(["Run result", "As in DeepEval: each case passes when its score meets the threshold, and the run passes only when every case passes. The score shown is the average of the case scores."]);
  return pairs;
}

// How far a failed run is from passing, on the metric's own scale.
function gapText(direction, score, threshold) {
  if (typeof score !== "number" || typeof threshold !== "number") return "";
  const gap = Math.abs(score - threshold);
  return `${formatScore(score)}, needs ${comparator(direction)} ${formatScore(threshold)} (gap ${formatScore(gap)})`;
}

// The judge's reasons from the worst failed cases: what actually went wrong.
function worstReasons(rows, direction, count = 3) {
  const failed = rows.filter((r) => !r.passed && r.reason);
  failed.sort((a, b) => (direction === "lower" ? (b.score ?? 0) - (a.score ?? 0) : (a.score ?? 1) - (b.score ?? 1)));
  return failed.slice(0, count).map((r) => ({ question: r.question || r.input || "", reason: r.reason }));
}

// "How to improve" for a failed metric: the judge's reasons, then what to
// change in the chatbot and what to check in the test (metric_advice.py).
function improvementBlock(metric, details) {
  const advice = metric.improve || {};
  const reasons = details ? worstReasons(details.rows || [], details.direction || metric.direction) : [];
  const list = (items) => el("ul", {}, ...items.map((text) => el("li", {}, text)));
  return el(
    "div",
    { className: "improve" },
    reasons.length ? el("p", { className: "improve-label" }, "What the judge said") : null,
    reasons.length ? el("ul", {}, ...reasons.map((r) => el("li", {}, el("i", {}, `“${r.question}”: `), r.reason))) : null,
    advice.chatbot && advice.chatbot.length ? el("p", { className: "improve-label" }, "Fix the chatbot") : null,
    advice.chatbot && advice.chatbot.length ? list(advice.chatbot) : null,
    advice.tests && advice.tests.length ? el("p", { className: "improve-label" }, "Check the test") : null,
    advice.tests && advice.tests.length ? list(advice.tests) : null,
  );
}

// Word-level difference between the actual and expected answer. Each stretch
// of differing words between two shared words is one change: "missing" (only
// the expected answer has it), "added" (only the reply has it) or "changed"
// (both have different words there: the reply overrode the expected text).
// Each side comes back as spans with those words marked, plus the list of
// changes. Case and punctuation are ignored when comparing. Very long answers
// are left plain (the LCS table grows with the product of their lengths).
function wordDiff(actual, expected) {
  const split = (text) => text.split(/(\s+)/).filter(Boolean);
  const key = (token) => token.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const a = split(actual);
  const b = split(expected);
  const aWords = a.map((t, i) => [key(t), i]).filter(([k]) => k);
  const bWords = b.map((t, i) => [key(t), i]).filter(([k]) => k);
  if (aWords.length * bWords.length > 250_000) return null;
  const n = aWords.length;
  const m = bWords.length;
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = aWords[i][0] === bWords[j][0] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  // Walk the LCS: each run of unshared words between shared ones is one hunk.
  const hunks = [];
  let hunk = { a: [], b: [] };
  const close = () => {
    if (hunk.a.length || hunk.b.length) hunks.push(hunk);
    hunk = { a: [], b: [] };
  };
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && aWords[i][0] === bWords[j][0]) {
      close();
      i++;
      j++;
    } else if (j >= m || (i < n && lcs[i + 1][j] >= lcs[i][j + 1])) hunk.a.push(aWords[i++][1]);
    else hunk.b.push(bWords[j++][1]);
  }
  close();
  const markA = new Map();
  const markB = new Map();
  const phrase = (tokens, idx) => tokens.slice(idx[0], idx[idx.length - 1] + 1).join("");
  const changes = hunks.map((h) => {
    const kind = h.a.length && h.b.length ? "changed" : h.a.length ? "added" : "missing";
    h.a.forEach((k) => markA.set(k, kind));
    h.b.forEach((k) => markB.set(k, kind));
    return { kind, actual: h.a.length ? phrase(a, h.a) : "", expected: h.b.length ? phrase(b, h.b) : "" };
  });
  const render = (tokens, marks) => tokens.map((t, k) => (marks.has(k)
    ? el("b", { className: `diff ${marks.get(k)}`, title: DIFF_LABELS[marks.get(k)] }, t) : t));
  return { actual: render(a, markA), expected: render(b, markB), changes };
}

const DIFF_LABELS = {
  missing: "missing from the reply",
  changed: "the reply says something else here",
  added: "in the reply, not in the expected answer",
};

// The differences as a list: what the reply left out, overrode and added.
function diffList(changes, limit = 8) {
  const quote = (text) => `“${text.trim()}”`;
  const items = changes.slice(0, limit).map((c) => el(
    "li",
    { className: `diff-item ${c.kind}` },
    c.kind === "missing" ? [el("b", {}, "Missing: "), "the reply leaves out ", quote(c.expected)]
      : c.kind === "added" ? [el("b", {}, "Added: "), "the reply adds ", quote(c.actual)]
        : [el("b", {}, "Changed: "), "the reply says ", quote(c.actual), " where the expected answer says ", quote(c.expected)],
  ));
  if (changes.length > limit) items.push(el("li", { className: "muted" }, `… and ${changes.length - limit} more`));
  return el("ul", { className: "diff-list" }, ...items);
}

// What would make this failed case pass, the way DeepEval decides it: the
// pass rule and how far off the score is, what the metric scores, then fixes
// drawn from this case's own differences (only for the fields the metric
// reads), then the metric's general advice.
function howToPass(metric, row, direction, threshold, changes) {
  const reads = new Set(metric.scores_on || []);
  const steps = [];
  const gap = typeof row.score === "number" && typeof threshold === "number"
    ? Math.abs(row.score - threshold) : null;
  steps.push(el("li", {}, el("b", {}, "Pass rule: "),
    `DeepEval passes this case when the ${metric.title} score is ${comparator(direction)} ${formatScore(threshold)}`,
    gap !== null ? `; it scored ${formatScore(row.score)}, ${formatScore(gap)} ${direction === "lower" ? "too high" : "short"}.` : "."));
  if (metric.scale_hint) {
    const fields = metric.scores_on && metric.scores_on.length ? ` The judge reads: ${metric.scores_on.map(fieldLabel).join(", ").toLowerCase()}.` : "";
    steps.push(el("li", {}, el("b", {}, "What it scores: "), `${metric.scale_hint}.${fields}`));
  }
  const missing = changes.filter((c) => c.kind === "missing");
  const changed = changes.filter((c) => c.kind === "changed");
  const added = changes.filter((c) => c.kind === "added");
  const quote = (list) => list.slice(0, 4).map((c) => `“${c.trim()}”`).join(", ");
  if (reads.has("expected_output") && reads.has("actual_output")) {
    if (missing.length) steps.push(el("li", {}, el("b", {}, "Add to the reply: "), quote(missing.map((c) => c.expected))));
    if (changed.length) {
      steps.push(el("li", {}, el("b", {}, "Correct in the reply: "),
        changed.slice(0, 4).map((c) => `“${c.actual.trim()}” → “${c.expected.trim()}”`).join(", ")));
    }
    if (added.length && ["exact_match", "bleu", "rouge_l"].includes(metric.key)) {
      steps.push(el("li", {}, el("b", {}, "Remove from the reply: "), quote(added.map((c) => c.actual)),
        " (this metric counts words, so extra words lower the score)"));
    }
  } else if (reads.has("expected_output")) {
    if (missing.length || changed.length) {
      steps.push(el("li", {}, el("b", {}, "Retrieve text that covers: "),
        quote([...missing, ...changed].map((c) => c.expected)), " (the expected answer's facts must be in the retrieved context)"));
    }
  } else if (changes.length && reads.has("actual_output")
    && (reads.has("context") || reads.has("retrieval_context"))) {
    const unsupported = [...added, ...changed].map((c) => c.actual);
    if (unsupported.length) {
      steps.push(el("li", {}, el("b", {}, "Check these claims against the context: "), quote(unsupported),
        " (keep them only if the context backs them; they are not in the expected answer)"));
    }
  } else if (added.length && metric.key === "answer_relevancy") {
    steps.push(el("li", {}, el("b", {}, "Cut off-topic extras: "), quote(added.map((c) => c.actual)),
      " (every sentence that does not answer the question lowers the score)"));
  }
  const advice = metric.improve || {};
  steps.push(...(advice.chatbot || []).map((t) => el("li", {}, el("b", {}, "Fix the chatbot: "), t)));
  steps.push(...(advice.tests || []).map((t) => el("li", {}, el("b", {}, "Check the test: "), t)));
  return el("ul", { className: "how-to-pass" }, ...steps);
}

// A failed single-turn case's actual and expected results with their
// differences marked, the list of differences and how to pass, for every page
// that shows cases. null when there is nothing to compare: a passed case (its
// wording may differ freely), a conversation (its expected output describes the
// outcome), or either side empty.
function failedCaseView(metric, row, direction, threshold) {
  const actual = row.actual_output || "";
  const expected = row.expected_output || "";
  if (row.passed || !metric || metric.kind === "conversation") return null;
  const diff = actual && expected ? wordDiff(actual, expected) : null;
  const changes = diff ? diff.changes : [];
  const reference = (metric.scores_on || []).includes("expected_output") ? null
    : el("p", { className: "muted small" }, `${metric.title} does not compare the reply with the expected answer; the differences are for reference.`);
  return {
    actual: diff ? el("span", {}, ...diff.actual) : null,
    expected: diff ? el("span", {}, ...diff.expected) : null,
    differences: changes.length ? el("div", { className: "differences" }, diffList(changes), reference) : null,
    howToPass: howToPass(metric, row, direction ?? metric.direction, threshold ?? metric.threshold, changes),
  };
}

// The same marks for a whole failed test case (no single metric): its reply
// and expected answer with differing words marked, and the list. null when it
// passed or either side is empty.
function outputsDiff(actual, expected, passed) {
  if (passed || !actual || !expected) return null;
  const diff = wordDiff(actual, expected);
  if (!diff) return null;
  return {
    actual: el("span", {}, ...diff.actual),
    expected: el("span", {}, ...diff.expected),
    differences: diff.changes.length ? diffList(diff.changes) : null,
  };
}

// A case's judge scorings (consistency check): two are averaged; a third,
// taken when the two disagreed, breaks the tie and the median is kept.
function scoringsText(scores) {
  const list = scores.map(formatScore).join(", ");
  return scores.length === 3 ? `${list} (disagreed, so scored a third time: median kept)` : `${list} (averaged)`;
}

// One case as plain text, for pasting into a bug report.
function caseAsText(metric, row, direction, threshold) {
  const lines = [
    `${metric.title}: ${row.passed ? "pass" : "fail"} · ${formatScore(row.score)} ${comparator(direction)} ${formatScore(threshold)}`,
    `Question: ${row.input || row.question || ""}`,
    `Actual result: ${row.actual_output || ""}`,
  ];
  if (row.expected_output) lines.push(`Expected result: ${row.expected_output}`);
  if (row.context && row.context.length) lines.push(`Context:\n- ${row.context.join("\n- ")}`);
  lines.push(`Why: ${row.reason || "No reason given."}`);
  return lines.join("\n");
}

// Case details come from the database only (runs.result_json); drop the copy
// older versions kept in this browser.
try {
  localStorage.removeItem(STORAGE_PREFIX + "caseDetails");
} catch {
  // storage blocked: nothing to clean up
}
