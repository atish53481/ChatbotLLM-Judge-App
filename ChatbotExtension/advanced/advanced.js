// Advanced tools: run options, custom metrics, run comparison, A/B tests,
// red-team coverage, user simulation, golden generation and review, judge
// calibration and CI export. Everything from the backend or a chatbot is
// inserted as text (el() never parses HTML).

const $ = (id) => document.getElementById(id);
const state = { targets: [], target: null, metrics: [], enhancers: null, section: "run-options", drafts: [] };

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

function pct(value) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "n/a";
}

function tile(value, label) {
  return el("div", { className: "tile" }, el("div", { className: "tile-value" }, value), el("div", { className: "tile-label" }, label));
}

function checkbox(name, value, label, checked) {
  const input = el("input", { type: "checkbox", name, value });
  input.checked = Boolean(checked);
  return el("label", {}, input, label);
}

function checkedValues(container) {
  return [...container.querySelectorAll("input[type=checkbox]:checked")].map((i) => i.value);
}

function numberOrNull(input) {
  return input.value === "" ? null : Number(input.value);
}

// --- Navigation and chatbot ---------------------------------------------------
const loaders = {};

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

async function loadTargets() {
  state.targets = await fetchTargets();
  const wanted = await settings.get("selectedTargetId", null);
  state.target = state.targets.find((t) => t.id === wanted) || state.targets[0] || null;
  fillSelect($("target-select"), state.targets.map((t) => ({ value: t.id, label: targetLabel(t) })),
    state.target ? state.target.id : "");
  if (!state.target) setStatus("Add a chatbot in the side panel first.", true);
  await loadMetrics();
}

async function loadMetrics() {
  state.metrics = await api(`/api/metrics${state.target ? `?target_id=${state.target.id}` : ""}`);
}

$("target-select").addEventListener("change", () => guarded(async () => {
  state.target = state.targets.find((t) => String(t.id) === $("target-select").value) || null;
  await settings.set("selectedTargetId", state.target ? state.target.id : null);
  await loadMetrics();
  showSection(state.section);
}));

for (const button of document.querySelectorAll(".adv-nav button")) {
  button.addEventListener("click", () => showSection(button.dataset.section));
}

function requireTarget() {
  if (!state.target) throw new Error("Pick a chatbot first (add one in the side panel).");
  return state.target;
}

// --- Run options ------------------------------------------------------------------
loaders["run-options"] = async () => {
  const options = await settings.get("runOptions", {});
  const perMetric = options.metricSettings || {};
  const tunable = state.metrics.filter((m) => m.settings && Object.keys(m.settings).length);
  $("metric-settings").replaceChildren(...(tunable.length ? tunable.map((m) => el(
    "div", { className: "setting-row" },
    el("span", { className: "setting-title" }, m.title),
    ...Object.entries(m.settings).map(([name, schema]) => {
      const saved = perMetric[m.key] && perMetric[m.key][name];
      const input = el("input", {
        type: "number", min: schema.min, max: schema.max, step: 1, className: "num-input",
        "data-metric": m.key, "data-setting": name, value: saved ?? schema.default,
      });
      return el("label", {}, input, el("span", { className: "secondary small" }, schema.label || name));
    }),
  )) : [el("p", { className: "muted" }, "No metric has tunable settings.")]));

  const target = state.target;
  const instructions = target && target.config.instructions;
  $("instructions").value = Array.isArray(instructions) ? instructions.join("\n") : (instructions || "");

  if (!state.enhancers) state.enhancers = await api("/api/security/enhancers");
  const chosen = new Set(options.enhancements || []);
  $("enhancer-list").replaceChildren(...state.enhancers.enhancers.map((e) =>
    checkbox("enhancer", e.key, e.uses_judge ? `${e.label} (uses the judge)` : e.label, chosen.has(e.key))));
  const eo = options.enhancementOptions || {};
  fillSelect($("many-shot"), state.enhancers.many_shot_counts.map((n) => ({ value: n, label: `${n} fake exchanges` })), eo.many_shot || 8);
  fillSelect($("language"), state.enhancers.languages.map((l) => ({ value: l, label: l })), eo.language || "Swahili");

  $("max-latency").value = typeof options.maxLatencyS === "number" ? options.maxLatencyS : "";
  $("use-calibration").checked = Boolean(options.useCalibration);
  const deepeval = options.deepeval || {};
  $("de-strict").checked = Boolean(deepeval.strict_mode);
  $("de-reason").checked = deepeval.include_reason !== false;
  $("de-async").checked = Boolean(deepeval.async_mode);
  const price = await api("/api/judge/price");
  $("judge-price").value = price.price_per_mtok ?? "";
};

