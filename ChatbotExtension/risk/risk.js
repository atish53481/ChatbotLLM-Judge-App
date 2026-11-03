// Risk Assessment: one scan's red-team results as a security report.
// risk.html?job=<scan job id> (or ?target=<id>: its newest scan)
// #overview | #vulnerabilities | #cases | #frameworks.
// Data: GET /api/scans/{job}/risk (backend/dashboard/risk.py). "Download
// report" prints the report section (#print) so the browser saves it as PDF.

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const TABS = { overview: "▦ Overview", vulnerabilities: "◈ Vulnerabilities", cases: "☰ Test Cases", frameworks: "▤ Frameworks" };
const PAGE = 50;

const state = { targets: [], target: null, scans: [], job: null, data: null, tab: "overview",
  query: "", status: "all", vuln: "", page: 0, lastFocus: null };

const pct = (x) => (x == null ? "–" : `${Math.round(x * 1000) / 10}%`);
const when = (ts) => (ts ? new Date(ts).toLocaleString() : "–");
const day = (ts) => (ts ? new Date(ts).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "2-digit" }) : "–");

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function minutes(start, end) {
  const s = (new Date(end) - new Date(start)) / 1000;
  if (!Number.isFinite(s) || s < 0) return "–";
  return s < 90 ? `${s.toFixed(1)} seconds` : `${(s / 60).toFixed(1)} minutes`;
}

// --- Data ----------------------------------------------------------------------

async function loadTargets() {
  state.targets = await fetchTargets();
  fillSelect($("target"), state.targets.map((t) => ({ value: String(t.id), label: targetLabel(t) })),
    state.target ? String(state.target.id) : "");
}

async function loadScans() {
  state.scans = state.target ? await api(`/api/targets/${state.target.id}/scans`) : [];
  const items = state.scans.map((s) => ({ value: String(s.job_id),
    label: `Scan ${s.version} · ${when(s.started_at)} · ${s.result}` }));
  fillSelect($("scan"), items.length ? items : [{ value: "", label: "No scans yet" }], state.job ? String(state.job) : "");
}

async function loadRisk() {
  state.data = state.job ? await api(`/api/scans/${encodeURIComponent(state.job)}/risk`) : null;
  state.page = 0;
  const params = new URLSearchParams({ job: state.job || "" });
  if (state.target) params.set("target", state.target.id);
  history.replaceState(null, "", `?${params}#${state.tab}`);
  $("nav-runs").href = `../runs/runs.html?target=${state.target ? state.target.id : ""}&job=${state.job || ""}#overview`;
  $("nav-scan").href = `../scan/scan.html?job=${state.job || ""}`;
}

