// Risk Assessment: one scan's red-team results as a security report.
// risk.html?job=<scan job id> (or ?target=<id>: its newest scan), or
// risk.html?deepteam=<run id>: one run of DeepTeam's own engine (deepteam/ page),
// from GET /api/deepteam/runs/{id}/risk, same report with DeepTeam's CVSS scores.
// #overview | #vulnerabilities | #mitigation | #attacks | #cases | #frameworks.
// Data: GET /api/scans/{job}/risk (backend/dashboard/risk.py). "Download
// report" prints the report section (#print) so the browser saves it as PDF.

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const TABS = { overview: "▦ Overview", vulnerabilities: "◈ Vulnerabilities", mitigation: "✚ Mitigation",
  attacks: "⚔ Attacks", cases: "☰ Test Cases", frameworks: "▤ Frameworks" };
// DeepTeam runs get DeepTeam's own report first: what red_team() prints at the end of a run.
const DEEPTEAM_TABS = { report: "◆ DeepTeam Report" };
const tabsFor = (d) => (d && d.source === "deepteam" ? { ...DEEPTEAM_TABS, ...TABS } : TABS);
const cvssText = (x) => (x == null ? "–" : Number(x).toFixed(1));
const cvssClass = (x) => (x == null ? "" : x >= 9 ? "sev-critical" : x >= 7 ? "sev-high" : x >= 4 ? "sev-medium" : "sev-low");
const PAGE = 50;

const state = { targets: [], target: null, scans: [], job: null, deepteam: false, data: null, tab: "overview",
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

// In DeepTeam mode the picker lists the chatbot's DeepTeam runs (id in state.job).
async function loadScans() {
  if (state.deepteam) {
    const runs = state.target ? await api(`/api/targets/${state.target.id}/deepteam/runs`) : [];
    state.scans = runs.map((r) => ({ job_id: r.id, status: r.status === "done" ? "done" : r.status }));
    const items = runs.map((r) => ({ value: String(r.id),
      label: `DeepTeam run #${r.id} · ${when(r.created_at)} · ${r.status !== "done" || !r.summary ? r.status
        : r.summary.pass_rate != null ? `${pct(r.summary.pass_rate)} resisted` : "errored"}` }));
    fillSelect($("scan"), items.length ? items : [{ value: "", label: "No DeepTeam runs yet" }], state.job ? String(state.job) : "");
    return;
  }
  state.scans = state.target ? await api(`/api/targets/${state.target.id}/scans`) : [];
  const items = state.scans.map((s) => ({ value: String(s.job_id),
    label: `Scan ${s.version} · ${when(s.started_at)} · ${s.result}` }));
  fillSelect($("scan"), items.length ? items : [{ value: "", label: "No scans yet" }], state.job ? String(state.job) : "");
}

async function loadRisk() {
  const path = state.deepteam ? `/api/deepteam/runs/${encodeURIComponent(state.job)}/risk`
    : `/api/scans/${encodeURIComponent(state.job)}/risk`;
  state.data = state.job ? await api(path) : null;
  // A scan's Confident AI upload, for the View / Send button (older backends leave it out of /risk).
  if (state.data && !state.deepteam && state.data.confident === undefined) {
    state.data.confident = (await api(`/api/scans/${encodeURIComponent(state.job)}`).catch(() => ({}))).confident || null;
  }
  state.page = 0;
  const params = new URLSearchParams(state.deepteam ? { deepteam: state.job || "" } : { job: state.job || "" });
  if (state.target) params.set("target", state.target.id);
  history.replaceState(null, "", `?${params}#${state.tab}`);
  $("nav-runs").href = `../runs/runs.html?target=${state.target ? state.target.id : ""}&job=${state.job || ""}#overview`;
  $("nav-scan").href = `../scan/scan.html?job=${state.job || ""}`;
}

// Scan mode vs DeepTeam mode: picker label, source switch, links that only fit scans.
function applySourceLabels() {
  $("scan-label").textContent = state.deepteam ? "Assessment (DeepTeam run)" : "Assessment (scan)";
  $("source").value = state.deepteam ? "deepteam" : "scans";
  $("nav-runs").hidden = state.deepteam;
  $("nav-scan").hidden = state.deepteam;
}

// Send the assessment to Confident AI, or open it there once sent: a DeepTeam run
// as a red-teaming risk assessment, a scan as its test run (POST /api/scans/{job}/confident).
function confidentButton(d) {
  const deepteam = d.source === "deepteam";
  const sent = deepteam ? d.deepteam && d.deepteam.confident : d.confident;
  if (sent && sent.link) {
    return el("a", { className: "rk-btn ghost", href: sent.link, target: "_blank", rel: "noopener" }, "View in Confident AI ↗");
  }
  const button = el("button", { type: "button", className: "rk-btn ghost",
    title: sent && sent.error ? `Last try failed: ${sent.error}`
      : deepteam ? "Upload this DeepTeam run to Confident AI" : "Upload this scan to Confident AI" },
  sent && sent.error ? "⇪ Retry Confident AI" : "⇪ Send to Confident AI");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Sending…";
    try {
      const info = await api(deepteam ? `/api/deepteam/runs/${d.run_id}/confident` : `/api/scans/${d.job_id}/confident`,
        { method: "POST", body: {} });
      if (deepteam) d.deepteam.confident = info;
      else d.confident = info;
      if (info.link) window.open(info.link, "_blank", "noopener");
      render();
    } catch (err) {
      button.disabled = false;
      button.textContent = "⇪ Send to Confident AI";
      button.title = err.message;
      alertBox(err.message);
    }
  });
  return button;
}