$("save-run-options").addEventListener("click", () => guarded(async () => {
  const metricSettings = {};
  for (const input of $("metric-settings").querySelectorAll("input[data-metric]")) {
    if (input.value === "") continue;
    metricSettings[input.dataset.metric] = { ...(metricSettings[input.dataset.metric] || {}), [input.dataset.setting]: Number(input.value) };
  }
  const maxLatency = numberOrNull($("max-latency"));
  await settings.set("runOptions", {
    metricSettings,
    enhancements: checkedValues($("enhancer-list")),
    enhancementOptions: { many_shot: Number($("many-shot").value), language: $("language").value },
    maxLatencyS: maxLatency && maxLatency > 0 ? maxLatency : null,
    useCalibration: $("use-calibration").checked,
    deepeval: { strict_mode: $("de-strict").checked, include_reason: $("de-reason").checked, async_mode: $("de-async").checked },
  });
  await api("/api/judge/price", { method: "PUT", body: { price_per_mtok: numberOrNull($("judge-price")) } });
  setStatus("Run options saved. They apply to the next run you start.");
}));

$("save-instructions").addEventListener("click", () => guarded(async () => {
  const target = requireTarget();
  const lines = $("instructions").value.split("\n").map((l) => l.trim()).filter(Boolean);
  // Headers are masked in what the page received: leaving them out keeps the stored ones.
  const { headers: _masked, ...config } = target.config;
  const saved = await api(`/api/targets/${target.id}`, {
    method: "PUT", body: { name: target.name, type: target.type, config: { ...config, instructions: lines } },
  });
  state.targets = state.targets.map((t) => (t.id === saved.id ? saved : t));
  state.target = saved;
  await loadMetrics();
  setStatus(lines.length ? `Saved ${lines.length} instruction(s): Prompt Alignment can run now.` : "Instructions cleared.");
}));

// --- Custom metrics ----------------------------------------------------------------
const PARAMS = ["input", "actual_output", "expected_output", "context", "retrieval_context"];
const DAG_EXAMPLE = {
  nodes: {
    asks_order: {
      type: "BinaryJudgementNode",
      criteria: "Does the actual output ask the user for their order number when it needs one?",
      evaluation_params: ["input", "actual_output"],
      children: ["no", "yes"],
    },
    no: { type: "VerdictNode", verdict: false, score: 3 },
    yes: { type: "VerdictNode", verdict: true, score: 10 },
  },
};

function fillParams(selected) {
  $("custom-params").replaceChildren(...PARAMS.map((p) => checkbox("param", p, fieldLabel(p), selected.includes(p))));
}

function clearCustomForm() {
  $("custom-form").reset();
  $("custom-id").value = "";
  $("custom-form-title").textContent = "New metric";
  $("custom-dag").value = "";
  fillParams(["input", "actual_output"]);
  toggleKind();
}

function toggleKind() {
  const dag = $("custom-kind").value === "dag";
  $("custom-geval-fields").classList.toggle("hidden", dag);
  $("custom-dag-fields").classList.toggle("hidden", !dag);
}

