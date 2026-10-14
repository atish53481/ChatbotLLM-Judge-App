// Side panel: pick or add a chatbot, manage its golden answers, run the judge,
// and see the latest scores. The dashboard tab follows the same selection
// through chrome.storage.

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
  // Metric keys ticked to run (saved as "checkedMetrics"; all by default).
  checked: new Set(),
  runControl: null,
};

function currentTarget() {
  const id = Number($("target-select").value);
  return state.targets.find((t) => t.id === id) || null;
}

function updateRunButton() {
  const button = $("run-button");
  const ready = state.judgeUp && currentTarget() !== null;
  const count = state.checked.size;
  button.textContent = state.runControl !== null ? "Running…"
    : count ? `Run judge (${count} metric${count === 1 ? "" : "s"})` : "Run judge";
  // The status refresh calls this every 10s; a run in flight keeps the button off.
  button.disabled = !ready || state.runControl !== null || count === 0;
  if (ready && count === 0) button.title = "Tick at least one metric";
  else if (ready) button.title = "";
  else if (state.judgeUp) button.title = "Add or pick a chatbot first";
  else button.title = "Judge not configured: add its API key in Judge settings";
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
    state.targets.map((t) => ({ value: t.id, label: targetLabel(t) })),
    selected,
  );
  await onTargetChanged();
}

async function onTargetChanged() {
  const target = currentTarget();
  await settings.set("selectedTargetId", target ? target.id : null);
  $("target-theme").textContent = target ? `Golden set: ${themeOf(target)}` : "";
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
  // A run started earlier (here before the panel closed, or from the dashboard)
  // is still going: show its progress and Stop again.
  const job = target ? await activeJob(target) : null;
  if (job && state.runControl === null) {
    const titles = new Map(state.metrics.map((m) => [m.key, m.title]));
    watchRun(target, new Map(), titles, (onProgress, control) => followJob(target, job.id, onProgress, control),
      (job.metric_keys || []).length);
  }
}

// The target form doubles as the editor; resetTargetForm returns it to "add".
function resetTargetForm() {
  state.editingTarget = null;
  $("target-form").reset();
  $("target-submit").textContent = "Save chatbot";
  $("target-cancel").classList.add("hidden");
  $("target-form-summary").textContent = "Add a chatbot";
  $("target-headers-hint").classList.add("hidden");
  $("curl-import").open = true;
  $("curl-status").textContent = "";
  $("llm-status").textContent = "";
  $("target-test-result").textContent = "";
}

function startEditTarget(target) {
  const config = target.config;
  state.editingTarget = target.id;
  $("target-name").value = target.name;
  $("target-method").value = config.method || "POST";
  $("target-url").value = config.url || "";
  // Header values are masked by the backend; empty keeps the stored ones.
  $("target-headers").value = "";
  $("target-headers-hint").classList.toggle("hidden", !config.headers || !Object.keys(config.headers).length);
  $("target-body").value = config.body_template || "";
  $("target-response-path").value = config.response_path || "";
  $("target-context-path").value = config.context_path || "";
  $("target-persona").value = config.persona || "";
  $("target-probe-set").value = config.probe_set || "generic";
  $("target-history-path").value = config.history_path || "";
  $("target-max-length").value = config.max_message_length ?? "";
  $("target-send-delay").value = config.send_delay || "";
  $("target-check-replies").checked = config.check_replies !== false;
  $("target-theme-input").value = themeOf(target);
  $("curl-import").open = false;
  $("curl-status").textContent = "";
  $("llm-import").open = false;
  $("target-test-result").textContent = "";
  $("target-submit").textContent = "Save changes";
  $("target-cancel").classList.remove("hidden");
  $("target-form-summary").textContent = "Edit chatbot";
  $("target-form-error").textContent = "";
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
    const rows = await api(`/api/runs/latest?target_id=${target.id}`);
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
    persona: value("target-persona"),
    probe_set: $("target-probe-set").value,
    history_path: value("target-history-path"),
    theme: value("target-theme-input") || DEFAULT_THEME,
    max_message_length: value("target-max-length") ? Number(value("target-max-length")) : null,
    send_delay: value("target-send-delay") ? Number(value("target-send-delay")) : 0,
    check_replies: $("target-check-replies").checked,
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

$("target-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("target-form-error").textContent = "";
  const editingId = state.editingTarget;
  try {
    const body = { name: $("target-name").value.trim(), type: "http", config: buildTargetConfig() };
    const saved = editingId
      ? await api(`/api/targets/${editingId}`, { method: "PUT", body })
      : await api("/api/targets", { method: "POST", body });
    resetTargetForm();
    $("add-target").open = false;
    await loadTargets(saved.id);
  } catch (error) {
    $("target-form-error").textContent = error.message;
  }
});

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
    $("target-history-path").value = parsed.history_path || "";
    if (!$("target-name").value.trim()) $("target-name").value = new URL(parsed.url).host;

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
    $("target-history-path").value = filled.history_path || "";
    if (!$("target-name").value.trim()) {
      $("target-name").value = `${$("llm-model").value.trim()} (LLM)`.slice(0, 80);
    }
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

