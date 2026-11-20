// Side panel: pick or add a chatbot, manage its golden answers, run the judge,
// and see the latest scores. The dashboard tab follows the same selection
// through the shared settings (localStorage).

const $ = (id) => document.getElementById(id);
const state = {
  targets: [],
  metrics: [],
  backendUp: false,
  judgeUp: false,
  chart: null,
  editingGolden: null,
  editingTarget: null,
  editingConversation: null,
  // The chatbot whose golden-preparation status is showing (prepareGoldens),
  // and the one just added, asked "Build test data for every metric with AI?".
  preparedFor: null,
  askBuildFor: null,
  // The test-data build being followed: {targetId, token} (see followBuild).
  build: null,
  // What "Add a chatbot" adds: "http" (website chatbot, cURL) or "mcp" (MCP server).
  addKind: "http",
  // Chatbots askToLearn already ran for in this panel load.
  learned: new Set(),
  // Chatbots whose build the panel started by itself in this page load.
  autoStarted: new Set(),
  // Metric keys ticked to run (saved as "checkedMetrics"; all by default).
  checked: new Set(),
  // Framework to scan with ("all", "deepeval", "deepteam"; saved as "metricFramework"):
  // the list shows, and Run judge sends, only that framework's ticked metrics.
  framework: "all",
  runControl: null,
  // The metric whose test cases (mode "list") or Add form ("add") is open.
  openCases: { key: null, mode: null },
  // The G-Eval builder (lib/geval-builder.js) is open in the G-Eval group.
  gevalOpen: false,
  // The custom-vulnerability builder in the Security group is open.
  vulnOpen: false,
};

function currentTarget() {
  const id = Number($("target-select").value);
  return state.targets.find((t) => t.id === id) || null;
}

function updateRunButton() {
  const button = $("run-button");
  const ready = state.judgeUp && currentTarget() !== null;
  const count = scopedChecked().length;
  const framework = FRAMEWORK_LABELS[state.framework] ? `${FRAMEWORK_LABELS[state.framework]} ` : "";
  button.textContent = state.runControl !== null ? "Running…"
    : count ? `Run judge (${count} ${framework}metric${count === 1 ? "" : "s"})` : "Run judge";
  // The status refresh calls this every 10s; a run in flight keeps the button off.
  button.disabled = !ready || state.runControl !== null || count === 0;
  if (ready && count === 0) button.title = "Tick at least one metric";
  else if (ready) button.title = "";
  else if (state.judgeUp) button.title = "Add or pick a chatbot first";
  else button.title = "Judge not configured: add its API key in Judge settings";
  updateBuildButton();
}

// "Build all test data" and the question's Yes need the judge (it writes the data).
function updateBuildButton() {
  const button = $("build-testdata");
  const target = currentTarget();
  const building = Boolean(state.build && target && state.build.targetId === target.id);
  button.disabled = !target || !state.judgeUp || building;
  button.textContent = building ? "Building…" : "Build all test data";
  if (!target) button.title = "Add or pick a chatbot first";
  else if (!state.judgeUp) button.title = "Judge not configured: add its API key in Judge settings";
  else if (building) button.title = "This chatbot's test data is being built";
  else button.title = "Build test data for every metric with AI: interviews the chatbot, reads its help pages, "
    + "lists its tools and writes only what is still missing. Uses judge tokens.";
  $("testdata-yes").disabled = !state.judgeUp;
  $("testdata-needs-judge").classList.toggle("hidden", state.judgeUp || $("testdata-ask").classList.contains("hidden"));
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    state.backendUp = true;
    state.judgeUp = status.judge.up;
    $("status").textContent = state.judgeUp
      ? `Judge ready · ${status.judge.model}`
      : "Judge not configured: add its API key in Judge settings below.";
  } catch (error) {
    state.backendUp = false;
    state.judgeUp = false;
    $("status").textContent = error.message;
  }
  updateRunButton();
}

async function loadMetrics() {
  // Case counts depend on the chatbot's golden set, so ask about the selected one.
  const target = currentTarget();
  state.metrics = await api(target ? `/api/metrics?target_id=${target.id}` : "/api/metrics");
  await renderThresholds();
}

// A metric with nothing to send for this chatbot's golden set (e.g. the context
// metrics on the generic set). The run skips it instead of reporting an error.
function hasNoCases(metric) {
  return metric.cases_available === 0;
}

async function loadTargets(preferredId) {
  state.targets = await fetchTargets();
  const wanted = preferredId ?? (await settings.get("selectedTargetId", null));
  const fallback = state.targets.length ? state.targets[0].id : null;
  const selected = state.targets.some((t) => t.id === wanted) ? wanted : fallback;
  // No chatbot yet: open the form, since nothing else works without one.
  if (!state.targets.length) $("add-target").open = true;
  fillSelect(
    $("target-select"),
    state.targets.map((t) => ({ value: t.id, label: targetOptionLabel(t) })),
    selected,
  );
  await onTargetChanged();
}

// MCP servers are marked in the picker, so they are told apart from chatbots.
function targetOptionLabel(target) {
  return target.type === "mcp" ? `${targetLabel(target)} (MCP)` : targetLabel(target);
}

function targetThemeText(target) {
  return `${target.type === "mcp" ? "MCP server · " : ""}Golden set: ${themeOf(target)}`;
}

async function onTargetChanged() {
  const target = currentTarget();
  await settings.set("selectedTargetId", target ? target.id : null);
  $("target-theme").textContent = target ? targetThemeText(target) : "";
  // The test-data block follows the selected chatbot: its question, or its running build.
  showBuildFor(target).catch(() => {});
  $("delete-target").disabled = !target;
  $("edit-target").disabled = !target;
  $("reset-target-runs").disabled = !target;
  $("run-results").replaceChildren();
  // Goldens belong to the target's theme, so drop any half-finished edit.
  resetGoldenForm();
  // A half-finished target edit refers to whichever target was selected
  // before; the selection just changed, so that edit no longer applies.
  resetTargetForm();
  updateRunButton();
  resetConversationForm();
  await Promise.all([loadMetrics(), loadGoldens(), loadConversations(), renderLatest(), loadDocuments()]);
  await followActiveJob(target);
  // A chatbot still on the shared generic set (added before this ran on save)
  // gets its own set and the question whether to generate its goldens, once.
  if (target && themeOf(target) === DEFAULT_THEME && !state.learned.has(target.id)) {
    state.learned.add(target.id);
    await askToLearn(target);
  }
}

// A run started elsewhere (here before the panel closed, or from the dashboard)
// is still going: show its progress and Stop again.
async function followActiveJob(target) {
  const job = target ? await activeJob(target) : null;
  if (job && state.runControl === null) {
    const titles = new Map(state.metrics.map((m) => [m.key, m.title]));
    watchRun(target, new Map(), titles, (onProgress, control) => followJob(target, job.id, onProgress, control),
      (job.metric_keys || []).length);
  }
}

// The Advanced section's summary names the overrides set, so a closed section
// still shows them; editing a chatbot with any set opens it.
const ADVANCED_FIELDS = [
  ["target-context-path", "context path"],
  ["target-sources-fallback", "sources"],
  ["target-tools-path", "tool calls path"],
  ["target-probe-set", "probes", "generic"],
  ["target-instructions", "instructions"],
];

function showAdvancedSummary() {
  const set = ADVANCED_FIELDS.filter(([id, , fallback = ""]) => $(id).value.trim() !== fallback).map(([, name]) => name);
  if (!$("target-tools-list-wrap").classList.contains("hidden")) set.push("tools list");
  $("target-advanced-summary").textContent = set.length ? `· set: ${set.join(", ")}` : "· all automatic";
  return set.length > 0;
}

// Limits section: open and name what is set when editing a chatbot.
function showLimitsSummary() {
  const bits = [];
  if ($("target-max-length").value.trim()) bits.push("max length");
  if (Number($("target-send-delay").value) > 0) bits.push("send delay");
  if ($("target-check-replies").checked) bits.push("service-message errors");
  const line = $("target-limits-summary");
  if (line) line.textContent = bits.length ? `· set: ${bits.join(", ")}` : "";
  return bits.length > 0;
}

$("target-advanced").addEventListener("input", showAdvancedSummary);
$("target-advanced").addEventListener("change", showAdvancedSummary);
showAdvancedSummary();
const limitsRoot = $("target-limits");
if (limitsRoot) {
  limitsRoot.addEventListener("input", showLimitsSummary);
  limitsRoot.addEventListener("change", showLimitsSummary);
}
showLimitsSummary();

// The target form doubles as the editor; resetTargetForm returns it to "add".
// Both forms (website chatbot, MCP server) reset; the kind picked for adding stays.
function resetTargetForm() {
  state.editingTarget = null;
  ownProbeOption(null);
  $("target-form").reset();
  $("target-submit").textContent = "Save chatbot";
  $("target-cancel").classList.add("hidden");
  $("target-form-summary").textContent = "Add a chatbot or MCP server";
  $("target-headers-hint").classList.add("hidden");
  $("target-existing").classList.add("hidden");
  $("curl-import").open = true;
  $("curl-status").textContent = "";
  $("llm-status").textContent = "";
  $("target-test-result").textContent = "";
  $("target-advanced").open = false;
  const limits = $("target-limits");
  if (limits) limits.open = false;
  fillToolsList({});
  showLearnedBanner(null);
  showAdvancedSummary();
  showLimitsSummary();
  resetMcpForm();
  $("target-kind").classList.remove("hidden");
  showAddKind(state.addKind);
}

// Shows the website chatbot form (cURL) or the MCP server form.
function showAddKind(kind) {
  const mcp = kind === "mcp";
  $("kind-mcp").checked = mcp;
  $("kind-http").checked = !mcp;
  $("target-form").classList.toggle("hidden", mcp);
  $("mcp-form").classList.toggle("hidden", !mcp);
}

for (const [id, kind] of [["kind-http", "http"], ["kind-mcp", "mcp"]]) {
  $(id).addEventListener("change", () => {
    if (!$(id).checked) return;
    // A pasted cURL may have switched the website form to "update the saved
    // chatbot"; an MCP server is a new target, so that update is dropped.
    if (kind === "mcp" && state.editingTarget) dropUpdateMode();
    state.addKind = kind;
    showAddKind(kind);
  });
}

// Everything about a chatbot except the request itself (url, headers, body,
// paths), which a freshly pasted cURL replaces.
// A saved chatbot's own probe set (written by Build all test data), offered
// next to Generic / E-commerce / Latest; null removes it (adding a new chatbot).
function ownProbeOption(target) {
  const select = $("target-probe-set");
  for (const option of [...select.options]) {
    if (/^target_/.test(option.getAttribute("value") || "")) option.remove();
  }
  if (target) select.append(el("option", { value: `target_${target.id}` }, "This chatbot's own (built with AI)"));
}

function fillTargetSettings(target) {
  const config = target.config || {};
  $("target-name").value = target.name;
  $("target-persona").value = config.persona || "";
  ownProbeOption(target);
  // Own probe set (target_<id>) must be on the list before the value is set.
  const probe = config.probe_set || "generic";
  if ([...$("target-probe-set").options].some((o) => o.value === probe)) {
    $("target-probe-set").value = probe;
  } else {
    $("target-probe-set").value = "generic";
  }
  $("target-sources-fallback").value = config.sources_fallback || "";
  $("target-max-length").value = config.max_message_length ?? "";
  $("target-send-delay").value = config.send_delay || "";
  $("target-check-replies").checked = config.check_replies === true;
  $("target-theme-input").value = themeOf(target);
  const instructions = config.instructions;
  $("target-instructions").value = Array.isArray(instructions) ? instructions.join("\n") : (instructions || "");
  $("target-help-url").value = config.help_url || "";
  fillToolsList(config);
  showLearnedBanner(target);
}

// Tools Build all / discovery saved on the chatbot (read-only list in Advanced).
function fillToolsList(config) {
  const tools = Array.isArray(config.available_tools) ? config.available_tools.filter((t) => t && t.name) : [];
  const wrap = $("target-tools-list-wrap");
  const box = $("target-tools-list");
  if (!wrap || !box) return;
  if (!tools.length) {
    wrap.classList.add("hidden");
    box.value = "";
    return;
  }
  wrap.classList.remove("hidden");
  box.value = tools.map((t) => {
    const name = String(t.name || "").trim();
    const desc = String(t.description || "").trim();
    return desc ? `${name} — ${desc}` : name;
  }).join("\n");
}

// One-line summary of what Build all / discovery / scans put on this chatbot,
// so Edit shows the learned parameters even when a nested section is closed.
function showLearnedBanner(target) {
  const line = $("target-learned");
  if (!line) return;
  if (!target || !target.config) {
    line.textContent = "";
    line.classList.add("hidden");
    return;
  }
  const c = target.config;
  const bits = [];
  const kind = c.profile && c.profile.kind;
  const kindLabel = { agent: "Agent (calls tools)", rag: "RAG bot (returns sources)",
    chat: "Chatbot", mcp: "MCP server" }[kind];
  if (kindLabel) bits.push(kindLabel);
  if (c.persona) bits.push(`role set`);
  if (c.tools_path) bits.push(`tools path: ${c.tools_path}`);
  if (c.context_path) bits.push(`context path: ${c.context_path}`);
  if (c.sources_fallback) bits.push(`sources: ${c.sources_fallback}`);
  if (c.probe_set && c.probe_set !== "generic") {
    bits.push(/^target_/.test(c.probe_set) ? "own security probes" : `probes: ${c.probe_set}`);
  }
  if (c.help_url) bits.push("help URL set");
  if (c.instructions && (Array.isArray(c.instructions) ? c.instructions.length : String(c.instructions).trim())) {
    bits.push("instructions set");
  }
  const nTools = Array.isArray(c.available_tools) ? c.available_tools.filter((t) => t && t.name).length : 0;
  if (nTools) bits.push(`${nTools} tool${nTools === 1 ? "" : "s"} listed`);
  if (c.max_message_length) bits.push(`max length ${c.max_message_length}`);
  if (c.send_delay) bits.push(`send delay ${c.send_delay}s`);
  if (c.check_replies) bits.push("service-message errors on");
  if (!bits.length) {
    line.textContent = "";
    line.classList.add("hidden");
    return;
  }
  line.textContent = (c.auto_built ? "Build all test data set: " : "Saved settings: ") + bits.join(" · ");
  line.classList.remove("hidden");
}

function startEditTarget(target) {
  if (target.type === "mcp") {
    startEditMcp(target);
    return;
  }
  const config = target.config || {};
  resetMcpForm();
  showAddKind("http");
  $("target-kind").classList.add("hidden");
  state.editingTarget = target.id;
  fillTargetSettings(target);
  $("target-method").value = config.method || "POST";
  $("target-url").value = config.url || "";
  // Header values come back masked as ***; show each name so the user can see
  // what was saved. Leaving a line as *** (or the box empty) keeps the secret.
  const headerNames = Object.keys(config.headers || {});
  $("target-headers").value = headerNames.map((name) => `${name}: ***`).join("\n");
  $("target-headers-hint").classList.toggle("hidden", !headerNames.length);
  $("target-body").value = config.body_template || "";
  $("target-response-path").value = config.response_path || "";
  $("target-context-path").value = config.context_path || "";
  $("target-tools-path").value = config.tools_path || "";
  $("target-history-path").value = config.history_path || "";
  $("target-existing").classList.add("hidden");
  $("curl-import").open = false;
  $("curl-status").textContent = "";
  $("llm-import").open = false;
  $("target-test-result").textContent = "";
  $("target-submit").textContent = "Save changes";
  $("target-cancel").classList.remove("hidden");
  $("target-form-summary").textContent = "Edit chatbot";
  $("target-form-error").textContent = "";
  // Open Advanced / Limits when Build all or the user set anything there, so
  // tools path, probes, instructions, limits are visible without hunting.
  $("target-advanced").open = showAdvancedSummary();
  const limits = $("target-limits");
  if (limits) limits.open = showLimitsSummary();
  $("add-target").open = true;
  $("target-name").focus();
}

// The golden form doubles as the editor: editing moves it into the list under
// the row being edited; resetGoldenForm moves it back and returns it to "add".
function resetGoldenForm() {
  state.editingGolden = null;
  const form = $("golden-form");
  form.reset();
  $("golden-submit").textContent = "Add golden answer";
  $("golden-cancel").classList.add("hidden");
  $("add-golden").append(form);
  $("golden-list").classList.remove("editing");
  document.querySelectorAll(".golden-editor").forEach((node) => node.remove());
  document.querySelectorAll(".golden-list li.selected").forEach((node) => node.classList.remove("selected"));
}