function editCustom(row) {
  $("custom-id").value = row.id;
  $("custom-form-title").textContent = `Edit “${row.name}”`;
  $("custom-name").value = row.name;
  $("custom-kind").value = row.kind;
  $("custom-threshold").value = row.threshold;
  $("custom-description").value = row.description || "";
  $("custom-criteria").value = row.criteria || "";
  $("custom-steps").value = (row.evaluation_steps || []).join("\n");
  $("custom-strict").checked = Boolean(row.strict);
  $("custom-dag").value = row.dag ? JSON.stringify(row.dag, null, 2) : "";
  fillParams(row.params || []);
  toggleKind();
  $("custom-form").scrollIntoView({ behavior: "smooth" });
}

loaders.custom = async () => {
  const rows = await api("/api/custom-metrics");
  $("custom-list").replaceChildren(...(rows.length ? rows.map((row) => el(
    "div", { className: "item-row" },
    el("div", { className: "item-body" },
      el("b", {}, row.name), " ", el("span", { className: "pill" }, row.kind === "dag" ? "DAG" : "G-Eval"),
      row.strict ? el("span", { className: "pill" }, "strict") : null,
      el("div", { className: "secondary small" }, row.description || row.criteria || ""),
      el("div", { className: "muted small" }, `key ${row.key} · threshold ${row.threshold} · reads ${row.params.map(fieldLabel).join(", ")}`)),
    el("div", { className: "item-actions" },
      el("button", { type: "button", onClick: () => editCustom(row) }, "Edit"),
      confirmButton("Delete", `Delete ${row.name}`, () => guarded(async () => {
        await api(`/api/custom-metrics/${row.id}`, { method: "DELETE" });
        await loadMetrics();
        await loaders.custom();
      }))),
  )) : [el("p", { className: "muted" }, "No custom metrics yet.")]));
  if (!$("custom-params").children.length) clearCustomForm();
};

$("custom-kind").addEventListener("change", toggleKind);
$("custom-cancel").addEventListener("click", clearCustomForm);
$("dag-example").addEventListener("click", () => { $("custom-dag").value = JSON.stringify(DAG_EXAMPLE, null, 2); });
armConfirm($("custom-reset"), () => guarded(async () => {
  await api("/api/custom-metrics/reset", { method: "POST", body: {} });
  await loadMetrics();
  await loaders.custom();
  setStatus("The example metrics are back; your own metrics were kept.");
}));

$("custom-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guarded(async () => {
    const kind = $("custom-kind").value;
    let dag = null;
    if (kind === "dag") {
      try {
        dag = JSON.parse($("custom-dag").value);
      } catch (error) {
        throw new Error(`The DAG is not valid JSON: ${error.message}`);
      }
    }
    const body = {
      name: $("custom-name").value.trim(),
      kind,
      threshold: Number($("custom-threshold").value || 0.7),
      description: $("custom-description").value.trim(),
      criteria: $("custom-criteria").value.trim(),
      evaluation_steps: $("custom-steps").value.split("\n").map((s) => s.trim()).filter(Boolean),
      strict: $("custom-strict").checked,
      params: checkedValues($("custom-params")),
      dag,
    };
    const id = $("custom-id").value;
    await api(id ? `/api/custom-metrics/${id}` : "/api/custom-metrics", { method: id ? "PUT" : "POST", body });
    setStatus(`Saved “${body.name}”. Find it under Custom in the side panel and the dashboard.`);
    clearCustomForm();
    await loadMetrics();
    await loaders.custom();
  });
});

// --- Compare runs --------------------------------------------------------------------
loaders.compare = async () => {
  const current = $("compare-metric").value;
  fillSelect($("compare-metric"), state.metrics.map((m) => ({ value: m.key, label: m.title })), current || "answer_relevancy");
  await loadCompareRuns();
};

