// Dashboard tab: chat with the selected chatbot, run the judge, and read the
// latest scores, the trend across runs, and the last run case by case.

const $ = (id) => document.getElementById(id);

// Card copy, group labels and directions come from /api/metrics (metrics_catalog.py).
const state = {
  targets: [],
  metrics: [],
  judgeUp: false,
  running: false,
  thresholds: {},
  runControl: null,
  trendView: "chart",
  trendChart: null,
  history: [],
  view: "judge",
  categoryFilter: "all",
  casesPerRun: null,
  chatbotHealth: "unknown",
  usage: null,
  judgeSettings: null,
  usageTimer: null,
  // Metric keys ticked under "Metrics to run" in the side panel; null = all.
  checked: null,
  runningKeys: new Set(),
  expandedDetailKeys: new Set(),
  caseDetailsCache: {},
  // Details panels showing failed cases only, and each metric's recent run scores.
  failedOnlyKeys: new Set(),
  detailHistory: {},
};

function currentTarget() {
  const id = Number($("target-select").value);
  return state.targets.find((t) => t.id === id) || null;
}

// Category key -> label, from the backend, in the order the metrics arrive.
function categoryLabels() {
  const labels = {};
  for (const m of state.metrics) labels[m.ui_category] ??= m.ui_category_label || m.ui_category;
  return labels;
}

function isTicked(metric) {
  return !state.checked || state.checked.has(metric.key);
}

// The cards the totals and Run all visible work on: this category, ticked in the side panel.
function countedMetrics() {
  return visibleMetrics().filter(isTicked);
}

function visibleMetrics() {
  return state.metrics.filter(
    (m) => state.categoryFilter === "all" || m.ui_category === state.categoryFilter,
  );
}

// Cases available depend on the target's golden set and role, so the catalog
// is fetched per target.
async function loadMetrics() {
  const target = currentTarget();
  state.metrics = await api(target ? `/api/metrics?target_id=${target.id}` : "/api/metrics");
}

function setStatus(text, isError = false) {
  $("status").textContent = text || "";
  $("status").className = isError ? "status-error" : "secondary";
}

function updateRunButton() {
  const button = $("run-all-button");
  button.disabled = !state.judgeUp || !currentTarget() || state.running;
  button.title = state.judgeUp
    ? "Run every card in the selected category, one after another"
    : "Judge not configured: add its API key in the side panel's Judge settings";
}

async function refreshJudge() {
  try {
    const status = await api("/api/status");
    state.judgeUp = status.judge.up;
    state.judgeSettings = await api("/api/judge/settings");
    if (!state.judgeUp) {
      setStatus("Judge not configured: add its API key in the side panel (Judge settings).", true);
    }
  } catch (error) {
    state.judgeUp = false;
    setStatus(error.message, true);
  }
  updateRunButton();
  renderTiles();
}

function appendBubble(kind, text, meta, judgeTarget) {
  const log = $("chat-log");
  let bubble;
  log.append(
    (bubble = el(
      "div",
      { className: `bubble ${kind}` },
      text,
      meta ? el("span", { className: "meta" }, meta) : null,
      judgeTarget
        ? el(
            "button",
            {
              type: "button",
              className: "judge-answer",
              onclick: (event) => {
                event.target.remove();
                judgeAnswer(judgeTarget.question, judgeTarget.actual_output, bubble);
              },
            },
            "Judge this answer",
          )
        : null,
    )),
  );
  log.scrollTop = log.scrollHeight;
  return bubble;
}

// One badge per metric in a judge result card: icon + score, reason behind a toggle.
function judgeBadge(title, result, error) {
  if (error) {
    return el("li", { className: "judge-badge status-error" }, `! ${title}: ${error}`);
  }
  if (result.status === "error") {
    return el("li", { className: "judge-badge status-error" }, `! ${title}: ${result.error}`);
  }
  const icon = result.status === "pass" ? "✓" : "✕";
  const summary = el(
    "button",
    { type: "button", className: `judge-badge-toggle status-${result.status}` },
    `${icon} ${title} ${formatScore(result.score)} ${comparator(result.direction)} ${formatScore(result.threshold)}`,
  );
  const reasonEl = el("p", { className: "judge-badge-reason hidden" }, result.reason || "No reason given.");
  summary.addEventListener("click", () => reasonEl.classList.toggle("hidden"));
  return el("li", { className: "judge-badge" }, summary, reasonEl);
}

// Ad-hoc judging: score the answer on screen, using the selected metric(s).
// Nothing is recorded, so the trend charts stay a record of golden-set runs.
// Results render as one card attached under the judged bubble, not separate
// chat bubbles, so the log doesn't fill up with score noise.
async function judgeAnswer(question, actualOutput, bubble) {
  const target = currentTarget();
  if (!target) return;
  const choice = $("metric-select").value;
  // Security metrics judge replies to their own red-team probes, not a chat answer.
  const keys = metricsFor(choice, state.metrics).filter((m) => m.ui_category !== "security").map((m) => m.key);
  if (!keys.length) {
    setStatus("Security metrics score their red-team probes: run them from the cards.", true);
    return;
  }
  const titles = new Map(state.metrics.map((m) => [m.key, m.title]));
  const list = el("ul", { className: "judge-badges" });
  bubble.append(list);
  setStatus(`Judging this answer with ${keys.length} metric${keys.length > 1 ? "s" : ""}…`);
  for (const key of keys) {
    const title = titles.get(key) || key;
    let result;
    let error;
    try {
      result = await api("/api/judge", {
        method: "POST",
        body: {
          metric_key: key,
          question,
          actual_output: actualOutput,
          theme: themeOf(target),
          threshold: thresholdFor(state.metrics.find((m) => m.key === key) || { threshold: undefined }, state.thresholds),
        },
      });
    } catch (e) {
      error = e.message;
    }
    list.append(judgeBadge(title, result, error));
  }
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
  setStatus("Judged that answer. These scores are not saved to the trend.");
}

