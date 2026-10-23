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
  const pct = (n) => (shown.length ? Math.round((n / shown.length) * 100) : 0);
  $("status-donut").style.setProperty("--pass", pct(pass));
  $("status-donut").style.setProperty("--fail", pct(fail));
  // Hover a pill to see which metrics it counts, with score against threshold.
  const line = (m) => {
    const row = byKey.get(m.key);
    const bound = `${comparator(m.direction)} ${formatScore(thresholdFor(m, state.thresholds))}`;
    return row ? `• ${m.title}: ${formatScore(row.score)} (needs ${bound})` : `• ${m.title} (needs ${bound})`;
  };
  const pillTitle = (label, metrics) => metrics.length
    ? `${label} (${metrics.length}):\n${metrics.map(line).join("\n")}`
    : `${label}: none`;
  $("status-pass").title = pillTitle("Passed", shown.filter((m) => byKey.has(m.key) && Boolean(byKey.get(m.key).passed)));
  $("status-fail").title = pillTitle("Failed", shown.filter((m) => byKey.has(m.key) && !byKey.get(m.key).passed));
  $("status-pending").title = pillTitle("Not run yet", shown.filter((m) => !byKey.has(m.key)));
  const avg = averageScore(shown, byKey);
  $("tile-average").textContent = avg ? formatScore(avg.value) : "–";
  $("tile-average").style.setProperty("--pct", avg ? Math.round(Math.max(0, Math.min(1, avg.value)) * 100) : 0);
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
            el("span", { className: "case-row-actions" },
              expandCaseButton(metric, row, direction, threshold),
              copyCaseButton(metric, row, direction, threshold)),
          ),
          el("dl", {
            className: "case-fields", title: "Double-click to read this case full size",
            ondblclick: () => openCaseViewer(metric, row, direction, threshold),
          }, ...caseFields(metric, row)),
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

function expandCaseButton(metric, row, direction, threshold) {
  return el("button", {
    type: "button", className: "small-button", title: "Read this case full size",
    "aria-label": "Expand this case", onclick: () => openCaseViewer(metric, row, direction, threshold),
  }, "⤢ Expand");
}

// One case at a readable size over the whole dashboard: Esc, the backdrop or
// Close shut it; Full screen uses the whole monitor.
function openCaseViewer(metric, row, direction, threshold) {
  document.querySelector(".case-viewer")?.remove();
  const opener = document.activeElement;
  const close = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    viewer.remove();
    document.removeEventListener("keydown", onKey);
    opener?.focus?.();
  };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const closeButton = el("button", { type: "button", onclick: close }, "Close ✕");
  const panel = el(
    "div",
    { className: "case-viewer-panel", role: "dialog", "aria-modal": "true", "aria-label": `${metric.title} case` },
    el(
      "div",
      { className: "case-viewer-head" },
      el("div", {},
        el("h2", {}, metric.title),
        el("p", { className: `case-score status-${row.passed ? "pass" : "fail"}` },
          `${row.passed ? "✓ pass" : "✕ fail"} · ${formatScore(row.score)} ${comparator(direction)} ${formatScore(threshold)}`)),
      el("div", { className: "case-viewer-actions" },
        document.fullscreenEnabled
          ? el("button", { type: "button", onclick: () => viewer.requestFullscreen().catch(() => {}) }, "⛶ Full screen")
          : null,
        copyCaseButton(metric, row, direction, threshold),
        closeButton),
    ),
    el("dl", { className: "case-fields" }, ...caseFields(metric, row)),
  );
  const viewer = el("div", { className: "case-viewer", onclick: (e) => { if (e.target === viewer) close(); } }, panel);
  document.addEventListener("keydown", onKey);
  document.body.append(viewer);
  closeButton.focus();
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
    if (row.context && row.context.length) {
      add(contextLabel(metric, row), el("ul", {}, ...row.context.map((line) => el("li", {}, line))));
    }
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
    if (row.tools_called && row.tools_called.length) {
      add("Tools called", el("ul", {}, ...row.tools_called.map((t) => el("li", {},
        t.input_parameters ? `${t.name}(${JSON.stringify(t.input_parameters)})` : t.name))));
    }
  }
  if (typeof row.latency_s === "number") add("Chatbot latency", `${row.latency_s.toFixed(2)} s`);
  add("Why", row.reason || "No reason given.");
  return pairs;
}