$("golden-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("golden-form-error").textContent = "";
  const target = currentTarget();
  if (!target) return;
  const context = $("golden-context").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const categories = $("golden-categories").value.split(",").map((s) => s.trim()).filter(Boolean);
  const editing = state.editingGolden;
  const body = {
    theme: themeOf(target),
    question: $("golden-question").value.trim(),
    expected_answer: $("golden-answer").value.trim(),
    context,
    categories,
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

$("golden-cancel").addEventListener("click", () => {
  resetGoldenForm();
  $("golden-form-error").textContent = "";
  $("add-golden").open = false;
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
  } catch (error) {
    $("conversation-form-error").textContent = error.message;
  }
});

$("conversation-cancel").addEventListener("click", () => {
  resetConversationForm();
  $("add-conversation").open = false;
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
    await clearCaseDetails(target.id);
    await renderLatest();
  } catch (error) {
    $("status").textContent = error.message;
  }
});

function renderSkippedRow(line, title, theme, reason) {
  line.className = "run-row skipped";
  line.replaceChildren(
    el("span", { className: "run-row-title" }, title),
    el("span", { className: "run-row-chip muted", title: reason || "" },
      reason ? "skipped: not applicable to this chatbot" : `skipped: no cases in '${theme}'`),
  );
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
  // Ticked metrics, in catalog order.
  const keys = state.metrics.filter((m) => state.checked.has(m.key)).map((m) => m.key);
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
    renderSkippedRow(lines.get(key), titles.get(key) || key, themeOf(target), metric && metric.unavailable);
  }
  const runnable = keys.filter((k) => !empty.has(k));
  await watchRun(target, lines, titles, (onProgress, control) => runMetrics(target, runnable, onProgress, control),
    runnable.length);
});

// Shows a run in the results list, with Stop, until it ends: a new job, or one
// already running (started before this panel was opened). lines holds the rows
// already shown; start(onProgress, control) runs or follows the job.
async function watchRun(target, lines, titles, start, count) {
  $("run-button").disabled = true;
  $("run-button").textContent = "Running…";
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
  let current = "";
  let queued = false;
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
      renderRunRow(lines.get(key), titles.get(key) || key, result, progress);
      if (result) finished.set(key, result);
      else {
        queued = Boolean(progress && progress.queued);
        current = titles.get(key) || key;
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
    $("stop-button").classList.add("hidden");
    updateRunButton();
  }
  showRunBanner(summary);
  announceRun(summary.text);
}

function showRunBanner({ tone, text }) {
  $("run-banner-text").textContent = text;
  $("run-banner").className = `run-banner run-banner-${tone}`;
}

// Screen readers hear the start and the end once, not every second's update.
function announceRun(text) {
  $("run-announce").textContent = text;
}