function startEditGolden(golden, row) {
  resetGoldenForm();
  state.editingGolden = golden.id;
  $("golden-question").value = golden.question;
  $("golden-answer").value = golden.expected_answer;
  $("golden-context").value = (golden.context || []).join("\n");
  $("golden-categories").value = (golden.categories || []).join(", ");
  $("golden-tools").value = (golden.expected_tools || []).join(", ");
  $("golden-submit").textContent = "Save changes";
  $("golden-cancel").classList.remove("hidden");
  $("golden-form-error").textContent = "";
  $("add-golden").open = false;
  row.classList.add("selected");
  row.after(el("li", { className: "golden-editor" }, $("golden-form")));
  $("golden-list").classList.add("editing");
  row.scrollIntoView({ block: "nearest" });
  $("golden-question").focus();
}

// Generation runs in the background on the backend (minutes of judge calls):
// the document list polls while any document is still in one of these states.
const DOCUMENT_WORKING = {
  queued: "queued…",
  enhancing: "AI is cleaning up the text…",
  generating: "DeepEval is generating golden answers…",
};
const DOCUMENT_POLL_MS = 4000;
let documentPollTimer = null;
let documentsInFlight = new Set();
let documentsOutcome = null;

function documentRow(doc) {
  const icon = doc.status === "ready" ? "✓" : doc.status === "error" ? "!" : "…";
  let detail = null;
  if (doc.status === "error") detail = el("span", { className: "status-error small" }, doc.error);
  else if (doc.status === "ready") detail = el("span", { className: "status-pass small" }, `${doc.goldens_created ?? 0} golden answers added`);
  else if (DOCUMENT_WORKING[doc.status]) detail = el("span", { className: "small" }, DOCUMENT_WORKING[doc.status]);
  return el(
    "li",
    { className: `document-row status-${doc.status}` },
    el("span", {}, `${icon} ${doc.filename}`),
    detail,
    // Rerun generation without re-uploading: the set replaces this document's
    // previous one, so the golden dataset can change as the document does.
    doc.status in DOCUMENT_WORKING ? null : el("button", {
      type: "button",
      "aria-label": `Regenerate golden answers from: ${doc.filename}`,
      title: "Rerun generation for this document, replacing its golden answers",
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api(`/api/documents/${doc.id}/regenerate`, { method: "POST", body: { enhance: true } });
        } catch (error) {
          $("document-form-error").textContent = error.message;
        }
        await loadDocuments();
      },
    }, "Regenerate"),
    confirmButton("Delete", `Delete document: ${doc.filename}`, async () => {
      await api(`/api/documents/${doc.id}`, { method: "DELETE" });
      await loadDocuments();
    }),
  );
}

async function loadDocuments() {
  clearTimeout(documentPollTimer);
  documentPollTimer = null;
  const target = currentTarget();
  const list = $("document-list");
  if (!target) {
    list.replaceChildren();
    documentsInFlight = new Set();
    documentsOutcome = null;
    return;
  }
  let docs;
  try {
    docs = await api(`/api/documents?theme=${encodeURIComponent(themeOf(target))}`);
  } catch {
    // Backend briefly unreachable: keep the list and try again.
    documentPollTimer = setTimeout(loadDocuments, DOCUMENT_POLL_MS);
    return;
  }
  list.replaceChildren(...docs.map(documentRow));
  const working = new Set(docs.filter((d) => d.status in DOCUMENT_WORKING).map((d) => d.id));
  // Reload the golden list whenever a document's outcome changes, not only when
  // this poll caught it mid-generation: a fast run can finish between polls.
  const outcome = docs.map((d) => `${d.id}:${d.status}:${d.goldens_created ?? 0}`).join("|");
  const changed = documentsOutcome !== null && outcome !== documentsOutcome;
  documentsOutcome = outcome;
  const finished = [...documentsInFlight].some((id) => !working.has(id));
  documentsInFlight = working;
  if (finished || changed) await loadGoldens();
  if (working.size) documentPollTimer = setTimeout(loadDocuments, DOCUMENT_POLL_MS);
}

// Shipped golden sets can lose rows (a delete in this panel lasts until the next
// load); say so, and point at Reset to defaults, instead of scoring on less.
async function renderGoldenHealth(theme) {
  const note = $("golden-missing");
  let health = [];
  try {
    health = await api("/api/goldens/health");
  } catch {
    // Health is advisory: an old backend without the endpoint shows nothing.
  }
  const h = health.find((row) => row.theme === theme);
  note.classList.toggle("hidden", !h || h.missing === 0);
  note.textContent = h && h.missing
    ? `${h.missing} of ${h.shipped} shipped golden answers in '${theme}' are missing. Press Reset to defaults to bring them back.`
    : "";
}

async function loadGoldens() {
  const target = currentTarget();
  renderGoldenHealth(target ? themeOf(target) : null);
  const list = $("golden-list");
  // Re-rendering the list would detach the form while it sits inside it.
  if (list.contains($("golden-form"))) resetGoldenForm();
  if (!target) {
    list.replaceChildren();
    $("golden-count").textContent = "";
    return;
  }
  let goldens = [];
  try {
    goldens = await api(`/api/goldens?theme=${encodeURIComponent(themeOf(target))}`);
  } catch (error) {
    $("golden-form-error").textContent = error.message;
  }
  $("golden-count").textContent = `(${goldens.length})`;
  // Six metrics need goldens with Context; say what to add rather than skip them silently.
  // Right after a new chatbot is saved, its preparation status shows here instead.
  const prompt = $("golden-prompt");
  const hasContext = goldens.some((g) => (g.context || []).length);
  if (hasContext) {
    state.preparedFor = null;
    prompt.classList.add("hidden");
    prompt.textContent = "";
  } else if (state.preparedFor !== target.id) {
    prompt.textContent = "No golden answers with Context for this chatbot yet: add some with Question, "
      + "Expected answer and Context (or set its Help / FAQ page URL, or generate from a document below) "
      + "to run Faithfulness, Hallucination, Summarization and the Contextual metrics.";
    prompt.classList.remove("hidden");
  }
  if (goldens.length === 0) {
    list.replaceChildren(
      el("li", { className: "muted" }, "No golden answers in this set yet. Add at least one; the judge scores against them."),
    );
    return;
  }
  list.replaceChildren(
    ...goldens.map((golden) =>
      el(
        "li",
        {},
        el(
          "div",
          { className: "text", title: `${golden.question}\n\n${golden.expected_answer}` },
          el("div", { className: "question" }, golden.question),
          el("div", { className: "answer" }, golden.expected_answer),
        ),
        el("button", {
          type: "button",
          "aria-label": `Edit golden answer: ${golden.question}`,
          onclick: (event) => startEditGolden(golden, event.currentTarget.closest("li")),
        }, "Edit"),
        confirmButton("Delete", `Delete golden answer: ${golden.question}`, async () => {
          try {
            await api(`/api/goldens/${encodeURIComponent(golden.id)}`, { method: "DELETE" });
          } catch (error) {
            $("golden-form-error").textContent = error.message;
          }
          if (state.editingGolden === golden.id) resetGoldenForm();
          await loadGoldens();
        }),
      ),
    ),
  );
}

async function renderLatest() {
  const target = currentTarget();
  const box = $("latest-chart-box");
  if (!target) return;
  box.classList.add("loading");
  try {
    const rows = await api(`/api/runs/latest?target_id=${target.id}&since_id=${await sessionSince()}`);
    const hasRuns = rows.length > 0;
    box.classList.toggle("hidden", !hasRuns);
    $("latest-empty").classList.toggle("hidden", hasRuns);
    if (hasRuns) {
      box.style.height = `${rows.length * 30 + 80}px`;
      state.chart = drawChart($("latest-chart"), state.chart, latestChartConfig(rows, withUserThresholds(state.metrics, await settings.get("thresholds", {})), "bar"));
    } else if (state.chart) {
      state.chart.destroy();
      state.chart = null;
    }
  } catch (error) {
    $("status").textContent = error.message;
  } finally {
    box.classList.remove("loading");
  }
}

function parseHeaderLines(text) {
  const headers = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const colon = line.indexOf(":");
    if (colon <= 0) throw new Error(`Header line needs "Name: value": ${line.trim()}`);
    headers[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
  }
  return headers;
}

// The connector config the form describes. Headers are left out when the
// box is empty while editing, so the backend keeps the stored (masked) ones.
function buildTargetConfig() {
  const value = (id) => $(id).value.trim();
  const config = {
    method: $("target-method").value,
    url: value("target-url"),
    body_template: $("target-body").value.trim(),
    response_path: value("target-response-path"),
    context_path: value("target-context-path"),
    sources_fallback: $("target-sources-fallback").value,
    tools_path: value("target-tools-path"),
    persona: value("target-persona"),
    probe_set: $("target-probe-set").value,
    history_path: value("target-history-path"),
    theme: value("target-theme-input") || DEFAULT_THEME,
    max_message_length: value("target-max-length") ? Number(value("target-max-length")) : null,
    send_delay: value("target-send-delay") ? Number(value("target-send-delay")) : 0,
    check_replies: $("target-check-replies").checked,
    instructions: $("target-instructions").value.split("\n").map((l) => l.trim()).filter(Boolean),
    help_url: value("target-help-url"),
  };
  if (config.max_message_length !== null
      && (!Number.isInteger(config.max_message_length) || config.max_message_length < 1)) {
    throw new Error("Max message length must be a whole number above 0.");
  }
  if (!Number.isFinite(config.send_delay) || config.send_delay < 0 || config.send_delay > 60) {
    throw new Error("Delay between messages must be between 0 and 60 seconds.");
  }
  if (!config.url) throw new Error("URL is required.");
  if (!`${config.url}${config.body_template}`.includes("{{message}}")) {
    throw new Error("Put {{message}} in the body (or URL) where the question goes.");
  }
  const headerText = $("target-headers").value;
  if (headerText.trim() || !state.editingTarget) config.headers = parseHeaderLines(headerText);
  return config;
}

// Saves the add / edit form (website chatbot or MCP server). A new one gets a
// golden set of its own and the question whether AI builds its test data.
// After a successful save the form stays open on the saved chatbot so the
// user can see every field that was stored (headers as Name: ***).
async function saveTarget(body) {
  const editingId = state.editingTarget;
  const saved = editingId
    ? await api(`/api/targets/${editingId}`, { method: "PUT", body })
    : await api("/api/targets", { method: "POST", body });
  if (!editingId) {
    state.learned.add(saved.id);
    await askToLearn(saved);
  } else {
    await loadTargets(saved.id);
  }
  // Prefer the list row (fresh from the API) so masked headers match the store.
  const target = state.targets.find((t) => t.id === saved.id) || saved;
  startEditTarget(target);
  const status = $("target-test-result");
  status.className = "small status-pass";
  status.textContent = editingId
    ? "Saved. The fields below are what this chatbot uses."
    : "Saved. This chatbot is selected; the fields below are what was stored.";
  // New chatbot: keyboard / screen-reader users still land on the build question.
  if (!editingId && state.askBuildFor === saved.id) $("testdata-question").focus();
}

$("target-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("target-form-error").textContent = "";
  try {
    await saveTarget({ name: $("target-name").value.trim(), type: "http", config: buildTargetConfig() });
  } catch (error) {
    $("target-form-error").textContent = error.message;
  }
});

// A newly added chatbot gets a golden set of its own (prepare-goldens without
// generate: nothing is written and no judge tokens are spent), so answers typed
// by hand never land in the shared default set. The test data itself is built
// only when the user says yes (startBuild).
async function prepareGoldens(saved) {
  const prompt = $("golden-prompt");
  try {
    const out = await api(`/api/targets/${saved.id}/prepare-goldens`, {
      method: "POST", body: { help_url: saved.config.help_url || "", generate: false },
    });
    const errors = (out.errors || []).join(" ");
    if (errors) {
      state.preparedFor = saved.id;
      prompt.textContent = `Golden set '${out.theme}': ${errors}`;
      prompt.classList.remove("hidden");
    }
    if (out.needs_context) $("add-golden").open = true;
  } catch (error) {
    state.preparedFor = saved.id;
    prompt.textContent = `Could not prepare golden answers: ${error.message}`;
    prompt.classList.remove("hidden");
  }
}

// A chatbot just added (or one still on the shared generic set): give it its
// own golden set, then ask whether AI builds test data for every metric (Build
// test data with AI) or the user enters it (No, I'll enter my own).
async function askToLearn(target) {
  await prepareGoldens(target);
  const auto = await autoBuildOn();
  if (auto) state.autoStarted.add(target.id);
  else state.askBuildFor = target.id;
  // Its theme changed to its own set: reload so its goldens and scenarios show
  // (and onTargetChanged shows the question).
  await loadTargets(target.id);
  // Automatic build: no question, the build starts and shows its progress.
  if (auto) startBuild(target).catch((error) => setBuildStatus(plainError(error), "error"));
}

// Whether new chatbots build their test data without asking. Any failure
// means no: the question is the safe default.
async function autoBuildOn() {
  try {
    const out = await api("/api/build/settings");
    return Boolean(out && out.auto_build);
  } catch {
    return false;
  }
}

// The chatbot's website, from the pasted request, as a first guess at its help page.
function siteOf(headers) {
  const entry = Object.entries(headers || {}).find(([k]) => /^(origin|referer)$/i.test(k));
  try {
    const url = new URL(entry[1]);
    return /^(localhost|127\.|\[::1\])/.test(url.hostname) ? "" : url.origin;
  } catch {
    return "";
  }
}

// A pasted request for a chatbot that is already saved updates that chatbot
// (fresh cookies/headers) instead of adding a duplicate: its name, settings,
// instructions and score history stay. "Save as new instead" undoes the switch.
function offerUpdate(parsed) {
  const saved = parsed.existing && state.targets.find((t) => t.id === parsed.existing.id);
  if (!saved || (state.editingTarget && state.editingTarget !== saved.id)) return;
  state.editingTarget = saved.id;
  fillTargetSettings(saved);
  if (!$("target-instructions").value.trim()) parsedInstructions(parsed);
  $("target-existing-text").textContent =
    `Already saved as "${saved.name}". Save updates it with this request; its settings, instructions and scores stay.`;
  $("target-existing").classList.remove("hidden");
  $("target-submit").textContent = "Update chatbot";
  $("target-cancel").classList.remove("hidden");
}

// A system prompt in the pasted request becomes the instructions, unless typed already.
function parsedInstructions(parsed) {
  if (!$("target-instructions").value.trim() && (parsed.instructions || []).length) {
    $("target-instructions").value = parsed.instructions.join("\n");
  }
}

// Undoes offerUpdate: Save adds a new chatbot again.
function dropUpdateMode() {
  state.editingTarget = null;
  ownProbeOption(null);
  if (/^target_/.test($("target-probe-set").value)) $("target-probe-set").value = "generic";
  $("target-existing").classList.add("hidden");
  $("target-submit").textContent = "Save chatbot";
  $("target-cancel").classList.add("hidden");
}

$("target-save-as-new").addEventListener("click", dropUpdateMode);

// Fills every field from the pasted cURL: the backend finds the question
// (or uses the corrected one in the message box), empties the conversation
// history, and sends the question once to find where the reply sits.
async function fillFromCurl() {
  const status = $("curl-status");
  const result = $("target-test-result");
  if (!$("curl-input").value.trim()) return;
  status.className = "small";
  status.textContent = "Reading the request and asking the chatbot once…";
  result.textContent = "";
  $("curl-fill").disabled = true;
  try {
    const parsed = await api("/api/targets/parse-curl", {
      method: "POST",
      body: { curl: $("curl-input").value, sample_message: $("curl-sample-message").value.trim() },
    });
    $("target-method").value = parsed.method;
    $("target-url").value = parsed.url;
    $("target-headers").value = Object.entries(parsed.headers).map(([k, v]) => `${k}: ${v}`).join("\n");
    $("target-body").value = parsed.body_template;
    $("curl-sample-message").value = parsed.sample_message;
    $("target-response-path").value = parsed.response_path;
    $("target-context-path").value = parsed.context_path || "";
    $("target-tools-path").value = parsed.tools_path || "";
    $("target-history-path").value = parsed.history_path || "";
    if (!$("target-name").value.trim()) $("target-name").value = new URL(parsed.url).host;
    if (!$("target-help-url").value.trim()) $("target-help-url").value = siteOf(parsed.headers);
    parsedInstructions(parsed);
    showAdvancedSummary();
    offerUpdate(parsed);

    if (!parsed.message_marked) {
      status.className = "small status-error";
      status.textContent = "Couldn't find the message: type it above and press Re-detect, or put {{message}} into the body.";
    } else if (parsed.probe_error) {
      status.className = "small status-error";
      status.textContent = "Filled, but the test request failed; check it with Test.";
      result.className = "small status-error";
      result.textContent = parsed.probe_error;
    } else {
      status.className = "small status-pass";
      status.textContent = `Filled. Reply found at "${parsed.response_path || "(whole response)"}". Check it and press Save chatbot.`;
      result.className = "small status-pass";
      result.textContent = `Reply: ${parsed.reply_preview.slice(0, 300)}`;
    }
  } catch (error) {
    status.className = "small status-error";
    status.textContent = error.message;
  } finally {
    $("curl-fill").disabled = false;
  }
}