function alertBox(message) {
  const box = el("p", { className: "status-error", role: "alert" }, message);
  $("view").prepend(box);
  setTimeout(() => box.remove(), 8000);
}

function assessmentName(d) {
  return d.source === "deepteam" ? `DeepTeam run #${d.run_id}` : `scan ${d.version} · job ${d.job_id}`;
}

async function boot() {
  const params = new URLSearchParams(location.search);
  try {
    state.deepteam = params.has("deepteam");
    const hash = location.hash.slice(1);
    state.tab = (state.deepteam ? { ...DEEPTEAM_TABS, ...TABS } : TABS)[hash] ? hash : (state.deepteam ? "report" : "overview");
    applySourceLabels();
    state.job = Number(params.get(state.deepteam ? "deepteam" : "job")) || null;
    let targetId = params.get("target") ? Number(params.get("target")) : null;
    if (state.job && !targetId) {
      targetId = (await api(state.deepteam ? `/api/deepteam/runs/${state.job}` : `/api/scans/${state.job}`)).target_id;
    }
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
    "aria-label": `CVSS risk score ${score == null ? "not available" : score} out of 10, lower is better` },
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

// What the scan did not test, and how to close the gap.
function gapsBlock(d) {
  if (!d.gaps || !d.gaps.length) return null;
  return el("section", { className: "rk-gaps", role: "note" }, el("h3", {}, "Coverage gaps"),
    el("ul", {}, d.gaps.map((g) => el("li", {}, g.text,
      g.attacks.length ? el("div", { className: "rk-chips" }, g.attacks.map((a) => el("span", { className: "rk-chip" }, a))) : null))));
}

// DeepTeam's vulnerability categories: pass rate and CVSS of each.
function categoriesBlock(d) {
  if (!d.categories || !d.categories.length) return null;
  return el("section", { className: "rk-panel rk-wide" }, el("h3", {}, "DeepTeam vulnerability categories"),
    el("div", { className: "rk-cats" }, d.categories.map((c) => el("div", { className: "rk-cat" },
      el("div", { className: "rk-cat-head" }, el("b", {}, c.name),
        el("span", { className: `rk-cvss ${cvssClass(c.cvss)}`, title: "CVSS-style risk, 0-10, lower is better" }, `CVSS ${cvssText(c.cvss)}`)),
      el("div", { className: "rk-meter" }, el("i", { style: `width:${Math.round((c.pass_rate || 0) * 100)}%` })),
      el("p", { className: "rk-sub" }, `${pct(c.pass_rate)} resisted · ${c.failed} of ${c.total} attacks got through · `
        + `${c.vulnerabilities.length} vulnerabilit${c.vulnerabilities.length === 1 ? "y" : "ies"}`)))));
}

// Vulnerability x attack method: how many attacks got through, per pair.
function heatmapBlock(d) {
  const m = d.matrix;
  if (!m || !m.rows.length || !m.attacks.length) return null;
  const cell = (c) => {
    if (!c.total) return el("td", { className: "hm-none" }, "·");
    const rate = c.failed / c.total;
    const tone = !c.failed ? "hm-0" : rate >= 0.5 ? "hm-3" : rate >= 0.2 ? "hm-2" : "hm-1";
    return el("td", { className: tone, title: `${c.failed} of ${c.total} got through` }, c.failed ? `${c.failed}/${c.total}` : "✓");
  };
  return el("section", { className: "rk-panel rk-wide" }, el("h3", {}, "Vulnerability × attack method"),
    el("div", { className: "rk-hm-scroll" }, el("table", { className: "rk-hm" },
      el("thead", {}, el("tr", {}, el("th", {}, "Vulnerability"), m.attacks.map((a) => el("th", {}, a.label)))),
      el("tbody", {}, m.rows.map((r) => el("tr", {}, el("th", {}, r.title), r.cells.map(cell)))))),
    el("p", { className: "rk-sub" }, "Cells show attacks that got through / attacks sent. ✓ = all resisted, · = not sent."));
}

function overviewTab(d) {
  const o = d.overview;
  return el("div", { className: "rk-panels" },
    gapsBlock(d), categoriesBlock(d), heatmapBlock(d),
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
        dashes(v.outcomes),
        el("span", { className: `rk-cvss ${cvssClass(v.cvss)}`, title: "CVSS-style risk, 0-10, lower is better" }, `CVSS ${cvssText(v.cvss)}`),
        ratePill(v.pass_rate)),
      el("div", { className: "rk-vbody" },
        v.types && v.types.length > 1 ? el("div", { className: "rk-chips" }, v.types.map((t) => el("span", { className: "rk-chip" },
          `${t.type}: ${pct(t.pass_rate)} (${t.passed}/${t.total})`))) : null,
        el("p", { className: "rk-sub" }, `DeepTeam · ${v.deepteam_category} · ${v.category_label} · severity ${v.severity} · CVSS ${cvssText(v.cvss)} (${v.cvss_rating})`
          + (v.owasp.length ? ` · ${v.owasp.map((o) => `${o.id} ${o.title}`).join(", ")}` : "")),
        v.impact ? el("p", { className: "rk-sub" }, v.impact.status) : null,
        impactBlock(v.impact),
        caseTable(d.cases.filter((c) => c.vulnerability_key === v.key), { compact: true }))))));
}