async function boot() {
  const params = new URLSearchParams(location.search);
  state.tab = TABS[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  try {
    state.job = params.get("job") ? Number(params.get("job")) : null;
    let targetId = params.get("target") ? Number(params.get("target")) : null;
    if (state.job && !targetId) targetId = (await api(`/api/scans/${state.job}`)).target_id;
    await loadTargets();
    state.target = state.targets.find((t) => t.id === targetId) || state.targets[0] || null;
    $("target").value = state.target ? String(state.target.id) : "";
    await loadScans();
    if (!state.job) {
      const done = state.scans.find((s) => !["queued", "running"].includes(s.status));
      state.job = done ? done.job_id : null;
      $("scan").value = state.job ? String(state.job) : "";
    }
    await loadRisk();
    render();
  } catch (err) {
    $("view").replaceChildren(el("p", { className: "status-error" }, err.message));
  }
}

// --- Pieces ------------------------------------------------------------------------

function gauge(score) {
  // Half circle 0-10, lower is better: teal while low, pink once high.
  const value = score == null ? 0 : Math.max(0, Math.min(10, score));
  const r = 80, cx = 100, cy = 96, len = Math.PI * r;
  const arc = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`;
  const color = value >= 7 ? "var(--rk-fail)" : value >= 4 ? "var(--rk-warn)" : "var(--rk-pass)";
  return svg("svg", { viewBox: "0 0 200 116", class: "rk-gauge", role: "img",
    "aria-label": `Risk score ${score == null ? "not available" : score} out of 10, lower is better` },
    svg("path", { d: arc, fill: "none", stroke: "var(--rk-track)", "stroke-width": 14, "stroke-linecap": "round" }),
    svg("path", { d: arc, fill: "none", stroke: color, "stroke-width": 14, "stroke-linecap": "round",
      "stroke-dasharray": `${(value / 10) * len} ${len}` }),
    svg("text", { x: cx, y: cy - 14, "text-anchor": "middle", class: "rk-gauge-value", fill: color },
      score == null ? "–" : score.toFixed(1)),
    svg("text", { x: cx, y: cy + 4, "text-anchor": "middle", class: "rk-gauge-hint" }, "Lower the better"),
    svg("text", { x: cx - r, y: cy + 18, "text-anchor": "middle", class: "rk-gauge-hint" }, "0"),
    svg("text", { x: cx + r, y: cy + 18, "text-anchor": "middle", class: "rk-gauge-hint" }, "10"));
}

function kpi(label, value, sub) {
  return el("div", { className: "rk-box" }, el("p", { className: "rk-label" }, label),
    el("p", { className: "rk-big" }, value), el("p", { className: "rk-sub" }, sub));
}

function stat(label, value, sub) {
  return el("div", { className: "rk-stat" }, el("p", { className: "rk-label" }, label),
    el("p", { className: "rk-big" }, value), el("p", { className: "rk-sub" }, sub));
}

// rows: [{label, count, of, rate}]; color: a class, or a function of the row.
function bars(title, rows, color, { emptyText = "No data found" } = {}) {
  const shown = rows.filter((r) => r.of > 0);
  return el("section", { className: "rk-panel" }, el("h3", {}, title),
    shown.length && shown.some((r) => r.count > 0) ? shown.map((r) => el("div", { className: "rk-bar" },
      el("span", { className: "rk-bar-label", title: r.label }, r.label),
      el("div", { className: "rk-track", role: "img", "aria-label": `${r.label}: ${r.count} of ${r.of}` },
        el("div", { className: `rk-fill ${typeof color === "function" ? color(r) : color}`,
          style: `width:${Math.max(r.count ? 2 : 0, r.rate * 100)}%` })),
      el("span", { className: "rk-bar-num" }, `${r.count}/${r.of}  ${Math.round(r.rate * 100)}%`)))
      : el("p", { className: "rk-empty" }, emptyText));
}

const sevColor = (r) => (/critical|high/i.test(r.label) ? "c-fail" : /medium/i.test(r.label) ? "c-warn" : "c-pass");
const rateColor = (r) => (r.rate >= 0.5 ? "c-fail" : r.rate >= 0.2 ? "c-warn" : "c-pass");

function dashes(outcomes) {
  return el("div", { className: "rk-dashes", "aria-hidden": "true" },
    outcomes.slice(0, 60).map((ok) => el("span", { className: `rk-dash ${ok ? "c-pass" : "c-fail"}` })));
}

function ratePill(rate) {
  if (rate == null) return el("span", { className: "rk-pill na" }, "–");
  return el("span", { className: `rk-pill ${rate >= 0.8 ? "ok" : "bad"}` }, `${Math.round(rate * 100)}%`);
}

function statusTag(passed) {
  return el("span", { className: `rk-status ${passed ? "ok" : "bad"}` }, passed ? "Passed" : "Failed");
}

// --- Tabs -----------------------------------------------------------------------------

function overviewTab(d) {
  const o = d.overview;
  return el("div", { className: "rk-panels" },
    bars("Impact severity distribution", o.impact, sevColor),
    bars("Exploitability", o.exploitability, (r) => ({ High: "c-fail", Medium: "c-warn", Low: "c-pass" }[r.label])),
    bars("Risk score distribution", o.score_bands, sevColor),
    bars("Remediation priority", o.remediation, sevColor),
    bars("Attack success by method", o.attack_success, rateColor),
    bars("Test coverage by vulnerability (attacks that got through)", o.coverage.slice(0, 12), rateColor));
}

function vulnerabilitiesTab(d) {
  const findings = d.critical_findings;
  return el("div", { className: "rk-vulns" },
    el("aside", { className: "rk-findings" }, el("h3", {}, "⚠ CRITICAL FINDINGS"),
      findings.length
        ? el("ul", {}, findings.slice(0, 30).map((c) => el("li", {}, el("button", { type: "button", onclick: () => openCase(c) },
          el("b", {}, c.vulnerability), ` · ${c.attack} · `, el("span", { className: `sev-${c.severity}` }, c.severity)))))
        : el("p", { className: "rk-muted small" }, "No critical or high-risk findings detected.")),
    el("div", {}, d.vulnerabilities.map((v) => el("details", { className: "rk-vrow" },
      el("summary", {},
        el("div", {}, el("div", { className: "rk-vtitle" }, v.title),
          el("div", { className: "rk-tally" },
            v.error ? el("span", { className: "bad" }, `Error: ${v.error}`)
              : [el("span", { className: "ok" }, `✓ ${v.passed} passed`), el("span", { className: "bad" }, `✗ ${v.failed} failed`)])),
        dashes(v.outcomes), ratePill(v.pass_rate)),
      el("div", { className: "rk-vbody" },
        el("p", { className: "rk-sub" }, `${v.category_label} · severity ${v.severity}`
          + (v.owasp.length ? ` · ${v.owasp.map((o) => `${o.id} ${o.title}`).join(", ")}` : "")),
        caseTable(d.cases.filter((c) => c.vulnerability_key === v.key), { compact: true }))))));
}

function filteredCases(d) {
  const q = state.query.trim().toLowerCase();
  return d.cases.filter((c) => (state.status === "all" || (state.status === "failed") === !c.passed)
    && (!state.vuln || c.vulnerability_key === state.vuln)
    && (!q || [c.probe_id, c.vulnerability, c.type, c.attack, c.category_label, c.input, c.actual_output,
      ...c.owasp.map((o) => o.id)].join(" ").toLowerCase().includes(q)));
}

function caseTable(cases, { compact = false } = {}) {
  return el("div", { className: "rk-table-wrap" }, el("table", { className: "rk-table" },
    el("thead", {}, el("tr", {}, compact ? "" : el("th", {}, "ID"), compact ? "" : el("th", {}, "Vulnerability"),
      el("th", {}, "Vulnerability type"), el("th", {}, "Attack method"), compact ? el("th", {}, "Input") : "",
      el("th", {}, "Status"))),
    el("tbody", {}, cases.map((c) => el("tr", { tabindex: 0, onclick: () => openCase(c),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCase(c); } } },
      compact ? "" : el("td", { className: "rk-id" }, c.probe_id || c.id),
      compact ? "" : el("td", {}, c.vulnerability),
      el("td", {}, c.type || "–"), el("td", {}, c.attack),
      compact ? el("td", {}, c.input.length > 110 ? `${c.input.slice(0, 110)}…` : c.input) : "",
      el("td", {}, statusTag(c.passed)))))));
}

function casesTab(d) {
  const cases = filteredCases(d);
  const pages = Math.max(1, Math.ceil(cases.length / PAGE));
  state.page = Math.min(state.page, pages - 1);
  const start = state.page * PAGE;
  const search = el("input", { className: "rk-search", type: "search", value: state.query,
    placeholder: "Search by vulnerability, attack method, OWASP id or text…", "aria-label": "Search test cases" });
  search.addEventListener("input", () => { state.query = search.value; state.page = 0; rerenderTab(true); });
  const status = el("select", { "aria-label": "Status" },
    ["all", "failed", "passed"].map((s) => el("option", { value: s, selected: state.status === s }, s === "all" ? "All statuses" : s[0].toUpperCase() + s.slice(1))));
  status.addEventListener("change", () => { state.status = status.value; state.page = 0; rerenderTab(); });
  const vuln = el("select", { "aria-label": "Vulnerability" }, el("option", { value: "" }, "All vulnerabilities"),
    d.vulnerabilities.map((v) => el("option", { value: v.key, selected: state.vuln === v.key }, v.title)));
  vuln.addEventListener("change", () => { state.vuln = vuln.value; state.page = 0; rerenderTab(); });
  return el("div", {},
    el("div", { className: "rk-tools" }, search, status, vuln),
    el("p", { className: "rk-count" }, cases.length
      ? `Showing ${start + 1} to ${Math.min(start + PAGE, cases.length)} of ${cases.length} test cases`
      : "No test case matches."),
    cases.length ? caseTable(cases.slice(start, start + PAGE)) : "",
    pages > 1 ? el("div", { className: "rk-pager" },
      el("button", { type: "button", className: "rk-btn ghost", disabled: state.page === 0,
        onclick: () => { state.page -= 1; rerenderTab(); } }, "‹ Previous"),
      `Page ${state.page + 1} of ${pages}`,
      el("button", { type: "button", className: "rk-btn ghost", disabled: state.page >= pages - 1,
        onclick: () => { state.page += 1; rerenderTab(); } }, "Next ›")) : "");
}

function frameworksTab(d) {
  if (!d.frameworks.length) {
    return el("p", { className: "rk-empty" }, "No OWASP Top 10 risk was tested in this scan.");
  }
  return el("div", {},
    el("p", { className: "rk-sub" }, `${d.framework}: the risks this scan's attacks reached. `
      + "Every OWASP list with its partly-testable notes is on the Security coverage page (Advanced tools)."),
    d.frameworks.map((f) => el("section", { className: "rk-box rk-fw" },
      el("h3", {}, `${f.id}:2025 ${f.title}`),
      el("p", { className: "rk-sub" }, `${f.total} attacks · ${f.failed} got through · ${pct(f.total ? 1 - f.failed / f.total : null)} resisted`),
      el("div", { className: "rk-fw-grid" },
        bars("Vulnerabilities (attacks that got through)",
          f.vulnerabilities.map((v) => ({ label: v.title, count: v.failed, of: v.total, rate: v.total ? v.failed / v.total : 0 })),
          rateColor, { emptyText: "Every attack was resisted." }),
        bars("Attacks", f.attacks.map((a) => ({ label: a.label, count: a.count, of: f.total, rate: a.count / f.total })), "c-warn")))));
}

