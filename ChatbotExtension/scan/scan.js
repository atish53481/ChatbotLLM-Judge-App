// One scan in full, opened from the dashboard's Scan history in its own tab:
// scan.html?job=<job id>. Header with the overall result, its facts, the metric
// summary, then every metric with its reason and the cases it scored.

const $ = (id) => document.getElementById(id);

const RESULT_LABEL = {
  passed: ["Passed", "status-pass"], failed: ["Failed", "status-fail"],
  incomplete: ["Incomplete", "status-error"], running: ["Running", ""], queued: ["Queued", ""],
};
const STATUS_LABEL = { pass: ["Pass", "status-pass"], fail: ["Fail", "status-fail"] };

const statusCell = (status) => {
  const [text, css] = STATUS_LABEL[status] || ["Not scored", "status-error"];
  return el("td", { className: css }, text);
};
const minimum = (r) => (r.threshold == null ? "–" : `${comparator(r.direction)} ${formatScore(r.threshold)}`);

function duration(start, end) {
  const seconds = (new Date(end) - new Date(start)) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return "–";
  return seconds < 90 ? `${Math.round(seconds)} s` : `${Math.round(seconds / 60)} min`;
}

function header(scan, chatbot, scans, extras = {}) {
  const [label, css] = RESULT_LABEL[scan.result] || [scan.result, ""];
  const index = scans.findIndex((s) => s.job_id === scan.job_id);
  const newer = scans[index - 1];
  const older = scans[index + 1];
  const link = (other, text) => el("a", { href: `scan.html?job=${other.job_id}` }, text);
  const pdfHref = `../report/report.html?target=${encodeURIComponent(scan.target_id)}`
    + `&job=${encodeURIComponent(scan.job_id)}&all=1`;
  return el("div", { className: "scan-head" },
    el("h1", {}, `${chatbot} · Scan ${scan.version}`),
    el("span", { className: `badge ${css}` }, `${label} · ${scanTally(scan)}`),
    el("div", { className: "scan-actions" },
      el("a", {
        className: "scan-action", href: pdfHref, target: "_blank", rel: "noopener",
        title: "Open the printable report with every section and case, then Save as PDF",
      }, "Download report (PDF)"),
      el("button", {
        type: "button", className: "scan-action",
        title: "Save this scan as one HTML file: summary, every metric, every case, reasons and security",
        onclick: () => downloadFullReport(scan, chatbot, extras.title, extras.security, extras.byKey),
      }, "Download full report (HTML)")),
    el("nav", { className: "scan-nav", "aria-label": "Other scans" },
      older ? link(older, `← Scan ${older.version}`) : "",
      newer ? link(newer, `Scan ${newer.version} →`) : ""),
  );
}

