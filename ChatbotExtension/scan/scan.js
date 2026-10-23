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

function header(scan, chatbot, scans) {
  const [label, css] = RESULT_LABEL[scan.result] || [scan.result, ""];
  const index = scans.findIndex((s) => s.job_id === scan.job_id);
  const newer = scans[index - 1];
  const older = scans[index + 1];
  const link = (other, text) => el("a", { href: `scan.html?job=${other.job_id}` }, text);
  return el("div", { className: "scan-head" },
    el("h1", {}, `${chatbot} · Scan ${scan.version}`),
    el("span", { className: `badge ${css}` }, `${label} · ${scan.passed} of ${scan.metrics} passed`),
    el("nav", { className: "scan-nav", "aria-label": "Other scans" },
      older ? link(older, `← Scan ${older.version}`) : "",
      newer ? link(newer, `Scan ${newer.version} →`) : ""),
  );
}

function facts(scan) {
  const fact = (name, value) => el("div", {}, el("dt", {}, name), el("dd", {}, value));
  return el("dl", { className: "facts" },
    fact("Started", scan.started_at ? formatRunTime(scan.started_at) : "–"),
    fact("Duration", duration(scan.started_at, scan.finished_at)),
    fact("Passed", String(scan.passed)),
    fact("Failed", String(scan.failed)),
    fact("Not scored", String(scan.errors)),
    fact("Average score", formatScore(scan.average)),
    fact("Judge", scan.judge_model || "–"),
    fact("Judge tokens", scan.judge_tokens ? scan.judge_tokens.toLocaleString() : "–"),
  );
}

function summary(results, title) {
  return el("div", { className: "table-scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Metric"), el("th", { className: "num" }, "Score"),
      el("th", { className: "num" }, "Minimum"), el("th", {}, "Result"), el("th", { className: "num" }, "Cases"))),
    el("tbody", {}, results.map((r) => el("tr", {},
      el("td", {}, el("a", { href: `#metric-${r.key}` }, title(r.key))),
      el("td", { className: "num" }, formatScore(r.score)),
      el("td", { className: "num" }, minimum(r)),
      statusCell(r.status),
      el("td", { className: "num" }, r.cases_total ? `${r.cases_run ?? 0} of ${r.cases_total}` : "–"),
    )))));
}

function cases(rows) {
  if (!rows || !rows.length) return el("p", { className: "muted" }, "No cases were scored.");
  return el("div", { className: "table-scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "Question"), el("th", {}, "Chatbot reply"),
      el("th", {}, "Expected"), el("th", { className: "num" }, "Score"), el("th", {}, "Result"), el("th", {}, "Judge's reason"))),
    el("tbody", {}, rows.map((row, i) => el("tr", {},
      el("td", { className: "num" }, String(i + 1)),
      el("td", { className: "text" }, row.input || row.question || ""),
      el("td", { className: "text" }, row.actual_output || ""),
      el("td", { className: "text" }, row.expected_output || "–"),
      el("td", { className: "num" }, formatScore(row.score)),
      statusCell(row.passed === undefined ? null : row.passed ? "pass" : "fail"),
      el("td", { className: "text" }, row.reason || ""),
    )))));
}

function metricSection(r, title) {
  return el("details", { className: "metric", id: `metric-${r.key}`, open: r.status !== "pass" },
    el("summary", {},
      el("b", {}, title(r.key)),
      el("span", { className: (STATUS_LABEL[r.status] || ["", "status-error"])[1] },
        (STATUS_LABEL[r.status] || ["Not scored"])[0]),
      el("span", {}, `Score ${formatScore(r.score)} (needs ${minimum(r)})`),
      el("span", { className: "muted" }, r.cases_total ? `${r.cases_run ?? 0} of ${r.cases_total} cases` : "")),
    el("div", { className: "body" },
      r.error ? el("p", { className: "reason status-error" }, r.error) : "",
      r.reason ? el("p", { className: "reason" }, r.reason) : "",
      r.note ? el("p", { className: "note" }, r.note) : "",
      cases(r.rows)),
  );
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
    const [targets, scans, metrics] = await Promise.all([
      api("/api/targets"), api(`/api/targets/${scan.target_id}/scans`), api("/api/metrics").catch(() => []),
    ]);
    const chatbot = (targets.find((t) => t.id === scan.target_id) || {}).name || `Chatbot ${scan.target_id}`;
    const titles = new Map(metrics.map((m) => [m.key, m.title]));
    const title = (key) => titles.get(key) || key;
    document.title = `${chatbot} · Scan ${scan.version}`;
    $("scan").replaceChildren(
      header(scan, chatbot, scans),
      facts(scan),
      // Paths this scan found in the chatbot's replies, or the scan that found them.
      scan.note ? el("p", { className: "note" }, scan.note) : "",
      el("h2", {}, "Metrics"),
      summary(scan.results, title),
      el("h2", {}, "Details"),
      el("p", { className: "muted" }, "Failed and unscored metrics are open; click any metric to show or hide its cases."),
      ...scan.results.map((r) => metricSection(r, title)),
    );
  } catch (error) {
    status.textContent = `Could not load the scan: ${error.message}`;
  }
}

loadScan();