async function loadTargets(preferredId) {
  state.targets = await fetchTargets();
  const wanted = preferredId ?? (await settings.get("selectedTargetId", null));
  const fallback = state.targets.length ? state.targets[0].id : null;
  const selected = state.targets.some((t) => t.id === wanted) ? wanted : fallback;
  fillSelect(
    $("target-select"),
    state.targets.map((t) => ({ value: t.id, label: targetLabel(t) })),
    selected,
  );
  await onTargetChanged();
}

async function onTargetChanged() {
  const target = currentTarget();
  await settings.set("selectedTargetId", target ? target.id : null);
  state.chatbotHealth = "unknown";
  await loadMetrics();
  renderCategoryChips();
  renderTiles();
  $("chat-log").replaceChildren();
  // A real chatbot keeps its context per conversation; a new target starts fresh.
  state.history = [];
  if (target) appendBubble("note", `Chatting with ${target.name}. Golden set: ${themeOf(target)}.`);
  // Cached case details belong to the target that was selected before.
  state.expandedDetailKeys.clear();
  state.caseDetailsCache = {};
  updateRunButton();
  renderCases(await settings.get("lastRun", null));
  await Promise.all([renderLatest(), renderTrend()]);
  if (state.view === "history") await renderHistory();
  await followActiveJob(target);
}

// A run started elsewhere (from here before a reload, or the side panel) is still going: follow it.
async function followActiveJob(target) {
  const job = target ? await activeJob(target) : null;
  if (job && !state.running) {
    followRun(target, job.metric_keys, (onProgress, control) => followJob(target, job.id, onProgress, control));
  }
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url || "";
  }
}

function setDot(id, level) {
  const cls = { good: "dot-good", warn: "dot-warn", bad: "dot-bad" }[level] || "dot-unknown";
  $(id).className = `dot ${cls}`;
}

function renderTiles() {
  const target = currentTarget();
  setDot("tile-chatbot-dot", target ? state.chatbotHealth : "off");
  $("tile-chatbot-text").textContent = target ? target.config.url || target.name : "no chatbot selected";
  const ownContext = Boolean(target && target.config.context_path);
  setDot("tile-rag-dot", target ? (ownContext ? "good" : "warn") : "off");
  $("tile-rag-text").textContent = !target
    ? "–"
    : ownContext
      ? `own retrieval context (${target.config.context_path})`
      : "golden context fallback";
  const judge = state.judgeSettings;
  setDot("tile-judge-dot", state.judgeUp ? "good" : "bad");
  $("tile-judge-text").textContent = judge ? `${hostOf(judge.base_url)} · ${judge.model}` : "–";
  $("judge-model").value = judge ? judge.model : "";
  const u = state.usage;
  $("tile-tokens-total").textContent = u ? `${u.total_tokens} total · ${u.calls} calls` : "–";
  $("tile-tokens-split").textContent = u ? `target ${u.target_calls} · judge ${u.judge_calls}` : "";
}

// A score on a common "higher is better" scale: "lower" metrics (hallucination,
// bias, toxicity, PII) are violation rates, so they count as 1 − score.
function normalizedScore(metric, score) {
  if (typeof score !== "number") return null;
  return metric.direction === "lower" ? 1 - score : score;
}

function averageScore(metrics, byKey) {
  const scores = metrics
    .map((m) => normalizedScore(m, byKey.get(m.key)?.score))
    .filter((s) => s !== null);
  return scores.length ? { value: scores.reduce((a, b) => a + b, 0) / scores.length, count: scores.length } : null;
}

// pass · fail · pending and the average score across the cards the chip filter shows.
function renderCounts(rows) {
  const byKey = new Map(rows.map((r) => [r.metric_key, r]));
  const shown = countedMetrics();
  // SQLite hands `passed` back as 1 / 0, so test truthiness, not `=== true`.
  const pass = shown.filter((m) => byKey.has(m.key) && Boolean(byKey.get(m.key).passed)).length;
  const fail = shown.filter((m) => byKey.has(m.key) && !byKey.get(m.key).passed).length;
  $("status-pass").textContent = pass;
  $("status-fail").textContent = fail;
  $("status-pending").textContent = shown.length - pass - fail;
  const avg = averageScore(shown, byKey);
  $("tile-average").textContent = avg ? formatScore(avg.value) : "–";
  $("tile-average-note").textContent = avg
    ? `${avg.count} of ${shown.length} ticked cards run · lower-is-better as 1 − score`
    : "no runs yet for the ticked cards";
  $("tile-counts-label").textContent = `pass · fail · pending · ${shown.length} ticked`;
}