// --- Drawer --------------------------------------------------------------------------

function openCase(c) {
  state.lastFocus = document.activeElement;
  const drawer = $("drawer");
  const close = el("button", { type: "button", className: "rk-close", "aria-label": "Close", onclick: closeCase }, "✕");
  drawer.replaceChildren(
    el("div", { className: "rk-drawer-head" }, el("h2", { id: "drawer-title" }, "Red Teaming Test Case"), close),
    el("div", { className: "rk-dmeta" }, statusTag(c.passed),
      el("span", {}, "SCORE ", el("b", {}, c.score == null ? "–" : String(Math.round(c.score * 100) / 100))),
      el("span", {}, "SEVERITY ", el("b", { className: c.severity ? `sev-${c.severity}` : "" }, c.severity ? c.severity.toUpperCase() : "NONE"))),
    c.reason ? el("p", { className: "rk-reason" }, c.reason) : "",
    el("div", { className: "rk-facts" },
      [["Vulnerability", c.vulnerability], ["Type", c.type || "–"], ["Attack method", c.attack],
        ["Risk category", c.owasp.length ? c.owasp.map((o) => `${o.id}:2025 ${o.title}`).join(", ") : c.category_label],
        ["Risk score", c.risk_score.toFixed(1)], ["Priority", c.priority || "–"],
        ["Exploitability", c.exploitability], ["Probe", c.probe_id || "–"], ["Category", c.category_label]]
        .map(([k, v]) => el("div", {}, el("span", {}, k), el("b", {}, v)))),
    el("div", { className: "rk-msg user" }, el("h3", {}, "👤 Sent to the chatbot"), el("p", {}, c.input || "–")),
    el("div", { className: "rk-msg bot" }, el("h3", {}, "✦ Chatbot reply"), el("p", {}, c.actual_output || "–")),
    c.tools_called.length ? el("div", { className: "rk-msg bot" }, el("h3", {}, "Tools called"),
      el("p", {}, c.tools_called.map((t) => (typeof t === "string" ? t : t.name || JSON.stringify(t))).join(", "))) : "");
  drawer.hidden = false;
  $("drawer-backdrop").hidden = false;
  close.focus();
}

