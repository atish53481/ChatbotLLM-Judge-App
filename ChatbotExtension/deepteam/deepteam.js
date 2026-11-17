// DeepTeam page: DeepTeam's own RedTeamer runs, risk assessments, guardrails
// and the YAML/CLI export for the chosen chatbot (backend/redteam/, /api/deepteam/*).
const $ = (id) => document.getElementById(id);

const state = {
  targets: [],
  target: null,
  options: null,
  section: "redteam",
  activeRun: null,
  pollTimer: null,
};

function setStatus(text, isError = false) {
  $("status").textContent = text || "";
  $("status").classList.toggle("status-error", Boolean(isError));
}

async function guarded(fn) {
  try {
    setStatus("");
    await fn();
  } catch (error) {
    setStatus(error.message || String(error), true);
  }
}

const pct = (v) => (typeof v === "number" ? `${Math.round(v * 100)}%` : "n/a");
const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);

// --- Navigation and chatbot ---------------------------------------------------
const loaders = { runs: () => loadRuns() };

function showSection(name) {
  state.section = name;
  for (const button of document.querySelectorAll(".adv-nav button")) {
    if (button.dataset.section === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll(".adv-section")) {
    section.classList.toggle("hidden", section.id !== `section-${name}`);
  }
  if (loaders[name]) guarded(loaders[name]);
}

for (const button of document.querySelectorAll(".adv-nav button")) {
  button.addEventListener("click", () => showSection(button.dataset.section));
}

async function loadTargets() {
  state.targets = await fetchTargets();
  const wanted = await settings.get("selectedTargetId", null);
  state.target = state.targets.find((t) => t.id === wanted) || state.targets[0] || null;
  fillSelect($("target-select"), state.targets.map((t) => ({ value: t.id, label: targetLabel(t) })),
    state.target ? state.target.id : "");
  if (!state.target) setStatus("Add a chatbot in the side panel first.", true);
  const persona = state.target && state.target.config && state.target.config.persona;
  if (persona) $("dt-purpose").placeholder = `Defaults to the chatbot's role: ${persona}`;
}

$("target-select").addEventListener("change", () => guarded(async () => {
  state.target = state.targets.find((t) => String(t.id) === $("target-select").value) || null;
  await settings.set("selectedTargetId", state.target ? state.target.id : null);
  $("dt-run-detail").replaceChildren();
  showSection(state.section);
}));

// --- Red team form --------------------------------------------------------------
function renderOptions() {
  const o = state.options;
  $("dt-version").textContent = o.version ? `deepteam ${o.version}` : "";
  // Vulnerabilities, grouped by DeepTeam category; each opens to its types.
  const byCategory = new Map();
  for (const v of o.vulnerabilities) {
    const category = v.category || "Other";
    if (!byCategory.has(category)) byCategory.set(category, []);
    byCategory.get(category).push(v);
  }
  const nodes = [];
  for (const [category, vulns] of byCategory) {
    nodes.push(el("p", { className: "dt-category" }, category));
    for (const v of vulns) {
      const tick = el("input", { type: "checkbox", value: v.class, className: "dt-vuln-tick",
        "aria-label": `Test ${v.title}` });
      tick.addEventListener("change", updateVulnCount);
      tick.addEventListener("click", (event) => event.stopPropagation());
      nodes.push(el("details", { className: "dt-vuln", title: v.description },
        el("summary", {}, tick, el("span", {}, v.title)),
        el("div", { className: "dt-types" },
          ...v.types.map((t) => el("label", { className: "row" },
            el("input", { type: "checkbox", value: t, className: "dt-type", "data-vuln": v.class }), ` ${t}`)))));
    }
  }
  $("dt-vulns").replaceChildren(...nodes);
  const attack = (a) => el("label", {}, el("input", { type: "checkbox", value: a.class, className: "dt-attack" }),
    ` ${a.class.replace(/([a-z])([A-Z])/g, "$1 $2")}`);
  $("dt-attacks-single").replaceChildren(...o.attacks.filter((a) => a.kind === "single").map(attack));
  $("dt-attacks-multi").replaceChildren(...o.attacks.filter((a) => a.kind === "multi").map(attack));
  fillSelect($("dt-framework"), o.frameworks.map((f) => ({ value: f.key, label: f.label })), "owasp_llm");
  fillSelect($("dt-exposure"), o.exposures.map((e) => ({ value: e, label: e })), "MEDIUM");
  $("dt-per-type").max = String(o.max_attacks_per_type || 10);
  const guard = (g) => el("label", {}, el("input", { type: "checkbox", value: g.key }), ` ${g.label}`);
  $("g-input-guards").replaceChildren(...o.guards.filter((g) => g.input).map(guard));
  $("g-output-guards").replaceChildren(...o.guards.filter((g) => g.output).map(guard));
  updateVulnCount();
  updateFrameworkBox();
}

function updateVulnCount() {
  const n = document.querySelectorAll(".dt-vuln-tick:checked").length;
  $("dt-vuln-count").textContent = `(${n} of ${state.options.vulnerabilities.length} ticked)`;
}

function setAllVulns(on) {
  for (const tick of document.querySelectorAll(".dt-vuln-tick")) tick.checked = on;
  updateVulnCount();
}
$("dt-vuln-all").addEventListener("click", () => setAllVulns(true));
$("dt-vuln-none").addEventListener("click", () => setAllVulns(false));

function mode() {
  return document.querySelector('input[name="dt-mode"]:checked').value;
}

function updateFrameworkBox() {
  const framework = mode() === "framework";
  $("dt-pick").classList.toggle("hidden", framework);
  $("dt-framework-box").classList.toggle("hidden", !framework);
  const picked = state.options && state.options.frameworks.find((f) => f.key === $("dt-framework").value);
  $("dt-num-attacks-label").classList.toggle("hidden", !(picked && picked.kind === "dataset"));
}
for (const radio of document.querySelectorAll('input[name="dt-mode"]')) radio.addEventListener("change", updateFrameworkBox);
$("dt-framework").addEventListener("change", updateFrameworkBox);
$("dt-engine").addEventListener("change", () => $("dt-engine-box").classList.toggle("hidden", !$("dt-engine").checked));

function runBody() {
  const body = {
    target_id: state.target.id,
    attacks_per_vulnerability_type: Number($("dt-per-type").value) || 1,
    exposure: $("dt-exposure").value || "MEDIUM",
    run_all_attacks: $("dt-run-all").checked,
    target_purpose: $("dt-purpose").value.trim(),
    attack_engine: $("dt-engine").checked,
    variations: Number($("dt-variations").value) || 1,
    generation_guidelines: lines($("dt-guidelines").value),
  };
  if (mode() === "framework") {
    body.framework = $("dt-framework").value;
    body.num_attacks = Number($("dt-num-attacks").value) || 15;
  } else {
    body.vulnerabilities = [...document.querySelectorAll(".dt-vuln-tick:checked")].map((tick) => ({
      class: tick.value,
      types: [...document.querySelectorAll(`.dt-type[data-vuln="${tick.value}"]:checked`)].map((t) => t.value),
    }));
    body.attacks = [...document.querySelectorAll(".dt-attack:checked")].map((a) => a.value);
  }
  return body;
}

$("dt-run").addEventListener("click", () => guarded(async () => {
  if (!state.target) throw new Error("Add a chatbot in the side panel first.");
  const body = runBody();
  if (!body.framework && !body.vulnerabilities.length) throw new Error("Tick at least one vulnerability, or pick a framework.");
  const { run_id: runId } = await api("/api/deepteam/runs", { method: "POST", body });
  state.activeRun = runId;
  $("dt-run").disabled = true;
  $("dt-stop").disabled = false;
  $("dt-live-result").replaceChildren();
  pollRun(runId);
}));

$("dt-stop").addEventListener("click", () => guarded(async () => {
  if (state.activeRun == null) return;
  await api(`/api/deepteam/runs/${state.activeRun}/cancel`, { method: "POST" });
  $("dt-run-status").textContent = "Stopping after the current call…";
}));

function pollRun(runId) {
  clearTimeout(state.pollTimer);
  const started = Date.now();
  const tick = async () => {
    try {
      const run = await api(`/api/deepteam/runs/${runId}`);
      if (run.status === "queued" || run.status === "running") {
        const seconds = Math.round((Date.now() - started) / 1000);
        $("dt-run-status").textContent = `Run #${runId} ${run.status}… ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
        state.pollTimer = setTimeout(tick, 2000);
        return;
      }
      $("dt-run").disabled = false;
      $("dt-stop").disabled = true;
      state.activeRun = null;
      $("dt-run-status").textContent = `Run #${run.id}: ${run.status}`;
      const reportButton = el("button", { type: "button", className: "primary" }, "Open DeepTeam risk assessment report");
      reportButton.addEventListener("click", () => openReport(run.id));
      $("dt-live-result").replaceChildren(
        run.status === "error" ? el("span") : el("div", { className: "row" }, reportButton), renderRun(run));
    } catch (error) {
      setStatus(error.message, true);
      state.pollTimer = setTimeout(tick, 4000);
    }
  };
  tick();
}

// --- Results (DeepTeam's risk assessment) ---------------------------------------
// The full report: the Risk Assessment page in DeepTeam mode (risk/risk.html?deepteam=<id>).
function openReport(runId) {
  const target = state.target ? `&target=${state.target.id}` : "";
  window.open(`../risk/risk.html?deepteam=${runId}${target}#report`, `deepteam-report-${runId}`);
}
function stat(value, label) {
  return el("div", { className: "dt-stat" }, el("b", {}, String(value)), el("span", {}, label));
}

function verdictPill(tc) {
  if (tc.error) return el("span", { className: "dt-pill dt-err", title: tc.error }, "error");
  return Number(tc.score) >= 1
    ? el("span", { className: "dt-pill dt-pass" }, "resisted")
    : el("span", { className: "dt-pill dt-fail" }, "vulnerable");
}

function table(headers, rows) {
  return el("div", { className: "dt-table-wrap" }, el("table", { className: "dt-table" },
    el("thead", {}, el("tr", {}, ...headers.map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, ...rows)));
}

function breakdown(title, buckets) {
  const rows = Object.entries(buckets || {})
    .sort((a, b) => (a[1].pass_rate ?? 2) - (b[1].pass_rate ?? 2))
    .map(([name, b]) => el("tr", {}, el("td", {}, name), el("td", {}, pct(b.pass_rate)),
      el("td", {}, String(b.passing)), el("td", {}, String(b.failing)), el("td", {}, String(b.errored))));
  return el("div", { className: "card stack" }, el("h3", {}, title),
    table(["Name", "Pass rate", "Resisted", "Vulnerable", "Errored"], rows));
}

function conversation(tc) {
  if (Array.isArray(tc.turns) && tc.turns.length) {
    return tc.turns.map((t) => `${t.role}: ${t.content}`).join("\n\n");
  }
  return null;
}

function renderRun(run) {
  if (run.status === "error") {
    return el("div", { className: "card" }, el("p", { className: "status-error" }, `Run failed: ${run.error || "unknown error"}`));
  }
  if (!run.result) return el("p", { className: "muted" }, `Status: ${run.status}`);
  const s = run.result.summary || {};
  const cases = run.result.test_cases || [];
  const rows = cases.map((tc) => {
    const convo = conversation(tc);
    return el("tr", {},
      el("td", {}, verdictPill(tc)),
      el("td", {}, tc.vulnerability || ""), el("td", {}, String(tc.vulnerability_type || "")),
      el("td", {}, tc.attack_method || ""),
      el("td", { className: "dt-text" }, convo || tc.input || ""),
      el("td", { className: "dt-text" }, convo ? "" : (tc.actual_output || "")),
      el("td", {}, tc.cvss_score != null ? Number(tc.cvss_score).toFixed(1) : ""),
      el("td", { className: "dt-text" }, tc.reason || tc.error || ""));
  });
  return el("div", { className: "stack" },
    el("div", { className: "dt-stats" },
      stat(s.test_cases ?? 0, "test cases"), stat(pct(s.pass_rate), "pass rate"),
      stat(s.failing ?? 0, "vulnerable"), stat(s.errored ?? 0, "errored"),
      stat(s.max_cvss != null ? Number(s.max_cvss).toFixed(1) : "n/a", "max CVSS"),
      stat(run.result.duration_s != null ? `${run.result.duration_s}s` : "n/a", "duration")),
    breakdown("By vulnerability", s.by_vulnerability),
    breakdown("By attack", s.by_attack),
    el("div", { className: "card stack" }, el("h3", {}, `Test cases (${cases.length})`),
      table(["Verdict", "Vulnerability", "Type", "Attack", "Attack input / conversation", "Reply", "CVSS", "Reason"], rows)));
}

async function loadRuns() {
  if (!state.target) return;
  const runs = await api(`/api/targets/${state.target.id}/deepteam/runs`);
  if (!runs.length) {
    $("dt-runs").replaceChildren(el("p", { className: "muted" }, "No DeepTeam runs for this chatbot yet."));
    return;
  }
  $("dt-runs").replaceChildren(...runs.map((run) => {
    const s = run.summary || {};
    const what = run.options.framework
      ? `Framework: ${run.options.framework}`
      : `${(run.options.vulnerabilities || []).length} vulnerabilities · ${(run.options.attacks || []).length || "baseline"} attacks`;
    const open = el("button", { type: "button", className: "small-button" }, "Open");
    open.addEventListener("click", () => guarded(async () => {
      $("dt-run-detail").replaceChildren(renderRun(await api(`/api/deepteam/runs/${run.id}`)));
    }));
    const report = el("button", { type: "button", className: "small-button primary",
      disabled: run.status !== "done" && run.status !== "cancelled" }, "Report");
    report.addEventListener("click", () => openReport(run.id));
    const confident = confidentControl(run);
    const del = el("button", { type: "button", className: "small-button",
      disabled: run.status === "queued" || run.status === "running" }, "Delete");
    del.addEventListener("click", () => guarded(async () => {
      await api(`/api/deepteam/runs/${run.id}`, { method: "DELETE" });
      $("dt-run-detail").replaceChildren();
      await loadRuns();
    }));
    const pill = run.status === "done" ? (s.failing ? "dt-fail" : "dt-pass") : "dt-err";
    return el("div", { className: "card dt-run-row" },
      el("b", {}, `#${run.id}`),
      el("span", { className: "grow" }, `${what} · ${run.created_at}`),
      el("span", { className: `dt-pill ${pill}` }, run.status === "done" ? `${pct(s.pass_rate)} resisted` : run.status),
      report, confident, open, del);
  }));
}

// Confident AI: send a finished run as a red-teaming risk assessment (Red Teaming >
// Risk Assessments there), with the key saved in the side panel's Confident AI card.
function confidentControl(run) {
  const sent = run.confident && run.confident.link;
  if (sent) {
    return el("a", { className: "small-button", href: run.confident.link, target: "_blank", rel: "noopener",
      title: `Sent ${run.confident.sent_at || ""}` }, "View in Confident AI ↗");
  }
  const button = el("button", { type: "button", className: "small-button",
    disabled: run.status !== "done" && run.status !== "cancelled",
    title: run.confident && run.confident.error ? `Last try failed: ${run.confident.error}` : "Upload this run to Confident AI" },
    run.confident && run.confident.error ? "Retry Confident AI" : "Send to Confident AI");
  button.addEventListener("click", () => guarded(async () => {
    button.disabled = true;
    button.textContent = "Sending…";
    try {
      const info = await api(`/api/deepteam/runs/${run.id}/confident`, { method: "POST" });
      setStatus(`Sent to Confident AI: ${info.link || "done"}`);
      if (info.link) window.open(info.link, "_blank", "noopener");
    } finally {
      await loadRuns();
    }
  }));
  return button;
}

// --- Guardrails ----------------------------------------------------------------
$("g-send").addEventListener("change", () => $("g-output-label").classList.toggle("hidden", $("g-send").checked));

const ticked = (id) => [...$(id).querySelectorAll("input:checked")].map((i) => i.value);

$("g-run").addEventListener("click", () => guarded(async () => {
  const body = {
    input: $("g-input").value.trim(),
    input_guards: ticked("g-input-guards"),
    output_guards: ticked("g-output-guards"),
    allowed_topics: lines($("g-topics").value),
  };
  if (!body.input) throw new Error("Type a message to check.");
  if ($("g-send").checked) {
    if (!state.target) throw new Error("Pick a chatbot, or untick sending to it.");
    body.target_id = state.target.id;
  } else if ($("g-output").value.trim()) {
    body.output = $("g-output").value.trim();
  }
  $("g-status").textContent = "Checking…";
  try {
    const out = await api("/api/deepteam/guardrails", { method: "POST", body });
    $("g-result").replaceChildren(renderGuards(out));
  } finally {
    $("g-status").textContent = "";
  }
}));

function guardBlock(title, result, text) {
  const rows = (result.verdicts || []).map((v) => el("tr", {},
    el("td", {}, v.name), el("td", {}, el("span", {
      className: `dt-pill ${v.safety_level === "safe" ? "dt-pass" : v.safety_level === "unsafe" ? "dt-fail" : "dt-err"}`,
    }, v.safety_level)), el("td", { className: "dt-text" }, v.reason || v.error || "")));
  return el("div", { className: "card stack" },
    el("h3", {}, title, " ", el("span", { className: `dt-pill ${result.breached ? "dt-fail" : "dt-pass"}` },
      result.breached ? "breached" : "passed")),
    text ? el("p", { className: "dt-text secondary" }, text) : null,
    table(["Guard", "Verdict", "Reason"], rows));
}

function renderGuards(out) {
  return el("div", { className: "stack" },
    out.input_result ? guardBlock("Input guards", out.input_result, out.input) : null,
    out.output_result ? guardBlock(`Output guards (${out.reply_source === "chatbot" ? "the chatbot's reply" : "the given reply"})`,
      out.output_result, out.output) : null);
}

// --- YAML / CLI export ---------------------------------------------------------
$("y-download").addEventListener("click", () => guarded(async () => {
  if (!state.target) throw new Error("Add a chatbot in the side panel first.");
  $("y-status").textContent = "Building…";
  try {
    const response = await fetch(`${BACKEND_URL}/api/targets/${state.target.id}/deepteam/export`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(runBody()),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(`${response.status}: ${data.detail || response.statusText}`);
    }
    const blob = await response.blob();
    const name = (response.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
    const link = el("a", { href: URL.createObjectURL(blob), download: name ? name[1] : "deepteam.zip" });
    document.body.append(link);
    link.click();
    link.remove();
    $("y-status").textContent = "Downloaded. Unzip it and run: deepteam run config.yaml";
  } catch (error) {
    $("y-status").textContent = "";
    throw error;
  }
}));

// --- Start ---------------------------------------------------------------------
guarded(async () => {
  await loadTargets();
  state.options = await api("/api/deepteam/options");
  if (!state.options.available) throw new Error(state.options.error || "deepteam is not installed");
  renderOptions();
  if (location.hash === "#runs") showSection("runs");
});