$("curl-fill").addEventListener("click", fillFromCurl);
// A new paste means a new request, so the old message is not reused.
$("curl-input").addEventListener("paste", () => {
  $("curl-sample-message").value = "";
  setTimeout(fillFromCurl, 0);
});

// Fills the form for an OpenAI-compatible LLM API, so another model can be
// judged directly (no request capture). The config saves as a normal http target.
async function fillFromLlm() {
  const status = $("llm-status");
  status.className = "small";
  status.textContent = "Filling the form…";
  $("llm-fill").disabled = true;
  try {
    const filled = await api("/api/targets/parse-llm", {
      method: "POST",
      body: {
        base_url: $("llm-base-url").value.trim(),
        model: $("llm-model").value.trim(),
        api_key: $("llm-api-key").value.trim(),
        system_prompt: $("llm-system-prompt").value.trim(),
      },
    });
    $("target-method").value = filled.method;
    $("target-url").value = filled.url;
    $("target-headers").value = Object.entries(filled.headers).map(([k, v]) => `${k}: ${v}`).join("\n");
    $("target-body").value = filled.body_template;
    $("target-response-path").value = filled.response_path;
    $("target-context-path").value = filled.context_path || "";
    $("target-tools-path").value = filled.tools_path || "";
    $("target-history-path").value = filled.history_path || "";
    if (!$("target-name").value.trim()) {
      $("target-name").value = `${$("llm-model").value.trim()} (LLM)`.slice(0, 80);
    }
    parsedInstructions(filled);
    showAdvancedSummary();
    offerUpdate(filled);
    status.className = "small status-pass";
    status.textContent = "Filled. Check the fields below, then Test and Save chatbot.";
  } catch (error) {
    status.className = "small status-error";
    status.textContent = error.message;
  } finally {
    $("llm-fill").disabled = false;
  }
}

$("llm-fill").addEventListener("click", fillFromLlm);

$("target-test").addEventListener("click", async () => {
  const result = $("target-test-result");
  result.className = "small";
  result.textContent = "Sending a test question…";
  try {
    const body = { type: "http", config: buildTargetConfig(), message: "Hello, what can you help me with?" };
    if (state.editingTarget) body.target_id = state.editingTarget;
    const test = await api("/api/targets/test", { method: "POST", body });
    result.className = test.ok ? "small status-pass" : "small status-error";
    result.textContent = test.ok ? `Reply: ${test.reply.slice(0, 300)}` : test.error;
  } catch (error) {
    result.className = "small status-error";
    result.textContent = error.message;
  }
});

$("target-cancel").addEventListener("click", () => {
  resetTargetForm();
  $("target-form-error").textContent = "";
  $("add-target").open = false;
});

$("edit-target").addEventListener("click", () => {
  const target = currentTarget();
  if (target) startEditTarget(target);
});

// --- MCP server: a target that offers tools instead of a chat ----------------
// The judge (or the Agent model) picks a tool per test message; the tool's
// output is the reply. Environment values and header values come back masked
// (***): editing shows them as NAME=*** lines, and a value left as *** keeps
// the saved one (the backend merges it); an empty box keeps them all.
const MCP_ENV_PLACEHOLDER = "API_KEY=…";
const MCP_HEADERS_PLACEHOLDER = "Authorization: Bearer …";
const MCP_FIELDS = ["mcp-name", "mcp-command", "mcp-args", "mcp-env", "mcp-url", "mcp-headers",
  "mcp-persona", "mcp-agent-model"];

// "400: The MCP server did not answer…" → the backend's message alone.
function plainError(error) {
  return String((error && error.message) || error).replace(/^\d{3}: /, "");
}

function textLines(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

// *** is how the backend shows a saved secret; saving it would replace the real value.
function refuseMasked(values, what) {
  const masked = Object.keys(values).filter((key) => values[key] === "***");
  if (masked.length) {
    throw new Error(`Type the real value of ${what} ${masked.join(", ")}: *** only stands for a saved value.`);
  }
  return values;
}

// "KEY=VALUE" per line → {KEY: VALUE}. A leading "export " and quotes around
// the value are dropped, so lines pasted from a shell or a .env file work.
function parseEnvLines(text) {
  const env = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim().replace(/^export\s+/, "");
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) throw new Error(`Environment line needs KEY=VALUE: ${line}`);
    const value = line.slice(eq + 1).trim();
    env[line.slice(0, eq).trim()] = /^(["']).*\1$/.test(value) ? value.slice(1, -1) : value;
  }
  return env;
}

// The MCP target being edited (null while adding).
function mcpEditing() {
  const target = state.targets.find((t) => t.id === state.editingTarget);
  return target && target.type === "mcp" ? target : null;
}

// The config the MCP form describes, as POST /api/targets and
// /api/targets/mcp/tools take it. Editing keeps what this form does not show
// (golden set, help page, probe set, found paths); an empty environment or
// headers box keeps the saved values. Each transport clears the other's
// fields, so a server moved from a command to a URL keeps no stale secrets.
function buildMcpConfig() {
  const value = (id) => $(id).value.trim();
  const editing = mcpEditing();
  const config = editing ? { ...editing.config } : {};
  delete config.env;
  delete config.headers;
  const http = $("mcp-transport-http").checked;
  config.transport = http ? "http" : "stdio";
  config.persona = value("mcp-persona");
  config.agent_model = value("mcp-agent-model");
  if (http) {
    const url = value("mcp-url");
    if (!/^https?:\/\/\S+$/i.test(url)) {
      throw new Error("URL must start with http:// or https:// (e.g. http://localhost:3000/mcp).");
    }
    Object.assign(config, { url, command: "", args: [], env: {} });
    const headerText = $("mcp-headers").value;
    if (headerText.trim() || !editing) {
      const headers = parseHeaderLines(headerText);
      config.headers = editing ? headers : refuseMasked(headers, "header");
    }
  } else {
    const command = value("mcp-command");
    if (!command) throw new Error("Command is required: the program that starts the server (e.g. npx, uvx or python).");
    Object.assign(config, { command, args: textLines($("mcp-args").value), url: "", headers: {} });
    const envText = $("mcp-env").value;
    if (envText.trim() || !editing) {
      const env = parseEnvLines(envText);
      config.env = editing ? env : refuseMasked(env, "environment variable");
    }
  }
  return config;
}

function showMcpTransport() {
  const http = $("mcp-transport-http").checked;
  $("mcp-stdio").classList.toggle("hidden", http);
  $("mcp-http").classList.toggle("hidden", !http);
}

for (const id of ["mcp-transport-stdio", "mcp-transport-http"]) {
  $(id).addEventListener("change", () => {
    showMcpTransport();
    // The tools listed belonged to the server reached the other way.
    clearMcpTools();
  });
}

function resetMcpForm() {
  for (const id of MCP_FIELDS) $(id).value = "";
  $("mcp-transport-stdio").checked = true;
  $("mcp-transport-http").checked = false;
  showMcpTransport();
  $("mcp-env").placeholder = MCP_ENV_PLACEHOLDER;
  $("mcp-headers").placeholder = MCP_HEADERS_PLACEHOLDER;
  $("mcp-env-hint").classList.add("hidden");
  $("mcp-headers-hint").classList.add("hidden");
  clearMcpTools();
  $("mcp-submit").textContent = "Save MCP server";
  $("mcp-cancel").classList.add("hidden");
  $("mcp-form-error").textContent = "";
}

function startEditMcp(target) {
  const config = target.config;
  resetMcpForm();
  state.editingTarget = target.id;
  showAddKind("mcp");
  $("target-kind").classList.add("hidden");
  const http = (config.transport || (config.url ? "http" : "stdio")) === "http";
  $("mcp-transport-http").checked = http;
  $("mcp-transport-stdio").checked = !http;
  showMcpTransport();
  $("mcp-name").value = target.name;
  $("mcp-command").value = config.command || "";
  $("mcp-args").value = (config.args || []).join("\n");
  $("mcp-url").value = config.url || "";
  // Saved values show as ***; a line left as *** keeps its value.
  const envNames = Object.keys(config.env || {});
  $("mcp-env").value = envNames.map((name) => `${name}=***`).join("\n");
  $("mcp-env-hint").classList.toggle("hidden", !envNames.length);
  const headerNames = Object.keys(config.headers || {});
  $("mcp-headers").value = headerNames.map((name) => `${name}: ***`).join("\n");
  $("mcp-headers-hint").classList.toggle("hidden", !headerNames.length);
  $("mcp-persona").value = config.persona || "";
  $("mcp-agent-model").value = config.agent_model || "";
  $("mcp-submit").textContent = "Save changes";
  $("mcp-cancel").classList.remove("hidden");
  $("target-form-summary").textContent = "Edit MCP server";
  showLearnedBanner(target);
  $("add-target").open = true;
  $("mcp-name").focus();
}

function clearMcpTools() {
  $("mcp-test-status").className = "small";
  $("mcp-test-status").textContent = "";
  $("mcp-server-instructions").textContent = "";
  $("mcp-server-instructions").classList.add("hidden");
  $("mcp-tool-list").replaceChildren();
  $("mcp-tool-list").classList.add("hidden");
}

// The server's name and each tool's name and description (shown as text).
function showMcpTools(result) {
  const tools = Array.isArray(result && result.tools) ? result.tools : [];
  const server = (result && result.server) || {};
  const name = [server.name, server.version].filter(Boolean).join(" ");
  const status = $("mcp-test-status");
  status.className = tools.length ? "small status-pass" : "small status-error";
  status.textContent = `Connected${name ? ` to ${name}` : ""}: ${tools.length
    ? `${tools.length} tool${tools.length === 1 ? "" : "s"}.`
    : "it lists no tools, so the agent has nothing to call."}`;
  // The server's own instructions (MCP initialize), when it gives any.
  const instructions = typeof server.instructions === "string" ? server.instructions.trim() : "";
  $("mcp-server-instructions").textContent = instructions ? `Server instructions: ${instructions}` : "";
  $("mcp-server-instructions").classList.toggle("hidden", !instructions);
  $("mcp-tool-list").replaceChildren(...tools.map((tool) => el("li", {},
    el("code", { className: "mcp-tool-name" }, tool.name),
    el("span", { className: "mcp-tool-desc muted" }, tool.description || "(no description)"))));
  $("mcp-tool-list").classList.toggle("hidden", !tools.length);
}

$("mcp-test").addEventListener("click", async () => {
  clearMcpTools();
  const status = $("mcp-test-status");
  let config;
  try {
    config = buildMcpConfig();
  } catch (error) {
    status.className = "small status-error";
    status.textContent = error.message;
    return;
  }
  status.textContent = config.transport === "http"
    ? "Connecting and listing its tools…"
    : "Starting the server and listing its tools (a first start that downloads it can take a minute)…";
  $("mcp-test").disabled = true;
  try {
    const body = { config };
    // Editing with the boxes left empty: the saved environment and headers apply.
    if (state.editingTarget) body.target_id = state.editingTarget;
    showMcpTools(await api("/api/targets/mcp/tools", { method: "POST", body }));
  } catch (error) {
    status.className = "small status-error";
    status.textContent = plainError(error);
  } finally {
    $("mcp-test").disabled = false;
  }
});

$("mcp-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("mcp-form-error").textContent = "";
  try {
    const name = $("mcp-name").value.trim();
    if (!name) throw new Error("Name is required.");
    await saveTarget({ name, type: "mcp", config: buildMcpConfig() });
  } catch (error) {
    $("mcp-form-error").textContent = plainError(error);
  }
});

$("mcp-cancel").addEventListener("click", () => {
  resetTargetForm();
  $("add-target").open = false;
});

// --- Test data for every metric (POST / GET /api/targets/{id}/prepare-all) ----
// The build runs on the backend: it interviews the chatbot, reads its help
// pages, lists its tools and has the judge write goldens, scenarios, tool tests
// and security probes fitted to it; each step fills only what is still missing.
// The panel follows the selected chatbot's build every 2 s until it ends.
const BUILD_POLL_MS = 2000;
const BUILD_STEPS = {
  pending: ["○", "waiting"],
  running: ["◔", "working…"],
  done: ["✓", "done"],
  skipped: ["–", "skipped"],
  error: ["!", "failed"],
};

function setBuildStatus(text, tone = "") {
  const line = $("testdata-status");
  // Unchanged text is not rewritten, so a screen reader hears each change once.
  if (line.textContent !== text) line.textContent = text;
  line.className = tone ? `small status-${tone}` : "small";
}

// The block under the chatbot: the question (asking) or a build's progress.
function showBuildBox(asking) {
  $("testdata").classList.remove("hidden");
  $("testdata-question").classList.toggle("hidden", !asking);
  $("testdata-ask").classList.toggle("hidden", !asking);
  updateBuildButton();
}

// Hides the block and stops following a build (the backend carries on).
function hideBuild() {
  state.build = null;
  $("testdata").classList.add("hidden");
  $("testdata-question").classList.add("hidden");
  $("testdata-ask").classList.add("hidden");
  $("testdata-close").classList.add("hidden");
  $("testdata-stop").classList.add("hidden");
  $("testdata-steps").replaceChildren();
  $("testdata-metrics").replaceChildren();
  $("testdata-kind").textContent = "";
  $("testdata-kind").classList.add("hidden");
  $("testdata-leftovers").replaceChildren();
  $("testdata-leftovers").classList.add("hidden");
  setBuildStatus("");
  updateBuildButton();
}

// What the block shows for the selected chatbot: the question right after it
// was added, its build while one runs (also one started before a reload), else nothing.
async function showBuildFor(target) {
  hideBuild();
  if (!target) return;
  if (state.askBuildFor === target.id) showBuildBox(true);
  let build;
  try {
    build = await api(`/api/targets/${target.id}/prepare-all`);
  } catch {
    return;  // nothing to follow (or a backend without the build)
  }
  const current = currentTarget();
  if (!current || current.id !== target.id || state.build) return;
  if (build && build.status === "running") {
    await followBuild(target, { first: build });
    return;
  }
  // An already built chatbot: show what it is and what is still missing.
  if (build && (KIND_LABELS[build.kind] || (Array.isArray(build.leftovers) && build.leftovers.length))) {
    $("testdata").classList.remove("hidden");
    renderKind(build.kind);
    renderLeftovers(build.leftovers);
  }
  // Never built automatically and some data still needs the user's hint: build once.
  const config = target.config || {};
  const hinted = Array.isArray(build && build.leftovers) && build.leftovers.some((item) => item && item.hint);
  if (build && hinted && !config.auto_built && !state.autoStarted.has(target.id)) {
    // Marked before any await, so overlapping calls and failed starts do not retry.
    state.autoStarted.add(target.id);
    if (!(await autoBuildOn())) return;
    const now = currentTarget();
    if (!now || now.id !== target.id || state.build) return;
    await startBuild(target);
  }
}

async function startBuild(target) {
  state.askBuildFor = null;
  const token = {};
  state.build = { targetId: target.id, token };
  const live = () => Boolean(state.build) && state.build.token === token;
  showBuildBox(false);
  $("testdata-close").classList.add("hidden");
  $("testdata-steps").replaceChildren();
  $("testdata-metrics").replaceChildren();
  setBuildStatus(`Starting the test-data build for ${target.name}…`);
  let first;
  try {
    // Answers with the build's status (same shape as the GET); 503 without a judge key.
    first = await api(`/api/targets/${target.id}/prepare-all`, { method: "POST", body: { generate: true } });
  } catch (error) {
    if (!live()) return;
    // Already building (started from another tab): follow that build.
    if (/^409: /.test(error.message)) {
      await followBuild(target);
      return;
    }
    state.build = null;
    updateBuildButton();
    // A backend started before this version has no such route.
    const oldBackend = /^40[45]: (Not Found|Method Not Allowed)$/.test(error.message);
    setBuildStatus(oldBackend
      ? "This backend cannot build test data yet: restart it (run-backend.bat), then press Build all test data."
      : `Could not start the build: ${plainError(error)}`, "error");
    $("testdata-close").classList.remove("hidden");
    return;
  }
  if (live()) {
    await followBuild(target, { first: first && typeof first.status === "string" ? first : null, started: true });
  }
}