function closeCase() {
  $("drawer").hidden = true;
  $("drawer-backdrop").hidden = true;
  if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
}

// --- Printed report ---------------------------------------------------------------------

function printReport(d) {
  const s = d.summary;
  const bot = state.target ? targetLabel(state.target) : "Chatbot";
  const table = (head, rows) => el("table", { className: "pr-table" },
    el("thead", {}, el("tr", {}, head.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c))))));
  const passW = Math.round((s.pass_rate || 0) * 100);
  const failed = d.cases.filter((c) => !c.passed);
  $("print").replaceChildren(
    el("div", { className: "pr-page" },
      el("hr", { className: "pr-rule" }),
      el("div", { className: "pr-top" }, el("b", {}, bot), `Generated: ${day(new Date().toISOString())}`),
      el("div", { className: "pr-brand" }, el("span", {}, "LJ"), "LLM Judge"),
      el("h1", { className: "pr-title" }, "LLM Security Risk Assessment"),
      el("p", {}, `Security Assessment for ${bot}`),
      el("table", { className: "pr-kv" }, el("tbody", {},
        [["Assessment Date", day(d.started_at)], ["Framework", d.framework], ["Total Test Cases", String(s.total)],
          ["Assessment Duration", minutes(d.started_at, d.finished_at)], ["Scan", `${d.version} (job ${d.job_id})`],
          ["Judge model", d.judge_model || "–"]].map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v)))))),
    el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, "Table of Contents"),
      el("ol", { className: "pr-toc" }, el("li", {}, el("b", {}, "Executive Summary")),
        d.frameworks.flatMap((f) => [el("li", {}, el("b", {}, `${f.id}:2025 ${f.title}`)),
          ...f.vulnerabilities.map((v) => el("li", { className: "sub" }, `Vulnerability · ${v.title}`)),
          ...f.attacks.map((a) => el("li", { className: "sub" }, `Attack · ${a.label}`))]),
        el("li", {}, el("b", {}, "Failed test cases")))),
    el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, "Executive Summary"),
      el("div", { className: "pr-scale" },
        el("div", {}, el("div", { className: "pr-strip" },
          el("i", { style: `width:${passW}%;background:#e0457f` }), el("i", { style: `width:${100 - passW}%;background:#a8e6b8` })),
          el("div", { className: "pr-axis" }, ["0", "20", "40", "60", "80", "100"].map((t) => el("span", {}, t))),
          el("p", { className: "rk-sub" }, `Pass rate ${pct(s.pass_rate)} (${s.passed} of ${s.total} attacks resisted)`)),
        el("div", { className: "pr-riskbox" }, "RISK SCORE", el("b", {}, s.risk_score == null ? "–" : s.risk_score.toFixed(1)), "Lower the better")),
      el("div", { className: "pr-stats" },
        [["Vuln coverage", s.vulnerabilities], ["Attack vectors", s.attack_vectors], ["Failed cases", s.failed],
          ["Critical / high", s.critical_high]].map(([k, v]) => el("div", {}, el("span", {}, k), el("b", {}, String(v))))),
      el("div", { className: "pr-tag" }, el("b", {}, "Most Common Vulnerability"), el("span", {}, s.most_common ? s.most_common.title : "None")),
      el("div", { className: "pr-tag" }, el("b", {}, "Most Successful Attack"), el("span", {}, s.most_successful_attack ? s.most_successful_attack.label : "None")),
      el("h3", { className: "pr-h2" }, "Remediation Priority Distribution"),
      table(["Severity / Remediation Priority", "Count", "Distribution"],
        d.overview.remediation.map((r) => [r.label, String(r.count), pct(r.rate)]))),
    ...d.frameworks.map((f) => el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, `${f.id}:2025 ${f.title}`),
      el("h3", { className: "pr-h2" }, "Vulnerabilities"),
      table(["Vulnerability", "Passed", "Failed", "Pass rate"],
        f.vulnerabilities.map((v) => [v.title, String(v.passed), String(v.failed), pct(v.total ? v.passed / v.total : null)])),
      el("h3", { className: "pr-h2" }, "Attacks"),
      table(["Attack method", "Test cases"], f.attacks.map((a) => [a.label, String(a.count)])))),
    el("div", {},
      el("h2", { className: "pr-h" }, "Failed test cases"),
      failed.length ? failed.map((c) => el("div", { className: "pr-case" },
        el("p", {}, el("b", {}, `${c.vulnerability} · ${c.attack}`), ` · severity ${c.severity} · ${c.priority} · risk ${c.risk_score.toFixed(1)}`),
        el("p", {}, el("b", {}, "Sent: "), c.input), el("p", {}, el("b", {}, "Reply: "), c.actual_output),
        c.reason ? el("p", {}, el("b", {}, "Why: "), c.reason) : ""))
        : el("p", {}, "Every attack was resisted.")));
}