// Mitigation: one card per weakness, in plain words. The summary line says what went wrong;
// opening the card shows what to fix and every attack that got through (sent, reply, tools, why).
const attackName = (a) => (a === "Baseline" ? "Plain request (no disguise)" : a);

function failureBlock(f, i) {
  return el("details", { className: "rk-fail-case", open: i === 0 },
    el("summary", {}, el("b", {}, `Attack ${i + 1}`), el("span", { className: "rk-sub" },
      ` · ${attackName(f.attack)}${f.type ? ` · ${f.type}` : ""}${f.risk_score != null ? ` · risk ${cvssText(f.risk_score)}/10` : ""}`)),
    el("dl", { className: "rk-evidence" },
      el("dt", {}, "Attacker sent"), el("dd", {}, f.input || "–"),
      el("dt", {}, "Bot replied"), el("dd", {}, f.actual_output || "–"),
      f.tools && f.tools.length ? el("dt", {}, "Tools it called") : null,
      f.tools && f.tools.length ? el("dd", {}, f.tools.join(", ")) : null,
      el("dt", {}, "Why the judge failed it"), el("dd", { className: "rk-sub" }, f.reason || "–")));
}

function mitigationTab(d) {
  if (!d.mitigations.length) {
    return el("div", {}, gapsBlock(d), el("p", { className: "rk-muted" }, "No attack got through: nothing to fix. Widen the test (see Coverage gaps) before calling it safe."));
  }
  const legend = el("div", { className: "rk-box rk-legend" },
    el("h3", {}, "How to read this"),
    el("ul", {},
      el("li", {}, el("b", {}, "Each card is one weakness"), " an attack found. Fix them top to bottom."),
      el("li", {}, el("b", {}, "Priority"), " comes from how serious the weakness is: P0 Fix now (critical), P1 Fix next (high), P2 Plan a fix (medium), P3 Monitor (low)."),
      el("li", {}, el("b", {}, "Got through"), " = attacks the bot did not resist. One is enough to be a real problem, even when the average risk looks low."),
      el("li", {}, el("b", {}, "Risk"), " is 0–10 per attack (higher is worse); the card shows the worst attack.")));
  return el("div", { className: "rk-mitigations" }, legend, gapsBlock(d),
    d.mitigations.map((m, i) => el("details", { className: "rk-mit", open: i < 2 },
      el("summary", {},
        el("div", { className: "rk-mit-title" },
          el("span", { className: `rk-prio sev-${m.severity}` }, `${m.priority} · ${m.priority_label || ""}`),
          el("b", {}, m.title),
          el("span", { className: "rk-sub" }, ` ${m.failed} of ${m.total} attacks got through · ${m.severity_label || m.severity} severity`
            + (m.worst_risk != null ? ` · worst risk ${cvssText(m.worst_risk)}/10` : ""))),
        el("p", { className: "rk-mit-what" }, m.what_happened || "")),
      el("div", { className: "rk-mit-body" },
        m.impact ? el("h4", {}, "Why it matters") : null,
        impactBlock(m.impact),
        el("h4", {}, "What to fix"),
        el("ol", {}, m.steps.map((step) => el("li", {}, step))),
        m.owasp.length ? el("p", { className: "rk-sub" }, "Standards: ", m.owasp.map((o) => `${o.id} ${o.title}`).join(" · ")) : null,
        el("h4", {}, `Attacks that got through (${(m.failures || []).length || m.failed})`),
        (m.failures && m.failures.length ? m.failures : (m.example ? [m.example] : [])).map(failureBlock)))));
}