// Self-contained HTML of this scan with every metric and every case (question,
// reply, expected, score, result, judge reason). Offline-friendly archive.
function downloadFullReport(scan, chatbot, title, security, byKey = new Map()) {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const statusLabel = (s) => ({ pass: "Pass", fail: "Fail", error: "Not scored" }[s] || s || "–");
  const resultLabel = (r) => (RESULT_LABEL[r] || [r || "–"])[0];
  const min = (r) => (r.threshold == null ? "–"
    : `${r.direction === "lower" ? "≤" : "≥"} ${formatScore(r.threshold)}`);
  const opts = scan.run_options || {};
  const de = opts.deepeval || {};
  const yes = (v) => (v === undefined ? "default" : v ? "on" : "off");
  const cost = (scan.results || []).reduce(
    (sum, r) => sum + (typeof r.judge_cost_usd === "number" ? r.judge_cost_usd : 0), 0);

  const summaryRows = (scan.results || []).map((r) => {
    const cls = r.status === "pass" ? "ok" : r.status === "fail" ? "bad" : "";
    return `<tr class="${cls}"><td>${esc(title(r.key))}</td>`
      + `<td class="num">${formatScore(r.score)}</td><td class="num">${esc(min(r))}</td>`
      + `<td>${esc(statusLabel(r.status))}</td>`
      + `<td class="num">${r.cases_run ? casesCell(r) : "–"}</td></tr>`;
  }).join("");

  const skippedRows = (scan.skipped || []).map((s) =>
    `<tr><td>${esc(s.title || title(s.key))}</td><td>${esc(s.reason || "")}</td></tr>`).join("");

  // Metric catalog rows (kind, scores_on, improve, …) so failedCaseHtml matches the app.
  const metricBlocks = (scan.results || []).map((r) => {
    const metric = byKey.get(r.key) || { key: r.key, title: title(r.key), kind: "single",
      direction: r.direction, threshold: r.threshold, scores_on: [] };
    const rows = r.rows || [];
    const caseBlocks = rows.map((row, i) => {
      const cls = row.passed === true ? "ok" : row.passed === false ? "bad" : "";
      const res = row.passed === undefined ? "–" : row.passed ? "Pass" : "Fail";
      const failed = row.passed === false
        ? failedCaseHtml(metric, row, r.direction ?? metric.direction, r.threshold ?? metric.threshold)
        : null;
      const reply = failed && failed.actual ? failed.actual : esc(row.actual_output || "");
      const expect = failed && failed.expected ? failed.expected : esc(row.expected_output || "–");
      let extra = "";
      if (failed && failed.differences) {
        extra += `<div class="case-help"><div class="case-help-label">Differences</div>${failed.differences}</div>`;
      }
      if (failed && failed.howToPass) {
        extra += `<div class="case-help"><div class="case-help-label">How to pass</div>${failed.howToPass}</div>`;
      }
      return `<tr class="${cls}"><td class="num">${i + 1}</td>`
        + `<td class="text">${esc(row.input || row.question || "")}</td>`
        + `<td class="text">${reply}</td>`
        + `<td class="text">${expect}</td>`
        + `<td class="num">${formatScore(row.score)}</td><td>${esc(res)}</td>`
        + `<td class="text">${esc(row.reason || "")}${extra}</td></tr>`;
    }).join("");
    const head = `${esc(title(r.key))} · ${esc(statusLabel(r.status))}`
      + ` · score ${formatScore(r.score)} (needs ${esc(min(r))})`
      + (r.cases_run ? ` · ${casesCell(r)} cases` : "");
    return `<section class="metric"><h3>${head}</h3>`
      + (r.error ? `<p class="err">${esc(r.error)}</p>` : "")
      + (r.reason ? `<p class="why">${esc(r.reason)}</p>` : "")
      + (r.note ? `<p class="muted">${esc(r.note)}</p>` : "")
      + (rows.length
        ? `<table><thead><tr><th>#</th><th>Question</th><th>Chatbot reply</th>`
          + `<th>Expected</th><th class="num">Score</th><th>Result</th><th>Judge's reason</th></tr></thead>`
          + `<tbody>${caseBlocks}</tbody></table>`
        : `<p class="muted">No cases were scored.</p>`)
      + `</section>`;
  }).join("\n");

  let securityHtml = "";
  if (security && Array.isArray(security.categories)) {
    const pct = (rate) => (rate == null ? "–" : `${Math.round(rate * 100)}%`);
    const catRows = security.categories.map((cat) =>
      `<tr><td>${esc(cat.label)}</td><td class="num">${cat.attacks}</td>`
      + `<td class="num">${pct(cat.pass_rate)}</td>`
      + `<td class="num">${cat.tested}/${cat.total}</td></tr>`).join("");
    const metricLines = security.categories.flatMap((cat) => (cat.metrics || []).map((m) => {
      const bit = m.tested
        ? `${formatScore(m.score)} · ${m.passed ? "resisted" : "gave in"}`
        : "not run in this scan";
      return `<li>${esc(m.title)}${m.owasp && m.owasp.length ? ` (${esc(m.owasp.join(", "))})` : ""}: ${esc(bit)}</li>`;
    })).join("");
    const fwBlocks = (security.frameworks || []).map((fw) => {
      const items = (fw.items || []).map((item) => {
        const bit = item.pass_rate == null
          ? `${item.tested}/${item.total} tested`
          : `${pct(item.pass_rate)} resisted (${item.resisted}/${item.attacks})`;
        return `<li><b>${esc(item.id)} ${esc(item.title)}</b> · ${esc(bit)}`
          + (item.partial ? " · partly testable" : "") + `</li>`;
      }).join("");
      return `<p><b>${esc(fw.label)}</b></p><ul>${items}</ul>`;
    }).join("");
    securityHtml = `<section><h2>Security · DeepTeam vulnerability report</h2>`
      + `<table><thead><tr><th>Risk category</th><th class="num">Attacks</th>`
      + `<th class="num">Resisted</th><th class="num">Metrics tested</th></tr></thead>`
      + `<tbody>${catRows}</tbody></table>`
      + `<h3>By metric</h3><ul>${metricLines}</ul>`
      + `<h3>OWASP lists</h3>${fwBlocks}</section>`;
  }

  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Judge · ${esc(chatbot)} · Scan ${scan.version}</title>
<style>
  :root { color-scheme: light; --ok:#0c8a72; --bad:#c2237c; --border:#e4e4e7; --muted:#6b7280; --accent:#c0654a; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 36px 44px; font: 14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:#18181b; background:#fff; }
  header { border-bottom: 3px solid var(--accent); padding-bottom: 16px; margin-bottom: 24px; }
  header .brand { display:flex; align-items:center; gap:10px; color:#52525b; font-size:12.5px; letter-spacing:.04em; text-transform:uppercase; }
  header .logo { display:grid; place-items:center; width:26px; height:26px; border-radius:7px; background:linear-gradient(135deg,#c0654a,#8b3a2a); color:#fff; font-weight:700; font-size:11px; }
  header h1 { margin:10px 0 4px; font-size:24px; letter-spacing:-.01em; }
  header p { margin:0; color:var(--muted); font-size:13px; }
  section { margin: 28px 0; }
  h2 { margin:0 0 8px; font-size:16px; }
  h3 { margin:18px 0 8px; font-size:14px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { padding:7px 10px; border-top:1px solid var(--border); text-align:left; vertical-align:top; }
  table.kv th { width:36%; color:var(--muted); font-weight:500; background:#fafafa; }
  table.kv td { white-space:pre-wrap; }
  .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .text { white-space:pre-wrap; word-break:break-word; max-width:420px; }
  tr.ok td:first-child { border-left:3px solid var(--ok); }
  tr.bad td:first-child { border-left:3px solid var(--bad); }
  .ok .num { color:var(--ok); font-weight:600; } .bad .num { color:var(--bad); font-weight:600; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin-bottom:8px; }
  .stat { border:1px solid var(--border); border-radius:10px; padding:12px 14px; }
  .stat b { display:block; font-size:22px; } .stat span { color:var(--muted); font-size:12px; }
  .stat.pass b { color:var(--ok); } .stat.fail b { color:var(--bad); }
  .why,.err { color:#52525b; font-size:13px; } .err { color:var(--bad); }
  .muted { color:var(--muted); font-size:12.5px; }
  .metric { break-inside:avoid; border-top:1px solid var(--border); padding-top:8px; }
  footer { margin-top:36px; padding-top:12px; border-top:1px solid var(--border); color:var(--muted); font-size:12px; }
  ${DIFF_REPORT_CSS}
  @media print { body { padding:18px; } section, .metric { break-inside:avoid; } }
</style></head><body>
<header>
  <div class="brand"><span class="logo">LJ</span> LLM Judge · full scan report</div>
  <h1>${esc(chatbot)} · Scan ${scan.version}</h1>
  <p>Generated ${esc(new Date().toLocaleString())} · ${esc(resultLabel(scan.result))} · ${esc(scanTally(scan))}</p>
</header>
${diffLegendHtml()}
<div class="stats">
  <div class="stat ${scan.result === "passed" ? "pass" : "fail"}"><b>${esc(resultLabel(scan.result))}</b><span>overall result</span></div>
  <div class="stat"><b>${scan.test_cases ? `${scan.test_cases_passed} / ${scan.test_cases}` : "–"}</b><span>test cases passed</span></div>
  <div class="stat pass"><b>${scan.passed ?? 0}</b><span>metrics passed</span></div>
  <div class="stat fail"><b>${scan.failed ?? 0}</b><span>metrics failed</span></div>
  <div class="stat"><b>${formatScore(scan.average)}</b><span>average score</span></div>
  <div class="stat"><b>${esc(duration(scan.started_at, scan.finished_at))}</b><span>duration</span></div>
  <div class="stat"><b>${(scan.judge_tokens || 0).toLocaleString()}</b><span>judge tokens</span></div>
  <div class="stat"><b>${cost ? `$${cost.toFixed(4)}` : "N/A"}</b><span>evaluation cost</span></div>
</div>
<section>
  <h2>Scan properties</h2>
  <table class="kv"><tbody>
    <tr><th>Scan</th><td>${scan.version} (job ${esc(scan.job_id)})</td></tr>
    <tr><th>Chatbot</th><td>${esc(chatbot)}</td></tr>
    <tr><th>Started</th><td>${esc(scan.started_at ? formatRunTime(scan.started_at) : "–")}</td></tr>
    <tr><th>Finished</th><td>${esc(scan.finished_at ? formatRunTime(scan.finished_at) : "–")}</td></tr>
    <tr><th>Judge</th><td>${esc(scan.judge_model || "–")}</td></tr>
    <tr><th>Not run</th><td>${(scan.skipped || []).length}</td></tr>
    <tr><th>Not scored</th><td>${scan.errors ?? 0}</td></tr>
    <tr><th>Cases per run</th><td>${opts.limit ? esc(String(opts.limit)) : "all"}</td></tr>
    <tr><th>Parallel metrics</th><td>${opts.metric_concurrency != null ? esc(String(opts.metric_concurrency)) : "default"}</td></tr>
    <tr><th>Strict mode</th><td>${yes(de.strict_mode)}</td></tr>
    <tr><th>Include reason</th><td>${yes(de.include_reason)}</td></tr>
    <tr><th>Consistency check</th><td>${yes(opts.check_consistency)}</td></tr>
    ${scan.note ? `<tr><th>Note</th><td>${esc(scan.note)}</td></tr>` : ""}
  </tbody></table>
</section>
<section>
  <h2>Metrics summary</h2>
  <table><thead><tr><th>Metric</th><th class="num">Score</th><th class="num">Minimum</th><th>Result</th><th class="num">Cases</th></tr></thead>
  <tbody>${summaryRows}</tbody></table>
</section>
${skippedRows ? `<section><h2>Not run</h2><table><thead><tr><th>Metric</th><th>Why</th></tr></thead><tbody>${skippedRows}</tbody></table></section>` : ""}
<section>
  <h2>Metric details (every case)</h2>
  <p class="muted">Question, chatbot reply, expected answer, score, result and the judge's reason for every scored case.
    Failed single-turn cases mark word differences the same way as the app (missing / changed / added) and list how to pass.</p>
  ${metricBlocks || "<p class=\"muted\">No metrics in this scan.</p>"}
</section>
${securityHtml}
<footer>Generated by LLM Judge · full details of scan ${scan.version} for ${esc(chatbot)}</footer>
</body></html>`;

  const name = `${chatbot}-scan-${scan.version}-full-report`.replace(/[^\w.-]+/g, "_");
  const a = el("a", {
    href: URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" })),
    download: `${name}.html`,
  });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function facts(scan) {
  const fact = (name, value) => el("div", {}, el("dt", {}, name), el("dd", {}, value));
  return el("dl", { className: "facts" },
    fact("Started", scan.started_at ? formatRunTime(scan.started_at) : "–"),
    fact("Duration", duration(scan.started_at, scan.finished_at)),
    fact("Test cases passed", scan.test_cases ? `${scan.test_cases_passed} of ${scan.test_cases}` : "–"),
    fact("Metrics passed", String(scan.passed)),
    fact("Metrics failed", String(scan.failed)),
    fact("Not run", String((scan.skipped || []).length)),
    fact("Not scored", String(scan.errors)),
    fact("Average score", formatScore(scan.average)),
    fact("Judge", scan.judge_model || "–"),
    fact("Judge tokens", scan.judge_tokens ? scan.judge_tokens.toLocaleString() : "–"),
  );
}

// The metric table; each header sorts it (click again to reverse). Blanks sort last.
const SUMMARY_COLUMNS = [
  { label: "Metric", value: (r, title) => title(r.key).toLowerCase() },
  { label: "Score", num: true, value: (r) => r.score },
  { label: "Minimum", num: true, value: (r) => r.threshold },
  { label: "Result", value: (r) => r.status || "" },
  { label: "Cases", num: true, value: (r) => (r.cases_run
    ? (r.cases_passed ?? (r.rows || []).filter((row) => row.passed).length) / r.cases_run : null) },
];

function summary(results, title) {
  const sort = { col: null, dir: 1 };
  const row = (r) => el("tr", {},
    el("td", {}, el("a", { href: `#metric-${r.key}`,
      onclick: () => { const d = document.getElementById(`metric-${r.key}`); if (d) d.open = true; } }, title(r.key))),
    el("td", { className: "num" }, formatScore(r.score)),
    el("td", { className: "num" }, minimum(r)),
    statusCell(r.status),
    el("td", { className: "num" }, casesCell(r)));
  const body = el("tbody", {}, results.map(row));
  const heads = SUMMARY_COLUMNS.map((c, i) => {
    const button = el("button", { type: "button", className: "sort-button",
      onclick: () => {
        sort.dir = sort.col === i ? -sort.dir : 1;
        sort.col = i;
        const key = (r) => c.value(r, title);
        const sorted = [...results].sort((a, b) => {
          const x = key(a), y = key(b);
          if (x == null || x === "") return 1;
          if (y == null || y === "") return -1;
          return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
        });
        body.replaceChildren(...sorted.map(row));
        heads.forEach((th, j) => {
          th.setAttribute("aria-sort", j === i ? (sort.dir === 1 ? "ascending" : "descending") : "none");
          th.querySelector(".sort-mark").textContent = j === i ? (sort.dir === 1 ? " ▲" : " ▼") : "";
        });
      } }, c.label, el("span", { className: "sort-mark", "aria-hidden": "true" }, ""));
    return el("th", { className: c.num ? "num" : "", "aria-sort": "none" }, button);
  });
  return el("div", { className: "table-scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, heads)), body));
}

// Cases that passed of those scored (DeepEval: the metric passes only when all do).
function casesCell(r) {
  if (!r.cases_run) return "–";
  const passed = r.cases_passed ?? (r.rows || []).filter((row) => row.passed).length;
  return `${passed} of ${r.cases_run} passed`;
}

// Metrics this scan left out, each with why: the justification for the gap.
function notRun(skipped, title) {
  return [
    el("h2", {}, "Not run"),
    el("p", { className: "muted" }, "These metrics were ticked but not sent: nothing was scored and no judge tokens were spent."),
    el("div", { className: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Metric"), el("th", {}, "Why it was not run"))),
      el("tbody", {}, skipped.map((s) => el("tr", {},
        el("td", {}, s.title || title(s.key)),
        el("td", { className: "text" }, s.reason)))))),
  ];
}

// Failed rows mark the words that differ between reply and expected answer,
// and say under the judge's reason how the case would pass.
function cases(rows, metric, r) {
  if (!rows || !rows.length) return el("p", { className: "muted" }, "No cases were scored.");
  return el("div", { className: "table-scroll case-table" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "Question"), el("th", {}, "Chatbot reply"),
      el("th", {}, "Expected"), el("th", { className: "num" }, "Score"), el("th", {}, "Result"), el("th", {}, "Judge's reason"))),
    el("tbody", {}, rows.map((row, i) => {
      const failed = row.passed === false ? failedCaseView(metric, row, r.direction, r.threshold) : null;
      const spoken = (value) => (typeof value === "string" ? speakerText(value) : value);
      return el("tr", {},
        el("td", { className: "num" }, String(i + 1)),
        el("td", { className: "text" }, speakerText(row.input || row.question || "")),
        el("td", { className: "text" }, spoken((failed && failed.actual) || row.actual_output || "")),
        el("td", { className: "text" }, (failed && failed.expected) || row.expected_output || "–"),
        el("td", { className: "num" }, formatScore(row.score)),
        statusCell(row.passed === undefined ? null : row.passed ? "pass" : "fail"),
        el("td", { className: "text" }, row.reason || "",
          failed && failed.differences ? el("details", { className: "case-help", open: true },
            el("summary", {}, "Differences"), failed.differences) : "",
          failed ? el("details", { className: "case-help", open: true },
            el("summary", {}, "How to pass"), failed.howToPass) : ""),
      );
    }))));
}

// Conversation transcripts come as plain text ("User: …\nBot: …"): the speaker
// labels render bold, so the turns read like a chat.
function speakerText(text) {
  const lines = String(text ?? "").split("\n");
  const parts = [];
  for (let i = 0; i < lines.length; i++) {
    const label = lines[i].match(/^(User|Bot):/);
    if (label) {
      parts.push(el("span", { className: "speaker" }, label[0]), lines[i].slice(label[0].length));
    } else {
      parts.push(lines[i]);
    }
    if (i < lines.length - 1) parts.push("\n");
  }
  return parts;
}

function metricSection(r, title, metric) {
  return el("details", { className: "metric", id: `metric-${r.key}`, open: r.status !== "pass" },
    el("summary", {},
      el("b", {}, title(r.key)),
      el("span", { className: (STATUS_LABEL[r.status] || ["", "status-error"])[1] },
        (STATUS_LABEL[r.status] || ["Not scored"])[0]),
      el("span", {}, `Score ${formatScore(r.score)} (needs ${minimum(r)})`),
      el("span", { className: "muted" }, r.cases_run ? casesCell(r) + " cases" : "")),
    el("div", { className: "body" },
      r.error ? el("p", { className: "reason status-error" }, r.error) : "",
      r.reason ? el("p", { className: "reason" }, r.reason) : "",
      r.note ? el("p", { className: "note" }, r.note) : "",
      promptInstructionsBlock(r.prompt_instructions),
      cases(r.rows, metric, r)),
  );
}

// The DeepTeam vulnerability report of this scan: its red-team results by risk
// category and OWASP id (backend/redteam_coverage.py, this scan's own results).
function securitySection(report) {
  const pct = (rate) => `${Math.round((rate || 0) * 100)}%`;
  return [
    el("div", { id: "security" }),
    el("h2", {}, "Security · DeepTeam vulnerability report"),
    el("p", { className: "muted" }, "This scan's red-team results by risk category and OWASP id. "
      + "Untested is not the same as safe; risks marked partly testable live mostly outside the chat. ",
      // The full Risk Assessment (risk score, vulnerabilities, test cases, PDF report).
      el("a", { href: `../risk/risk.html?job=${encodeURIComponent(new URLSearchParams(location.search).get("job") || "")}#overview`,
        target: "_blank", rel: "noopener" }, "Open the Risk Assessment →")),
    el("div", { className: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Risk category"), el("th", { className: "num" }, "Attacks"),
        el("th", { className: "num" }, "Resisted"), el("th", {}, "Metrics tested"))),
      el("tbody", {}, report.categories.map((cat) => el("tr", {},
        el("td", {}, el("b", {}, cat.label)),
        el("td", { className: "num" }, String(cat.attacks)),
        el("td", { className: "num" }, cat.pass_rate === null ? "–" : pct(cat.pass_rate)),
        el("td", { className: "num" }, `${cat.tested}/${cat.total}`)))))),
    el("details", { className: "metric" },
      el("summary", {}, el("b", {}, "By metric")),
      el("div", { className: "body" },
        el("ul", {}, ...report.categories.flatMap((cat) => cat.metrics.map((m) => el("li", {},
          `${m.title}${m.owasp.length ? ` (${m.owasp.join(", ")})` : ""}: `,
          m.tested
            ? el("span", { className: m.passed ? "status-pass" : "status-fail" },
                `${formatScore(m.score)} · ${m.passed ? "resisted" : "gave in"}`)
            : el("span", { className: "muted" }, "not run in this scan"),
        )))))),
    el("details", { className: "metric" },
      el("summary", {}, el("b", {}, "OWASP lists")),
      el("div", { className: "body" },
        ...report.frameworks.map((fw) => [
          el("p", {}, el("b", {}, fw.label)),
          el("ul", {}, ...fw.items.map((item) => el("li", {},
            el("b", {}, `${item.id} ${item.title}`), " · ",
            item.pass_rate === null
              ? el("span", { className: "muted" }, `${item.tested}/${item.total} tested`)
              : el("span", { className: item.pass_rate >= 0.8 ? "status-pass" : "status-fail" },
                  `${pct(item.pass_rate)} resisted (${item.resisted}/${item.attacks})`),
            item.partial ? el("span", { className: "muted" }, " · partly testable") : ""))),
        ]))),
  ];
}