async function loadCompareRuns() {
  const target = requireTarget();
  const runs = await api(`/api/history?target_id=${target.id}&metric_key=${encodeURIComponent($("compare-metric").value)}`);
  const items = runs.map((r) => ({ value: r.id, label: `${formatRunTime(r.ts)} · ${formatScore(r.score)} · ${resultLabel(r.passed)}` })).reverse();
  fillSelect($("compare-a"), items, items[1] ? items[1].value : "");
  fillSelect($("compare-b"), items, items[0] ? items[0].value : "");
  $("compare-result").replaceChildren(items.length < 2
    ? el("p", { className: "muted" }, "This metric needs at least two runs for this chatbot to compare.")
    : "");
}

$("compare-metric").addEventListener("change", () => guarded(loadCompareRuns));

$("compare-button").addEventListener("click", () => guarded(async () => {
  const a = $("compare-a").value;
  const b = $("compare-b").value;
  if (!a || !b || a === b) throw new Error("Pick two different runs.");
  const diff = await api(`/api/runs/compare?a=${a}&b=${b}`);
  const delta = typeof diff.delta === "number" ? `${diff.delta >= 0 ? "+" : ""}${diff.delta.toFixed(2)}` : "n/a";
  const statsLine = (s) => (s ? `median ${formatScore(s.median)} · std ${formatScore(s.std)} · ${s.n} cases` : "");
  $("compare-result").replaceChildren(
    el("div", { className: "summary-tiles" },
      tile(`${formatScore(diff.a.score)} → ${formatScore(diff.b.score)}`, `average (${delta})`),
      tile(String(diff.counts.regressed), "regressed"),
      tile(String(diff.counts.fixed), "fixed"),
      tile(String(diff.counts.added + diff.counts.removed), "added / removed cases")),
    el("p", { className: "secondary small" }, `Before: ${statsLine(diff.a.stats)}. After: ${statsLine(diff.b.stats)}.`),
    diff.a.judge_model && diff.b.judge_model && diff.a.judge_model !== diff.b.judge_model
      ? el("p", { className: "status-error small" }, `The judge model changed (${diff.a.judge_model} → ${diff.b.judge_model}): differences may come from the judge.`)
      : null,
    el("div", { className: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Change"), el("th", {}, "Question"), el("th", { className: "num" }, "Before"),
        el("th", { className: "num" }, "After"), el("th", { className: "num" }, "Δ"), el("th", {}, "Judge now says"))),
      el("tbody", {}, ...diff.cases.map((c) => el("tr", { className: c.change },
        el("td", {}, c.change), el("td", {}, c.question), el("td", { className: "num" }, formatScore(c.score_a)),
        el("td", { className: "num" }, formatScore(c.score_b)),
        el("td", { className: "num" }, typeof c.delta === "number" ? c.delta.toFixed(2) : ""),
        el("td", { className: "small" }, c.reason_b || "")))))),
  );
}));

// --- Pairwise ---------------------------------------------------------------------------
loaders.pairwise = async () => {
  const target = requireTarget();
  $("pair-a-name").textContent = target.name;
  const others = state.targets.filter((t) => t.id !== target.id);
  fillSelect($("pair-b"), others.map((t) => ({ value: t.id, label: t.name })), $("pair-b").value);
  $("pair-run").disabled = !others.length;
  $("pair-progress").textContent = others.length ? "" : "Add a second chatbot in the side panel to compare against.";
  const history = await api(`/api/pairwise?target_id=${target.id}`);
  $("pair-history").replaceChildren(...(history.length ? history.map((h) => pairSummary(h.result, h.ts)) : [el("p", { className: "muted" }, "No comparisons yet.")]));
};

function pairSummary(result, ts) {
  const names = result.names || { a: "A", b: "B" };
  const winner = result.winner === "tie" ? "Tie" : `${names[result.winner]} wins`;
  return el("div", { className: "item-row" }, el("div", { className: "item-body" },
    el("b", {}, `${names.a} vs ${names.b}: ${winner}`),
    el("div", { className: "secondary small" },
      `${result.wins_a} – ${result.wins_b} with ${result.ties} tie(s) over ${result.cases_run} case(s)${ts ? ` · ${formatRunTime(ts)}` : ""}`)));
}

