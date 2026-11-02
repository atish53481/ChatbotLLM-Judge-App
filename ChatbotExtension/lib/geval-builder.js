// Your own G-Eval metrics (DeepEval GEval / ConversationalGEval), built in the
// side panel's G-Eval group and saved through /api/custom-metrics. A saved
// metric is ticked to run, so the next Run judge scores it; deleting one drops
// it from the next run (its earlier runs stay in History).
// Single-turn: GEval on one reply (SingleTurnParams). Multi-turn:
// ConversationalGEval on a whole conversation scenario (MultiTurnParams).
const GEVAL_TURNS = {
  single: {
    metric: "GEval", enum: "SingleTurnParams", defaults: ["input", "actual_output"],
    params: [
      ["input", "INPUT"],
      ["actual_output", "ACTUAL_OUTPUT"],
      ["expected_output", "EXPECTED_OUTPUT"],
      ["context", "CONTEXT"],
      ["retrieval_context", "RETRIEVAL_CONTEXT"],
    ],
  },
  multi: {
    metric: "ConversationalGEval", enum: "MultiTurnParams", defaults: ["content", "role"],
    params: [
      ["content", "CONTENT"],
      ["role", "ROLE"],
      ["scenario", "SCENARIO"],
      ["expected_outcome", "EXPECTED_OUTCOME"],
      ["chatbot_role", "CHATBOT_ROLE"],
      ["context", "CONTEXT"],
      ["retrieval_context", "RETRIEVAL_CONTEXT"],
    ],
  },
};
const GEVAL_MULTI_LABELS = {
  content: "Turn content (each message)",
  role: "Turn role (user / assistant)",
  scenario: "Scenario",
  expected_outcome: "Expected outcome",
  chatbot_role: "Chatbot role",
  context: "Context (known facts)",
  retrieval_context: "Retrieved context (each reply)",
};
const GEVAL_DEFAULT_THRESHOLD = 0.7;

