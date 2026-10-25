// Printable evaluation report for one chatbot: the dashboard opens this page
// with ?target=<id>, it renders every metric's latest run from the backend and
// the case details the extension saved, then opens the print dialog so the
// user can choose "Save as PDF". Nothing here changes any data.

const $ = (id) => document.getElementById(id);

function normalized(metric, score) {
  if (typeof score !== "number") return null;
  return metric.direction === "lower" ? 1 - score : score;
}

// Only the origin and path: a captured URL's query string can carry tokens.
function safeUrl(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return "";
  }
}

function dl(pairs) {
  return el("dl", { className: "facts" }, ...pairs.flatMap(([label, value]) => [
    el("dt", {}, label),
    el("dd", {}, value),
  ]));
}

function resultCell(run) {
  if (!run) return el("span", { className: "result pending" }, "not run");
  return el("span", { className: `result ${run.passed ? "pass" : "fail"}` }, run.passed ? "pass" : "fail");
}

// Share of judged test cases that passed, over every metric with case details.
function successRate(cases) {
  const passed = cases.filter((c) => c.row.passed).length;
  return { passed, total: cases.length, rate: cases.length ? passed / cases.length : null };
}

// DeepEval's test-case verdict: a test case passes only when every metric that scored it passes.
function testCasesPassed(cases) {
  const byId = new Map();
  for (const c of cases) byId.set(c.id, (byId.get(c.id) ?? true) && Boolean(c.row.passed));
  return { passed: [...byId.values()].filter(Boolean).length, total: byId.size };
}

function formatPercent(rate) {
  return rate === null ? "–" : `${Math.round(rate * 100)}%`;
}

function cover(target, judge, metrics, latestByKey, cases) {
  const run = metrics.filter((m) => latestByKey[m.key]);
  const scores = run.map((m) => normalized(m, latestByKey[m.key].score)).filter((s) => s !== null);
  const average = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
  const passed = run.filter((m) => latestByKey[m.key].passed).length;
  const success = successRate(cases);
  const testCases = testCasesPassed(cases);
  return el(
    "section",
    { className: "cover" },
    el("h1", {}, "Chatbot evaluation report"),
    el("p", { className: "subtitle" }, target.name),
    dl([
      ["Chatbot URL", safeUrl(target.config.url) || "–"],
      ["Golden set", themeOf(target)],
      ["Judge", judge ? `${judge.model} (${safeUrl(judge.base_url) || judge.base_url})` : "–"],
      ["Generated", new Date().toLocaleString()],
    ]),
    el(
      "div",
      { className: "headline" },
      el("div", {}, el("b", {}, formatPercent(success.rate)),
        el("small", {}, `overall success rate (${success.passed} of ${success.total} checks)`)),
      el("div", {}, el("b", {}, testCases.total ? `${testCases.passed}/${testCases.total}` : "–"),
        el("small", {}, "test cases passed (every metric passed)")),
      el("div", {}, el("b", {}, average === null ? "–" : formatScore(average)), el("small", {}, "average score")),
      el("div", {}, el("b", { className: "pass" }, String(passed)), el("small", {}, "metrics passed")),
      el("div", {}, el("b", { className: "fail" }, String(run.length - passed)), el("small", {}, "metrics failed")),
      el("div", {}, el("b", {}, String(metrics.length - run.length)), el("small", {}, "metrics not run")),
    ),
    el("p", { className: "muted small" }, "Overall success rate: metric checks that passed their threshold, out of every judged check (one check = one test case scored by one metric). Average score: each metric's latest run on a higher-is-better scale (lower-is-better metrics counted as 1 − score)."),
  );
}