function pairDetails(result) {
  const names = result.names || { a: "A", b: "B" };
  return el("div", { className: "stack" },
    el("div", { className: "summary-tiles" },
      tile(String(result.wins_a), `${names.a} wins`), tile(String(result.wins_b), `${names.b} wins`),
      tile(String(result.ties), "ties"), tile(pct(result.score), `${names.a} win rate (decided cases)`)),
    ...result.rows.map((row) => el("div", { className: "item-row" }, el("div", { className: "item-body" },
      el("b", {}, row.question), " ",
      el("span", { className: `pill ${row.winner === "a" ? "good" : row.winner === "b" ? "bad" : ""}` },
        row.winner === "tie" ? "tie" : `${names[row.winner]} better`),
      el("p", { className: "case-text" }, el("b", {}, `${names.a}: `), row.reply_a),
      el("p", { className: "case-text" }, el("b", {}, `${names.b}: `), row.reply_b),
      el("p", { className: "secondary small" }, row.reasons.filter(Boolean).join(" / ")),
      row.note ? el("p", { className: "status-error small" }, row.note) : null))));
}

$("pair-run").addEventListener("click", () => guarded(async () => {
  const target = requireTarget();
  const b = Number($("pair-b").value);
  const job = await api("/api/pairwise", { method: "POST", body: {
    target_a: target.id, target_b: b, criteria: $("pair-criteria").value.trim(),
    limit: Number($("pair-limit").value) || null,
  } });
  $("pair-run").disabled = true;
  try {
    for (;;) {
      const status = await api(`/api/jobs/${job.job_id}`);
      const cur = status.current;
      $("pair-progress").textContent = status.status === "queued" ? "Waiting for the run before this one…"
        : cur && cur.total ? `${cur.done}/${cur.total} · ${cur.phase === "judge" ? "judging" : "asking both chatbots"}` : "Starting…";
      if (!["queued", "running"].includes(status.status)) {
        $("pair-progress").textContent = "";
        const result = status.results[0];
        if (status.status !== "done" || !result || result.status === "error") {
          throw new Error(status.error || (result && result.error) || `A/B test ${status.status}`);
        }
        $("pair-result").replaceChildren(pairDetails(result));
        await loaders.pairwise();
        return;
      }
      await sleep(1500);
    }
  } finally {
    $("pair-run").disabled = false;
  }
}));

// --- Coverage -----------------------------------------------------------------------------
loaders.coverage = async () => {
  const target = requireTarget();
  const report = await api(`/api/security/coverage?target_id=${target.id}`);
  $("coverage-grid").replaceChildren(...report.categories.map((cat) => {
    const rate = cat.pass_rate;
    const fill = el("span");
    fill.style.width = `${Math.round((rate || 0) * 100)}%`;
    return el("div", { className: "card stack" },
      el("div", { className: "row" }, el("h3", { className: "grow" }, cat.label),
        el("span", { className: "pill" }, `${cat.tested}/${cat.total} tested`)),
      el("div", { className: `meter${rate !== null && rate < 0.8 ? " low" : ""}` }, fill),
      el("p", { className: "secondary small" }, rate === null ? "No attacks run yet." : `${cat.resisted} of ${cat.attacks} attacks resisted (${pct(rate)})`),
      el("ul", { className: "small" }, ...cat.metrics.map((m) => el("li", {},
        `${m.title}${m.owasp.length ? ` (${m.owasp.join(", ")})` : ""}: `,
        m.tested ? el("span", { className: m.passed ? "status-pass" : "status-fail" }, `${formatScore(m.score)} ${resultLabel(m.passed)}`)
          : el("span", { className: "muted" }, "not run")))));
  }));
};