// Polls the build until it ends, then reloads what it wrote. Switching chatbot
// or a newer follow (hideBuild / another followBuild) retires this one.
async function followBuild(target, { first = null, started = false } = {}) {
  const token = {};
  state.build = { targetId: target.id, token };
  updateBuildButton();
  const live = () => Boolean(state.build) && state.build.token === token;
  let build = first;
  let idlePolls = 0;
  for (;;) {
    if (!build) {
      try {
        build = await api(`/api/targets/${target.id}/prepare-all`);
      } catch (error) {
        if (!live()) return;
        setBuildStatus(`Backend not reachable, retrying… (${plainError(error)})`, "error");
        await sleep(BUILD_POLL_MS);
        if (!live()) return;
        continue;
      }
      if (!live()) return;
    }
    // Just started: the backend may not have marked it running yet.
    if (started && build.status === "idle" && idlePolls < 3) {
      idlePolls += 1;
      build = { ...build, status: "running" };
    }
    renderBuild(target, build);
    if (build.status !== "running") break;
    build = null;
    await sleep(BUILD_POLL_MS);
    if (!live()) return;
  }
  if (started && build.status === "idle") {
    setBuildStatus("The build did not start; press Build all test data to try again.", "error");
  }
  state.build = null;
  updateBuildButton();
  await refreshAfterBuild(target);
}

function renderBuild(target, build) {
  const steps = Array.isArray(build.steps) ? build.steps : [];
  const running = build.status === "running";
  const failed = steps.filter((step) => step.status === "error").length;
  const now = steps.find((step) => step.status === "running");
  showBuildBox(false);
  if (running) {
    setBuildStatus(`Building test data for ${target.name}${now ? `: ${now.label || now.key}` : ""}… `
      + "It runs on the backend; you can keep working.");
  } else if (build.status === "error") {
    setBuildStatus(`The build stopped${build.error ? `: ${build.error}` : "."}`, "error");
  } else if (build.status === "stopped") {
    setBuildStatus(`Build stopped for ${target.name}. What it wrote so far is kept; `
      + "press Build all test data to continue.");
  } else if (build.status === "done") {
    setBuildStatus(failed
      ? `Test data built for ${target.name}; ${failed} step${failed === 1 ? "" : "s"} failed (see below).`
      : `Test data built for ${target.name}.`, failed ? "" : "pass");
  } else {
    setBuildStatus("Nothing is being built for this chatbot.");
  }
  $("testdata-steps").replaceChildren(...steps.map(buildStepRow));
  $("testdata-metrics").replaceChildren(...(running ? [] : buildMetricsSummary(build.metrics)));
  $("testdata-close").classList.toggle("hidden", running);
  const stop = $("testdata-stop");
  stop.classList.toggle("hidden", !running);
  if (!running) stop.disabled = false;
  renderKind(build.kind);
  renderLeftovers(build.leftovers);
}

const NOT_APPLICABLE = "Does not apply to this target";
const KIND_LABELS = {
  agent: "Detected: Agent (calls tools)",
  rag: "Detected: RAG bot (returns sources)",
  chat: "Detected: Chatbot",
  mcp: "Detected: MCP server",
};

function renderKind(kind) {
  const line = $("testdata-kind");
  const label = KIND_LABELS[kind] || "";
  line.textContent = label;
  line.classList.toggle("hidden", !label);
}

// What the build could not fill: "Title: reason. hint", and the ones that do
// not apply to this chatbot under their own heading. All text, never markup.
function renderLeftovers(leftovers) {
  const box = $("testdata-leftovers");
  const items = (Array.isArray(leftovers) ? leftovers : []).filter((item) => item && typeof item === "object");
  const row = (item) => el("li", {}, `${item.title || item.key}: ${String(item.reason || "").replace(/\.+$/, "")}${item.hint ? `. ${item.hint}` : "."}`);
  const isNa = (item) => item.reason === NOT_APPLICABLE;
  const open = items.filter((item) => !isNa(item));
  const na = items.filter(isNa);
  const parts = [];
  if (open.length) parts.push(el("ul", { className: "testdata-missing" }, ...open.map(row)));
  if (na.length) {
    parts.push(el("p", { className: "small" }, el("strong", {}, "Does not apply")));
    parts.push(el("ul", { className: "testdata-missing" }, ...na.map(row)));
  }
  box.replaceChildren(...parts);
  box.classList.toggle("hidden", !parts.length);
}

// One step: a mark and a word for its status (not colour alone), and its detail.
function buildStepRow(step) {
  const status = BUILD_STEPS[step.status] ? step.status : "pending";
  const [mark, word] = BUILD_STEPS[status];
  return el("li", { className: `build-step build-${status}` },
    el("span", { className: "build-mark", "aria-hidden": "true" }, mark),
    el("span", { className: "build-text" },
      el("span", { className: "build-label" }, step.label || step.key || "Step"),
      el("span", { className: "build-state" }, ` · ${word}`),
      step.detail ? el("span", { className: "build-detail" }, step.detail) : null));
}

// After the build: how many metrics have test cases now, and which still have
// none, with why and a shortcut to add one by hand.
function buildMetricsSummary(metrics) {
  if (!metrics || typeof metrics !== "object") return [];
  const ready = Number(metrics.ready) || 0;
  const missing = Array.isArray(metrics.missing) ? metrics.missing : [];
  if (!missing.length) return [el("p", { className: "status-pass" }, `Every metric has test cases (${ready}).`)];
  return [
    el("p", {}, `${ready} metric${ready === 1 ? " has" : "s have"} test cases; these still have none:`),
    el("ul", { className: "testdata-missing" }, ...missing.map((metric) => el("li", {},
      el("strong", {}, metric.title || metric.key),
      metric.reason ? `: ${metric.reason}` : "",
      addCaseButton(metric)))),
  ];
}

function addCaseButton(missing) {
  if (!state.metrics.some((m) => m.key === missing.key)) return null;
  return el("button", {
    type: "button",
    className: "small-button testdata-add",
    "aria-label": `Add a test case for ${missing.title || missing.key}`,
    title: "Open this metric's Add form under Metrics to run",
    onclick: () => { openMetricCases(missing.key).catch(() => {}); },
  }, "Add one");
}

// Opens a metric's Add form under Judge › Metrics to run and moves there.
async function openMetricCases(key) {
  state.openCases = { key, mode: "add" };
  $("threshold-details").open = true;
  await renderThresholds();
  const row = document.querySelector(".threshold-row.cases-open");
  const button = row && row.querySelector(".case-button.active");
  if (button) button.focus();
  if (row) row.scrollIntoView({ block: "center" });
}

// The build wrote goldens, scenarios, probes and documents and may have
// changed the chatbot's config (persona, tools/context paths, probe set, help
// URL, available_tools, limits): reload what shows them, put the fresh values
// into Edit chatbot (Advanced + Limits open when set), and tell every view that
// case counts changed (metricsChangedAt reloads the metric list here and on the
// dashboard).
async function refreshAfterBuild(target) {
  try {
    state.targets = await fetchTargets();
  } catch {
    return;
  }
  const current = state.targets.find((t) => t.id === target.id) || currentTarget();
  fillSelect($("target-select"), state.targets.map((t) => ({ value: t.id, label: targetOptionLabel(t) })),
    current ? current.id : "");
  if (current && current.id === target.id) {
    $("target-theme").textContent = targetThemeText(current);
    await Promise.all([loadGoldens(), loadConversations(), loadDocuments()]);
    // Show every parameter Build all set in the chatbot form (Edit mode).
    startEditTarget(current);
    const status = $("target-test-result");
    status.className = "small status-pass";
    status.textContent = "Build finished. Fields below include what Build all test data set "
      + "(role, paths, probes, help URL, tools, limits).";
  }
  await settings.set("metricsChangedAt", Date.now());
}

// Re-analyze which metrics are applicable after user adds goldens or scenarios manually.
// This helps unlock Contextual, Faithfulness, RAGAS and other metrics when user provides data.
async function refreshMetricsApplicability() {
  const target = currentTarget();
  if (!target) return;

  try {
    // Trigger a fresh metrics + leftovers calculation
    await settings.set("metricsChangedAt", Date.now());
    await loadMetrics(); // reloads thresholds and applicability

    // Also refresh build status to show updated leftovers
    const build = await api(`/api/targets/${target.id}/prepare-all`);
    if (build && build.leftovers) {
      renderLeftovers(build.leftovers);
    }
  } catch (e) {
    console.warn("Could not refresh metrics applicability:", e);
  }
}

$("build-testdata").addEventListener("click", () => {
  const target = currentTarget();
  if (target) startBuild(target).catch((error) => setBuildStatus(plainError(error), "error"));
});

$("testdata-yes").addEventListener("click", () => {
  const target = currentTarget();
  if (target) startBuild(target).catch((error) => setBuildStatus(plainError(error), "error"));
});

// No: the user enters this chatbot's test data by hand, starting with golden answers.
$("testdata-no").addEventListener("click", async () => {
  state.askBuildFor = null;
  state.preparedFor = null;
  hideBuild();
  await loadGoldens();
  $("add-golden").open = true;
  $("golden-question").focus();
});

// Stop: the backend ends the build before its next chatbot message or judge
// call; the follow loop then shows it stopped.
$("testdata-stop").addEventListener("click", async () => {
  const build = state.build;
  if (!build) return;
  const stop = $("testdata-stop");
  stop.disabled = true;
  try {
    await api(`/api/targets/${build.targetId}/prepare-all/cancel`, { method: "POST", body: {} });
    setBuildStatus("Stopping the build…");
  } catch (error) {
    stop.disabled = false;
    setBuildStatus(`Could not stop the build: ${plainError(error)}`, "error");
  }
});

$("testdata-close").addEventListener("click", () => {
  hideBuild();
  $("build-testdata").focus();
});

$("golden-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("golden-form-error").textContent = "";
  const target = currentTarget();
  if (!target) {
    // Goldens belong to the selected chatbot's theme, so there is nowhere to add one yet.
    $("golden-form-error").textContent = "Add or select a chatbot first: golden answers are saved to its theme.";
    return;
  }
  const context = $("golden-context").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const categories = $("golden-categories").value.split(",").map((s) => s.trim()).filter(Boolean);
  const expectedTools = $("golden-tools").value.split(",").map((s) => s.trim()).filter(Boolean);
  const editing = state.editingGolden;
  const body = {
    theme: themeOf(target),
    question: $("golden-question").value.trim(),
    expected_answer: $("golden-answer").value.trim(),
    context,
    categories,
    expected_tools: expectedTools,
  };
  try {
    if (editing) {
      await api(`/api/goldens/${encodeURIComponent(editing)}`, { method: "PUT", body });
    } else {
      await api("/api/goldens", { method: "POST", body });
    }
    resetGoldenForm();
    $("add-golden").open = false;
    await loadGoldens();
    await refreshMetricsApplicability();
  } catch (error) {
    $("golden-form-error").textContent = error.message;
  }
});

$("document-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("document-form-error").textContent = "";
  const target = currentTarget();
  if (!target) return;
  const fileInput = $("document-file");
  const file = fileInput.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append("theme", themeOf(target));
  formData.append("file", file);
  $("document-submit").disabled = true;
  $("document-submit").textContent = "Uploading…";
  try {
    // Returns at once with status "queued"; loadDocuments polls until it is done.
    const response = await fetch(`${BACKEND_URL}/api/documents`, { method: "POST", body: formData });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Upload failed.");
    fileInput.value = "";
    await loadDocuments();
  } catch (error) {
    $("document-form-error").textContent = error.message;
  } finally {
    $("document-submit").disabled = false;
    $("document-submit").textContent = "Generate goldens";
  }
});

$("url-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("url-form-error").textContent = "";
  const target = currentTarget();
  if (!target) return;
  const url = $("document-url").value.trim();
  if (!url) return;
  $("url-submit").disabled = true;
  $("url-submit").textContent = "Fetching page…";
  try {
    // The page is fetched in the request; generation then runs in the background.
    await api("/api/documents/url", { method: "POST", body: { theme: themeOf(target), url } });
    $("document-url").value = "";
    await loadDocuments();
  } catch (error) {
    $("url-form-error").textContent = error.message;
  } finally {
    $("url-submit").disabled = false;
    $("url-submit").textContent = "Generate from URL";
  }
});

$("golden-cancel").addEventListener("click", async () => {
  resetGoldenForm();
  $("golden-form-error").textContent = "";
  $("add-golden").open = false;
  await refreshMetricsApplicability();
});

// --- Conversation scenarios (multi-turn metrics) -----------------------------
function resetConversationForm() {
  state.editingConversation = null;
  $("conversation-form").reset();
  $("conversation-submit").textContent = "Add scenario";
  $("conversation-cancel").classList.add("hidden");
  $("conversation-form-summary").textContent = "Add a scenario";
  $("conversation-form-error").textContent = "";
}

function startEditConversation(scenario) {
  state.editingConversation = scenario.id;
  $("conversation-name").value = scenario.name;
  $("conversation-turns").value = scenario.user_turns.join("\n");
  $("conversation-outcome").value = scenario.expected_outcome || "";
  $("conversation-scenario").value = scenario.scenario || "";
  $("conversation-role").value = scenario.chatbot_role || "";
  $("conversation-submit").textContent = "Save changes";
  $("conversation-cancel").classList.remove("hidden");
  $("conversation-form-summary").textContent = "Edit scenario";
  $("add-conversation").open = true;
  $("conversation-name").focus();
}

async function loadConversations() {
  const target = currentTarget();
  const list = $("conversation-list");
  if (!target) {
    list.replaceChildren();
    $("conversation-count").textContent = "";
    return;
  }
  let scenarios = [];
  try {
    scenarios = await api(`/api/conversations?theme=${encodeURIComponent(themeOf(target))}`);
  } catch (error) {
    $("conversation-form-error").textContent = error.message;
  }
  $("conversation-count").textContent = `(${scenarios.length})`;
  if (!scenarios.length) {
    list.replaceChildren(el("li", { className: "muted" },
      "No scenarios for this golden set yet: the multi-turn metrics need at least one."));
    return;
  }
  list.replaceChildren(...scenarios.map((scenario) => el(
    "li",
    {},
    el(
      "div",
      { className: "text", title: scenario.user_turns.map((t, i) => `${i + 1}. ${t}`).join("\n") },
      el("div", { className: "question" }, scenario.name),
      el("div", { className: "answer" },
        `${scenario.user_turns.length} turn${scenario.user_turns.length === 1 ? "" : "s"} · ${scenario.expected_outcome || "no expected outcome"}`),
    ),
    el("button", {
      type: "button",
      "aria-label": `Edit scenario: ${scenario.name}`,
      onclick: () => startEditConversation(scenario),
    }, "Edit"),
    confirmButton("Delete", `Delete scenario: ${scenario.name}`, async () => {
      try {
        await api(`/api/conversations/${encodeURIComponent(scenario.id)}`, { method: "DELETE" });
      } catch (error) {
        $("conversation-form-error").textContent = error.message;
      }
      if (state.editingConversation === scenario.id) resetConversationForm();
      await loadConversations();
    }),
  )));
}

$("conversation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("conversation-form-error").textContent = "";
  const target = currentTarget();
  if (!target) return;
  const body = {
    theme: themeOf(target),
    name: $("conversation-name").value.trim(),
    user_turns: $("conversation-turns").value.split("\n").map((s) => s.trim()).filter(Boolean),
    expected_outcome: $("conversation-outcome").value.trim(),
    scenario: $("conversation-scenario").value.trim(),
    chatbot_role: $("conversation-role").value.trim(),
  };
  if (!body.user_turns.length) {
    $("conversation-form-error").textContent = "Add at least one user turn.";
    return;
  }
  try {
    const editing = state.editingConversation;
    if (editing) {
      await api(`/api/conversations/${encodeURIComponent(editing)}`, { method: "PUT", body });
    } else {
      await api("/api/conversations", { method: "POST", body });
    }
    resetConversationForm();
    $("add-conversation").open = false;
    await loadConversations();
    await refreshMetricsApplicability();
  } catch (error) {
    $("conversation-form-error").textContent = error.message;
  }
});