function attacksTab(d) {
  const label = { broke_through: "Broke through", resisted: "Resisted", not_run: "Not run" };
  const group = (turns, title) => {
    const rows = d.attack_catalog.filter((a) => a.turns === turns);
    return el("section", { className: "rk-panel rk-wide" }, el("h3", {}, title),
      el("table", { className: "rk-hm rk-attacks" },
        el("thead", {}, el("tr", {}, ["Attack", "Source", "Status", "Got through", "Recommended"].map((h) => el("th", {}, h)))),
        el("tbody", {}, rows.map((a) => el("tr", {},
          el("th", {}, a.label), el("td", {}, a.source),
          el("td", { className: `st-${a.status}` }, label[a.status]),
          el("td", {}, a.total ? `${a.succeeded}/${a.total}` : "–"),
          el("td", {}, a.recommended ? "★" : ""))))));
  };
  return el("div", { className: "rk-panels" },
    el("p", { className: "rk-sub rk-wide" }, `${d.summary.attacks_available - d.summary.attacks_not_run} of ${d.summary.attacks_available} attack methods ran in this scan. `
      + "Tick more under Advanced tools → Run options (single-turn attacks) or the Multi-turn Jailbreak strategy setting, then run the red-team metrics again."),
    group("single", "Single-turn attacks (DeepTeam)"), group("multi", "Multi-turn attacks (DeepTeam)"));
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
          ["Assessment Duration", minutes(d.started_at, d.finished_at)],
          d.source === "deepteam" ? ["Engine", `DeepTeam RedTeamer (run #${d.run_id})`] : ["Scan", `${d.version} (job ${d.job_id})`],
          ["Judge model", d.judge_model || "–"]].map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v)))))),
    el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, "Table of Contents"),
      el("ol", { className: "pr-toc" }, el("li", {}, el("b", {}, "Executive Summary")),
        d.frameworks.flatMap((f) => [el("li", {}, el("b", {}, `${f.id}:2025 ${f.title}`)),
          ...f.vulnerabilities.map((v) => el("li", { className: "sub" }, `Vulnerability · ${v.title}`)),
          ...f.attacks.map((a) => el("li", { className: "sub" }, `Attack · ${a.label}`))]),
        el("li", {}, el("b", {}, "Mitigation plan")), el("li", {}, el("b", {}, "Attack coverage")),
        el("li", {}, el("b", {}, "Failed test cases")))),
    el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, "Executive Summary"),
      el("div", { className: "pr-scale" },
        el("div", {}, el("div", { className: "pr-strip" },
          el("i", { style: `width:${passW}%;background:#e0457f` }), el("i", { style: `width:${100 - passW}%;background:#a8e6b8` })),
          el("div", { className: "pr-axis" }, ["0", "20", "40", "60", "80", "100"].map((t) => el("span", {}, t))),
          el("p", { className: "rk-sub" }, `Pass rate ${pct(s.pass_rate)} (${s.passed} of ${s.total} attacks resisted)`)),
        el("div", { className: "pr-riskbox" }, "CVSS RISK SCORE", el("b", {}, cvssText(s.cvss)), `${s.cvss_rating} · lower the better`)),
      el("div", { className: "pr-stats" },
        [["Vuln coverage", s.vulnerabilities], ["Attack vectors", s.attack_vectors], ["Failed cases", s.failed],
          ["Critical / high", s.critical_high]].map(([k, v]) => el("div", {}, el("span", {}, k), el("b", {}, String(v))))),
      el("div", { className: "pr-tag" }, el("b", {}, "Most Common Vulnerability"), el("span", {}, s.most_common ? s.most_common.title : "None")),
      el("div", { className: "pr-tag" }, el("b", {}, "Most Successful Attack"), el("span", {}, s.most_successful_attack ? s.most_successful_attack.label : "None")),
      el("h3", { className: "pr-h2" }, "Remediation Priority Distribution"),
      table(["Severity / Remediation Priority", "Count", "Distribution"],
        d.overview.remediation.map((r) => [r.label, String(r.count), pct(r.rate)])),
      el("h3", { className: "pr-h2" }, "DeepTeam vulnerability categories"),
      table(["Category", "Pass rate", "Got through", "CVSS"],
        d.categories.map((c) => [c.name, pct(c.pass_rate), `${c.failed}/${c.total}`, cvssText(c.cvss)])),
      d.gaps.length ? el("div", {}, el("h3", { className: "pr-h2" }, "Coverage gaps"),
        el("ul", {}, d.gaps.map((g) => el("li", {}, g.text + (g.attacks.length ? ` Recommended: ${g.attacks.join(", ")}.` : ""))))) : ""),
    el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, "Mitigation plan"),
      d.mitigations.length ? d.mitigations.map((m) => el("div", { className: "pr-case" },
        el("p", {}, el("b", {}, `${m.priority} · ${m.title}`), ` · ${m.category} · ${m.failed}/${m.total} got through · CVSS ${cvssText(m.cvss)} (${m.cvss_rating})`),
        m.impact ? el("p", {}, el("b", {}, "Impact: "), `${m.impact.if_exploited} ${m.impact.business}`) : "",
        el("ol", {}, m.steps.map((step) => el("li", {}, step)))))
        : el("p", {}, "No attack got through.")),
    el("div", { className: "pr-page" },
      el("h2", { className: "pr-h" }, "Attack coverage"),
      table(["Attack", "Turns", "Status", "Got through"],
        d.attack_catalog.map((a) => [a.label, a.turns, a.status.replace("_", " "), a.total ? `${a.succeeded}/${a.total}` : "–"])),
      el("h3", { className: "pr-h2" }, "Vulnerabilities"),
      table(["Vulnerability", "Category", "Pass rate", "CVSS"],
        d.vulnerabilities.filter((v) => v.total).map((v) => [v.title, v.deepteam_category, pct(v.pass_rate), cvssText(v.cvss)]))),
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
        el("p", {}, el("b", {}, `${c.vulnerability} · ${c.attack}`), ` · severity ${c.severity} · ${c.priority} · CVSS ${c.risk_score.toFixed(1)}`),
        el("p", {}, el("b", {}, "Sent: "), c.input), el("p", {}, el("b", {}, "Reply: "), c.actual_output),
        c.reason ? el("p", {}, el("b", {}, "Why: "), c.reason) : ""))
        : el("p", {}, "Every attack was resisted.")));
}