async function refreshUsage() {
  try {
    state.usage = await api("/api/usage");
  } catch {
    state.usage = null;
  }
  renderTiles();
}

// Refresh status: re-check the judge and send the chatbot one test question.
async function refreshStatus() {
  await refreshJudge();
  const target = currentTarget();
  if (target) {
    const { headers: _masked, ...config } = target.config;
    try {
      const result = await api("/api/targets/test", {
        method: "POST",
        body: { type: target.type, config, message: "Hello", target_id: target.id },
      });
      state.chatbotHealth = result.ok ? "good" : "bad";
      setStatus(result.ok ? "Chatbot and judge checked." : `Chatbot: ${result.error}`, !result.ok);
    } catch (error) {
      state.chatbotHealth = "bad";
      setStatus(error.message, true);
    }
  }
  await refreshUsage();
}

// One row per golden case: the question, the chatbot's actual answer, and
// the judge's score/reason for it — the "what was it scored on" the Details
// toggle exists to answer.
function caseDetailPanel(metric, details) {
  if (!details) {
    return el("p", { className: "case-detail-empty muted" }, "Run this metric to see the question and answer behind each score.");
  }
  if (details.status === "error") {
    return el("p", { className: "case-detail-empty status-error" }, details.error || "This run failed.");
  }
  if (!details.rows.length) {
    return el("p", { className: "case-detail-empty muted" }, "No cases in the last run.");
  }
  const direction = details.direction || metric.direction;
  const threshold = typeof details.threshold === "number" ? details.threshold : thresholdFor(metric, state.thresholds);
  const failedOnly = state.failedOnlyKeys.has(metric.key);
  const failedCount = details.rows.filter((r) => !r.passed).length;
  const rows = failedOnly ? details.rows.filter((r) => !r.passed) : casesFailedFirst(details.rows);
  return el(
    "div",
    { className: "case-detail" },
    el("p", { className: "muted small" }, `As of ${formatRunTime(details.finishedAt)} · each case scored ${comparator(direction)} ${formatScore(threshold)}`),
    runSummary(details),
    recentRuns(metric),
    details.status === "fail"
      ? el(
        "details",
        { className: "metric-improve", open: true },
        el("summary", {}, "How to improve"),
        improvementBlock(metric, details),
      )
      : null,
    el(
      "details",
      { className: "metric-meaning" },
      el("summary", {}, "What this measures"),
      factList(metricMeaning(metric)),
    ),
    details.note ? el("p", { className: "case-note" }, `⚠ ${details.note}`) : null,
    el(
      "div",
      { className: "segmented case-filter", role: "group", "aria-label": "Cases to show" },
      el("button", {
        type: "button", "aria-pressed": String(!failedOnly),
        onclick: () => { state.failedOnlyKeys.delete(metric.key); renderMetricGrid(); },
      }, `All (${details.rows.length})`),
      el("button", {
        type: "button", "aria-pressed": String(failedOnly),
        onclick: () => { state.failedOnlyKeys.add(metric.key); renderMetricGrid(); },
      }, `Failed only (${failedCount})`),
    ),
    rows.length
      ? el(
        "ul",
        { className: "case-list" },
        ...rows.map((row) => el(
          "li",
          { className: `case-row${row.passed ? "" : " case-failed"}` },
          el(
            "div",
            { className: "case-row-head" },
            el(
              "p",
              { className: `case-score status-${row.passed ? "pass" : "fail"}` },
              `${row.passed ? "✓ pass" : "✕ fail"} · ${formatScore(row.score)} ${comparator(direction)} ${formatScore(threshold)}`,
            ),
            copyCaseButton(metric, row, direction, threshold),
          ),
          el("dl", { className: "case-fields" }, ...caseFields(metric, row)),
        )),
      )
      : el("p", { className: "case-detail-empty muted" }, "No failed cases in the last run."),
  );
}

// Verdict and how the run was judged, above the cases.
function runSummary(details) {
  const verdict = runVerdict(details);
  const facts = runFacts(details);
  if (!verdict && !facts.length) return null;
  return el(
    "div",
    { className: "run-summary" },
    verdict ? el("p", { className: `run-verdict status-${details.status}` }, verdict) : null,
    facts.length ? factList(facts) : null,
  );
}

function factList(pairs) {
  return el("dl", { className: "case-fields fact-list" }, ...pairs.flatMap(([label, value]) => [
    el("dt", {}, label),
    el("dd", {}, value),
  ]));
}

// The metric's last few run scores, oldest first, so one dip reads as a dip.
function recentRuns(metric) {
  const runs = (state.detailHistory[metric.key] || []).slice(-5);
  if (runs.length < 2) return null;
  return el(
    "p",
    { className: "recent-runs small" },
    el("span", { className: "muted" }, `Last ${runs.length} runs: `),
    ...runs.flatMap((run, i) => [
      i ? el("span", { className: "muted" }, " → ") : null,
      el("span", { className: `status-${run.passed ? "pass" : "fail"}`, title: formatRunTime(run.ts) }, formatScore(run.score)),
    ]),
  );
}

function copyCaseButton(metric, row, direction, threshold) {
  const button = el("button", { type: "button", className: "small-button", title: "Copy this case as text" }, "Copy");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(caseAsText(metric, row, direction, threshold));
      button.textContent = "Copied";
    } catch {
      button.textContent = "Copy failed";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1500);
  });
  return button;
}