$("conversation-cancel").addEventListener("click", () => {
  resetConversationForm();
  $("add-conversation").open = false;
});

// Builds scenarios from this chatbot's goldens and saves them, so they can be
// reviewed and edited (a run builds the same ones on the fly when there are none).
$("generate-conversations").addEventListener("click", async () => {
  const target = currentTarget();
  if (!target) return;
  const status = $("conversation-status");
  $("conversation-form-error").textContent = "";
  status.className = "small";
  status.textContent = "Building scenarios from the golden answers…";
  try {
    const saved = await api("/api/conversations/from-goldens", {
      method: "POST",
      body: { theme: themeOf(target), chatbot_role: target.config.persona || "" },
    });
    status.className = "small status-pass";
    status.textContent = `Added ${saved.length} scenario${saved.length === 1 ? "" : "s"} from the golden answers. Edit them to fit real user journeys.`;
    await loadConversations();
  } catch (error) {
    status.className = "small status-error";
    status.textContent = error.message;
  }
});

armConfirm($("reset-conversations"), async () => {
  try {
    await api("/api/conversations/reset", { method: "POST", body: {} });
    resetConversationForm();
    await loadConversations();
  } catch (error) {
    $("conversation-form-error").textContent = error.message;
  }
});

// Two clicks (Confirm), since it discards this session's golden edits.
armConfirm($("reset-goldens"), async () => {
  $("golden-form-error").textContent = "";
  try {
    await api("/api/goldens/reset", { method: "POST", body: {} });
    resetGoldenForm();
    await loadGoldens();
  } catch (error) {
    $("golden-form-error").textContent = error.message;
  }
});

armConfirm($("delete-target"), async () => {
  const target = currentTarget();
  if (!target) return;
  try {
    await api(`/api/targets/${target.id}`, { method: "DELETE" });
    await loadTargets(null);
  } catch (error) {
    $("status").textContent = error.message;
  }
});

// Clears only this target's run history (goldens and config are untouched) —
// mainly for after Edit changes a target's type, when old scores describe a
// chatbot that no longer exists under this id.
armConfirm($("reset-target-runs"), async () => {
  const target = currentTarget();
  if (!target) return;
  try {
    await api(`/api/targets/${target.id}/runs`, { method: "DELETE" });
    const lastRun = await settings.get("lastRun", null);
    if (lastRun && lastRun.targetId === target.id) await settings.set("lastRun", null);
    await settings.set("runsChangedAt", Date.now());
    await renderLatest();
  } catch (error) {
    $("status").textContent = error.message;
  }
});

// A ticked metric the run leaves out, with why (the backend's skip_reason; the
// job records the same reason, so the scan and the report show it too).
function renderSkippedRow(line, title, theme, reason, metric) {
  line.className = "run-row skipped";
  const why = reason || `No test case for it in the golden set '${theme}'.`;
  // The reason is cut to one line in the narrow panel: hover shows it all, a click unfolds it.
  line.title = `${title} was not run: ${why}`;
  const detail = el("span", {
    className: "run-row-detail muted", title: why, role: "button", tabindex: "0",
    "aria-expanded": "false", "aria-label": `Why ${title} was not run: ${why}`,
  }, why);
  const toggle = () => {
    const open = detail.classList.toggle("expanded");
    detail.setAttribute("aria-expanded", String(open));
  };
  detail.addEventListener("click", toggle);
  detail.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
  });
  line.replaceChildren(
    el("span", { className: "run-row-title" }, title),
    el("span", { className: "run-row-chip muted" }, "not run"),
    detail,
    metric && metric.dataset === "goldens_without_context" && !metric.unavailable ? scratchButton() : "",
  );
}

// Fills the gap a reference-free metric reports: DeepEval writes goldens without context.
function scratchButton() {
  const button = el("button", { type: "button", className: "small" }, "Add 5 with DeepEval");
  button.addEventListener("click", async () => {
    const target = currentTarget();
    if (!target) return;
    button.disabled = true;
    button.textContent = "Generating…";
    try {
      const data = await api("/api/goldens/generate-scratch", { method: "POST", body: { target_id: target.id, count: 5 } });
      button.textContent = `Added ${data.created}: run again`;
      await loadMetrics();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Add 5 with DeepEval";
      $("status").textContent = error.message;
    }
  });
  return button;
}

// One score-bar row per metric: bar fill mirrors the dashboard's chart bars,
// a chip on the right gives the pass/fail read at a glance.
function renderRunRow(line, title, result, progress) {
  if (result === null) {
    const known = progress && progress.total;
    const fill = known
      ? el("span", { className: "run-bar-fill", style: `width:${progressFraction(progress) * 100}%` })
      : el("span", { className: "run-bar-fill" });
    line.className = "run-row";
    line.replaceChildren(
      el("span", { className: "run-row-title" }, title),
      el("span", { className: known ? "run-bar" : "run-bar running" }, fill),
      el("span", { className: "run-row-chip muted" }, known ? `${progress.done}/${progress.total}` : "running…"),
      el("span", { className: "run-row-detail muted" }, progressText(progress)),
    );
    return;
  }
  if (result.status === "cancelled") {
    line.className = "run-row";
    line.replaceChildren(
      el("span", { className: "run-row-title" }, title),
      el("span", { className: "run-row-chip muted" }, `■ ${result.error} (not saved)`),
    );
    return;
  }
  if (result.status === "error") {
    line.className = "run-row";
    line.replaceChildren(
      el("span", { className: "run-row-title" }, title),
      el("span", { className: "run-row-chip status-error" }, `! ${result.error}`),
    );
    return;
  }
  const pct = Math.max(0, Math.min(1, result.score ?? 0)) * 100;
  line.className = `run-row status-${result.status}`;
  line.replaceChildren(
    el("span", { className: "run-row-title" }, title),
    el("span", { className: "run-bar" }, el("span", { className: "run-bar-fill", style: `width:${pct}%` })),
    el(
      "span",
      { className: "run-row-chip" },
      `${formatScore(result.score)} ${comparator(result.direction)} ${formatScore(result.threshold)} ${result.status === "pass" ? "✓" : "✕"} (${[`${result.cases_run} cases`, judgingSummary(result)].filter(Boolean).join(" · ")})`,
    ),
  );
}

$("run-button").addEventListener("click", async () => {
  const target = currentTarget();
  if (!target) return;
  // Ticked metrics of the chosen framework, in catalog order.
  const keys = scopedChecked().map((m) => m.key);
  if (!keys.length) return;
  const titles = new Map(state.metrics.map((m) => [m.key, m.title]));
  const lines = new Map();
  $("run-results").replaceChildren();
  // Metrics with no cases for this chatbot are listed as skipped, not sent.
  const empty = new Set(state.metrics.filter(hasNoCases).map((m) => m.key));
  for (const key of keys.filter((k) => empty.has(k))) {
    lines.set(key, el("li"));
    $("run-results").append(lines.get(key));
    const metric = state.metrics.find((m) => m.key === key);
    renderSkippedRow(lines.get(key), titles.get(key) || key, themeOf(target),
      metric && (metric.skip_reason || metric.unavailable), metric);
  }
  const runnable = keys.filter((k) => !empty.has(k));
  // Every ticked metric goes to the backend: it leaves out the same ones and
  // keeps them, with why, in the job (its scan and the report list them).
  await watchRun(target, lines, titles, (onProgress, control) => runMetrics(target, keys, onProgress, control),
    runnable.length);
});

// Shows a run in the results list, with Stop, until it ends: a new job, or one
// already running (started before this panel was opened). lines holds the rows
// already shown; start(onProgress, control) runs or follows the job.
async function watchRun(target, lines, titles, start, count) {
  $("run-button").disabled = true;
  $("run-button").textContent = "Running…";
  state.bannerDismissed = false;  // a new run re-shows the banner until dismissed again
  const control = {
    metrics: state.metrics,
    onOffline: (off) => { $("status").textContent = off ? "Backend not reachable, retrying…" : ""; },
  };
  state.runControl = control;
  $("stop-button").disabled = false;
  $("stop-button").textContent = "Stop";
  $("stop-button").classList.remove("hidden");
  // The banner at the top of the panel says how the run is going and, once it
  // ends, how it ended; it stays until dismissed or the next run starts.
  const startedAt = Date.now();
  const finished = new Map();
  const running = new Map();  // key -> latest progress of each metric still being scored
  let current = "";
  let queued = false;
  // Every test case step seen (asked / judged), in order, for the hover panel's log.
  const caseLog = [];
  const lastStep = new Map();  // key -> "phase|question" last logged for that metric
  const logStep = (key, progress) => {
    if (!progress || !progress.question || !progress.total) return;
    const step = `${progress.phase}|${progress.question}`;
    if (lastStep.get(key) === step) return;
    lastStep.set(key, step);
    caseLog.push({ key, at: Date.now(), phase: progress.phase, question: progress.question,
      n: Math.min(progress.done + 1, progress.total), total: progress.total });
    if (caseLog.length > 200) caseLog.shift();
  };
  // What the hover panel describes (see pauseRunView).
  state.runInfo = { target, titles, finished, running, count, startedAt, caseLog, isQueued: () => queued };
  const showProgress = () => showRunBanner(runSummary({
    status: queued ? "queued" : "running", results: [...finished.values()], count, current,
    elapsedMs: Date.now() - startedAt,
  }));
  showProgress();
  announceRun(`Run started for ${target.name}.`);
  const ticker = setInterval(showProgress, 1000);
  let summary;
  try {
    await start((key, result, progress) => {
      if (!lines.has(key)) {
        lines.set(key, el("li"));
        $("run-results").append(lines.get(key));
      }
      // While the user hovers the banner or the list, rows wait (see pauseRunView).
      if (state.runPaused) state.pendingRows.set(key, () => renderRunRow(lines.get(key), titles.get(key) || key, result, progress));
      else renderRunRow(lines.get(key), titles.get(key) || key, result, progress);
      if (result) {
        finished.set(key, result);
        running.delete(key);
      } else {
        queued = Boolean(progress && progress.queued);
        current = titles.get(key) || key;
        running.set(key, progress);
        logStep(key, progress);
      }
      showProgress();
    }, control);
    const status = control.jobStatus || (finished.size ? "done" : "empty");
    summary = runSummary({
      status, results: [...finished.values()], count, error: control.jobError,
      elapsedMs: Date.now() - startedAt, at: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    });
  } catch (error) {
    summary = runSummary({ status: "failed", error: error.message });
  } finally {
    clearInterval(ticker);
    state.runControl = null;
    state.runInfo = null;
    $("stop-button").classList.add("hidden");
    updateRunButton();
  }
  // The end of the run is shown even mid-hover: there is nothing left to freeze.
  resumeRunView();
  showRunBanner(summary);
  announceRun(summary.text);
}

function showRunBanner(summary) {
  if (state.runPaused) {
    state.pendingBanner = summary;
    return;
  }
  if (state.bannerDismissed) return;  // the × hid it: the next run starts shows it again
  const { tone, text } = summary;
  $("run-banner-text").textContent = text;
  // Keeps the hover panel's hidden state through the class swap.
  $("run-banner").className = `run-banner run-banner-${tone}`;
}

// --- Hover to read a running run -------------------------------------------
// The banner ticks every second and rows redraw as cases finish, so while the
// pointer (or keyboard focus) is on the banner or the results list, nothing
// moves: updates wait, and a panel says what the run is doing right now.
state.pendingRows = new Map();
function pauseRunView() {
  if (!state.runInfo || state.runPaused) return;
  state.runPaused = true;
  renderRunDetail();
}

function resumeRunView() {
  if (!state.runPaused) return;
  state.runPaused = false;
  $("run-detail").classList.add("hidden");
  for (const draw of state.pendingRows.values()) draw();
  state.pendingRows.clear();
  if (state.pendingBanner) showRunBanner(state.pendingBanner);
  state.pendingBanner = null;
}

function renderRunDetail() {
  const info = state.runInfo;
  const results = [...info.finished.values()];
  const tally = (status) => results.filter((r) => r.status === status).length;
  const waiting = Math.max(0, info.count - info.finished.size - info.running.size);
  const now = [...info.running.entries()].map(([key, progress]) => el("li", {},
    el("strong", {}, info.titles.get(key) || key),
    el("span", { className: "muted" }, ` · ${progressText(progress) || "starting"}`)));
  $("run-detail").replaceChildren(
    el("p", { className: "run-detail-head" }, `Paused while you read · ${info.target.name} · ${formatElapsed(Date.now() - info.startedAt)} so far`),
    info.isQueued() ? el("p", {}, "Waiting for the run before this one to finish.") : "",
    now.length ? el("div", {}, el("p", { className: "run-detail-label" }, "Running now"), el("ul", {}, ...now)) : "",
    el("p", { className: "run-detail-label" }, "So far"),
    el("p", {}, `${info.finished.size} of ${info.count} metrics finished`
      + (results.length ? ` · ${tally("pass")} passed · ${tally("fail")} failed · ${tally("error")} could not run` : "")
      + (waiting ? ` · ${waiting} waiting` : "")),
    ...caseLogBlock(info),
    el("p", { className: "muted small" }, "Move the pointer away to see live updates again."),
  );
  $("run-detail").classList.remove("hidden");
}

// The test cases this run has sent so far: each step (asking the chatbot, then
// the judge scoring), oldest first, with the time since the run started.
function caseLogBlock(info) {
  const log = info.caseLog || [];
  if (!log.length) return [];
  const multi = info.count > 1;
  const list = el("ol", { className: "run-case-log" }, ...log.map((e) => el("li", {},
    el("span", { className: "muted" }, `${formatElapsed(e.at - info.startedAt)} · `),
    multi ? el("strong", {}, `${info.titles.get(e.key) || e.key} `) : "",
    el("span", { className: "muted" }, `${e.n}/${e.total} ${e.phase === "judge" ? "judging" : "asking"}: `),
    e.question)));
  const box = el("div", {}, el("p", { className: "run-detail-label" }, `Test cases log (${log.length})`), list);
  requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
  return [box];
}

for (const id of ["run-banner", "run-results"]) {
  $(id).addEventListener("mouseenter", pauseRunView);
  $(id).addEventListener("mouseleave", resumeRunView);
  $(id).addEventListener("focusin", pauseRunView);
  $(id).addEventListener("focusout", resumeRunView);
}

// Screen readers hear the start and the end once, not every second's update.
function announceRun(text) {
  $("run-announce").textContent = text;
}

$("run-banner-show").addEventListener("click", () => {
  $("run-results").scrollIntoView({ behavior: "smooth", block: "center" });
});
$("run-banner-close").addEventListener("click", () => {
  state.bannerDismissed = true;
  $("run-banner").classList.add("hidden");
});

$("stop-button").addEventListener("click", async () => {
  if (!state.runControl) return;
  $("stop-button").disabled = true;
  $("stop-button").textContent = "Stopping…";
  try {
    await stopRun(state.runControl);
  } catch (error) {
    $("status").textContent = error.message;
  }
});

$("target-select").addEventListener("change", () => {
  onTargetChanged().catch((error) => {
    $("status").textContent = error.message;
  });
});
$("check-consistency").addEventListener("change", () =>
  settings.set("checkConsistency", $("check-consistency").checked));
// Run mode: Low = 1 case per metric, High = all (null); shared with the dashboard.
$("run-mode").addEventListener("change", () =>
  settings.set("casesPerRun", $("run-mode").value ? Number($("run-mode").value) : null));

// --- Metrics to run: a tick and a threshold per metric ------------------------
async function setChecked(keys) {
  await settings.set("checkedMetrics", [...keys]);
}

// --- Framework picker: DeepEval or DeepTeam metrics (or both) ---------------
const FRAMEWORK_LABELS = { deepeval: "DeepEval", deepteam: "DeepTeam" };

// The backend sends `framework`; red teaming (group "security") is DeepTeam's.
function frameworkOf(metric) {
  return metric.framework || (metric.ui_category === "security" ? "deepteam" : "deepeval");
}

function inFramework(metric) {
  return state.framework === "all" || frameworkOf(metric) === state.framework;
}

function scopedMetrics() {
  return state.metrics.filter(inFramework);
}

function scopedChecked() {
  return scopedMetrics().filter((m) => state.checked.has(m.key));
}