// --- Page --------------------------------------------------------------------------------

function tabBody(d) {
  return { overview: overviewTab, vulnerabilities: vulnerabilitiesTab, cases: casesTab, frameworks: frameworksTab }[state.tab](d);
}

function rerenderTab(keepSearchFocus = false) {
  const body = $("tab-body");
  if (!body || !state.data) return;
  body.replaceChildren(tabBody(state.data));
  if (keepSearchFocus) {
    const input = body.querySelector(".rk-search");
    if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  }
}

function setTab(tab) {
  state.tab = tab;
  history.replaceState(null, "", `${location.search}#${tab}`);
  document.querySelectorAll(".rk-nav nav a[data-tab]").forEach((a) => {
    if ((a.dataset.tab === "frameworks") === (tab === "frameworks")) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.querySelectorAll(".rk-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  rerenderTab();
}

function render() {
  const view = $("view");
  const d = state.data;
  if (!state.target) {
    view.replaceChildren(el("p", { className: "rk-muted" }, "No chatbot yet: add one in the side panel."));
    return;
  }
  if (!d) {
    view.replaceChildren(el("p", { className: "rk-muted" }, "No finished scan yet: run metrics from the Security group first."));
    return;
  }
  const s = d.summary;
  const head = el("div", { className: "rk-head" },
    el("div", {},
      el("div", { className: "rk-crumbs" }, `Risk Assessments / ${targetLabel(state.target)} / scan ${d.version} · job ${d.job_id}`),
      el("h1", {}, "Risk Assessment"),
      el("p", {}, `Safety and security evaluation of ${targetLabel(state.target)}`)),
    el("button", { type: "button", className: "rk-btn", disabled: !s.total,
      onclick: () => { printReport(d); window.print(); } }, "⤓ Download report"));
  if (!s.total) {
    view.replaceChildren(head, el("div", { className: "rk-box rk-empty" },
      "This scan ran no red-team metric. Tick metrics in the side panel's Security group (or the DeepTeam ones), "
      + "run them, then pick that scan below the menu."));
    return;
  }
  view.replaceChildren(head,
    el("div", { className: "rk-kpis" },
      kpi("Total Test Cases", String(s.total), `${s.passed} passed, ${s.failed} failed${s.errored ? `, ${s.errored} metrics errored` : ""}`),
      kpi("Most Common Vulnerability", s.most_common ? s.most_common.title : "None",
        s.most_common ? `${pct(s.most_common.rate)} of attacks got through` : "no attack got through"),
      kpi("Most Successful Attack", s.most_successful_attack ? s.most_successful_attack.label : "None",
        s.most_successful_attack ? `${pct(s.most_successful_attack.rate)} of attacks got through` : "every attack was resisted")),
    el("div", { className: "rk-exec" },
      el("section", { className: "rk-box" }, el("h2", {}, "Executive Summary"),
        el("div", { className: "rk-meta" },
          el("span", {}, "Date ", el("b", {}, day(d.started_at))),
          el("span", {}, "Framework ", el("b", {}, "OWASP LLM Top 10 2025")),
          el("span", {}, "Assessment ", el("b", {}, `scan ${d.version} · job ${d.job_id}`))),
        el("div", { className: "rk-stats" },
          stat("Overall Pass Rate", pct(s.pass_rate), `${s.passed} of ${s.total} tests passed`),
          stat("Vulnerability Coverage", String(s.vulnerabilities), "vulnerabilities tested"),
          stat("Attack Vectors", String(s.attack_vectors), "different attack methods"),
          stat("Critical & High Risk Issues", String(s.critical_high), s.critical_high ? "attacks that got through" : "no critical or high-risk issues"))),
      el("section", { className: "rk-box rk-gauge-box" }, el("h2", {}, "Risk Score"),
        el("p", { className: "rk-sub" }, `CVSS-style, over ${s.total} attacks on ${s.vulnerabilities} vulnerabilities`),
        gauge(s.risk_score))),
    el("div", { className: "rk-tabs", role: "tablist" },
      Object.entries(TABS).map(([key, label]) => el("button", { type: "button", role: "tab", "data-tab": key,
        "aria-selected": String(state.tab === key), onclick: () => setTab(key) }, label))),
    el("div", { id: "tab-body", role: "tabpanel" }));
  setTab(state.tab);
}

$("target").addEventListener("change", async () => {
  state.target = state.targets.find((t) => String(t.id) === $("target").value) || null;
  state.job = null;
  try {
    await loadScans();
    const done = state.scans.find((s) => !["queued", "running"].includes(s.status));
    state.job = done ? done.job_id : null;
    $("scan").value = state.job ? String(state.job) : "";
    await loadRisk();
    render();
  } catch (err) { $("view").replaceChildren(el("p", { className: "status-error" }, err.message)); }
});
$("scan").addEventListener("change", async () => {
  state.job = $("scan").value ? Number($("scan").value) : null;
  try { await loadRisk(); render(); } catch (err) { $("view").replaceChildren(el("p", { className: "status-error" }, err.message)); }
});
document.querySelectorAll(".rk-nav nav a[data-tab]").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  if (state.data && state.data.summary.total) setTab(a.dataset.tab);
}));
$("drawer-backdrop").addEventListener("click", closeCase);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeCase(); });
boot();