// --- Simulate ---------------------------------------------------------------------------------
loaders.simulate = async () => {
  const target = requireTarget();
  const scenarios = await api(`/api/conversations?theme=${encodeURIComponent(themeOf(target))}`);
  state.scenarios = scenarios;
  fillSelect($("sim-scenario"), [{ value: "", label: "(write my own)" }, ...scenarios.map((s) => ({ value: s.id, label: s.name }))], $("sim-scenario").value);
  const conversational = state.metrics.filter((m) => m.kind === "conversation");
  if (!$("sim-metrics").children.length) {
    $("sim-metrics").replaceChildren(...conversational.map((m) => checkbox("sim-metric", m.key, m.title, m.key === "conversation_completeness")));
  }
};

$("sim-scenario").addEventListener("change", () => {
  const picked = (state.scenarios || []).find((s) => s.id === $("sim-scenario").value);
  if (!picked) return;
  $("sim-text").value = picked.scenario || "";
  $("sim-outcome").value = picked.expected_outcome || "";
  $("sim-role").value = picked.chatbot_role || "";
});

$("sim-run").addEventListener("click", () => guarded(async () => {
  const target = requireTarget();
  $("sim-run").disabled = true;
  $("sim-progress").textContent = "The simulated user and the chatbot are talking… (this takes a while)";
  try {
    const result = await api("/api/simulate", { method: "POST", body: {
      target_id: target.id,
      conversation_id: $("sim-scenario").value || null,
      scenario: $("sim-text").value.trim(),
      expected_outcome: $("sim-outcome").value.trim(),
      chatbot_role: $("sim-role").value.trim(),
      user_persona: $("sim-persona").value.trim(),
      max_turns: Number($("sim-turns").value) || 5,
      metric_keys: checkedValues($("sim-metrics")),
      save_as: $("sim-save").value.trim(),
    } });
    $("sim-result").replaceChildren(
      el("p", { className: "secondary" }, `${result.user_turns.length} user turn(s) · stopped: ${result.stop_reason}${result.saved ? ` · saved as “${result.saved.name}”` : ""}`),
      el("div", { className: "card transcript" }, ...result.turns.map((t) => el("div", { className: `bubble ${t.role === "user" ? "user" : "bot"}` }, t.content))),
      ...result.scores.map((s) => el("div", { className: "item-row" }, el("div", { className: "item-body" },
        el("b", {}, s.title || s.key), " ",
        s.status === "error" ? el("span", { className: "status-error" }, s.error)
          : el("span", { className: s.status === "pass" ? "status-pass" : "status-fail" }, `${formatScore(s.score)} ${resultLabel(s.status === "pass")}`),
        s.reason ? el("p", { className: "secondary small" }, s.reason) : null))),
    );
  } finally {
    $("sim-run").disabled = false;
    $("sim-progress").textContent = "";
  }
}));

// --- Generate and review goldens ---------------------------------------------------------------
const EVOLUTIONS = [
  ["reasoning", "Multi-step reasoning"], ["multi_context", "Multi-context"], ["concretizing", "Concretizing"],
  ["constrained", "Constrained"], ["comparative", "Comparative"], ["hypothetical", "Hypothetical"], ["in_breadth", "In-breadth"],
];

loaders.goldens = async () => {
  if (!$("gen-evolutions").children.length) {
    $("gen-evolutions").replaceChildren(...EVOLUTIONS.map(([key, label]) => checkbox("evolution", key, label, false)));
  }
  await loadDrafts();
};