// Quick select within the chosen framework: the other framework's ticks stay as they are.
async function setScopeChecked(keys) {
  const scope = new Set(scopedMetrics().map((m) => m.key));
  const next = new Set([...state.checked].filter((key) => !scope.has(key)));
  for (const key of keys) if (scope.has(key)) next.add(key);
  await setChecked(next);
}

// Category groups of one framework; DeepTeam's security group splits into the
// app's attack probes and DeepTeam's vulnerability metrics.
function frameworkGroups(metrics) {
  return metricGroups(metrics).flatMap((group) => {
    if (group.group !== "security") return [group];
    return [
      { group: "security", label: "Red-team attacks", metrics: group.metrics.filter((m) => m.source !== "deepteam") },
      { group: "security_deepteam", label: "DeepTeam vulnerabilities", metrics: group.metrics.filter((m) => m.source === "deepteam") },
    ].filter((g) => g.metrics.length);
  });
}

// A framework heading with a tick that selects or clears all of its metrics.
function frameworkHeading(framework, metrics) {
  const keys = metrics.map((m) => m.key);
  const ticked = keys.filter((key) => state.checked.has(key)).length;
  const tick = el("input", {
    type: "checkbox",
    checked: keys.length > 0 && ticked === keys.length,
    "aria-label": `Run all ${FRAMEWORK_LABELS[framework]} metrics`,
  });
  tick.indeterminate = ticked > 0 && ticked < keys.length;
  tick.addEventListener("change", async () => {
    const next = new Set(state.checked);
    for (const key of keys) {
      if (tick.checked) next.add(key);
      else next.delete(key);
    }
    await setChecked(next);
  });
  return el("label", { className: "framework-heading" }, tick,
    `${FRAMEWORK_LABELS[framework]} framework (${ticked}/${keys.length})`);
}

for (const radio of document.querySelectorAll('input[name="metric-framework"]')) {
  radio.addEventListener("change", () => {
    if (radio.checked) settings.set("metricFramework", radio.value);
  });
}

async function renderThresholds() {
  const env = await settings.get("thresholdEnv", "default");
  const thresholds = await settings.get("thresholds", {});
  const saved = await settings.get("checkedMetrics", null);
  $("check-consistency").checked = await settings.get("checkConsistency", false);
  $("run-mode").value = String((await settings.get("casesPerRun", null)) || "");
  bindParallelSelect($("parallel-metrics")).catch(() => {});
  const known = new Set(state.metrics.map((m) => m.key));
  // Nothing saved yet: every metric but the 32 DeepTeam ones (opt-in, so a first scan stays the size it was).
  const byDefault = state.metrics.filter((m) => m.source !== "deepteam" && !m.opt_in).map((m) => m.key);
  state.checked = new Set((saved ?? byDefault).filter((key) => known.has(key)));
  const framework = await settings.get("metricFramework", "all");
  state.framework = framework === "deepeval" || framework === "deepteam" ? framework : "all";
  for (const radio of document.querySelectorAll('input[name="metric-framework"]')) {
    radio.checked = radio.value === state.framework;
  }
  for (const name of Object.keys(FRAMEWORK_LABELS)) {
    $(`framework-count-${name}`).textContent = String(state.metrics.filter((m) => frameworkOf(m) === name).length);
  }
  $("framework-count-all").textContent = String(state.metrics.length);
  const scoped = scopedMetrics();
  $("metric-check-count").textContent = `(${scopedChecked().length} of ${scoped.length} ticked)`;
  updateRunButton();
  fillSelect($("threshold-env"), THRESHOLD_ENVS.map((e) => ({ value: e.key, label: e.label })), env);
  // One section per framework (a heading only when both are listed), then its category groups.
  const sections = Object.keys(FRAMEWORK_LABELS)
    .filter((name) => state.framework === "all" || state.framework === name)
    .map((name) => ({ name, metrics: scoped.filter((m) => frameworkOf(m) === name) }))
    .filter((section) => section.metrics.length);
  $("threshold-table").replaceChildren(
    ...sections.flatMap((section) => [
      ...(state.framework === "all" ? [frameworkHeading(section.name, section.metrics)] : []),
      ...frameworkGroups(section.metrics),
    ]).flatMap((group) => group instanceof Node ? [group] : [
      groupCheckRow(group),
      ...(group.group === "geval" ? [gevalBar()] : []),
      ...(group.group === "security" ? [vulnBar()] : []),
      ...group.metrics.flatMap((metric) => {
        const tick = el("input", {
          type: "checkbox",
          checked: state.checked.has(metric.key),
          "aria-label": `Run ${metric.title}`,
        });
        tick.addEventListener("change", async () => {
          const next = new Set(state.checked);
          if (tick.checked) next.add(metric.key);
          else next.delete(metric.key);
          await setChecked(next);
        });
        const input = el("input", {
          type: "number", min: "0", max: "1", step: "0.05",
          value: String(thresholdFor(metric, thresholds)),
          "aria-label": `${metric.title} threshold`,
        });
        input.addEventListener("change", async () => {
          const value = Number(input.value);
          if (!Number.isFinite(value) || value < 0 || value > 1) {
            input.value = String(thresholdFor(metric, await settings.get("thresholds", {})));
            return;
          }
          const current = await settings.get("thresholds", {});
          await settings.set("thresholds", { ...current, [metric.key]: value });
          await settings.set("thresholdEnv", "custom");
          $("threshold-env").value = "custom";
        });
        const panel = el("div", { className: "case-panel", hidden: true });
        // Title, ≥/≤ and threshold share one row; Test cases / Add sit on the
        // next grid row so the number column stays lined up across metrics.
        const title = el(
          "span",
          { className: "threshold-title" },
          metric.title,
          hasNoCases(metric)
            ? el("small", {
                className: "no-cases",
                title: metric.unavailable || `No cases for this metric in the golden set '${themeOf(currentTarget())}': a run skips it.`,
              }, " · 0 cases")
            : null,
        );
        const row = el(
          "label",
          {
            className: `threshold-row${state.checked.has(metric.key) ? "" : " unchecked"}`,
            "aria-description": metric.description || "",
          },
          tick,
          title,
          el("span", { className: "direction" }, comparator(metric.direction)),
          input,
          caseButtons(metric),
        );
        attachMetricTip(row, metric);
        // Re-rendering (a tick, a threshold) keeps an open list or form open.
        if (state.openCases.key === metric.key) {
          showCasePanel(metric, panel, state.openCases.mode);
          // Clicking the metric's name closes its list or form (the tick box still ticks).
          row.classList.add("cases-open");
          row.addEventListener("click", (event) => {
            if (event.target.closest("input, button")) return;
            event.preventDefault();
            state.openCases = { key: null, mode: null };
            renderThresholds();
          });
          // Auto-scroll the opened panel into view so user doesn't have to scroll down
          setTimeout(() => {
            panel.scrollIntoView({ behavior: "smooth", block: "center" });
          }, 100);
        }
        return [row, panel];
      }),
    ]),
  );
}

// --- Hover card per metric: category and the DeepEval test-case fields it needs ---
// `scores_on` (from /api/metrics) lists the LLMTestCase / ConversationalTestCase
// fields the judge reads, i.e. DeepEval's required arguments for that metric.
const SINGLE_FIELDS = [
  ["input", "Input (question)"],
  ["actual_output", "Actual output (chatbot reply)"],
  ["expected_output", "Expected output (golden answer)"],
  ["context", "Context (golden facts)"],
  ["retrieval_context", "Retrieval context (chatbot's sources)"],
];
const TOOL_FIELDS = [
  ["tools_called", "Tools called"],
  ["expected_tools", "Expected tools"],
];
const CONVERSATION_FIELDS = [
  ["turns", "Turns (whole conversation)"],
  ["scenario", "Scenario"],
  ["expected_outcome", "Expected outcome"],
  ["chatbot_role", "Chatbot role"],
];
const FIELD_NOTES = {
  retrieval_context: "the chatbot's own sources only (Retrieved context path or Find sources); never golden data",
  context: "facts stored with the golden",
  expected_output: "the golden's expected answer",
  tools_called: "needs the chatbot's Tool calls path",
};

// Security probes: "context" is the probe's goal plus the chatbot's persona, not golden facts.
const PROBE_LABELS = { context: "Context (probe goal + chatbot persona)" };
const PROBE_NOTES = {
  context: "added to every probe automatically; nothing to fill in",
  tools_called: "read when the chatbot reports its tool calls (Tool calls path)",
  retrieval_context: "read by DeepTeam when present; probes send none",
};

function fieldRow(key, label, state, note) {
  const marks = { need: "✓", opt: "○", skip: "–" };
  const words = { need: "Needed", opt: "Optional", skip: "Not needed" };
  return el("li", { className: state },
    el("span", { className: "mark", "aria-hidden": "true" }, marks[state]),
    el("span", { className: "field" }, label),
    el("span", { className: "state" }, words[state]),
    state !== "skip" && note ? el("small", { className: "note" }, note) : null);
}

function metricFieldRows(metric) {
  const needed = new Set(metric.scores_on || []);
  const optional = new Set(metric.optional_on || []);
  const probe = metric.ui_category === "security";
  const conversation = metric.kind === "conversation" || needed.has("turns");
  const base = conversation ? CONVERSATION_FIELDS : SINGLE_FIELDS;
  const usesTools = [...needed, ...optional].some((k) => k === "tools_called" || k === "expected_tools");
  const fields = [...base, ...(usesTools ? TOOL_FIELDS : [])];
  const known = new Set(fields.map(([key]) => key));
  for (const key of [...needed, ...optional]) if (!known.has(key)) fields.push([key, key.replace(/_/g, " ")]);
  return fields.map(([key, label]) => {
    const state = needed.has(key) ? "need" : optional.has(key) ? "opt" : "skip";
    const note = (probe && PROBE_NOTES[key]) || FIELD_NOTES[key];
    return fieldRow(key, (probe && PROBE_LABELS[key]) || label, state, note);
  });
}

// DeepTeam's own scorer (setting "scorer": "deepteam"): the RTTestCase fields its metric class reads.
function deepteamFieldRows(metric) {
  const read = new Set(metric.deepteam_fields || []);
  return [...SINGLE_FIELDS, ...TOOL_FIELDS.slice(0, 1)].map(([key, label]) => {
    const state = key === "input" || key === "actual_output" ? "need" : read.has(key) ? "opt" : "skip";
    return fieldRow(key, label, state, PROBE_NOTES[key]);
  });
}

function metricTipContent(metric) {
  const kindLabel = {
    conversation: "Multi-turn (conversation)",
    attack: "Multi-turn attack",
    multi_sample: "Single-turn, several samples",
  }[metric.kind] || "Single-turn";
  const chips = [metric.ui_category_label, metric.group_label, kindLabel,
    metric.source === "deepteam" ? "DeepTeam" : null].filter(Boolean);
  const source = metric.question_source?.label;
  return [
    el("div", { className: "tip-title" }, metric.title),
    el("div", { className: "tip-chips" }, ...[...new Set(chips)].map((c) => el("span", { className: "tip-chip" }, c))),
    metric.description ? el("p", { className: "tip-desc" }, metric.description) : null,
    metric.question ? el("p", { className: "tip-q" }, el("b", {}, "Asks: "), metric.question) : null,
    el("div", { className: "tip-head" }, metric.deepteam_metric
      ? "Test case fields (app's G-Eval rubric, default scorer)"
      : metric.ui_category === "security" ? "Test case fields (G-Eval rubric)" : "Test case fields (DeepEval)"),
    el("ul", { className: "tip-fields" }, ...metricFieldRows(metric)),
    metric.deepteam_metric ? el("div", { className: "tip-head" }, "DeepTeam scorer ", el("code", {}, metric.deepteam_metric)) : null,
    metric.deepteam_metric ? el("ul", { className: "tip-fields" }, ...deepteamFieldRows(metric)) : null,
    (metric.combines || []).length
      ? el("p", { className: "tip-extra" }, el("b", {}, "Combines (average): "), metric.combines.join(" · "))
      : null,
    metric.opt_in ? el("p", { className: "tip-extra" }, el("b", {}, "Opt-in: "), "not ticked by default") : null,
    ...promptInstructionTip(metric.prompt_instructions),
    metric.unavailable ? el("p", { className: "tip-extra tip-warn" }, el("b", {}, "Not available: "), metric.unavailable) : null,
    (metric.needs_target || []).length
      ? el("p", { className: "tip-extra" }, el("b", {}, "From the chatbot: "), metric.needs_target.join(", "))
      : null,
    source ? el("p", { className: "tip-extra" }, el("b", {}, "Questions from: "), source) : null,
    metric.scale_hint ? el("p", { className: "tip-extra" }, el("b", {}, "Score: "), metric.scale_hint) : null,
  ];
}

// Prompt Alignment: the rules a run judges replies against, listed in the hover
// card (first few; the full list is under Test cases).
const TIP_RULES_MAX = 8;
function promptInstructionTip(info) {
  const rules = info && Array.isArray(info.rules) ? info.rules : [];
  if (!rules.length) return [];
  const more = rules.length - TIP_RULES_MAX;
  return [
    el("div", { className: "tip-head" }, `Prompt instructions checked (${rules.length})`,
      el("span", { className: "muted" }, ` · ${info.source_label || info.source || ""}`)),
    el("ol", { className: "tip-rules" },
      ...rules.slice(0, TIP_RULES_MAX).map((rule) => el("li", {}, rule)),
      more > 0 ? el("li", { className: "muted" }, `…and ${more} more (see Test cases)`) : null),
  ];
}

let metricTipNode = null;
let metricTipTimer = null;
function hideMetricTip() {
  clearTimeout(metricTipTimer);
  if (metricTipNode) metricTipNode.hidden = true;
}

function showMetricTip(row, metric) {
  if (!metricTipNode) {
    metricTipNode = el("div", { className: "metric-tip", role: "tooltip", id: "metric-tip", hidden: true });
    document.body.append(metricTipNode);
  }
  metricTipNode.replaceChildren(...metricTipContent(metric).filter(Boolean));
  metricTipNode.hidden = false;
  const box = row.getBoundingClientRect();
  const gutter = 8;
  const width = Math.min(340, window.innerWidth - gutter * 2);
  metricTipNode.style.width = `${width}px`;
  const height = metricTipNode.offsetHeight;
  const below = box.bottom + 4;
  const top = below + height > window.innerHeight - gutter && box.top - height - 4 > gutter
    ? box.top - height - 4
    : Math.max(gutter, Math.min(below, window.innerHeight - height - gutter));
  const left = Math.max(gutter, Math.min(box.left, window.innerWidth - width - gutter));
  metricTipNode.style.top = `${top}px`;
  metricTipNode.style.left = `${left}px`;
}

function attachMetricTip(row, metric) {
  // Only the name area opens the card, so it doesn't cover the threshold or buttons.
  const anchor = row.querySelector(".threshold-title") || row;
  anchor.addEventListener("mouseenter", () => {
    clearTimeout(metricTipTimer);
    metricTipTimer = setTimeout(() => showMetricTip(row, metric), 250);
  });
  anchor.addEventListener("mouseleave", hideMetricTip);
  row.addEventListener("focusin", () => showMetricTip(row, metric));
  row.addEventListener("focusout", hideMetricTip);
}
window.addEventListener("scroll", hideMetricTip, true);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideMetricTip(); });

// --- Your own G-Eval metrics: built, edited and deleted in the G-Eval group ---
let gevalBuilderForm = null;
function gevalForm() {
  gevalBuilderForm ||= createGevalBuilder((row) => {
    state.gevalOpen = false;
    if (row) $("status").textContent = `Saved “${row.name}”: ticked, it runs with Run judge.`;
    renderThresholds();
  });
  return gevalBuilderForm;
}

// Your own red-team vulnerabilities (DeepTeam's CustomVulnerability), in the Security group.
let vulnBuilderForm = null;
function vulnForm() {
  vulnBuilderForm ||= createVulnerabilityBuilder((row) => {
    state.vulnOpen = false;
    if (row) $("status").textContent = `Saved “${row.name}”: ticked, it runs with Run judge.`;
    renderThresholds();
  });
  return vulnBuilderForm;
}

