// Helpers shared by the side panel and the dashboard. Everything that comes
// from a chatbot or the backend is inserted as text, never as HTML.

const ALL_METRICS = "__all__";
const DEFAULT_THEME = "generic";
// Thresholds per metric live in chrome.storage.local ("thresholds": {key: value});
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

// The pages run either as the Chrome extension or as a web app served by the
// backend (http://127.0.0.1:8765/). Outside the extension, chrome.storage,
// the service worker and the toolbar badge are replaced by localStorage,
// in-page notifications and the tab title.
const IN_EXTENSION = Boolean(globalThis.chrome && chrome.storage && chrome.storage.local);
const STORAGE_PREFIX = "llmJudge:";
const webListeners = [];

function parseStored(raw) {
  try {
    return raw === null ? undefined : JSON.parse(raw);
  } catch {
    return undefined;
  }
}

function webRead(key) {
  return parseStored(localStorage.getItem(STORAGE_PREFIX + key));
}

// Like chrome.storage.onChanged: every listener hears every change, the
// writing page's own included; other tabs and frames hear it via "storage".
function webWrite(key, value) {
  const oldValue = webRead(key);
  localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value));
  const changes = { [key]: { oldValue, newValue: value } };
  setTimeout(() => webListeners.forEach((callback) => callback(changes)), 0);
}

// The selected chatbot, the selected metric and the latest run are shared by
// the side panel and the dashboard.
const settings = {
  async get(key, fallback) {
    if (!IN_EXTENSION) return webRead(key) ?? fallback;
    const data = await chrome.storage.local.get(key);
    return data[key] ?? fallback;
  },
  async set(key, value) {
    if (!IN_EXTENSION) return webWrite(key, value);
    return chrome.storage.local.set({ [key]: value });
  },
  onChange(callback) {
    if (!IN_EXTENSION) {
      webListeners.push(callback);
      return;
    }
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === "local") callback(changes);
    });
  },
};

// A page of this app (path relative to the app root, e.g. "report/report.html").
function pageUrl(path) {
  if (IN_EXTENSION) return chrome.runtime.getURL(path);
  return new URL(`../${path}`, location.href).href;
}

function openDashboard() {
  if (IN_EXTENSION) {
    chrome.runtime.sendMessage({ type: "OPEN_DASHBOARD" });
  } else {
    window.open(pageUrl("dashboard/dashboard.html"), "llm-judge-dashboard");
  }
}

// The web app's stand-in for the toolbar badge: a mark in the tab title.
const BASE_TITLE = document.title;
function clearBadge() {
  if (IN_EXTENSION) chrome.runtime.sendMessage({ type: "CLEAR_BADGE" }).catch(() => {});
  else document.title = BASE_TITLE;
}

// Web app only: announces a finished job this browser started, once, even
// with the side panel and the dashboard both following it.
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

if (!IN_EXTENSION) {
  // Changes written by the other pane or another tab.
  window.addEventListener("storage", (event) => {
    if (!event.key || !event.key.startsWith(STORAGE_PREFIX)) return;
    const key = event.key.slice(STORAGE_PREFIX.length);
    const changes = { [key]: { oldValue: parseStored(event.oldValue), newValue: parseStored(event.newValue) } };
    webListeners.forEach((callback) => callback(changes));
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) clearBadge(); });
}

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
  const picked = Object.fromEntries(metricKeys
    .filter((key) => typeof thresholds[key] === "number")
    .map((key) => [key, thresholds[key]]));
  const job = await api("/api/jobs", {
    method: "POST",
    body: {
      target_id: target.id, metric_keys: metricKeys, thresholds: picked,
      limit: control.limit || undefined, check_consistency: checkConsistency,
    },
  });
  watchJob(target, job.job_id, control.metrics || []);
  // Lets the other page (side panel or dashboard) pick up and show this run.
  await settings.set("startedJob", { targetId: target.id, jobId: job.job_id, at: Date.now() });
  return followJob(target, job.job_id, onProgress, control);
}

function watchJob(target, jobId, metrics) {
  const info = { target: { id: target.id, name: target.name }, metrics: metrics.map((m) => ({ key: m.key, title: m.title })) };
  if (!IN_EXTENSION) {
    webWrite("watchedJobs", { ...(webRead("watchedJobs") || {}), [jobId]: info });
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
    return;
  }
  chrome.runtime.sendMessage({ type: "WATCH_JOB", jobId, ...info }).catch(() => {});
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
        await saveCaseDetails(target.id, result.key, result);
      }
    }
    if (job.current && !reported.has(job.current.metric_key)) {
      onProgress(job.current.metric_key, null, jobProgress(job.current));
    } else if (job.status === "queued" && job.metric_keys && job.metric_keys.length) {
      // Waiting behind another run: say so instead of looking hung.
      onProgress(job.metric_keys[0], null, {
        queued: true, metricIndex: 0, metricCount: job.metric_keys.length, startedAt: Date.now(),
      });
    }
    if (job.status !== "queued" && job.status !== "running") {
      if (IN_EXTENSION) chrome.runtime.sendMessage({ type: "JOB_ENDED", jobId }).catch(() => {});
      else announceWebJob(job).catch(() => {});
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

async function stopRun(control) {
  control.stopped = true;
  if (control.jobId) {
    await api(`/api/jobs/${control.jobId}/cancel`, { method: "POST", body: {} });
  }
}

// Case details for Details and the report from a /api/runs/latest row's stored result.
function caseDetailsFromRun(run) {
  if (!run || !run.result) return null;
  return { ...caseDetailsOf(run.result), finishedAt: Date.parse(run.ts) };
}

// Opening the side panel, the dashboard or a report means the user has seen the
// outcome: clear the toolbar badge a finished run left.
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
  const sign = lower ? (passed ? "≤" : ">") : (passed ? "≥" : "<");
  return `Average ${formatScore(details.score)} ${sign} ${formatScore(details.threshold)} → ${passed ? "pass" : "fail"}`;
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
  pairs.push(["Run score", "The average of the case scores; the run passes when that average meets the threshold."]);
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

async function saveCaseDetails(targetId, metricKey, result) {
  const all = await settings.get("caseDetails", {});
  const forTarget = all[targetId] || {};
  forTarget[metricKey] = caseDetailsOf(result);
  await settings.set("caseDetails", { ...all, [targetId]: forTarget });
}

async function loadCaseDetails(targetId, metricKey) {
  const all = await settings.get("caseDetails", {});
  return (all[targetId] && all[targetId][metricKey]) || null;
}

async function clearCaseDetails(targetId) {
  const all = await settings.get("caseDetails", {});
  if (!(targetId in all)) return;
  const { [targetId]: _dropped, ...rest } = all;
  await settings.set("caseDetails", rest);
}