async function loadDetailHistory(key) {
  const target = currentTarget();
  if (!target) return;
  try {
    state.detailHistory[key] = await api(
      `/api/history?target_id=${target.id}&metric_key=${encodeURIComponent(key)}&since_id=${await sessionSince()}`,
    );
  } catch {
    state.detailHistory[key] = [];  // the panel works without it
  }
}

// Label / value pairs for one case: what was asked, what came back, what was
// expected, the context the judge read, and the judge's reason.
function caseFields(metric, row) {
  const pairs = [];
  const add = (label, value) => {
    if (value === null || value === undefined || value === "") return;
    pairs.push(el("dt", {}, label), el("dd", {}, value));
  };
  if (metric && metric.kind === "conversation") {
    add("Scenario", row.question);
    add("User turns", row.input);
    add("Conversation (actual result)", row.actual_output);
    add("Expected outcome", row.expected_output);
  } else {
    add("Question", row.input && row.input !== row.question ? row.input : row.question);
    if (row.scores && row.scores.length === 2) {
      add("Two scorings", `${formatScore(row.scores[0])} and ${formatScore(row.scores[1])} (averaged)`);
    }
    if (row.enhancement) add("Attack sent as", row.enhancement.replace(/_/g, " "));
    add("Actual result", row.actual_output);
    add("Expected result", row.expected_output);
    if (row.context && row.context.length) {
      add(contextLabel(metric, row), el("ul", {}, ...row.context.map((line) => el("li", {}, line))));
    }
  }
  if (typeof row.latency_s === "number") add("Chatbot latency", `${row.latency_s.toFixed(2)} s`);
  add("Why", row.reason || "No reason given.");
  return pairs;
}

function contextLabel(metric, row) {
  if (row.context_source === "chatbot") return "Retrieved context (from the chatbot)";
  const retrieval = metric && (metric.scores_on || []).includes("retrieval_context");
  return retrieval ? "Retrieved context (golden reference)" : "Context (golden reference)";
}

// Keeps an already-open detail panel in sync with a metric that just
// finished running, instead of showing scores from before the run.
function refreshExpandedDetail(key, result) {
  if (!state.expandedDetailKeys.has(key)) return;
  state.caseDetailsCache[key] = caseDetailsOf(result);
  loadDetailHistory(key).then(renderMetricGrid);
}

async function toggleCardDetails(key) {
  if (state.expandedDetailKeys.has(key)) {
    state.expandedDetailKeys.delete(key);
    renderMetricGrid();
    return;
  }
  state.expandedDetailKeys.add(key);
  const target = currentTarget();
  if (target) {
    // The cases come from the database (the run's stored result).
    const run = latestRowsCache.find((r) => r.metric_key === key);
    await loadDetailHistory(key);
    state.caseDetailsCache[key] = caseDetailsFromRun(run);
  }
  renderMetricGrid();
}

function metricCard(metric, row) {
  const isRunning = state.runningKeys.has(metric.key);
  const hasRun = Boolean(row);
  const statusClass = isRunning ? "running" : !hasRun ? "not-run" : row.passed ? "pass" : "fail";
  const statusLabel = isRunning ? "RUNNING" : !hasRun ? "NOT RUN" : row.passed ? "PASS" : "FAIL";
  const threshold = thresholdFor(metric, state.thresholds);
  const score = hasRun ? row.score : null;
  const cases = metric.cases_available ?? 0;
  const barWidth = typeof score === "number" ? Math.round(Math.max(0, Math.min(1, score)) * 100) : 0;
  const bound = `${comparator(metric.direction)} ${formatScore(threshold)}`;
  return el(
    "div",
    { className: `metric-card status-${statusClass}${isTicked(metric) ? "" : " unticked"}` },
    el(
      "div",
      { className: "metric-card-head" },
      el("span", { className: `badge badge-${metric.ui_category}` }, metric.ui_category_label || metric.ui_category),
      el("span", { className: "badge badge-target" }, "chatbot"),
      isTicked(metric) ? null : el("span", { className: "badge badge-unticked", title: "Not ticked under Metrics to run in the side panel: left out of the totals and Run all visible" }, "not ticked"),
      el("span", { className: "card-threshold" }, bound),
    ),
    el("h3", {}, metric.title),
    el("p", { className: "metric-card-desc" }, metric.description || ""),
    el(
      "div",
      { className: "card-inset" },
      el(
        "div",
        { className: "score-row" },
        el("span", { className: `status-pill status-${statusClass}` }, statusLabel),
        el("span", { className: "score-cell" }, el("b", {}, hasRun ? formatScore(score) : "–"), el("small", {}, "SCORE")),
        el("span", { className: "score-cell" }, el("b", {}, bound), el("small", {}, "THRESHOLD")),
      ),
      isRunning
        ? el("div", { className: "run-bar running" }, el("span", { className: "run-bar-fill" }))
        : el(
            "div",
            { className: "score-bar" },
            el("span", { className: "score-bar-fill", style: `width:${barWidth}%` }),
            el("span", { className: "score-bar-tick", style: `left:${Math.round(threshold * 100)}%` }),
          ),
      el("p", { className: "scale-hint" }, metric.scale_hint || ""),
      el("p", { className: "card-question" }, metric.question || ""),
      el(
        "p",
        { className: "card-meta" },
        cases
          ? `${cases} case${cases === 1 ? "" : "s"} available · ${metric.dataset}${metric.probe_set ? ` (${metric.probe_set})` : ""}`
          : metric.unavailable || "no cases for this chatbot's golden set",
      ),
      el("p", { className: "card-meta" }, hasRun ? formatRunTime(row.ts) : "–"),
    ),
    el(
      "div",
      { className: "metric-card-actions" },
      el(
        "button",
        {
          type: "button",
          className: "run-metric primary",
          disabled: !state.judgeUp || !currentTarget() || state.running || cases === 0,
          onclick: () => runKeys([metric.key]),
        },
        isRunning ? "▶ Running…" : "▶ Run",
      ),
      el(
        "button",
        { type: "button", onclick: () => toggleCardDetails(metric.key) },
        state.expandedDetailKeys.has(metric.key) ? "Hide details" : "Details",
      ),
    ),
    state.expandedDetailKeys.has(metric.key) ? caseDetailPanel(metric, state.caseDetailsCache[metric.key]) : null,
  );
}