// Context the judge app found itself (target sources_fallback), not the chatbot's.
const FOUND_CONTEXT = {
  knowledge_base: "your documents",
  cited_pages: "pages the reply links to",
};

function contextLabel(metric, row) {
  if (row.context_source === "chatbot") return "Retrieved context (from the chatbot)";
  if (FOUND_CONTEXT[row.context_source]) return `Context (found in ${FOUND_CONTEXT[row.context_source]}, not the chatbot's retrieval)`;
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

// Why a card cannot run, as a readable callout instead of a grey footnote:
// "not applicable" (the chatbot lacks what the metric scores) or "no cases".
function cardNotice(notApplicable, reason) {
  const text = (reason || "").replace(/^Not applicable:\s*/i, "");
  return el(
    "div",
    { className: `card-notice ${notApplicable ? "card-notice-na" : "card-notice-empty"}`, role: "note" },
    el("strong", {}, notApplicable ? "Not applicable to this chatbot" : "No cases yet"),
    el("span", {}, notApplicable ? text : text || "Add golden answers for this chatbot's theme to run it."),
  );
}

function metricCard(metric, row) {
  const isRunning = state.runningKeys.has(metric.key);
  const hasRun = Boolean(row);
  // Not applicable: this chatbot does not report what the metric scores (no
  // tool calls, no retrieved context...). Shown apart, with how to turn it on.
  const notApplicable = Boolean(metric.unavailable) && !isRunning;
  const statusClass = isRunning ? "running" : notApplicable ? "na" : !hasRun ? "not-run" : row.passed ? "pass" : "fail";
  const statusLabel = isRunning ? "RUNNING" : notApplicable ? "N/A" : !hasRun ? "NOT RUN" : row.passed ? "PASS" : "FAIL";
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
        el(
          "span",
          {
            className: `ring${hasRun ? (row.passed ? " ring-pass" : " ring-fail") : ""}`,
            style: `--pct:${barWidth}`,
            role: "img",
            "aria-label": `Score ${hasRun ? formatScore(score) : "not run"}`,
          },
          hasRun ? formatScore(score) : "–",
        ),
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
      notApplicable || !cases
        ? cardNotice(notApplicable, metric.unavailable)
        : el(
          "p",
          { className: "card-meta" },
          `${cases} case${cases === 1 ? "" : "s"} available · ${metric.dataset}${metric.probe_set ? ` (${metric.probe_set})` : ""}`,
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
        isRunning ? "▶ Running…" : notApplicable ? "Not applicable" : "▶ Run",
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
  // Metrics that cannot run for this chatbot go last, so the runnable ones lead.
  const ordered = [...shown.filter((m) => !m.unavailable), ...shown.filter((m) => m.unavailable)];
  $("metric-grid").replaceChildren(...ordered.map((m) => metricCard(m, byKey.get(m.key))));
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

// The chatbot's history as scans: each Run is one numbered cycle (1, 2, ...)
// with one overall result; Open shows its metrics, Compare the change since the
// scan before. Scores, trend and cases start fresh each panel load; this keeps all.
const SCAN_RESULT = {
  passed: ["Passed", "status-pass"], failed: ["Failed", "status-fail"],
  incomplete: ["Incomplete", "status-error"], running: ["Running", ""], queued: ["Queued", ""],
};
const scanTime = (iso) => (iso ? formatRunTime(iso) : "–");
const metricTitle = (key) => state.metrics.find((m) => m.key === key)?.title ?? key;

async function renderHistory() {
  const target = currentTarget();
  if (!target) return;
  const table = $("history-table");
  const empty = $("history-empty");
  table.classList.add("hidden");
  $("scan-detail").classList.add("hidden");
  try {
    const scans = await api(`/api/targets/${target.id}/scans`);
    $("history-count").textContent = scans.length ? `(${scans.length} scan${scans.length > 1 ? "s" : ""})` : "";
    empty.classList.toggle("hidden", scans.length > 0);
    table.classList.toggle("hidden", scans.length === 0);
    table.tBodies[0].replaceChildren(...scans.map((scan, i) => {
      const [label, css] = SCAN_RESULT[scan.result] || [scan.result, ""];
      const previous = scans[i + 1];
      return el(
        "tr",
        {},
        el("td", {}, el("strong", {}, String(scan.version))),
        el("td", {}, scanTime(scan.started_at)),
        el("td", { className: css }, `${label} · ${scan.passed} of ${scan.metrics}`,
          // Paths found in the chatbot's replies, or a scan started for them.
          scan.note ? el("span", { className: "small", title: scan.note, "aria-label": scan.note }, " ⓘ") : ""),
        el("td", { className: "num" }, String(scan.passed)),
        el("td", { className: "num" }, String(scan.failed)),
        el("td", { className: "num" }, String(scan.errors)),
        el("td", { className: "num" }, formatScore(scan.average)),
        el("td", { className: "small" }, scan.judge_model || "–"),
        el("td", {},
          // Opens the scan's full report in its own tab (scan/scan.html).
          el("a", { className: "button-link", href: `../scan/scan.html?job=${scan.job_id}`,
            target: "_blank", rel: "noopener" }, "Open"),
          // Overview / Test Cases view of this run (runs/runs.html).
          el("a", { className: "button-link", href: `../runs/runs.html?target=${target.id}&job=${scan.job_id}#overview`,
            target: "_blank", rel: "noopener" }, "Overview"),
          previous
            ? el("button", { type: "button", onclick: () => compareScan(scan, previous) }, `Compare with ${previous.version}`)
            : "",
          // Case by case, side by side (regressions/regressions.html).
          previous
            ? el("a", { className: "button-link", href: `../regressions/regressions.html?job=${scan.job_id}&with=${previous.job_id}`,
              target: "_blank", rel: "noopener" }, "Regressions")
            : ""),
      );
    }));
  } catch (error) {
    setStatus(error.message, true);
  }
}

function showScanTable(heading, columns, rows) {
  $("scan-detail-heading").textContent = heading;
  const table = $("scan-detail-table");
  table.tHead.rows[0].replaceChildren(...columns.map(([text, css]) => el("th", { className: css || "" }, text)));
  table.tBodies[0].replaceChildren(...rows);
  $("scan-detail").classList.remove("hidden");
  $("scan-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function compareScan(scan, previous) {
  try {
    const diff = await api(`/api/scans/${scan.job_id}/compare?with=${previous.job_id}`);
    const label = { better: "Better", worse: "Worse", same: "Same", new: "New", removed: "Removed", changed: "Changed" };
    const cell = (side) => (side ? `${formatScore(side.score)} (${side.status})` : "–");
    showScanTable(
      `Scan ${scan.version} compared with scan ${previous.version}`,
      [["Metric"], [`Scan ${previous.version}`, "num"], [`Scan ${scan.version}`, "num"], ["Change"]],
      diff.metrics.map((m) => el(
        "tr",
        {},
        el("td", {}, metricTitle(m.key)),
        el("td", { className: "num" }, cell(m.before)),
        el("td", { className: "num" }, cell(m.now)),
        el("td", { className: m.change === "better" ? "status-pass" : m.change === "worse" ? "status-fail" : "" },
          label[m.change] || m.change),
      )),
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
          ? `${row.context_source === "chatbot" ? "(chatbot) " : FOUND_CONTEXT[row.context_source] ? `(${FOUND_CONTEXT[row.context_source]}) ` : "(golden) "}${row.context.map((line) => `• ${line}`).join("\n")}`
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
// Opens Regressions on the selected chatbot (its two newest scans), in a new tab.
$("regressions-link").addEventListener("click", (event) => {
  const target = currentTarget();
  event.currentTarget.href = `../regressions/regressions.html${target ? `?target=${target.id}` : ""}`;
});
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