async function loadDrafts() {
  const target = requireTarget();
  const goldens = await api(`/api/goldens?theme=${encodeURIComponent(themeOf(target))}`);
  state.drafts = goldens.filter((g) => g.status === "draft");
  $("draft-list").replaceChildren(...(state.drafts.length ? state.drafts.map((g) => el("div", { className: "item-row" },
    el("div", { className: "item-body" },
      el("b", {}, g.question),
      el("p", { className: "case-text secondary" }, g.expected_answer || "(no expected answer)"),
      g.evolutions && g.evolutions.length ? el("span", { className: "pill" }, g.evolutions.join(" → ")) : null,
      g.source_document ? el("span", { className: "muted small" }, ` from ${g.source_document}`) : null),
    el("div", { className: "item-actions" },
      el("button", { type: "button", onClick: () => guarded(() => review([g.id], "accept")) }, "Accept"),
      el("button", { type: "button", className: "danger", onClick: () => guarded(() => review([g.id], "reject")) }, "Reject")),
  )) : [el("p", { className: "muted" }, `No drafts in theme “${themeOf(target)}”. Generated goldens land here when “review first” is ticked.`)]));
}

async function review(ids, action) {
  if (!ids.length) return;
  const { changed } = await api("/api/goldens/review", { method: "POST", body: { golden_ids: ids, action } });
  setStatus(`${action === "accept" ? "Accepted" : "Rejected"} ${changed} golden(s).`);
  await loadDrafts();
}

$("drafts-accept-all").addEventListener("click", () => guarded(() => review(state.drafts.map((g) => g.id), "accept")));
armConfirm($("drafts-reject-all"), () => guarded(() => review(state.drafts.map((g) => g.id), "reject")));

$("gen-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guarded(async () => {
    const target = requireTarget();
    const generation = {
      evolutions: checkedValues($("gen-evolutions")),
      num_evolutions: numberOrNull($("gen-steps")),
      quality_threshold: numberOrNull($("gen-quality")),
      max_retries: numberOrNull($("gen-retries")),
      scenario: $("gen-scenario").value.trim(),
      task: $("gen-task").value.trim(),
      input_format: $("gen-input-format").value.trim(),
      expected_output_format: $("gen-output-format").value.trim(),
      review: $("gen-review").checked,
    };
    const file = $("gen-file").files[0];
    const url = $("gen-url").value.trim();
    if (!file && !url) throw new Error("Enter a page URL or choose a file.");
    let doc;
    if (file) {
      // Multipart upload: the generation options travel as one JSON form field.
      const form = new FormData();
      form.append("theme", themeOf(target));
      form.append("file", file);
      form.append("generation", JSON.stringify(generation));
      const response = await fetch(`${BACKEND_URL}/api/documents`, { method: "POST", body: form });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(`${response.status}: ${data.detail || response.statusText}`);
      doc = data;
    } else {
      doc = await api("/api/documents/url", { method: "POST", body: { theme: themeOf(target), url, generation } });
    }
    while (!["ready", "error"].includes(doc.status)) {
      $("gen-progress").textContent = `${doc.status}…`;
      await sleep(2000);
      doc = await api(`/api/documents/${doc.id}`);
    }
    $("gen-progress").textContent = "";
    if (doc.status === "error") throw new Error(doc.error || "Generation failed.");
    setStatus(`Created ${doc.goldens_created} golden(s)${generation.review ? ": review them below" : ""}.`);
    await loadDrafts();
  });
});

$("scratch-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guarded(async () => {
    const target = requireTarget();
    const review = $("scratch-review").checked;
    $("scratch-progress").textContent = "DeepEval is writing questions and answers…";
    try {
      const data = await api("/api/goldens/generate-scratch", {
        method: "POST", body: { target_id: target.id, count: numberOrNull($("scratch-count")) || 5, review },
      });
      setStatus(`Added ${data.created} golden(s) without context to '${data.theme}'${review ? ": review them below" : ""}.`);
    } finally {
      $("scratch-progress").textContent = "";
    }
    await loadDrafts();
  });
});

// --- Calibration -----------------------------------------------------------------------------------
loaders.calibration = async () => {
  fillSelect($("cal-metric"), state.metrics.map((m) => ({ value: m.key, label: m.title })), $("cal-metric").value || "correctness");
  await loadCalibration();
};