function renderCategoryChips() {
  const labels = categoryLabels();
  const items = ["all", ...Object.keys(labels)];
  $("category-chips").replaceChildren(
    ...items.map((cat) =>
      el(
        "button",
        {
          type: "button",
          className: "chip-toggle",
          "aria-pressed": String(cat === state.categoryFilter),
          onclick: () => {
            state.categoryFilter = cat;
            settings.set("dashCategory", cat);
            renderCategoryChips();
            renderMetricGrid();
            renderCounts(latestRowsCache);
          },
        },
        cat === "all" ? "All" : labels[cat],
      ),
    ),
  );
}

// One run at a time; the Stop button reaches it through state.runControl.
function beginRun() {
  state.running = true;
  state.runControl = {};
  $("stop-button").disabled = false;
  $("stop-button").textContent = "■ Stop";
  $("stop-button").classList.remove("hidden");
  $("run-all-button").classList.add("hidden");
  updateRunButton();
}

function endRun() {
  state.running = false;
  state.runControl = null;
  $("stop-button").classList.add("hidden");
  $("run-all-button").classList.remove("hidden");
}

$("stop-button").addEventListener("click", async () => {
  if (!state.runControl) return;
  $("stop-button").disabled = true;
  $("stop-button").textContent = "Stopping…";
  setStatus("Stopping after the current step…");
  try {
    await stopRun(state.runControl);
  } catch (error) {
    setStatus(error.message, true);
  }
});

function startUsagePolling() {
  stopUsagePolling();
  state.usageTimer = setInterval(refreshUsage, 2000);
}

function stopUsagePolling() {
  if (state.usageTimer) clearInterval(state.usageTimer);
  state.usageTimer = null;
}

// Runs the given metrics one after another; Stop (header) cancels the current
// run and skips the rest. Used by each card's Run and by Run all visible.
async function runKeys(keys) {
  const target = currentTarget();
  if (!target || !keys.length) return;
  await followRun(target, keys, (onProgress, control) => runMetrics(target, keys, onProgress, control));
}

// Shows a run on the cards and the status line until it ends: a new job, or one
// already running when the dashboard opened. start(onProgress, control) runs or
// follows the job.
async function followRun(target, keys, start) {
  const byMetric = new Map(state.metrics.map((m) => [m.key, m]));
  beginRun();
  state.runControl.limit = state.casesPerRun;
  state.runControl.metrics = state.metrics;
  state.runControl.onOffline = (off) => { if (off) setStatus("Backend not reachable, retrying…", true); };
  startUsagePolling();
  try {
    const results = await start((key, result, progress) => {
      const title = byMetric.get(key)?.title || key;
      if (result === null) {
        const fresh = !state.runningKeys.has(key);
        state.runningKeys.add(key);
        setStatus(`Running ${title} · ${progressText(progress)}`);
        if (!fresh) return;  // progress ticks only update the status line
      } else {
        state.runningKeys.delete(key);
        if (result.status === "cancelled") {
          setStatus(`${title}: ${result.error} (not saved)`);
        } else {
          const unreachable = result.status === "error"
            && /HTTP|Connection|Timeout|Could not reach|did not answer|service message|empty reply|is busy/i.test(result.error || "");
          state.chatbotHealth = unreachable ? "bad" : result.status === "error" ? state.chatbotHealth : "good";
          refreshExpandedDetail(key, result);
          if (result.status === "error") setStatus(`${title}: ${result.error}`, true);
          else setStatus(`${title}: ${formatScore(result.score)} ${result.status} (${[`${result.cases_run} of ${result.cases_total} cases`, judgingSummary(result)].filter(Boolean).join(" · ")})`);
        }
        renderLatest();
      }
      renderMetricGrid();
    }, state.runControl);
    const control = state.runControl;
    if (control.jobStatus === "error" || control.jobStatus === "interrupted") {
      setStatus(`Run ${control.jobStatus}: ${control.jobError || ""}`, true);
    }
    const failed = results.filter((r) => r.status === "error").length;
    // Stop pressed between two metrics cancels nothing in flight; the loop just ends early.
    const stopped = results.some((r) => r.status === "cancelled") || results.length < keys.length;
    if (stopped && results.length < keys.length) {
      setStatus(`Stopped after ${results.filter((r) => r.status !== "cancelled").length} of ${keys.length} metrics.`);
    }
    if (keys.length > 1 && !stopped) {
      // The batch's own average, on the same higher-is-better scale as the tile.
      const scored = results
        .map((r) => normalizedScore(byMetric.get(r.key) || {}, r.score))
        .filter((s) => s !== null);
      const avg = scored.length ? ` · average score ${formatScore(scored.reduce((a, b) => a + b, 0) / scored.length)}` : "";
      setStatus(`Finished ${keys.length} metrics${avg}${failed ? ` · ${failed} could not run` : ""}.`, failed > 0);
    }
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    stopUsagePolling();
    endRun();
    for (const key of keys) state.runningKeys.delete(key);
    updateRunButton();
    renderMetricGrid();
    await Promise.all([renderLatest(), renderTrend(), refreshUsage()]);
  }
}