$("run-banner-show").addEventListener("click", () => {
  $("run-results").scrollIntoView({ behavior: "smooth", block: "center" });
});
$("run-banner-close").addEventListener("click", () => {
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

// --- Metrics to run: a tick and a threshold per metric ------------------------
async function setChecked(keys) {
  await settings.set("checkedMetrics", [...keys]);
}

async function renderThresholds() {
  const env = await settings.get("thresholdEnv", "default");
  const thresholds = await settings.get("thresholds", {});
  const saved = await settings.get("checkedMetrics", null);
  $("check-consistency").checked = await settings.get("checkConsistency", false);
  const known = new Set(state.metrics.map((m) => m.key));
  state.checked = new Set((saved ?? [...known]).filter((key) => known.has(key)));
  $("metric-check-count").textContent = `(${state.checked.size} of ${state.metrics.length} ticked)`;
  updateRunButton();
  fillSelect($("threshold-env"), THRESHOLD_ENVS.map((e) => ({ value: e.key, label: e.label })), env);
  $("threshold-table").replaceChildren(
    ...metricGroups(state.metrics).flatMap((group) => [
      groupCheckRow(group),
      ...group.metrics.map((metric) => {
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
        return el(
          "label",
          {
            className: `threshold-row${state.checked.has(metric.key) ? "" : " unchecked"}`,
            title: metric.description || "",
          },
          tick,
          el(
            "span",
            {},
            metric.title,
            hasNoCases(metric)
              ? el("small", {
                  className: "no-cases",
                  title: metric.unavailable || `No cases for this metric in the golden set '${themeOf(currentTarget())}': a run skips it.`,
                }, " · 0 cases")
              : null,
          ),
          el("span", { className: "direction" }, comparator(metric.direction)),
          input,
        );
      }),
    ]),
  );
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

$("check-all-metrics").addEventListener("click", () => setChecked(state.metrics.map((m) => m.key)));
$("check-no-metrics").addEventListener("click", () => setChecked([]));

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
// Inside the web app's two-pane page the dashboard is already beside the panel.
if (!IN_EXTENSION && window.parent !== window) $("open-dashboard").hidden = true;
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderLatest());

settings.onChange((changes) => {
  const target = currentTarget();
  const run = changes.lastRun && changes.lastRun.newValue;
  if (run && target && run.targetId === target.id) renderLatest();
  if (changes.selectedTargetId && changes.selectedTargetId.newValue !== (target ? target.id : null)) {
    loadTargets(changes.selectedTargetId.newValue).catch((error) => {
      $("status").textContent = error.message;
    });
  }
  if ((changes.thresholds || changes.thresholdEnv || changes.checkedMetrics) && state.metrics.length) {
    renderThresholds();
  }
  if (changes.thresholds && target) renderLatest();
});

async function loadEverything() {
  await refreshStatus();
  if (!state.backendUp) return false;
  try {
    // Golden edits and deletes last one session: every fresh load of the
    // panel starts again from the shipped golden answers.
    await api("/api/goldens/reset", { method: "POST", body: {} });
    await loadJudgeSettings();
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
  $("judge-result").className = ok ? "small status-pass" : "small status-error";
  $("judge-result").textContent = text;
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
    const test = await api("/api/judge/settings/test", { method: "POST", body: judgeFormBody() });
    showJudgeResult(test.ok, test.ok ? `Works: ${test.model} replied.` : test.error);
  } catch (error) {
    showJudgeResult(false, error.message);
  }
});

$("judge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/judge/settings", { method: "PUT", body: judgeFormBody() });
    await loadJudgeSettings();
    await refreshStatus();
    showJudgeResult(true, "Saved. The next run uses these settings.");
  } catch (error) {
    showJudgeResult(false, error.message);
  }
});

armConfirm($("judge-clear-key"), async () => {
  try {
    await api("/api/judge/settings", { method: "PUT", body: { ...judgeFormBody(), api_key: "", clear_key: true } });
    await loadJudgeSettings();
    await refreshStatus();
    showJudgeResult(true, "Saved key removed.");
  } catch (error) {
    showJudgeResult(false, error.message);
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