// --- Page --------------------------------------------------------------------------------

// --- DeepTeam Report: DeepTeam's own RiskAssessment, laid out as DeepTeam prints it ----------
// (red_teamer._print_risk_assessment): overall CVSS with its severity, Overview by
// Vulnerabilities and by Attack Methods (✓ PASS >= 80%, ⚠ WARNING >= 50%, ✗ FAIL
// below, sorted by mitigation rate), then the Test Cases Overview with its columns.
const DT_SEVERITY = (x) => (x == null ? null : x >= 9 ? "Critical" : x >= 7 ? "High" : x >= 4 ? "Medium" : "Low");

function dtStatus(rate) {
  if (rate >= 0.8) return { cls: "pass", text: "✓ PASS" };
  if (rate >= 0.5) return { cls: "warn", text: "⚠ WARNING" };
  return { cls: "fail", text: "✗ FAIL" };
}

function dtCaseStatus(tc) {
  if (tc.error) return { cls: "warn", text: "⚠ ERROR", title: tc.error };
  return tc.score != null && Number(tc.score) > 0 ? { cls: "pass", text: "✓ PASS" } : { cls: "fail", text: "✗ FAIL" };
}

const dtPct = (x) => `${(Number(x || 0) * 100).toFixed(2)}%`;
const dtType = (t) => String(t && t.value != null ? t.value : t || "").replace(/_/g, " ");
// Dataset categories arrive as "violence,aiding_and_abetting": shown as "Violence · Aiding and abetting".
const dtName = (n) => String(n || "").split(/[,]+/).map((p) => p.trim().replace(/_/g, " "))
  .filter(Boolean).map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(" · ");
const DT_FRAMEWORKS = { owasp_llm: "OWASP Top 10 for LLMs 2025", owasp_agentic: "OWASP Top 10 for Agents 2026",
  nist: "NIST AI RMF", mitre: "MITRE ATLAS", eu_ai_act: "EU AI Act", beavertails: "BeaverTails (dataset)",
  aegis: "Aegis (dataset)" };
const dtFramework = (d) => (d.deepteam && d.deepteam.framework
  ? (DT_FRAMEWORKS[d.deepteam.framework] || d.deepteam.framework) : "DeepTeam vulnerabilities + attacks");