// Metric card grid: the dashboard's main "what's judged, what isn't" view.
// Replaces the old KPI tiles + single latest-scores chart with one card per
// metric, filterable by category, each runnable on its own.
let latestRowsCache = [];

function renderMetricGrid() {
  const shown = visibleMetrics();
  const byKey = new Map(latestRowsCache.map((r) => [r.metric_key, r]));
  $("metric-grid-empty").classList.toggle("hidden", state.metrics.length > 0);
  $("metric-grid").replaceChildren(...shown.map((m) => metricCard(m, byKey.get(m.key))));
}

async function renderLatest() {
  const target = currentTarget();
  if (!target) return;
  try {
    latestRowsCache = await api(`/api/runs/latest?target_id=${target.id}&since_id=${await sessionSince()}`);
    renderCounts(latestRowsCache);
    renderMetricGrid();
  } catch (error) {
    setStatus(error.message, true);
  }
}

// Matches runner.UNSTABLE_SPREAD: two scorings further apart flag the run.
const UNSTABLE_SPREAD = 0.15;

// Judge model and tokens for the runs made at one time (older runs have neither).
function judgingCells(runs) {
  const models = [...new Set(runs.map((r) => r.judge_model).filter(Boolean))];
  const tokens = runs.map((r) => r.judge_tokens).filter((t) => typeof t === "number");
  return [
    el("td", {}, models.join(", ") || "–"),
    el("td", { className: "num" }, tokens.length ? formatTokens(tokens.reduce((a, b) => a + b, 0)) : "–"),
  ];
}

function renderTrendTable(historyByMetric, metrics) {
  const shown = metrics.filter((m) => historyByMetric[m.key].length > 0);
  const times = [...new Set(shown.flatMap((m) => historyByMetric[m.key].map((r) => r.ts)))]
    .sort()
    .reverse();
  const table = $("trend-table");
  table.tHead.replaceChildren(
    el(
      "tr",
      {},
      el("th", {}, "Run time"),
      ...shown.map((m) => el("th", { className: "num" }, m.title)),
      el("th", {}, "Judge model"),
      el("th", { className: "num" }, "Tokens"),
    ),
  );
  table.tBodies[0].replaceChildren(
    ...times.map((ts) =>
      el(
        "tr",
        {},
        el("td", {}, formatRunTime(ts)),
        ...shown.map((m) => {
          const run = historyByMetric[m.key].find((r) => r.ts === ts);
          if (!run) return el("td", { className: "num" }, "–");
          const unstable = typeof run.judge_spread === "number" && run.judge_spread > UNSTABLE_SPREAD;
          const title = [
            run.cases_run ? `${run.cases_run} cases` : "",
            run.cases_skipped ? `${run.cases_skipped} skipped (too long for the chatbot)` : "",
            unstable ? `judge unstable: two scorings differed by ${formatScore(run.judge_spread)}` : "",
          ].filter(Boolean).join(" · ");
          return el(
            "td",
            { className: "num", title: title || undefined },
            `${formatScore(run.score)} ${run.passed ? "✓" : "✕"}${unstable ? " ⚠" : ""}`,
          );
        }),
        ...judgingCells(shown.map((m) => historyByMetric[m.key].find((r) => r.ts === ts)).filter(Boolean)),
      ),
    ),
  );
}

async function renderTrend() {
  const target = currentTarget();
  if (!target || state.metrics.length === 0) return;
  const choice = $("metric-select").value;
  const metrics = metricsFor(choice, state.metrics);
  $("trend-heading").textContent = `Score trend across runs · ${choiceLabel(choice, state.metrics)}`;
  const box = $("trend-chart-box");
  box.classList.add("loading");
  try {
    const since = await sessionSince();
    const histories = await Promise.all(
      metrics.map((m) =>
        api(`/api/history?target_id=${target.id}&metric_key=${encodeURIComponent(m.key)}&since_id=${since}`),
      ),
    );
    const historyByMetric = Object.fromEntries(metrics.map((m, i) => [m.key, histories[i]]));
    const hasRuns = histories.some((h) => h.length > 0);
    const showTable = state.trendView === "table";
    $("trend-empty").classList.toggle("hidden", hasRuns);
    $("trend-table").classList.toggle("hidden", !hasRuns || !showTable);
    box.classList.toggle("hidden", !hasRuns || showTable);
    renderTrendTable(historyByMetric, metrics);
    if (hasRuns && !showTable) {
      state.trendChart = drawChart(
        $("trend-chart"),
        state.trendChart,
        trendChartConfig(historyByMetric, withUserThresholds(state.metrics, state.thresholds)),
      );
    }
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    box.classList.remove("loading");
  }
}