function vulnBar() {
  const editing = state.vulnOpen && vulnForm().editingId();
  const toggle = el("button", {
    type: "button",
    className: `case-button${state.vulnOpen ? " active" : ""}`,
    "aria-expanded": String(state.vulnOpen),
    title: "Define a vulnerability of your own (name, criteria, types) and its attacks, like DeepTeam's CustomVulnerability",
    onclick: () => {
      if (!state.vulnOpen || editing) vulnForm().fill(null);
      state.vulnOpen = !state.vulnOpen || Boolean(editing);
      renderThresholds();
    },
  }, state.vulnOpen && !editing ? "Close" : "＋ New custom vulnerability");
  return el("div", { className: "geval-bar" }, toggle,
    state.vulnOpen ? el("div", { className: "case-panel geval-panel" }, vulnForm().form) : null);
}

// "＋ New G-Eval metric" under the group heading, and the form when open.
function gevalBar() {
  const editing = state.gevalOpen && gevalForm().editingId();
  const toggle = el("button", {
    type: "button",
    className: `case-button${state.gevalOpen ? " active" : ""}`,
    "aria-expanded": String(state.gevalOpen),
    onclick: () => {
      if (!state.gevalOpen || editing) gevalForm().fill(null);
      state.gevalOpen = !state.gevalOpen || Boolean(editing);
      renderThresholds();
    },
  }, state.gevalOpen && !editing ? "Close" : "＋ New G-Eval metric");
  return el("div", { className: "geval-bar" }, toggle,
    state.gevalOpen ? el("div", { className: "case-panel geval-panel" }, gevalForm().form) : null);
}

async function editOwnGeval(metric) {
  const row = (await api("/api/custom-metrics")).find((r) => r.id === metric.custom_id);
  if (!row) throw new Error("This metric is no longer saved; reload the panel.");
  if (row.kind === "vulnerability") {
    vulnForm().fill(row);
    state.vulnOpen = true;
    state.openCases = { key: null, mode: null };
    await renderThresholds();
    vulnForm().form.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  gevalForm().fill(row);
  state.gevalOpen = true;
  // An open Test cases list re-renders with a delayed scrollIntoView of its own,
  // which would pull the view away from the form: close it first.
  state.openCases = { key: null, mode: null };
  await renderThresholds();
  gevalForm().form.scrollIntoView({ behavior: "smooth", block: "center" });
}

// Edit / Delete for a G-Eval metric of your own; Delete asks once inline.
function ownGevalButtons(metric) {
  const edit = el("button", {
    type: "button",
    className: "case-button",
    title: "Change this metric of your own",
    onclick: (event) => {
      event.preventDefault();
      editOwnGeval(metric).catch((error) => { $("status").textContent = error.message; });
    },
  }, "Edit");
  const remove = el("button", { type: "button", className: "case-button", title: "Delete this metric of your own (its earlier runs stay in History)" }, "Delete");
  let armed = false;
  remove.addEventListener("click", async (event) => {
    event.preventDefault();
    if (!armed) {
      armed = true;
      remove.textContent = "Confirm delete";
      remove.classList.add("armed");
      setTimeout(() => {
        if (!armed) return;
        armed = false;
        remove.textContent = "Delete";
        remove.classList.remove("armed");
      }, 4000);
      return;
    }
    armed = false;
    remove.disabled = true;
    try {
      await deleteOwnGeval(metric);
      if (gevalForm().editingId() === metric.custom_id) state.gevalOpen = false;
      $("status").textContent = `Deleted “${metric.title}”: it no longer runs. Its earlier runs stay in History.`;
    } catch (error) {
      remove.disabled = false;
      remove.textContent = "Delete";
      remove.classList.remove("armed");
      $("status").textContent = `Not deleted: ${error.message}`;
    }
  });
  return [edit, remove];
}

// --- Test cases per metric: what a run sends, and adding one -----------------
function caseButtons(metric) {
  const open = (mode) => state.openCases.key === metric.key && state.openCases.mode === mode;
  const button = (label, mode, title) => el("button", {
    type: "button",
    className: `case-button${open(mode) ? " active" : ""}`,
    "aria-expanded": String(open(mode)),
    title,
    onclick: (event) => {
      event.preventDefault();
      const open = state.openCases.key === metric.key && state.openCases.mode === mode;
      state.openCases = open ? { key: null, mode: null } : { key: metric.key, mode };
      renderThresholds();
    },
  }, label);
  return el("span", { className: "case-actions" },
    button(`Test cases (${metric.cases_available ?? 0})`, "list", "Show the questions/scenarios a run of this metric sends the chatbot"),
    button("Add", "add", (metric.kind === "conversation" || (metric.dataset_name || "").startsWith("conversations"))
      ? "Add a multi-turn conversation scenario for this (and other) Turns metrics"
      : "Add a test case this metric will run"),
    metric.custom_id && ["geval", "security"].includes(metric.ui_category) ? ownGevalButtons(metric) : null);
}

async function showCasePanel(metric, panel, mode) {
  panel.hidden = false;
  panel.replaceChildren(el("p", { className: "muted small" }, "Loading test cases…"));
  const target = currentTarget();
  const query = target ? `?target_id=${target.id}` : "";
  try {
    const data = await api(`/api/metrics/${encodeURIComponent(metric.key)}/cases${query}`);
    panel.replaceChildren(
      // Prompt Alignment: the rules this chatbot's run will be judged against, and their source.
      mode === "list" ? promptInstructionsBlock(metric.prompt_instructions,
        { heading: "Prompt instructions a run will use" }) : "",
      mode === "add" ? caseForm(metric, data) : caseList(metric, data));
  } catch (error) {
    panel.replaceChildren(el("p", { className: "small error" }, error.message));
  }
}

// Delete asks once inline ("Delete?" → click again) instead of a browser dialog.
function caseDeleteButton(metric, c) {
  const shared = c.kind === "golden" ? "Deletes this golden: every metric reading the golden set loses it." : "";
  const button = el("button", { type: "button", className: "case-delete", title: shared || "Delete this test case" }, "Delete");
  let armed = false;
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = "Confirm delete";
      button.classList.add("armed");
      setTimeout(() => {
        if (!armed) return;
        armed = false;
        button.textContent = "Delete";
        button.classList.remove("armed");
      }, 4000);
      return;
    }
    armed = false;
    button.disabled = true;
    const target = currentTarget();
    const query = target ? `?target_id=${target.id}` : "";
    try {
      await api(`/api/metrics/${encodeURIComponent(metric.key)}/cases/${encodeURIComponent(c.id)}${query}`, { method: "DELETE" });
      // The list reloads with the new counts (other metrics reading the set change too).
      await settings.set("metricsChangedAt", Date.now());
      if (c.kind === "golden") loadGoldens().catch(() => {});
      if (c.kind === "conversation") loadConversations().catch(() => {});
    } catch (error) {
      button.disabled = false;
      button.textContent = "Delete";
      button.classList.remove("armed");
      // Shown under the case: the panel's status line is usually scrolled out of view.
      const item = button.closest("li");
      const old = item.querySelector(".case-error");
      if (old) old.remove();
      item.append(el("div", { className: "small case-error" }, `Not deleted: ${error.message}`));
    }
  });
  return button;
}

// Edit swaps the case's row for an inline form prefilled with its fields.
function caseEditButton(metric, data, c) {
  const shared = c.kind === "golden" ? "Edits this golden: every metric reading the golden set sees the change." : "";
  const button = el("button", { type: "button", className: "case-edit", title: shared || "Edit this test case" }, "Edit");
  button.addEventListener("click", () => {
    const item = button.closest("li");
    const original = [...item.childNodes];
    item.replaceChildren(caseEditForm(metric, data, c, () => item.replaceChildren(...original)));
  });
  return button;
}

function caseEditForm(metric, data, c, cancel) {
  const field = (label, control) => el("label", { className: "case-field small" }, label, control);
  const area = (value, rows = "2", placeholder = "") => {
    const node = el("textarea", { rows, placeholder });
    node.value = value || "";
    return node;
  };
  const input = (value, placeholder = "") => {
    const node = el("input", { type: "text", placeholder });
    node.value = value || "";
    return node;
  };
  const fields = {};
  const rows = [];
  if (c.kind === "conversation") {
    fields.name = input(c.question);
    fields.turns = area((c.turns || []).join("\n"), "3", "One user message per line");
    fields.expected = area(c.expected_answer, "2", "What the conversation should achieve (optional)");
    rows.push(field("Name", fields.name), field("User turns", fields.turns), field("Expected outcome", fields.expected));
    if (data.dataset === "conversations_with_role") {
      fields.role = input(c.chatbot_role, "The chatbot's role (blank = the chatbot's persona)");
      rows.push(field("Chatbot role", fields.role));
    }
  } else {
    fields.question = area(c.question);
    rows.push(field(c.kind === "probe" ? "Probe" : "Question", fields.question));
    if (c.kind === "golden") {
      fields.expected = area(c.expected_answer, "2", "The answer you expect");
      rows.push(field("Expected answer", fields.expected));
      if (data.dataset !== "goldens_without_context") {
        fields.context = area((c.context || []).join("\n"), "2", "One fact per line");
        rows.push(field(data.dataset === "goldens_with_context" ? "Context" : "Context (optional)", fields.context));
      }
      if (data.dataset === "goldens_with_tools") {
        fields.tools = input((c.expected_tools || []).join(", "), "e.g. search_orders, refund");
        rows.push(field("Expected tools", fields.tools));
      }
    }
  }
  const lines = (node) => (node ? node.value.split("\n").map((s) => s.trim()).filter(Boolean) : []);
  const status = el("p", { className: "small" });
  const save = el("button", { type: "submit" }, "Save changes");
  const form = el("form", { className: "case-form" },
    c.kind === "golden" ? el("p", { className: "muted small" }, "Goldens are shared: every metric reading this golden set runs the edited version.") : null,
    ...rows,
    el("div", { className: "row" }, save, el("button", { type: "button", onclick: cancel }, "Cancel")),
    status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const target = currentTarget();
    const query = target ? `?target_id=${target.id}` : "";
    save.disabled = true;
    status.className = "small";
    status.textContent = "Saving…";
    try {
      await api(`/api/metrics/${encodeURIComponent(metric.key)}/cases/${encodeURIComponent(c.id)}${query}`, {
        method: "PUT",
        body: {
          target_id: target ? target.id : null,
          question: fields.question ? fields.question.value : "",
          expected_answer: fields.expected ? fields.expected.value : "",
          context: lines(fields.context),
          expected_tools: fields.tools ? fields.tools.value.split(",").map((s) => s.trim()).filter(Boolean) : [],
          name: fields.name ? fields.name.value : "",
          user_turns: lines(fields.turns),
          chatbot_role: fields.role ? fields.role.value : "",
        },
      });
      // The list reloads with the edited case (other metrics reading the set change too).
      await settings.set("metricsChangedAt", Date.now());
      if (c.kind === "golden") loadGoldens().catch(() => {});
      if (c.kind === "conversation") loadConversations().catch(() => {});
    } catch (error) {
      save.disabled = false;
      status.className = "small status-error";
      status.textContent = `Not saved: ${error.message}`;
    }
  });
  return form;
}

function caseList(metric, data) {
  const cap = $("run-mode").value ? Number($("run-mode").value) : null;
  const head = el("p", { className: "muted small" },
    `${data.source.label} · ${data.cases.length} case${data.cases.length === 1 ? "" : "s"}`,
    cap && data.cases.length > cap ? ` (Run mode sends the first ${cap})` : "",
    data.unavailable ? ` · Not run: ${data.unavailable}` : "");
  if (!data.cases.length) return el("div", {}, head, el("p", { className: "small" }, "No test cases yet: use Add."));
  return el("div", {}, head, el("ol", { className: "case-list" },
    ...data.cases.map((c) => el("li", {},
      el("div", { className: "case-head" },
        el("div", { className: "case-question" }, c.question,
          c.kind === "probe" ? el("small", { className: "muted" }, " · probe") : null,
          c.context_in_prompt ? el("small", { className: "muted", title: "Sent with its context facts in the message" }, " · with facts") : null),
        el("span", { className: "case-item-actions" },
          caseEditButton(metric, data, c),
          caseDeleteButton(metric, c))),
      c.turns ? el("ol", { className: "case-turns small" }, ...c.turns.map((t) => el("li", {}, t))) : null,
      c.expected_answer ? el("div", { className: "muted small" }, `Expected: ${c.expected_answer}`) : null,
      c.context && c.context.length ? el("div", { className: "muted small" }, `Context: ${c.context.join(" · ")}`) : null,
      c.expected_tools && c.expected_tools.length ? el("div", { className: "muted small" }, `Tools: ${c.expected_tools.join(", ")}`) : null,
    ))));
}

function caseForm(metric, data) {
  if (!data.add) {
    // All conversational metrics now support adding scenarios directly from their Test cases panel.
    if (metric.kind === "conversation" || metric.dataset_name?.startsWith("conversations")) {
      return el("p", { className: "small" }, "Use the dedicated **Conversation scenarios** section at the bottom of the side panel (or click Add on a Turns metric to open the form here).");
    }
    return el("p", { className: "small" }, `Test cases cannot be added to ${metric.title} here.`);
  }
  const field = (label, control) => el("label", { className: "case-field small" }, label, control);
  const area = (placeholder) => el("textarea", { rows: "2", placeholder });
  const fields = {};
  const rows = [];
  if (data.add === "conversation") {
    fields.name = el("input", { type: "text", placeholder: "e.g. Order then change address" });
    fields.turns = el("textarea", { rows: "3", placeholder: "One user message per line" });
    fields.expected = area("What the conversation should achieve (optional)");
    rows.push(field("Name", fields.name), field("User turns", fields.turns), field("Expected outcome", fields.expected));
    if (data.dataset === "conversations_with_role") {
      fields.role = el("input", { type: "text", placeholder: "The chatbot's role (blank = the chatbot's persona)" });
      rows.push(field("Chatbot role", fields.role));
    }
  } else {
    fields.question = area(data.add === "probe" ? "The adversarial message to send" : "The question to send the chatbot");
    rows.push(field(data.add === "probe" ? "Probe" : "Question", fields.question));
    if (data.add === "golden") {
      fields.expected = area("The answer you expect");
      rows.push(field("Expected answer", fields.expected));
      if (data.dataset !== "goldens_without_context") {
        fields.context = el("textarea", { rows: "2", placeholder: "One fact per line" });
        rows.push(field(data.dataset === "goldens_with_context" ? "Context" : "Context (optional)", fields.context));
      }
      if (data.dataset === "goldens_with_tools") {
        fields.tools = el("input", { type: "text", placeholder: "e.g. search_orders, refund" });
        rows.push(field("Expected tools", fields.tools));
      }
    }
  }
  const where = { golden: `golden set '${data.theme}'`, probe: `probe set '${data.probe_set}'`,
    conversation: `conversation scenarios of '${data.theme}'` }[data.add];
  const status = el("p", { className: "small" });
  const lines = (node) => (node ? node.value.split("\n").map((s) => s.trim()).filter(Boolean) : []);
  const save = el("button", { type: "submit" }, "Save test case");

  // Probe metrics: generate related probes from the seed question (DeepTeam / OWASP-style).
  let aiBlock = null;
  if (data.add === "probe") {
    const countInput = el("input", {
      type: "number",
      className: "case-count-input",
      min: "1",
      max: "20",
      step: "1",
      value: "5",
      "aria-label": "Number of related probes to generate",
      title: "How many related probes the judge should write (1–20)",
    });
    const aiHelper = el("button", {
      type: "button",
      className: "small-button primary",
      title: "Enter a seed question above, pick how many, then generate related probes from DeepTeam, OWASP LLM, Agentic, MCP and RAG techniques for this metric.",
    }, "Generate similar probes with AI");
    aiHelper.addEventListener("click", async () => {
      if (!fields.question || !fields.question.value.trim()) {
        status.textContent = "Enter your seed question first.";
        status.className = "small status-error";
        return;
      }
      let count = Number(countInput.value);
      if (!Number.isFinite(count) || count < 1) count = 5;
      count = Math.min(20, Math.max(1, Math.round(count)));
      countInput.value = String(count);
      aiHelper.disabled = true;
      countInput.disabled = true;
      aiHelper.textContent = "Generating related probes…";
      status.className = "small";
      status.textContent = `Asking the judge for ${count} related probe${count === 1 ? "" : "s"} (DeepTeam / OWASP techniques)…`;
      try {
        const target = currentTarget();
        const response = await api(`/api/metrics/${encodeURIComponent(metric.key)}/generate-similar-probes`, {
          method: "POST",
          body: {
            question: fields.question.value.trim(),
            count,
            target_id: target ? target.id : null,
          },
        });
        const n = response.generated || 0;
        await settings.set("metricsChangedAt", Date.now());
        // Show the list with the new probes and a short status on the metric row.
        state.openCases = { key: metric.key, mode: "list" };
        $("status").textContent = n
          ? `Generated ${n} related probe${n === 1 ? "" : "s"} for ${metric.title}. Your seed question was not saved — open Add again to save it too.`
          : `No new probes for ${metric.title}.`;
        await loadMetrics().catch(() => {});
        return;
      } catch (e) {
        status.className = "small status-error";
        status.textContent = "Failed to generate: " + e.message;
        aiHelper.textContent = "Generate similar probes with AI";
      } finally {
        aiHelper.disabled = false;
        countInput.disabled = false;
      }
    });
    aiBlock = el("div", { className: "case-ai-block" },
      el("p", { className: "muted small" },
        "Optional: generate related security probes from your seed question (uses judge tokens)."),
      el("div", { className: "row case-ai-row" },
        el("label", { className: "case-count-label small" },
          "No. of test cases",
          countInput),
        aiHelper));
  }

  const form = el("form", { className: "case-form" },
    el("p", { className: "muted small" }, `Adds to the ${where}; other metrics reading it run it too. Shipped sets reset when the panel reloads.`),
    ...rows,
    aiBlock,
    el("div", { className: "row" }, save,
      el("button", { type: "button", onclick: () => { state.openCases = { key: null, mode: null }; renderThresholds(); } }, "Cancel")),
    status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const target = currentTarget();
    save.disabled = true;
    status.textContent = "Saving…";
    try {
      await api(`/api/metrics/${encodeURIComponent(metric.key)}/cases`, {
        method: "POST",
        body: {
          target_id: target ? target.id : null,
          question: fields.question ? fields.question.value : "",
          expected_answer: fields.expected ? fields.expected.value : "",
          context: lines(fields.context),
          expected_tools: fields.tools ? fields.tools.value.split(",").map((s) => s.trim()).filter(Boolean) : [],
          name: fields.name ? fields.name.value : "",
          user_turns: lines(fields.turns),
          chatbot_role: fields.role ? fields.role.value : "",
        },
      });
      // Show the list with the new case; counts change for every metric reading the set.
      state.openCases = { key: metric.key, mode: "list" };
      await settings.set("metricsChangedAt", Date.now());
      // Auto-scroll to the newly added case list
      setTimeout(() => {
        const panel = document.querySelector(`[data-metric-key="${metric.key}"]`);
        if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 200);
      if (data.add === "golden") loadGoldens().catch(() => {});
      if (data.add === "conversation") loadConversations().catch(() => {});
    } catch (error) {
      save.disabled = false;
      status.textContent = error.message;
    }
  });
  return form;
}