function summaryTable(metrics, latestByKey, thresholds) {
  return el(
    "section",
    {},
    el("h2", {}, "Summary"),
    el(
      "table",
      { className: "summary" },
      el("thead", {}, el("tr", {}, ...["Metric", "Category", "Score", "Threshold", "Result", "Cases", "Judge tokens", "Last run"]
        .map((h) => el("th", {}, h)))),
      el("tbody", {}, ...metrics.map((m) => {
        const run = latestByKey[m.key];
        const threshold = thresholdFor(m, thresholds);
        return el(
          "tr",
          {},
          el("td", {}, m.title),
          el("td", {}, m.ui_category_label || m.ui_category || ""),
          el("td", { className: "num" }, run ? formatScore(run.score) : "–"),
          el("td", { className: "num" }, `${comparator(m.direction)} ${formatScore(threshold)}`),
          el("td", {}, resultCell(run)),
          el("td", { className: "num" }, run && run.cases_run != null
            ? `${run.cases_run}${run.cases_skipped ? ` (+${run.cases_skipped} skipped)` : ""}` : "–"),
          el("td", { className: "num" }, run && typeof run.judge_tokens === "number" ? formatTokens(run.judge_tokens) : "–"),
          el("td", {}, run ? formatRunTime(run.ts) : "–"),
        );
      })),
    ),
  );
}

// Details (from the run's stored result) belong to this run only if not older than it.
function detailsFor(run, details) {
  return details && run && details.status !== "error" && details.finishedAt >= Date.parse(run.ts) - 60_000
    ? details : null;
}

// The threshold and direction a run was judged with (the saved run's own, else the current setting).
function judgedWith(metric, details, thresholds) {
  return {
    threshold: details && typeof details.threshold === "number" ? details.threshold : thresholdFor(metric, thresholds),
    direction: (details && details.direction) || metric.direction,
  };
}

// Every judged (metric, case) pair, in metric order. Pairs that ask the same
// question form one test case, numbered TC-01, TC-02, … in order of appearance.
function collectCases(metrics, latestByKey, detailsByKey, thresholds) {
  const cases = metrics.flatMap((metric) => {
    const run = latestByKey[metric.key];
    const details = detailsFor(run, detailsByKey[metric.key]);
    if (!details) return [];
    const { threshold, direction } = judgedWith(metric, details, thresholds);
    const judgeModel = (details.judge && details.judge.model) || run.judge_model || "–";
    return details.rows.map((row) => ({ metric, row, threshold, direction, judgeModel }));
  });
  const ids = new Map();
  for (const c of cases) {
    // The golden's question (a conversation's scenario name), not what was sent:
    // grounded goldens send the question with facts attached, so `input` differs per metric.
    const question = c.row.question || c.row.input || "";
    if (!ids.has(question)) ids.set(question, `TC-${String(ids.size + 1).padStart(2, "0")}`);
    c.question = question;
    c.id = ids.get(question);
  }
  return cases;
}

// DeepEval-style score cell: the score, then how the judge reached it.
function scoreDetail({ metric, row, threshold, direction, judgeModel }, options = DEFAULT_OPTIONS) {
  const read = metric.scores_on && metric.scores_on.length ? metric.scores_on.map(fieldLabel).join(", ").toLowerCase() : "";
  return el(
    "div",
    {},
    el("b", {}, formatScore(row.score)),
    ` (threshold ${comparator(direction)} ${formatScore(threshold)}, evaluation model=${judgeModel}`,
    read ? `, judge read=${read}` : "",
    ", reason=", row.reason || "No reason given.",
    row.error ? `, error=${row.error}` : "",
    ")",
    options.improve && !row.passed && caseTip(metric) ? el("p", { className: "tip" }, el("b", {}, "How to improve: "), caseTip(metric)) : null,
  );
}