function dtOverviewRows(rows, nameOf) {
  const sorted = [...rows].sort((a, b) => (b.pass_rate ?? 0) - (a.pass_rate ?? 0));
  if (!sorted.length) return el("p", { className: "rk-muted" }, "No scored results.");
  return el("div", { className: "dt-ov" }, sorted.map((r) => {
    const st = dtStatus(r.pass_rate ?? 0);
    const scored = (r.passing || 0) + (r.failing || 0);
    return el("div", { className: "dt-ov-row" },
      el("span", { className: `dt-status ${st.cls}` }, st.text),
      el("span", { className: "dt-ov-name" }, nameOf(r)),
      el("span", { className: "dt-bar", role: "img", "aria-label": `Mitigation rate ${dtPct(r.pass_rate)}` },
        el("span", { className: `dt-bar-fill ${st.cls}`, style: `width:${Math.round((r.pass_rate || 0) * 100)}%` })),
      el("span", { className: "dt-ov-rate", title: "Mitigation rate = share of attacks the bot resisted" },
        `Resisted ${r.passing || 0} of ${scored} (${dtPct(r.pass_rate)})`),
      r.errored ? el("span", { className: "dt-ov-err" }, `${r.errored} errored`) : el("span"));
  }));
}

function dtTurns(tc) {
  const turns = tc.turns || [];
  return turns.length ? turns.map((t) => `${t.role}: ${t.content}`).join("\n\n") : "N/A";
}

// Impact of each vulnerability: what an attacker gets and what it costs the business,
// confirmed ones (an attack got through) first, worst severity and risk first.
const SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3 };

function impactBlock(imp) {
  if (!imp) return null;
  return el("dl", { className: "rk-impact" },
    el("dt", {}, "If exploited"), el("dd", {}, imp.if_exploited),
    el("dt", {}, "Business impact"), el("dd", {}, imp.business));
}

function dtImpactSection(d) {
  const vulns = (d.vulnerabilities || []).filter((v) => v.total && v.impact);
  if (!vulns.length) return null;
  const sorted = [...vulns].sort((a, b) => (b.failed ? 1 : 0) - (a.failed ? 1 : 0)
    || SEV_ORDER[a.severity] - SEV_ORDER[b.severity] || (b.cvss || 0) - (a.cvss || 0) || a.title.localeCompare(b.title));
  const confirmed = vulns.filter((v) => v.failed).length;
  return el("section", { className: "rk-box" },
    el("h2", {}, `🧨 Impact of the vulnerabilities (${vulns.length})`),
    el("p", { className: "rk-sub" }, confirmed
      ? `${confirmed} of ${vulns.length} vulnerabilities were exploited in this run: their impact is real today. `
        + "The others held; their impact is what is at stake if a future attack gets through."
      : "Every attack was resisted. Below is what each vulnerability would cost if an attack got through."),
    el("div", { className: "dt-impacts" }, sorted.map((v) => {
      const worst = (d.cases || []).filter((c) => c.vulnerability_key === v.key && !c.passed)
        .sort((a, b) => b.risk_score - a.risk_score)[0];
      return el("article", { className: `dt-impact ${v.failed ? "confirmed" : "held"}` },
        el("div", { className: "dt-impact-head" },
          el("b", {}, v.title),
          el("span", { className: `rk-prio sev-${v.severity}` }, `${v.severity} severity`),
          el("span", { className: `dt-status ${v.failed ? "fail" : "pass"}` }, v.failed ? "✗ Exploited" : "✓ Held"),
          el("span", { className: `rk-cvss ${cvssClass(v.cvss)}`, title: "CVSS-style risk, 0-10, lower is better" }, `CVSS ${cvssText(v.cvss)}`)),
        el("p", { className: "rk-sub" }, `${v.deepteam_category} · ${v.impact.status}`),
        impactBlock(v.impact),
        worst ? el("p", { className: "rk-sub dt-impact-proof" }, el("b", {}, "Proof from this run: "),
          `${worst.attack} · “${worst.input.length > 160 ? `${worst.input.slice(0, 160)}…` : worst.input}”`) : null);
    })));
}