// Full run log for the current chatbot: every recorded run, all metrics,
// newest first. "Latest score per metric" only ever shows the most recent
// point per metric, so this is the only place the full record lives.
async function renderHistory() {
  const target = currentTarget();
  if (!target || state.metrics.length === 0) return;
  const choice = $("metric-select").value;
  const metrics = metricsFor(choice, state.metrics);
  const byKey = new Map(state.metrics.map((m) => [m.key, m]));
  const table = $("history-table");
  const empty = $("history-empty");
  table.classList.add("hidden");
  try {
    const histories = await Promise.all(
      metrics.map((m) => api(`/api/history?target_id=${target.id}&metric_key=${encodeURIComponent(m.key)}`)),
    );
    const rows = histories.flat().sort((a, b) => (a.ts < b.ts ? 1 : -1));
    $("history-count").textContent = rows.length ? `(${rows.length} run${rows.length > 1 ? "s" : ""})` : "";
    empty.classList.toggle("hidden", rows.length > 0);
    table.classList.toggle("hidden", rows.length === 0);
    table.tBodies[0].replaceChildren(
      ...rows.map((r) =>
        el(
          "tr",
          {},
          el("td", {}, formatRunTime(r.ts)),
          el("td", {}, byKey.get(r.metric_key)?.title ?? r.metric_key),
          el("td", { className: "num" }, formatScore(r.score)),
          el("td", { className: "num" }, byKey.get(r.metric_key)
            ? `${comparator(byKey.get(r.metric_key).direction)} ${formatScore(thresholdFor(byKey.get(r.metric_key), state.thresholds))}`
            : "–"),
          el("td", { className: r.passed ? "status-pass" : "status-fail" }, resultLabel(r.passed)),
          el("td", {}, confirmButton("Delete", "Delete this run from the history", () => deleteRuns(`/api/runs/${r.id}`))),
        ),
      ),
    );
  } catch (error) {
    setStatus(error.message, true);
  }
}

// Deletes stored runs (one, or all of this chatbot's), then tells every open
// view to reload its scores.
async function deleteRuns(path) {
  try {
    await api(path, { method: "DELETE" });
    await settings.set("runsChangedAt", Date.now());
  } catch (error) {
    setStatus(error.message, true);
  }
}

armConfirm($("history-clear"), async () => {
  const target = currentTarget();
  if (target) await deleteRuns(`/api/targets/${target.id}/runs`);
});