// One row per test case (question), its metrics listed under it, with the
// share of those metrics it passed; the footer gives the overall rate.
function testCaseTable(cases, ranMetrics, latestByKey, detailsByKey, options = DEFAULT_OPTIONS) {
  const success = successRate(cases);
  const withoutDetails = ranMetrics.filter((m) => !detailsFor(latestByKey[m.key], detailsByKey[m.key])).length;
  if (!cases.length) {
    return el("section", {}, el("h2", {}, "Test case results"),
      el("p", { className: "muted small" }, "No case details were saved for the latest runs; run the metrics from the dashboard or side panel to include them."));
  }
  const groups = new Map();
  for (const c of cases) {
    if (!groups.has(c.id)) groups.set(c.id, []);
    groups.get(c.id).push(c);
  }
  const rows = [...groups.entries()].flatMap(([id, items]) => {
    const own = successRate(items);
    return items.map((c, i) => el(
      "tr",
      { className: i === 0 ? "group-start" : "" },
      i === 0 ? el("td", { rowspan: items.length }, el("b", {}, id), el("br"), c.question) : null,
      el("td", {}, c.metric.title),
      el("td", {}, scoreDetail(c, options)),
      el("td", {}, el("span", { className: `status ${c.row.passed ? "pass" : "fail"}` }, c.row.passed ? "PASSED" : "FAILED")),
      i === 0 ? el("td", { rowspan: items.length, className: "num" }, formatPercent(own.rate)) : null,
    ));
  });
  return el(
    "section",
    { className: "page-break" },
    el("h2", {}, "Test case results"),
    el(
      "table",
      { className: "summary cases" },
      el("thead", {}, el("tr", {}, ...["Test case", "Metric", "Score", "Status", "Overall Success Rate"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...rows),
      el("tfoot", {}, el("tr", {}, el("td", { colSpan: 5 },
        el("b", {}, `Overall success rate: ${formatPercent(success.rate)}`),
        ` (${success.passed} of ${success.total} metric checks passed across ${groups.size} test cases)`))),
    ),
    el("p", { className: "muted small" }, "Each test case is one golden question; every metric that asked it is listed under it. Its success rate is the share of those metrics it passed."),
    withoutDetails
      ? el("p", { className: "muted small" }, `${withoutDetails} metric run${withoutDetails === 1 ? "" : "s"} had no saved case details and ${withoutDetails === 1 ? "is" : "are"} not counted.`)
      : null,
  );
}

// Failed metrics, furthest from passing first, each with the judge's reasons
// and what to change in the chatbot or check in the test.
// The run average if every failed case scored perfectly (1, or 0 for a
// lower-is-better metric): how much fixing the failed cases alone would buy.
function scoreIfFixed(rows, direction) {
  if (!rows.length) return null;
  const best = direction === "lower" ? 0 : 1;
  const scores = rows.map((r) => (r.passed ? r.score : best)).filter((s) => typeof s === "number");
  return scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
}

// How to improve the score: every failed metric, plus passed metrics that
// still had failed cases, furthest from passing first. Each lists its failed
// test cases with the judge's reasons, what to change in the chatbot and what
// to check in the test.
function actionPlan(metrics, latestByKey, detailsByKey, thresholds, caseIds) {
  const items = metrics
    .filter((m) => latestByKey[m.key])
    .map((m) => {
      const run = latestByKey[m.key];
      const details = detailsFor(run, detailsByKey[m.key]);
      const { threshold, direction } = judgedWith(m, details, thresholds);
      const failedRows = details ? details.rows.filter((r) => !r.passed) : [];
      const shortfall = direction === "lower" ? run.score - threshold : threshold - run.score;
      return { metric: m, run, details, threshold, direction, failedRows, shortfall };
    })
    .filter((it) => !it.run.passed || it.failedRows.length)
    .sort((a, b) => (a.run.passed - b.run.passed) || (b.shortfall - a.shortfall));
  if (!items.length) {
    return el("section", {}, el("h2", {}, "How to improve the score"),
      el("p", {}, "Every metric that ran passed its threshold, and no test case failed."));
  }
  const failedMetrics = items.filter((it) => !it.run.passed).length;
  return el(
    "section",
    { className: "action-plan" },
    el("h2", {}, "How to improve the score"),
    el("p", { className: "muted small" },
      `${failedMetrics} failed metric${failedMetrics === 1 ? "" : "s"}, then ${items.length - failedMetrics} passed metric${items.length - failedMetrics === 1 ? "" : "s"} with failed test cases; furthest from passing first. `
      + "A failing score does not always mean the chatbot is wrong: check the test too."),
    ...items.map(({ metric, run, details, threshold, direction, failedRows }) => {
      const fixed = details ? scoreIfFixed(details.rows, direction) : null;
      const target = [
        ["Now", run.passed
          ? `${formatScore(run.score)}, meets ${comparator(direction)} ${formatScore(threshold)} (run passed, but some cases failed)`
          : gapText(direction, run.score, threshold) || formatScore(run.score)],
      ];
      if (details) target.push(["Failed cases", `${failedRows.length} of ${details.rows.length}`
        + (failedRows.length ? `: ${[...new Set(failedRows.map((r) => caseIds.get(r)).filter(Boolean))].join(", ")}` : "")]);
      if (fixed !== null && failedRows.length) {
        target.push(["If they are fixed", `average ${formatScore(fixed)} ${direction === "lower" ? (fixed <= threshold ? "≤" : ">") : (fixed >= threshold ? "≥" : "<")} ${formatScore(threshold)} → ${(direction === "lower" ? fixed <= threshold : fixed >= threshold) ? "pass" : "still fail: other cases must score higher too"}`]);
      }
      return el(
        "div",
        { className: "plan-item" },
        el("h3", {}, metric.title, " ", resultCell(run)),
        dl(target),
        improvementBlock(metric, details),
      );
    }),
  );
}

// The first chatbot tip for a failed case, for the test case table and case cards.
function caseTip(metric) {
  const advice = metric.improve || {};
  return (advice.chatbot && advice.chatbot[0]) || (advice.tests && advice.tests[0]) || "";
}

// Word-level difference between the actual and expected answer: each side comes
// back as spans, words the other side lacks in bold. Case and punctuation are
// ignored when comparing. Very long answers are left plain (the LCS table grows
// with the product of their lengths).
function wordDiff(actual, expected) {
  const split = (text) => text.split(/(\s+)/).filter(Boolean);
  const key = (token) => token.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const a = split(actual);
  const b = split(expected);
  const aWords = a.map((t, i) => [key(t), i]).filter(([k]) => k);
  const bWords = b.map((t, i) => [key(t), i]).filter(([k]) => k);
  if (aWords.length * bWords.length > 250_000) return null;
  const n = aWords.length;
  const m = bWords.length;
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = aWords[i][0] === bWords[j][0] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const sameA = new Set();
  const sameB = new Set();
  for (let i = 0, j = 0; i < n && j < m;) {
    if (aWords[i][0] === bWords[j][0]) {
      sameA.add(aWords[i][1]);
      sameB.add(bWords[j][1]);
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) i++;
    else j++;
  }
  const render = (tokens, same) => tokens.map((t, i) => (key(t) && !same.has(i) ? el("b", { className: "diff" }, t) : t));
  return { actual: render(a, sameA), expected: render(b, sameB) };
}

// A conversation's expected output describes the outcome, not a reply to
// compare word by word, so only single-turn cases get the bold diff.
function caseBlock(metric, row, direction, threshold, id, options = DEFAULT_OPTIONS) {
  const actual = row.actual_output || "";
  const expected = row.expected_output || "";
  const diff = options.diff && metric.kind !== "conversation" && actual && expected ? wordDiff(actual, expected) : null;
  const question = row.question || row.input || "";
  const pairs = [[metric.kind === "conversation" ? "Scenario" : "Question", question]];
  // What was actually sent, when it differs (facts attached, or a conversation's user turns).
  if (row.input && row.input !== question) pairs.push(["Sent to the chatbot", row.input]);
  pairs.push(
    ["Actual result", diff ? el("span", {}, ...diff.actual) : actual || "–"],
    ["Expected result", diff ? el("span", {}, ...diff.expected) : expected || "– (this case has no expected answer)"],
  );
  if (row.context && row.context.length) pairs.push(["Context", el("ul", {}, ...row.context.map((c) => el("li", {}, c)))]);
  if (row.scores && row.scores.length === 2) {
    pairs.push(["Two scorings", `${formatScore(row.scores[0])} and ${formatScore(row.scores[1])} (averaged)`]);
  }
  pairs.push(["Why", row.reason || "No reason given."]);
  const advice = metric.improve || {};
  if (options.improve && !row.passed && ((advice.chatbot && advice.chatbot.length) || (advice.tests && advice.tests.length))) {
    pairs.push(["How to improve", el("ul", {},
      ...(advice.chatbot || []).map((t) => el("li", {}, t)),
      ...(advice.tests || []).map((t) => el("li", {}, "Check the test: ", t)))]);
  }
  const outcome = row.passed ? "pass" : "fail";
  return el(
    "div",
    { className: `case ${outcome}` },
    el("p", { className: `case-head ${outcome}` },
      `${id ? `${id} · ` : ""}${row.passed ? "✓" : "✕"} ${outcome} · ${formatScore(row.score)} ${comparator(direction)} ${formatScore(threshold)}`),
    dl(pairs),
  );
}

function metricSection(metric, run, details, thresholds, caseIds, options = DEFAULT_OPTIONS) {
  const { threshold, direction } = judgedWith(metric, details, thresholds);
  const block = (row) => caseBlock(metric, row, direction, threshold, caseIds.get(row), options);
  const parts = [
    el("h3", {}, metric.title, " ", resultCell(run)),
    dl(metricMeaning(metric)),
  ];
  // Details (from the run's stored result) match this run only if they are not older than it.
  const sameRun = details && run && details.finishedAt >= Date.parse(run.ts) - 60_000;
  if (!sameRun || details.status === "error") {
    parts.push(dl([
      ["Latest run", `${formatScore(run.score)} ${comparator(direction)} ${formatScore(threshold)} · ${formatRunTime(run.ts)}`],
      ["Cases", run.cases_run != null ? String(run.cases_run) : "–"],
      ["Judge", run.judge_model || "–"],
    ]));
    parts.push(el("p", { className: "muted small" }, "Case-by-case details are not stored for this run (it predates storing them, or it failed); run the metric again to include them."));
    return el("section", { className: "metric" }, ...parts);
  }
  const verdict = runVerdict(details);
  if (verdict) parts.push(el("p", { className: `verdict ${details.status}` }, verdict));
  const facts = runFacts(details);
  if (facts.length) parts.push(dl(facts));
  if (details.note) parts.push(el("p", { className: "note" }, `⚠ ${details.note}`));
  const failed = details.rows.filter((r) => !r.passed);
  const passed = details.rows.filter((r) => r.passed);
  if (failed.length) {
    parts.push(el("h4", {}, `Failed cases (${failed.length})`));
    parts.push(...failed.map(block));
  }
  if (passed.length && options.passed) {
    parts.push(el("h4", {}, `Passed cases (${passed.length})`));
    parts.push(...passed.map(block));
  } else if (passed.length) {
    parts.push(el("p", { className: "muted small" }, `${passed.length} passed case${passed.length === 1 ? "" : "s"} not listed.`));
  }
  if (!details.rows.length) parts.push(el("p", { className: "muted small" }, "No cases were recorded for this run."));
  return el("section", { className: "metric" }, ...parts);
}

// Metrics with no run in this session, each with why: the backend's skip_reason
// (not applicable to this chatbot, or no test case in its golden set), else not picked.
function notRunSection(metrics, latestByKey) {
  const missing = metrics.filter((m) => !latestByKey[m.key]);
  if (!missing.length) return null;
  return el(
    "section",
    {},
    el("h2", {}, "Metrics not run"),
    el("p", { className: "muted small" }, "Nothing was scored for these metrics, so they count neither as passed nor as failed."),
    el(
      "table",
      { className: "summary" },
      el("thead", {}, el("tr", {}, el("th", {}, "Metric"), el("th", {}, "Category"), el("th", {}, "Why it was not run"))),
      el("tbody", {}, ...missing.map((m) => el(
        "tr",
        {},
        el("td", {}, m.title),
        el("td", {}, m.ui_category_label || m.ui_category || ""),
        el("td", {}, m.skip_reason || "Not selected in this session's runs: tick it and run to include it."),
      ))),
    ),
  );
}

function appendix(metrics) {
  const lower = metrics.filter((m) => m.direction === "lower").map((m) => m.title);
  const lowerText = lower.length
    ? `${lower.join(", ")} ${lower.length === 1 ? "is a violation rate" : "are violation rates"}: lower is better (≤), shown as 1 − DeepEval's score.`
    : "";
  return el(
    "section",
    { className: "appendix" },
    el("h2", {}, "How to read the scores"),
    el(
      "ul",
      {},
      el("li", {}, "Each metric sends the golden questions to the chatbot and has the judge model score every reply from 0 to 1."),
      el("li", {}, "As in DeepEval, a case passes when its score meets the metric's threshold, and a run passes only when every one of its cases passes. A run's score is the average of its case scores, shown for trends; it does not decide the result."),
      el("li", {}, "Test case results: a test case (TC-01, …) is one golden question. Each metric that asked it is one check: PASSED when that case's score meets the metric's threshold. A test case's success rate is the share of its checks that passed; the overall success rate is the share of all checks."),
      el("li", {}, `Higher is better (≥) for most metrics. ${lowerText}`),
      el("li", {}, "In single-turn cases (when bold differences are on), bold words in the actual result are missing from the expected result, and bold words in the expected result are missing from the actual one (case and punctuation ignored). Different wording is not always wrong: the judge scores meaning."),
      el("li", {}, "A test case passes only when every metric that scored it passes (DeepEval's test-run rule)."),
      el("li", {}, "Skipped cases were longer than the chatbot's max message length and were not sent."),
      el("li", {}, "With the judge-consistency check on, every reply is scored twice; a run whose two scorings of one case differ by more than 0.15 is marked judge unstable."),
      el("li", {}, "Replies such as \"limit reached\" or \"please log in\" stop a run as an error; such runs are not scored or listed."),
    ),
  );
}

// What goes into the report is the viewer's choice, made on the page before
// saving the PDF (and remembered in this browser). Every metric is handled the
// same way from the catalog's data, so new metrics need no change here.
const SECTION_OPTIONS = [
  ["summary", "Summary table"],
  ["notrun", "Metrics not run (with why)"],
  ["cases", "Test case results"],
  ["improve", "How to improve the score"],
  ["details", "Metric details (case cards)"],
  ["passed", "Passed cases in metric details (doubles the page count)"],
  ["diff", "Bold differing words"],
  ["appendix", "How to read the scores"],
];
const DEFAULT_OPTIONS = Object.freeze({
  ...Object.fromEntries(SECTION_OPTIONS.map(([key]) => [key, true])),
  // Passed cases are already rows in the test case table; their cards made
  // the PDF about twice as long and slow to preview, so they are opt-in.
  passed: false,
  metrics: null, // null = every metric that ran; else the chosen metric keys
});

// Saved options with unknown keys dropped and missing ones defaulted, so an
// older or damaged saved value can never break the report.
function readOptions(saved) {
  const options = { ...DEFAULT_OPTIONS };
  if (!saved || typeof saved !== "object") return options;
  for (const [key] of SECTION_OPTIONS) if (typeof saved[key] === "boolean") options[key] = saved[key];
  if (Array.isArray(saved.metrics)) options.metrics = saved.metrics.filter((k) => typeof k === "string");
  return options;
}

function selectedMetrics(ranMetrics, options) {
  if (!options.metrics) return ranMetrics;
  const keys = new Set(options.metrics);
  return ranMetrics.filter((m) => keys.has(m.key));
}

// The report's sections for the chosen options.
function renderReport(data, options) {
  const { target, judge, metrics, latestByKey, details, thresholds } = data;
  const ranMetrics = metrics.filter((m) => latestByKey[m.key]);
  const ran = selectedMetrics(ranMetrics, options);
  const cases = collectCases(ran, latestByKey, details, thresholds);
  const caseIds = new Map(cases.map((c) => [c.row, c.id]));
  // With a hand-picked set, metrics that were not picked (or never ran) stay out.
  const shown = options.metrics ? ran : metrics;
  const parts = [cover(target, judge, shown, latestByKey, cases)];
  if (ranMetrics.length && !ran.length) {
    parts.push(el("p", { className: "note" }, "No metric is selected: tick at least one under \"Metrics\" above."));
  }
  if (options.summary) parts.push(summaryTable(shown, latestByKey, thresholds));
  if (options.notrun) {
    // A hand-picked set still lists the metrics this chatbot cannot run (they
    // cannot be picked), just not the ones left unticked.
    const section = notRunSection(options.metrics ? metrics.filter((m) => m.skip_reason) : metrics, latestByKey);
    if (section) parts.push(section);
  }
  if (options.cases && ran.length) parts.push(testCaseTable(cases, ran, latestByKey, details, options));
  if (options.improve && ran.length) parts.push(actionPlan(ran, latestByKey, details, thresholds, caseIds));
  if (options.details && ran.length) {
    parts.push(el("h2", { className: "page-break" }, "Metric details"),
      ...ran.map((m) => metricSection(m, latestByKey[m.key], details[m.key], thresholds, caseIds, options)));
  }
  if (options.appendix) parts.push(appendix(metrics));
  return parts;
}

// The on-page choices (never printed): sections, then metrics grouped by category.
function optionsForm(data, options, onChange) {
  const ranMetrics = data.metrics.filter((m) => data.latestByKey[m.key]);
  const allKeys = ranMetrics.map((m) => m.key);
  const chosen = new Set(options.metrics || allKeys);
  const setMetrics = (keys) => onChange({ ...options, metrics: keys.length === allKeys.length ? null : keys });
  const toggleMetric = (key, on) => {
    const next = new Set(chosen);
    if (on) next.add(key); else next.delete(key);
    setMetrics(allKeys.filter((k) => next.has(k)));
  };
  const box = (id, label, checked, onToggle) => el(
    "label",
    { className: "option" },
    el("input", { type: "checkbox", id, checked, onchange: (event) => onToggle(event.target.checked) }),
    " ",
    label,
  );
  const groups = new Map();
  for (const m of ranMetrics) {
    const group = m.ui_category_label || m.ui_category || "Other";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(m);
  }
  const failedKeys = ranMetrics.filter((m) => !data.latestByKey[m.key].passed).map((m) => m.key);
  const button = (id, label, onClick) => el("button", { type: "button", id, onclick: onClick }, label);
  return [
    el(
      "fieldset",
      {},
      el("legend", {}, "Sections"),
      ...SECTION_OPTIONS.map(([key, label]) => box(`opt-${key}`, label, options[key],
        (on) => onChange({ ...options, [key]: on }))),
    ),
    ranMetrics.length ? el(
      "fieldset",
      {},
      el("legend", {}, `Metrics (${chosen.size} of ${ranMetrics.length})`),
      el(
        "p",
        { className: "option-actions" },
        button("opt-all", "All", () => setMetrics(allKeys)),
        button("opt-failed", "Failed only", () => setMetrics(failedKeys)),
        button("opt-none", "None", () => setMetrics([])),
      ),
      ...[...groups.entries()].map(([group, items]) => el(
        "div",
        { className: "option-group" },
        el("b", {}, group),
        ...items.map((m) => box(`opt-metric-${m.key}`, m.title, chosen.has(m.key), (on) => toggleMetric(m.key, on))),
      )),
    ) : null,
    el("p", { className: "muted small" }, "Your choices are remembered in this browser. Nothing here changes any data."),
  ];
}

async function loadReportData() {
  const targetId = Number(new URLSearchParams(location.search).get("target"));
  const [targets, judge, thresholds] = await Promise.all([
    fetchTargets(),
    api("/api/judge/settings").catch(() => null),
    settings.get("thresholds", {}),
  ]);
  const target = targets.find((t) => t.id === targetId);
  if (!target) throw new Error("Chatbot not found: open the report from the dashboard.");
  const [metrics, latest] = await Promise.all([
    api(`/api/metrics?target_id=${target.id}`),
    api(`/api/runs/latest?target_id=${target.id}&since_id=${await sessionSince()}`),
  ]);
  const latestByKey = Object.fromEntries(latest.map((r) => [r.metric_key, r]));
  // Every case comes from the database: each run's stored result.
  const details = Object.fromEntries(metrics.map((m) => [m.key, caseDetailsFromRun(latestByKey[m.key])]));
  return { target, judge, metrics, latestByKey, details, thresholds };
}

// Renders the choices and the report; a change re-renders both and keeps
// keyboard focus on the control that was used.
function showReport(data, options) {
  const focusedId = document.activeElement && document.activeElement.id;
  const update = (next) => {
    showReport(data, next);
    settings.set("reportOptions", next).catch(() => {});
  };
  $("report-options").replaceChildren(...optionsForm(data, options, update).filter(Boolean));
  $("report").replaceChildren(...renderReport(data, options));
  if (focusedId && $(focusedId)) $(focusedId).focus();
}

async function start() {
  const [data, saved] = await Promise.all([
    loadReportData(),
    settings.get("reportOptions", null).catch(() => null),
  ]);
  document.title = `Evaluation report · ${data.target.name}`;
  showReport(data, readOptions(saved));
  return data.metrics.some((m) => data.latestByKey[m.key]);
}

$("print-button").addEventListener("click", () => window.print());
$("report-options").addEventListener("submit", (event) => event.preventDefault());

start()
  .then((ran) => {
    $("report-status").textContent = ran
      ? "Choose what to include, then click Save as PDF (pick \"Save as PDF\" as the printer)."
      : "No metric has been run for this chatbot yet: the report has only the summary.";
    $("print-button").disabled = false;
  })
  .catch((error) => {
    $("report-status").textContent = error.message;
  });