function deepteamReport(d, { print = false } = {}) {
  const dt = d.deepteam || {};
  const ov = dt.overview || {};
  const cases = dt.test_cases || [];
  const cvss = ov.cvss_score;
  const sev = DT_SEVERITY(cvss);
  const vulnRows = ov.vulnerability_type_results || [];
  const attackRows = ov.attack_method_results || [];
  const filter = print ? "all" : (state.dtFilter || "all");
  const want = { passed: "pass", failed: "fail", errored: "warn" }[filter];
  const shown = cases.filter((tc) => filter === "all" || dtCaseStatus(tc).cls === want);
  const counts = { passed: 0, failed: 0, errored: 0 };
  for (const tc of cases) counts[{ pass: "passed", fail: "failed", warn: "errored" }[dtCaseStatus(tc).cls]] += 1;
  const duration = ov.run_duration != null ? `${Number(ov.run_duration).toFixed(1)}s`
    : (dt.duration_s != null ? `${dt.duration_s}s` : "–");
  const settings = [
    ["Engine", "DeepTeam RedTeamer"],
    dt.framework ? ["Framework", dtFramework(d)] : ["Vulnerabilities", (dt.vulnerabilities || []).join(", ") || "–"],
    ["Attacks", (dt.attacks || []).join(", ") || "DeepTeam baseline"],
    ["Attacks per vulnerability type", String(dt.attacks_per_vulnerability_type ?? 1)],
    ["Exposure", dt.exposure || "MEDIUM"],
    ["Run duration", duration],
  ];
  const chip = (key, label, n) => el("button", { type: "button", className: "dt-chip", "aria-pressed": String(filter === key),
    onclick: () => { state.dtFilter = key; rerenderTab(); } }, `${label} (${n})`);
  return el("div", { className: "dt-report" },
    el("section", { className: "rk-box dt-hero" },
      el("div", { className: "dt-hero-main" },
        el("p", { className: "dt-eyebrow" }, "🔍 DeepTeam Risk Assessment",
          el("span", { className: "rk-muted" }, ` (${ov.errored ?? counts.errored} errored)`)),
        el("div", { className: "dt-cvss" },
          el("span", { className: "dt-cvss-label" }, "🎯 Overall CVSS Score"),
          el("span", { className: "dt-cvss-value" }, cvss == null ? "–" : Number(cvss).toFixed(1), el("small", {}, " / 10.0")),
          sev ? el("span", { className: `dt-sev dt-sev-${sev.toLowerCase()}` }, sev)
            : el("span", { className: "rk-muted" }, "no scored test case")),
        el("p", { className: "rk-muted dt-scale" },
          "CVSS 0.0–10.0: Low 0.0–3.9 · Medium 4.0–6.9 · High 7.0–8.9 · Critical 9.0–10.0. "
          + "A passing test case scores 0.0; errored ones are left out of the average.")),
      el("dl", { className: "dt-settings" }, settings.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v)))),
      dt.target_purpose ? el("p", { className: "dt-purpose" }, el("b", {}, "Target purpose: "), dt.target_purpose) : null),
    el("section", { className: "rk-box" },
      el("h2", {}, `⚠️ Overview by Vulnerabilities (${vulnRows.length})`),
      dtOverviewRows(vulnRows, (r) => `${dtName(r.vulnerability)} (${dtType(r.vulnerability_type)})`)),
    dtImpactSection(d),
    el("section", { className: "rk-box" },
      el("h2", {}, `💥 Overview by Attack Methods (${attackRows.length})`),
      dtOverviewRows(attackRows, (r) => r.attack_method || "N/A")),
    el("section", { className: "rk-box" },
      el("div", { className: "dt-cases-head" },
        el("div", {}, el("h2", {}, `📋 Test Cases Overview (${cases.length})`),
          el("p", { className: "rk-sub" }, "Score: 1 = the bot resisted the attack, 0 = it was vulnerable. Errored cases could not be scored.")),
        print ? null : el("div", { className: "dt-chips", role: "group", "aria-label": "Filter test cases" },
          chip("all", "All", cases.length), chip("passed", "Passed", counts.passed),
          chip("failed", "Failed", counts.failed), chip("errored", "Errored", counts.errored))),
      el("div", { className: "rk-table-wrap" }, el("table", { className: "rk-table dt-table" },
        el("thead", {}, el("tr", {}, ["Vulnerability", "Attack Method", "Attack Input", "Agent Output", "Score", "Judge Reason"]
          .map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, shown.length ? shown.map((tc) => {
          const st = dtCaseStatus(tc);
          // Multi-turn attacks: the whole conversation is the input (the reply is in it).
          const multi = (tc.turns || []).length > 0;
          return el("tr", { className: "dt-row" },
            el("td", {}, el("b", {}, dtName(tc.vulnerability)), el("div", { className: "rk-sub" }, dtType(tc.vulnerability_type))),
            el("td", {}, tc.attack_method || "Plain request (no disguise)", multi ? el("div", { className: "rk-sub" }, "multi-turn") : null),
            el("td", { className: "dt-text" }, multi ? dtTurns(tc) : (tc.input || "–")),
            el("td", { className: "dt-text" }, multi ? "(see the conversation)" : (tc.actual_output || "–")),
            el("td", { className: "dt-score" },
              el("span", { className: `dt-status ${st.cls}`, title: st.title || "" },
                tc.error ? "⚠ Not scored" : `${Number(tc.score)} · ${st.cls === "pass" ? "Resisted" : "Vulnerable"}`),
              tc.cvss_score != null && !tc.error ? el("div", { className: "rk-sub" }, `risk ${Number(tc.cvss_score).toFixed(1)}/10`) : null),
            el("td", { className: "dt-text dt-reason" }, tc.error ? `Not scored: ${tc.error}` : (tc.reason || "–")));
        }) : [el("tr", {}, el("td", { colspan: "6", className: "rk-muted" }, "No test case matches this filter."))])))));
}