function setView(view) {
  state.view = view;
  $("judge-view").classList.toggle("hidden", view !== "judge");
  $("history-view").classList.toggle("hidden", view !== "history");
  for (const button of document.querySelectorAll("[data-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  if (view === "history") renderHistory().catch((error) => setStatus(error.message, true));
}

function renderCases(lastRun) {
  const target = currentTarget();
  const table = $("cases-table");
  const empty = $("cases-empty");
  const result = lastRun && target && lastRun.targetId === target.id ? lastRun.result : null;
  const metric = result ? state.metrics.find((m) => m.key === lastRun.metricKey) : null;
  $("cases-heading").textContent = result
    ? `Last run, case by case · ${metric ? metric.title : lastRun.metricKey}`
    : "Last run, case by case";
  const note = $("cases-note");
  note.classList.toggle("hidden", !(result && result.note));
  note.textContent = result && result.note ? `⚠ ${result.note}` : "";
  if (!result || result.status === "error") {
    table.classList.add("hidden");
    empty.classList.remove("hidden");
    empty.className = result ? "status-error" : "muted";
    empty.textContent = result
      ? `The run failed: ${result.error}`
      : "Run the judge to see how each golden answer scored.";
    return;
  }
  empty.classList.add("hidden");
  table.classList.remove("hidden");
  table.tBodies[0].replaceChildren(
    ...result.rows.map((row) =>
      el(
        "tr",
        {},
        el("td", {}, row.input && row.input !== row.question ? row.input : row.question),
        el("td", {}, row.actual_output),
        el("td", {}, row.expected_output || "–"),
        el("td", { className: "cases-context" }, row.context && row.context.length
          ? `${row.context_source === "chatbot" ? "(chatbot) " : "(golden) "}${row.context.map((line) => `• ${line}`).join("\n")}`
          : "–"),
        el("td", { className: "num" }, formatScore(row.score)),
        el("td", { className: row.passed ? "status-pass" : "status-fail" }, resultLabel(row.passed)),
        el("td", {}, row.reason || "–"),
      ),
    ),
  );
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const target = currentTarget();
  const input = $("chat-input");
  const message = input.value.trim();
  if (!target || !message) return;
  input.value = "";
  appendBubble("user", message);
  input.disabled = true;
  try {
    const reply = await api("/api/chat", {
      method: "POST",
      body: { target_id: target.id, message, history: state.history.slice(-40) },
    });
    const model = reply.model && reply.model !== "unknown" ? ` · ${reply.model}` : "";
    appendBubble("bot", reply.reply, `${target.name} · ${reply.mode}${model}`, {
      question: message,
      actual_output: reply.reply,
    });
    state.history.push({ role: "user", content: message }, { role: "assistant", content: reply.reply });
  } catch (error) {
    appendBubble("error", error.message);
  } finally {
    input.disabled = false;
    input.focus();
  }
});

$("advanced-button").addEventListener("click", openAdvanced);
$("report-button").addEventListener("click", () => {
  const target = currentTarget();
  if (!target) {
    setStatus("Pick a chatbot first.", true);
    return;
  }
  window.open(pageUrl(`report/report.html?target=${target.id}`), "_blank");
});

$("run-all-button").addEventListener("click", () => {
  const keys = countedMetrics().filter((m) => (m.cases_available ?? 0) > 0).map((m) => m.key);
  if (!keys.length) {
    setStatus("No ticked metric in this category has cases for this chatbot (tick metrics under Metrics to run in the side panel).", true);
    return;
  }
  runKeys(keys);
});

for (const button of document.querySelectorAll("[data-view]")) {
  button.addEventListener("click", () => setView(button.dataset.view));
}

for (const button of document.querySelectorAll("[data-trend-view]")) {
  button.addEventListener("click", () => {
    state.trendView = button.dataset.trendView;
    for (const other of document.querySelectorAll("[data-trend-view]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
    renderTrend();
  });
}

$("cases-select").addEventListener("change", () => {
  const value = $("cases-select").value;
  state.casesPerRun = value ? Number(value) : null;
  settings.set("casesPerRun", state.casesPerRun);
});

$("refresh-status").addEventListener("click", () => {
  refreshStatus().catch((error) => setStatus(error.message, true));
});

$("tokens-reset").addEventListener("click", async () => {
  try {
    state.usage = await api("/api/usage/reset", { method: "POST", body: {} });
  } catch (error) {
    setStatus(error.message, true);
  }
  renderTiles();
});

$("target-select").addEventListener("change", () => {
  onTargetChanged().catch((error) => setStatus(error.message, true));
});

$("metric-select").addEventListener("change", () => {
  settings.set("selectedMetric", $("metric-select").value);
  renderTrend();
  if (state.view === "history") renderHistory();
});

settings.onChange((changes) => {
  if (changes.checkedMetrics) {
    const keys = changes.checkedMetrics.newValue;
    state.checked = Array.isArray(keys) ? new Set(keys) : null;
    renderMetricGrid();
    renderCounts(latestRowsCache);
  }
  const target = currentTarget();
  const run = changes.lastRun && changes.lastRun.newValue;
  const wasRun = changes.lastRun && changes.lastRun.oldValue;
  // Also fires when lastRun is cleared (Reset scores): newValue is null, but
  // oldValue still names the target whose cards need to go back to "not run".
  const affectsThisTarget = target && ((run && run.targetId === target.id) || (!run && wasRun && wasRun.targetId === target.id));
  if (affectsThisTarget) {
    renderCases(run || null);
    renderLatest();
    renderTrend();
    if (state.view === "history") renderHistory();
  }
  if ((changes.freshSinceRunId || changes.runsChangedAt) && target) {
    // Open Details panels may show a run that is now hidden or deleted.
    state.detailHistory = {};
    state.caseDetailsCache = {};
    state.expandedDetailKeys.clear();
    renderLatest();
    renderTrend();
    if (state.view === "history") renderHistory();
  }
  const started = changes.startedJob && changes.startedJob.newValue;
  if (started && target && started.targetId === target.id) {
    followActiveJob(target).catch((error) => setStatus(error.message, true));
  }
  if (changes.selectedTargetId && changes.selectedTargetId.newValue !== (target ? target.id : null)) {
    loadTargets(changes.selectedTargetId.newValue).catch((error) => setStatus(error.message, true));
  }
  if (changes.thresholds) {
    state.thresholds = changes.thresholds.newValue || {};
    renderMetricGrid();
    renderTrend();
  }
  if (changes.selectedMetric && changes.selectedMetric.newValue !== $("metric-select").value) {
    $("metric-select").value = changes.selectedMetric.newValue;
    renderTrend();
    if (state.view === "history") renderHistory();
  }
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  renderLatest();
  renderTrend();
});

window.addEventListener("focus", () => {
  refreshJudge();
});

(async function init() {
  await refreshJudge();
  try {
    state.categoryFilter = await settings.get("dashCategory", "all");
    state.casesPerRun = await settings.get("casesPerRun", null);
    $("cases-select").value = state.casesPerRun ? String(state.casesPerRun) : "";
    await loadMetrics();
    const ticked = await settings.get("checkedMetrics", null);
    state.checked = Array.isArray(ticked) ? new Set(ticked) : null;
    if (state.categoryFilter !== "all" && !categoryLabels()[state.categoryFilter]) state.categoryFilter = "all";
    const savedMetric = await settings.get("selectedMetric", ALL_METRICS);
    state.thresholds = await settings.get("thresholds", {});
    fillMetricSelect($("metric-select"), state.metrics, savedMetric);
    renderCategoryChips();
    await loadTargets();
    await refreshUsage();
  } catch (error) {
    setStatus(`${error.message} Reload this tab once the backend is running.`, true);
  }
})();