async function loadScan() {
  const status = $("scan-status");
  const job = new URLSearchParams(location.search).get("job");
  if (!job) {
    status.textContent = "No scan given: open one from the dashboard's Scan history.";
    return;
  }
  try {
    const scan = await api(`/api/scans/${encodeURIComponent(job)}`);
    const [targets, scans, metrics, security] = await Promise.all([
      api("/api/targets"), api(`/api/targets/${scan.target_id}/scans`), api("/api/metrics").catch(() => []),
      api(`/api/scans/${encodeURIComponent(job)}/security`).catch(() => null),
    ]);
    const chatbot = (targets.find((t) => t.id === scan.target_id) || {}).name || `Chatbot ${scan.target_id}`;
    const titles = new Map(metrics.map((m) => [m.key, m.title]));
    const title = (key) => titles.get(key) || key;
    const byKey = new Map(metrics.map((m) => [m.key, m]));
    document.title = `${chatbot} · Scan ${scan.version}`;
    $("scan").replaceChildren(
      header(scan, chatbot, scans, { title, security, byKey }),
      facts(scan),
      // Paths this scan found in the chatbot's replies, or the scan that found them.
      scan.note ? el("p", { className: "note" }, scan.note) : "",
      el("h2", {}, "Metrics"),
      summary(scan.results, title),
      ...(scan.skipped && scan.skipped.length ? notRun(scan.skipped, title) : []),
      el("h2", {}, "Details"),
      el("p", { className: "muted" }, "Failed and unscored metrics are open; click any metric to show or hide its cases. "
        + "Download report (PDF) opens the printable report with every section; "
        + "Download full report (HTML) saves one offline file with every case."),
      ...scan.results.map((r) => metricSection(r, title, byKey.get(r.key))),
      ...(security ? securitySection(security)
        : [el("p", { className: "muted" }, "The security report for this scan could not be loaded.")]),
    );
    openFromHash();
  } catch (error) {
    status.textContent = `Could not load the scan: ${error.message}`;
  }
}

// Passed metrics start closed; a link to one (#metric-<key>, from the summary
// table or a pasted URL) opens it so its cases show, then scrolls to it.
function openFromHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  const target = id && document.getElementById(id);
  if (!target || target.tagName !== "DETAILS") return;
  target.open = true;
  target.scrollIntoView({ block: "start" });
}

window.addEventListener("hashchange", openFromHash);

loadScan();