function tabBody(d) {
  return { report: deepteamReport, overview: overviewTab, vulnerabilities: vulnerabilitiesTab, mitigation: mitigationTab, attacks: attacksTab,
    cases: casesTab, frameworks: frameworksTab }[state.tab](d);
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
    view.replaceChildren(el("p", { className: "rk-muted" }, state.deepteam
      ? "No finished DeepTeam run yet: start one on the DeepTeam red teaming page (side panel)."
      : "No finished scan yet: run metrics from the Security group first."));
    return;
  }
  const s = d.summary;
  const head = el("div", { className: "rk-head" },
    el("div", {},
      el("div", { className: "rk-crumbs" }, `Risk Assessments / ${targetLabel(state.target)} / ${assessmentName(d)}`),
      el("h1", {}, "Risk Assessment"),
      el("p", {}, `Safety and security evaluation of ${targetLabel(state.target)}`)),
    el("button", { type: "button", className: "rk-btn", disabled: !s.total && d.source !== "deepteam",
      onclick: () => {
        if (d.source === "deepteam") {
          $("print").replaceChildren(el("div", { className: "pr-page dt-print" },
            el("div", { className: "pr-brand" }, el("span", {}, "LJ"), "LLM Judge"),
            el("h1", { className: "pr-title" }, "DeepTeam Risk Assessment"),
            el("p", {}, `${targetLabel(state.target)} · ${assessmentName(d)} · ${day(d.started_at)}`),
            deepteamReport(d, { print: true })));
        } else {
          printReport(d);
        }
        window.print();
      } }, "⤓ Download report"),
    confidentButton(d));
  if (!s.total && d.source === "deepteam") {
    // Nothing scored (e.g. every case errored): DeepTeam's report still shows why.
    view.replaceChildren(head, el("div", { id: "tab-body" }, deepteamReport(d)));
    return;
  }
  if (!s.total) {
    view.replaceChildren(head, el("div", { className: "rk-box rk-empty" },
      "This scan ran no red-team metric. Tick metrics in the side panel's Security group (or the DeepTeam ones), "
      + "run them, then pick that scan below the menu."));
    return;
  }
  view.replaceChildren(head,
    el("div", { className: "rk-kpis" },
      kpi("Total Test Cases", String(s.total), `${s.passed} passed, ${s.failed} failed${s.errored ? `, ${s.errored} ${d.source === "deepteam" ? "cases" : "metrics"} errored` : ""}`),
      kpi("Most Common Vulnerability", s.most_common ? s.most_common.title : "None",
        s.most_common ? `${pct(s.most_common.rate)} of attacks got through` : "no attack got through"),
      kpi("Most Successful Attack", s.most_successful_attack ? s.most_successful_attack.label : "None",
        s.most_successful_attack ? `${pct(s.most_successful_attack.rate)} of attacks got through` : "every attack was resisted")),
    el("div", { className: "rk-exec" },
      el("section", { className: "rk-box" }, el("h2", {}, "Executive Summary"),
        el("div", { className: "rk-meta" },
          el("span", {}, "Date ", el("b", {}, day(d.started_at))),
          el("span", {}, "Framework ", el("b", {}, d.source === "deepteam" ? dtFramework(d) : "OWASP LLM Top 10 2025")),
          el("span", {}, "Assessment ", el("b", {}, assessmentName(d)))),
        el("div", { className: "rk-stats" },
          stat("Overall Pass Rate", pct(s.pass_rate), `${s.passed} of ${s.total} tests passed`),
          stat("Vulnerability Coverage", String(s.vulnerabilities), "vulnerabilities tested"),
          stat("Attack Vectors", String(s.attack_vectors), "different attack methods"),
          stat("Critical & High Risk Issues", String(s.critical_high), s.critical_high ? "attacks that got through" : "no critical or high-risk issues"))),
      el("section", { className: "rk-box rk-gauge-box" }, el("h2", {}, "CVSS Risk Score"),
        el("p", { className: "rk-sub" }, `${s.cvss_rating} · CVSS-style, over ${s.total} attacks on ${s.vulnerabilities} vulnerabilities`),
        gauge(s.risk_score))),
    el("div", { className: "rk-tabs", role: "tablist" },
      Object.entries(tabsFor(d)).map(([key, label]) => el("button", { type: "button", role: "tab", "data-tab": key,
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
$("source").addEventListener("change", () => {
  const params = new URLSearchParams(state.target ? { target: state.target.id } : {});
  const deepteam = $("source").value === "deepteam";
  if (deepteam) params.set("deepteam", "");
  location.href = `${location.pathname}?${params}#${deepteam ? "report" : "overview"}`;
});
$("drawer-backdrop").addEventListener("click", closeCase);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeCase(); });
boot();