async function loadCalibration() {
  const target = requireTarget();
  const key = $("cal-metric").value;
  const [latest, labels] = await Promise.all([
    api(`/api/runs/latest?target_id=${target.id}`),
    api(`/api/calibration?target_id=${target.id}&metric_key=${encodeURIComponent(key)}`),
  ]);
  const agreement = labels.agreement;
  $("cal-agreement").textContent = agreement
    ? `${agreement.n} labelled · same verdict ${pct(agreement.verdict_agreement)} · average gap ${formatScore(agreement.mean_abs_gap)} · within 0.2: ${pct(agreement.within_0_2)}`
    : "No labels yet for this metric.";
  const run = latest.find((r) => r.metric_key === key);
  const rows = (run && run.result && run.result.rows) || [];
  const byIndex = new Map(labels.labels.filter((lb) => run && lb.run_id === run.id).map((lb) => [lb.case_index, lb]));
  $("cal-cases").replaceChildren(...(rows.length ? rows.map((row, index) => {
    const label = byIndex.get(index);
    const input = el("input", { type: "number", min: 0, max: 1, step: 0.1, className: "num-input", value: label ? label.human_score : "" });
    const note = el("input", { className: "grow", placeholder: "why (optional)", maxlength: 500, value: label ? label.note || "" : "" });
    return el("div", { className: "item-row" },
      el("div", { className: "item-body" },
        el("b", {}, row.question || row.input),
        el("p", { className: "case-text" }, row.actual_output),
        el("p", { className: "secondary small" }, `Judge: ${formatScore(row.score)} ${resultLabel(row.passed)} · ${row.reason || ""}`)),
      el("div", { className: "item-actions" }, el("label", {}, "My score", input), note,
        el("button", { type: "button", className: "primary", onClick: () => guarded(async () => {
          if (input.value === "") throw new Error("Enter a score from 0 to 1.");
          await api("/api/calibration", { method: "POST", body: { run_id: run.id, case_index: index, human_score: Number(input.value), note: note.value.trim() } });
          setStatus("Label saved.");
          await loadCalibration();
        }) }, label ? "Update" : "Save")));
  }) : [el("p", { className: "muted" }, "Run this metric for this chatbot first; its latest cases show up here to label.")]));
}

$("cal-metric").addEventListener("change", () => guarded(loadCalibration));

// --- Export ---------------------------------------------------------------------------------------
loaders.export = async () => {
  const envs = await api("/api/metrics/environments");
  fillSelect($("export-env"), envs.map((e) => ({ value: e.key, label: e.label })), $("export-env").value || "pr");
  const singles = state.metrics.filter((m) => m.kind === "single" && !["security_probes", "prompt_leak_probes"].includes(m.dataset));
  const defaults = new Set(["answer_relevancy", "correctness", "faithfulness", "helpfulness"]);
  $("export-metrics").replaceChildren(...singles.map((m) => checkbox("export-metric", m.key, m.title, defaults.has(m.key))));
};

$("export-button").addEventListener("click", () => guarded(async () => {
  const target = requireTarget();
  const keys = checkedValues($("export-metrics"));
  if (!keys.length) throw new Error("Tick at least one metric.");
  const response = await fetch(`${BACKEND_URL}/api/export/suite?target_id=${target.id}&env=${encodeURIComponent($("export-env").value)}&metrics=${encodeURIComponent(keys.join(","))}`);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  const blob = await response.blob();
  const link = el("a", { href: URL.createObjectURL(blob), download: `llm-evals-${target.name.replace(/[^A-Za-z0-9_-]+/g, "_")}.zip` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  setStatus("Suite downloaded. Unzip it into your repo and add the JUDGE_API_KEY and LLM_EVAL_HEADERS secrets.");
}));

// --- Start ------------------------------------------------------------------------------------------
settings.onChange((changes) => {
  if (changes.selectedTargetId && state.target && changes.selectedTargetId.newValue !== state.target.id) {
    guarded(async () => { await loadTargets(); showSection(state.section); });
  }
});

guarded(async () => {
  await loadTargets();
  showSection("run-options");
});
