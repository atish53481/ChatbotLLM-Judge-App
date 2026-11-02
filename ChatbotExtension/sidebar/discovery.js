// Find missing metric data: asks the backend to look for what the selected
// chatbot's metrics need that its cURL does not show (backend/targets/discovery.py),
// shows the report per metric group, and uses only the items the user ticks.
// Everything from the chatbot is shown with textContent, never as HTML.

(function () {
  const STATUS = {
    ready: "Ready",
    found: "Found",
    inferred: "Inferred, review it",
    black_box: "Black box",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function describe(item) {
    const value = item.value;
    if (item.kind === "request") {
      const parts = [`Adds "${value.flag}" to each request`];
      if (value.tools_path) parts.push(`tool calls at ${value.tools_path}`);
      if (value.context_path) parts.push(`sources at ${value.context_path}`);
      return [parts.join("; ")];
    }
    if (item.kind === "instructions") return value;
    if (item.kind === "available_tools") {
      return value.map((t) => (t.description ? `${t.name}: ${t.description}` : t.name));
    }
    return [String(value)];
  }

  function render(report) {
    const box = $("discovery-report");
    box.replaceChildren();
    $("discovery-summary").textContent = report ? `· last run ${report.ran_at.replace("T", " ")}` : "";
    if (!report) return;

    const groups = el("ul", "stack");
    for (const group of report.metrics || []) {
      const row = el("li", "small");
      row.append(el("strong", "", `${group.title}: ${STATUS[group.status] || group.status}`));
      row.append(el("div", "muted", group.metrics));
      row.append(el("div", "", group.note));
      if (group.ask) row.append(el("div", "muted", group.ask));
      groups.append(row);
    }
    box.append(groups);

    const approved = new Set(report.approved || []);
    const items = report.items || [];
    if (items.length) {
      const list = el("div", "stack");
      for (const item of items) {
        const label = el("label", "row small");
        const tick = el("input");
        tick.type = "checkbox";
        tick.value = item.id;
        tick.checked = approved.has(item.id);
        const text = el("span");
        text.append(el("strong", "", `${item.label} (${STATUS[item.status] || item.status})`));
        text.append(el("span", "muted", ` from ${item.source}`));
        const lines = el("ul");
        for (const line of describe(item)) lines.append(el("li", "", line));
        text.append(lines);
        label.append(tick, text);
        list.append(label);
      }
      const use = el("button", "primary", "Use selected");
      use.type = "button";
      use.addEventListener("click", () => {
        const ids = [...list.querySelectorAll("input:checked")].map((input) => input.value);
        applyItems(ids).catch((error) => { $("discovery-status").textContent = error.message; });
      });
      box.append(list, use);
    }
    for (const note of report.notes || []) box.append(el("p", "muted small", note));
  }

  async function fileText(input) {
    const file = input.files && input.files[0];
    return file ? file.text() : "";
  }

  async function helpFileText(targetId) {
    // PDF/DOCX are binary: the backend extracts their text; other files are text.
    const file = $("discovery-help-file").files[0];
    if (!file) return "";
    const name = file.name.toLowerCase();
    if (name.endsWith(".pdf") || name.endsWith(".docx")) {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(`${BACKEND_URL}/api/targets/${targetId}/discovery/extract`, {
        method: "POST",
        body: form,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(data && data.detail ? data.detail : `Could not read ${file.name}`);
      }
      return data.text;
    }
    return file.text();
  }

  async function run() {
    const target = currentTarget();
    if (!target) return;
    const status = $("discovery-status");
    if (!$("discovery-authorized").checked) {
      status.textContent = "Tick that you own this chatbot or may test it first.";
      return;
    }
    $("discovery-run").disabled = true;
    status.textContent = "Looking (API docs, request flags, help pages, a few chat messages)…";
    try {
      const specUrl = $("discovery-spec-url").value.trim();
      const specText = await fileText($("discovery-spec-file"));
      const helpText = [await helpFileText(target.id), $("discovery-help-text").value.trim()]
        .filter(Boolean).join("\n\n");
      const report = await api(`/api/targets/${target.id}/discover`, {
        method: "POST",
        body: {
          authorized: true,
          send_headers: $("discovery-send-headers").checked,
          spec_url: specUrl,
          spec_text: specText,
          help_text: helpText,
        },
      });
      target.config.discovery = report;
      render(report);
      status.textContent = report.items.length
        ? "Tick what to use, then Use selected."
        : "Nothing new found; metrics marked Black box stay as they are.";
    } finally {
      $("discovery-run").disabled = false;
    }
  }

  async function applyItems(ids) {
    const target = currentTarget();
    if (!target) return;
    const saved = await api(`/api/targets/${target.id}/discovery/apply`, { method: "POST", body: { items: ids } });
    await loadTargets(saved.id);
    show();
    $("discovery-status").textContent = ids.length ? "Saved on the chatbot." : "Nothing used.";
  }

  function show() {
    const target = currentTarget();
    $("discovery-status").textContent = "";
    render(target ? target.config.discovery : null);
  }

  $("discovery-run").addEventListener("click", () => {
    run().catch((error) => { $("discovery-status").textContent = error.message; });
  });
  $("target-select").addEventListener("change", show);
  $("discovery").addEventListener("toggle", show);
})();