// A group heading with a tick that selects or clears the whole group.
function groupCheckRow(group) {
  const keys = group.metrics.map((m) => m.key);
  const ticked = keys.filter((key) => state.checked.has(key)).length;
  const tick = el("input", {
    type: "checkbox",
    checked: ticked === keys.length,
    "aria-label": `Run all of ${group.label}`,
  });
  tick.indeterminate = ticked > 0 && ticked < keys.length;
  tick.addEventListener("change", async () => {
    const next = new Set(state.checked);
    for (const key of keys) {
      if (tick.checked) next.add(key);
      else next.delete(key);
    }
    await setChecked(next);
  });
  return el("label", { className: "threshold-group" }, tick, `${group.label} (${ticked}/${keys.length})`);
}

// Quick select: each acts on the chosen framework's metrics only.
$("check-all-metrics").addEventListener("click", () => setScopeChecked(scopedMetrics().map((m) => m.key)));
$("check-golden-metrics").addEventListener("click", () => {
  const goldenKeys = scopedMetrics()
    .filter(m => !m.kind || m.kind !== "conversation")
    .map(m => m.key);
  setScopeChecked(goldenKeys);
});
$("check-scenario-metrics").addEventListener("click", () => {
  const scenarioKeys = scopedMetrics()
    .filter(m => m.kind === "conversation" || (m.dataset_name || "").includes("conversation"))
    .map(m => m.key);
  setScopeChecked(scenarioKeys);
});
$("check-own-question-metrics").addEventListener("click", () => {
  const ownKeys = scopedMetrics()
    .filter(m => m.custom_id || m.ui_category === "geval" || m.source === "deepteam")
    .map(m => m.key);
  setScopeChecked(ownKeys);
});
$("check-no-metrics").addEventListener("click", () => setScopeChecked([]));

$("threshold-env").addEventListener("change", async () => {
  const env = $("threshold-env").value;
  if (env === "custom") {
    $("threshold-details").open = true;
    await settings.set("thresholdEnv", "custom");
    return;
  }
  // A preset sets every metric; "default" clears them back to the catalog values.
  const thresholds = env === "default"
    ? {}
    : Object.fromEntries(state.metrics.map((m) => [m.key, m.presets[env]]));
  await settings.set("thresholds", thresholds);
  await settings.set("thresholdEnv", env);
  await renderThresholds();
});
$("open-dashboard").addEventListener("click", openDashboard);
$("open-advanced").addEventListener("click", openAdvanced);
$("open-deepteam").addEventListener("click", openDeepTeam);
// Inside the web app's two-pane page the dashboard is already beside the panel.
if (window.parent !== window) $("open-dashboard").hidden = true;
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderLatest());

settings.onChange((changes) => {
  const target = currentTarget();
  const run = changes.lastRun && changes.lastRun.newValue;
  if (run && target && run.targetId === target.id) renderLatest();
  if ((changes.freshSinceRunId || changes.runsChangedAt) && target) renderLatest();
  const started = changes.startedJob && changes.startedJob.newValue;
  if (started && target && started.targetId === target.id) followActiveJob(target).catch(() => {});
  if (changes.selectedTargetId && changes.selectedTargetId.newValue !== (target ? target.id : null)) {
    loadTargets(changes.selectedTargetId.newValue).catch((error) => {
      $("status").textContent = error.message;
    });
  }
  if ((changes.thresholds || changes.thresholdEnv || changes.checkedMetrics || changes.metricFramework)
      && state.metrics.length) {
    renderThresholds();
  }
  // A metric was added, edited or deleted on the dashboard or Advanced tools.
  if (changes.metricsChangedAt) loadMetrics().catch(() => {});
  if (changes.thresholds && target) renderLatest();
  if (changes.casesPerRun) $("run-mode").value = String(changes.casesPerRun.newValue || "");
});

async function loadEverything() {
  await refreshStatus();
  if (!state.backendUp) return false;
  try {
    // Golden edits and deletes last one session: every fresh load of the
    // panel starts again from the shipped golden answers.
    await api("/api/goldens/reset", { method: "POST", body: {} });
    // Scores start fresh too: earlier runs stay stored, shown under History.
    const session = await api("/api/session/start", { method: "POST", body: {} });
    await settings.set("freshSinceRunId", session.since_id);
    await settings.set("lastRun", null);
    await loadJudgeSettings();
    await loadConfidentSettings();
    // Optional, so a backend without Langfuse support never stops the panel loading.
    await loadLangfuseSettings().catch((error) => showLangfuseResult(false, error.message));
    await loadMetrics();
    await loadTargets();
    return true;
  } catch (error) {
    $("status").textContent = error.message;
    return false;
  }
}

// --- Judge settings: the key, model and base URL the judge uses -------------
function providerFor(baseUrl) {
  const option = [...$("judge-provider").options].find((o) => o.dataset.baseUrl === baseUrl);
  return option ? option.value : "custom";
}

async function loadJudgeSettings() {
  const current = await api("/api/judge/settings");
  $("judge-provider").value = providerFor(current.base_url);
  $("judge-model").value = current.model;
  $("judge-base-url").value = current.base_url;
  $("judge-key").value = "";
  const source = { panel: "saved here", env: "from the .env file" }[current.key_source];
  $("judge-key").placeholder = current.has_key ? `${current.key_hint} (leave empty to keep it)` : "paste the judge's API key";
  $("judge-key-hint").textContent = current.has_key ? `Key ${current.key_hint}, ${source}.` : "No key yet: the judge cannot run.";
  $("judge-clear-key").classList.toggle("hidden", current.key_source !== "panel");
  $("judge-summary").textContent = current.has_key ? `· ${current.model}` : "· key missing";
  // Nothing can be judged without a key, so show the form straight away.
  if (!current.has_key) $("judge-settings").open = true;
}

function judgeFormBody() {
  return {
    api_key: $("judge-key").value.trim(),
    model: $("judge-model").value.trim(),
    base_url: $("judge-base-url").value.trim(),
  };
}

function showJudgeResult(ok, text) {
  const el = $("judge-result");
  el.className = ok ? "small status-pass" : "small status-error";
  el.textContent = text;
}

$("judge-provider").addEventListener("change", () => {
  const option = $("judge-provider").selectedOptions[0];
  if (option.dataset.baseUrl) {
    $("judge-base-url").value = option.dataset.baseUrl;
    $("judge-model").value = option.dataset.model;
  }
});

$("judge-test").addEventListener("click", async () => {
  showJudgeResult(true, "Asking the judge…");
  try {
    const body = judgeFormBody();
    // Ensure we always send a clean object (prevents undefined response)
    const test = await api("/api/judge/settings/test", {
      method: "POST",
      body: {
        api_key: body.api_key || "",
        model: body.model || "",
        base_url: body.base_url || ""
      }
    });
    showJudgeResult(
      test.ok,
      test.ok
        ? `Works: ${test.model || "the judge"} replied.`
        : (test.error || "Unknown error")
    );
  } catch (error) {
    showJudgeResult(false, error.message || String(error));
  }
});

$("judge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const body = judgeFormBody();
    await api("/api/judge/settings", { method: "PUT", body });
    await loadJudgeSettings();
    await refreshStatus();
    showJudgeResult(true, "Saved. The next run uses these settings.");
  } catch (error) {
    showJudgeResult(false, error.message || String(error));
  }
});

armConfirm($("judge-clear-key"), async () => {
  try {
    const body = judgeFormBody();
    await api("/api/judge/settings", {
      method: "PUT",
      body: { ...body, api_key: "", clear_key: true }
    });
    await loadJudgeSettings();
    await refreshStatus();
    showJudgeResult(true, "Saved key removed.");
  } catch (error) {
    showJudgeResult(false, error.message || String(error));
  }
});

// --- Confident AI: optional copy of each run in the Confident AI portal -------
// The key stays on the backend; the panel only ever sees its last 4 characters.
async function loadConfidentSettings() {
  const current = await api("/api/confident/settings");
  const source = { panel: "saved here", env: "from the .env file" }[current.key_source];
  $("confident-key").value = "";
  $("confident-key").placeholder = current.has_key ? `${current.key_hint} (leave empty to keep it)` : "paste your Confident AI API key";
  $("confident-key-hint").textContent = current.has_key
    ? `Key ${current.key_hint}, ${source}${current.project ? `, project '${current.project}'` : ""}.`
    : "No key: runs are not sent anywhere.";
  if (current.project) $("confident-project").value = current.project;
  $("confident-auto").checked = current.has_key ? current.auto : true;
  $("confident-auto").disabled = false;
  $("confident-clear-key").classList.toggle("hidden", current.key_source !== "panel");
  $("confident-summary").textContent = !current.has_key ? "· optional, off"
    : current.auto ? "· sending every run" : "· on request";
}

function showConfidentResult(ok, text) {
  $("confident-result").className = ok ? "small status-pass" : "small status-error";
  $("confident-result").textContent = text;
}

$("confident-reports").addEventListener("click", () => openConfidentReports());

$("confident-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const saved = await api("/api/confident/settings", { method: "PUT",
      body: { api_key: $("confident-key").value.trim(), project: $("confident-project").value.trim(),
        auto: $("confident-auto").checked } });
    await loadConfidentSettings();
    showConfidentResult(true, !saved.has_key ? "No key saved yet."
      : saved.auto ? "Saved. Each finished run will also appear in Confident AI." : "Saved. Send runs from a run's Overview page.");
  } catch (error) {
    showConfidentResult(false, error.message);
  }
});

armConfirm($("confident-clear-key"), async () => {
  try {
    await api("/api/confident/settings", { method: "PUT", body: { clear_key: true, auto: false } });
    await loadConfidentSettings();
    showConfidentResult(true, "Saved key removed.");
  } catch (error) {
    showConfidentResult(false, error.message);
  }
});

// --- Langfuse: optional trace of each run in Langfuse (cloud or self-hosted) -----
// The keys stay on the backend; the panel only ever sees their last 4 characters.
async function loadLangfuseSettings() {
  const current = await api("/api/langfuse/settings");
  const source = { panel: "saved here", env: "from the .env file" }[current.key_source];
  $("langfuse-public-key").value = "";
  $("langfuse-secret-key").value = "";
  $("langfuse-public-key").placeholder = current.has_keys ? `${current.public_key_hint} (leave empty to keep it)` : "pk-lf-…";
  $("langfuse-secret-key").placeholder = current.has_keys ? `${current.secret_key_hint} (leave empty to keep it)` : "sk-lf-…";
  $("langfuse-key-hint").textContent = current.has_keys
    ? `Keys ${current.public_key_hint} and ${current.secret_key_hint}, ${source}; traces go to ${current.host}.`
    : "No keys: runs are not sent anywhere.";
  $("langfuse-host").value = current.host || "";
  $("langfuse-auto").checked = current.has_keys ? current.auto : true;
  $("langfuse-send-latest").disabled = !current.has_keys;
  $("langfuse-clear-keys").classList.toggle("hidden", current.key_source !== "panel");
  $("langfuse-summary").textContent = !current.has_keys ? "· optional, off"
    : current.auto ? "· sending every run" : "· on request";
}

function showLangfuseResult(ok, text, link) {
  const box = $("langfuse-result");
  box.className = ok ? "small status-pass" : "small status-error";
  box.textContent = text;
  if (link) {
    const anchor = document.createElement("a");
    anchor.href = link;
    anchor.target = "_blank";
    anchor.rel = "noopener";
    anchor.textContent = "Open the trace ↗";
    box.append(" ", anchor);
  }
}

$("langfuse-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const saved = await api("/api/langfuse/settings", { method: "PUT",
      body: { public_key: $("langfuse-public-key").value.trim(), secret_key: $("langfuse-secret-key").value.trim(),
        host: $("langfuse-host").value.trim(), auto: $("langfuse-auto").checked } });
    await loadLangfuseSettings();
    showLangfuseResult(true, !saved.has_keys ? "No keys saved yet."
      : saved.auto ? "Saved. Each finished run will also appear in Langfuse." : "Saved. Send a run with \"Send latest run\".");
  } catch (error) {
    showLangfuseResult(false, error.message);
  }
});

// Sends the selected chatbot's newest finished run: a quick check that the keys
// and host work, and the way to send runs while automatic sending is off.
$("langfuse-send-latest").addEventListener("click", async () => {
  const target = currentTarget();
  if (!target) {
    showLangfuseResult(false, "Pick a chatbot first: its latest finished run is sent.");
    return;
  }
  const button = $("langfuse-send-latest");
  button.disabled = true;
  try {
    const scans = await api(`/api/targets/${target.id}/scans`);
    const latest = scans.find((scan) => scan.status !== "queued" && scan.status !== "running");
    if (!latest) {
      showLangfuseResult(false, `${target.name} has no finished run yet.`);
      return;
    }
    showLangfuseResult(true, `Sending run ${latest.version} of ${target.name}…`);
    const sent = await api(`/api/scans/${latest.job_id}/langfuse`, { method: "POST", body: {} });
    showLangfuseResult(true, `Run ${latest.version} of ${target.name} is in Langfuse${sent.warning ? ` (${sent.warning})` : ""}.`,
      sent.url);
  } catch (error) {
    showLangfuseResult(false, error.message);
  } finally {
    button.disabled = false;
  }
});

armConfirm($("langfuse-clear-keys"), async () => {
  try {
    await api("/api/langfuse/settings", { method: "PUT", body: { clear_keys: true, auto: false } });
    await loadLangfuseSettings();
    showLangfuseResult(true, "Saved keys removed.");
  } catch (error) {
    showLangfuseResult(false, error.message);
  }
});

(async function init() {
  let loaded = await loadEverything();
  // Keep checking, so the panel recovers once the backend is started.
  setInterval(async () => {
    if (loaded) await refreshStatus();
    else loaded = await loadEverything();
  }, 10000);
})();