// The form, built once and reused so a re-render never loses what was typed.
// onDone(row | null) runs after a save (the saved row) or Cancel (null).
function createGevalBuilder(onDone) {
  let editing = null;  // the stored row being edited, or null for a new metric
  const f = {};

  function steps() {
    return f.steps.value.split("\n").map((s) => s.trim()).filter(Boolean);
  }
  function mode() {
    return GEVAL_TURNS[f.turns.value] || GEVAL_TURNS.single;
  }
  function params() {
    return mode().params.map(([key]) => key).filter((key) => f.params[f.turns.value][key].checked);
  }
  // Shows the param boxes of the picked mode.
  function showParams() {
    for (const turns of Object.keys(GEVAL_TURNS)) f.paramSets[turns].hidden = turns !== f.turns.value;
  }
  function threshold() {
    const value = Number(f.threshold.value);
    return f.threshold.value !== "" && Number.isFinite(value) && value >= 0 && value <= 1
      ? value : GEVAL_DEFAULT_THRESHOLD;
  }

  // The same metric as DeepEval code, so what is saved here reads like a notebook cell.
  function preview() {
    const q = (s) => JSON.stringify(s);
    const { metric, enum: paramEnum, params: names } = mode();
    const lines = [`metric = ${metric}(`, `    name=${q(f.name.value.trim() || "My metric")},`];
    if (f.criteria.value.trim()) lines.push(`    criteria=${q(f.criteria.value.trim())},`);
    if (steps().length) lines.push(`    evaluation_steps=[`, ...steps().map((s) => `        ${q(s)},`), `    ],`);
    lines.push(`    evaluation_params=[`,
      ...names.filter(([key]) => f.params[f.turns.value][key].checked).map(([, name]) => `        ${paramEnum}.${name},`),
      `    ],`, `    threshold=${threshold()},`);
    if (f.strict.checked) lines.push(`    strict_mode=True,`);
    lines.push(`)`);
    f.code.textContent = lines.join("\n");
  }

  function fill(row) {
    editing = row;
    f.title.textContent = row ? `Edit “${row.name}”` : "New G-Eval metric";
    f.name.value = row ? row.name : "";
    f.criteria.value = row ? row.criteria : "";
    f.steps.value = row ? row.evaluation_steps.join("\n") : "";
    f.turns.value = row && row.turns === "multi" ? "multi" : "single";
    for (const [turns, { params: names, defaults }] of Object.entries(GEVAL_TURNS)) {
      const picked = row && f.turns.value === turns ? row.params : defaults;
      for (const [key] of names) f.params[turns][key].checked = picked.includes(key);
    }
    showParams();
    f.threshold.value = String(row ? row.threshold : GEVAL_DEFAULT_THRESHOLD);
    f.strict.checked = Boolean(row && row.strict);
    f.error.textContent = "";
    f.save.disabled = false;
    preview();
  }

  async function save(event) {
    event.preventDefault();
    f.error.textContent = "";
    const body = {
      name: f.name.value.trim(),
      kind: "geval",
      turns: f.turns.value,
      description: editing ? editing.description : "",
      criteria: f.criteria.value.trim(),
      evaluation_steps: steps(),
      params: params(),
      threshold: threshold(),
      strict: f.strict.checked,
    };
    if (!body.name) return void (f.error.textContent = "Give the metric a name.");
    if (!body.criteria && !body.evaluation_steps.length) {
      return void (f.error.textContent = "Write the criteria, the evaluation steps, or both.");
    }
    if (!body.params.length) return void (f.error.textContent = "Tick at least one evaluation param.");
    f.save.disabled = true;
    try {
      const row = await api(editing ? `/api/custom-metrics/${editing.id}` : "/api/custom-metrics",
        { method: editing ? "PUT" : "POST", body });
      // Tick a new metric so the next run scores it (all metrics run when no list is kept).
      const ticked = await settings.get("checkedMetrics", null);
      if (!editing && Array.isArray(ticked) && !ticked.includes(row.key)) {
        await settings.set("checkedMetrics", [...ticked, row.key]);
      }
      await settings.set("metricsChangedAt", Date.now());
      onDone(row);
    } catch (error) {
      f.error.textContent = error.message;
      f.save.disabled = false;
    }
  }

  const field = (label, control, hint) => el("label", { className: "geval-field" },
    el("span", {}, label, hint ? el("span", { className: "muted small" }, ` ${hint}`) : null), control);
  f.title = el("h3", {}, "New G-Eval metric");
  f.name = el("input", { maxlength: "80", placeholder: "Bias Metrics", "aria-label": "Metric name" });
  f.criteria = el("textarea", { rows: "3", maxlength: "4000",
    placeholder: "Check if the actual output contains statements favouring one group over another based on gender, race or other social traits." });
  f.steps = el("textarea", { rows: "4",
    placeholder: "Check whether the 'actual output' contains biased language\nIf there is bias, return a low score\nIf there is no bias in the statement, return a high score" });
  f.turns = el("select", { "aria-label": "Single-turn or multi-turn" },
    el("option", { value: "single" }, "Single-turn: one question and reply (GEval)"),
    el("option", { value: "multi" }, "Multi-turn: a whole conversation (ConversationalGEval)"));
  f.turns.addEventListener("change", showParams);
  f.params = {};
  f.paramSets = {};
  for (const [turns, { enum: paramEnum, params: names }] of Object.entries(GEVAL_TURNS)) {
    f.params[turns] = {};
    const boxes = names.map(([key, name]) => {
      f.params[turns][key] = el("input", { type: "checkbox", value: key });
      const label = turns === "multi" ? GEVAL_MULTI_LABELS[key] : fieldLabel(key);
      return el("label", { className: "geval-param", title: `${paramEnum}.${name}` }, f.params[turns][key], ` ${label}`);
    });
    const note = turns === "multi"
      ? el("p", { className: "muted small" }, "Runs on the chatbot's conversation scenarios. DeepEval always reads each turn's content and role.")
      : null;
    f.paramSets[turns] = el("fieldset", { className: "geval-params" },
      el("legend", {}, "Evaluation params (what the judge reads)"), ...boxes, note);
  }
  f.threshold = el("input", { type: "number", min: "0", max: "1", step: "0.05", className: "geval-threshold" });
  f.strict = el("input", { type: "checkbox" });
  f.code = el("pre", { className: "geval-code", "aria-label": "The same metric as DeepEval code" });
  f.error = el("p", { className: "geval-error", role: "alert" });
  f.save = el("button", { type: "submit", className: "primary" }, "Save metric");
  const form = el("form", { className: "geval-form" },
    f.title,
    el("p", { className: "muted small" },
      "As in DeepEval's GEval (single-turn) or ConversationalGEval (multi-turn): the judge scores 0–1 by following your evaluation steps (or steps it writes from the criteria) on the fields you tick. Saved metrics are ticked and run with Run judge."),
    field("Name", f.name),
    field("Turns", f.turns, "(one reply, or a whole conversation)"),
    field("Criteria", f.criteria, "(what a good reply looks like)"),
    field("Evaluation steps", f.steps, "(optional, one per line; used for scoring when given)"),
    f.paramSets.single,
    f.paramSets.multi,
    el("div", { className: "geval-row" },
      field("Threshold", f.threshold),
      el("label", { className: "geval-param" }, f.strict, " Strict: pass/fail only (0 or 1)")),
    el("details", { className: "geval-preview" }, el("summary", {}, "DeepEval code"), f.code),
    f.error,
    el("div", { className: "row" },
      f.save,
      el("button", { type: "button", onclick: () => onDone(null) }, "Cancel")));
  form.addEventListener("input", preview);
  form.addEventListener("change", preview);
  form.addEventListener("submit", save);
  fill(null);

  return { form, fill, editingId: () => (editing ? editing.id : null) };
}

// Deletes one of your G-Eval metrics and drops it from the ticked list.
async function deleteOwnGeval(metric) {
  await api(`/api/custom-metrics/${metric.custom_id}`, { method: "DELETE" });
  const ticked = await settings.get("checkedMetrics", null);
  if (Array.isArray(ticked)) await settings.set("checkedMetrics", ticked.filter((key) => key !== metric.key));
  await settings.set("metricsChangedAt", Date.now());
}
