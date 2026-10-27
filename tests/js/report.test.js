// Tests for the printable report (ChatbotExtension/report/report.js).
// Runs the real api.js, ui.js and report.js in a VM with a small fake DOM,
// a fake backend and a fake localStorage, so it needs no browser and no
// npm packages: `node --test tests/js`. tests/test_report_js.py runs it with pytest.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXT = path.join(__dirname, "..", "..", "ChatbotExtension");
const SCRIPTS = ["lib/api.js", "lib/announce.js", "lib/ui.js", "report/report.js"];

// --- fake DOM: just what el(), the report and its options form use ----------
class FakeNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.id = "";
    this.disabled = false;
  }
  append(...children) {
    for (const c of children) this.children.push(c instanceof FakeNode ? c : new FakeText(String(c)));
  }
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name === "id") this.id = value;
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  fire(type, event) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  focus() {
    this.ownerDocument.activeElement = this;
  }
  get textContent() {
    return this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this.children = [new FakeText(String(value))];
  }
}

class FakeText extends FakeNode {
  constructor(data) {
    super("#text");
    this.data = data;
  }
  get textContent() {
    return this.data;
  }
}

function walk(node, visit) {
  visit(node);
  for (const c of node.children) walk(c, visit);
}

function findAll(root, predicate) {
  const found = [];
  walk(root, (n) => { if (n.tagName !== "#text" && predicate(n)) found.push(n); });
  return found;
}

const byClass = (root, cls) => findAll(root, (n) => n.className.split(/\s+/).includes(cls));
const byTag = (root, tag) => findAll(root, (n) => n.tagName === tag);

// --- fixture: one target, metrics of every kind -----------------------------
const TS = "2026-09-29T10:00:00Z";
const ADVICE = { chatbot: ["Answer the question first."], tests: ["Check the golden."] };

const METRICS = [
  { key: "answer_relevancy", title: "Answer Relevancy", direction: "higher", threshold: 0.7, kind: "single",
    ui_category_label: "Quality", scores_on: ["input", "actual_output"], improve: ADVICE },
  { key: "hallucination", title: "Hallucination", direction: "lower", threshold: 0.5, kind: "single",
    ui_category_label: "Quality", scores_on: ["actual_output", "context"], improve: ADVICE },
  { key: "knowledge_retention", title: "Knowledge Retention", direction: "higher", threshold: 0.7,
    kind: "conversation", ui_category_label: "Conversational", improve: {} },
  { key: "toxicity", title: "Toxicity", direction: "lower", threshold: 0.5, kind: "single", ui_category_label: "Safety" },
  { key: "role_violation", title: "Role Violation", direction: "higher", threshold: 0.7, kind: "single", ui_category_label: "Security" },
];

const REFUND = "What is the refund policy?";

function run(key, score, passed, rows, direction = "higher", threshold = 0.7) {
  return {
    metric_key: key, score, passed, ts: TS, judge_model: "judge-x",
    result: { status: passed ? "pass" : "fail", rows, threshold, direction, judge: { model: "judge-x" } },
  };
}

function latestRuns() {
  return [
    run("answer_relevancy", 0.6, false, [
      { question: REFUND, input: REFUND, actual_output: "Refunds take 5 days.", expected_output: "Refunds take 7 days.",
        score: 0.4, passed: false, reason: "Wrong <img src=x onerror=alert(1)> number." },
      { question: "Do you ship abroad?", input: "Do you ship abroad?", actual_output: "Yes.", expected_output: "Yes.",
        score: 0.8, passed: true, reason: "Direct." },
    ]),
    // Grounded golden: the same question is sent with facts attached.
    run("hallucination", 0.1, true, [
      { question: REFUND, input: `Facts: 7 days.\n\nQuestion: ${REFUND}`, actual_output: "Refunds take 5 days.",
        expected_output: "Refunds take 7 days.", context: ["7 days."], score: 0.1, passed: true, reason: "Consistent." },
    ], "lower", 0.5),
    run("knowledge_retention", 0.9, true, [
      { question: "Returns scenario", input: "hi\nreturn my order", actual_output: "user: hi\nbot: hello",
        expected_output: "The bot remembers the order number.", score: 0.9, passed: true, reason: "Remembered." },
    ]),
    { metric_key: "toxicity", score: 0, passed: true, ts: TS, judge_model: "judge-x" }, // no saved details
  ];
}

// --- loading the page --------------------------------------------------------
async function loadReport({ saved = {}, runs = latestRuns() } = {}) {
  const storage = { ...saved };
  const document = {
    title: "",
    activeElement: null,
    createElement(tag) {
      const node = new FakeNode(tag);
      node.ownerDocument = document;
      return node;
    },
    getElementById(id) {
      for (const root of roots) {
        const hit = findAll(root, (n) => n.id === id)[0];
        if (hit) return hit;
      }
      return null;
    },
  };
  const roots = ["report-status", "print-button", "report-options", "report"].map((id) => {
    const node = document.createElement(id === "print-button" ? "button" : "div");
    node.id = id;
    return node;
  });
  const responses = {
    "/api/targets": [{ id: 1, name: "Shop bot", config: { url: "https://bot.example/chat?token=secret", theme: "generic" } }],
    "/api/judge/settings": { model: "judge-x", base_url: "https://judge.example/v1" },
    "/api/metrics?target_id=1": METRICS,
    "/api/runs/latest?target_id=1&since_id=0": runs,
  };
  const printed = [];
  const context = {
    console, setTimeout, clearTimeout, URL, URLSearchParams, Promise, Date, Map, Set, Math, JSON, Uint16Array,
    Node: FakeNode,
    document,
    location: { search: "?target=1" },
    window: { print: () => printed.push(Date.now()) },
    fetch: async (url) => {
      const key = url.replace("http://127.0.0.1:8765", "");
      if (!(key in responses)) return { ok: false, status: 404, statusText: "Not Found", text: async () => "" };
      return { ok: true, status: 200, text: async () => JSON.stringify(responses[key]) };
    },
    // ui.js keeps settings in localStorage as "llmJudge:<key>" → JSON;
    // `storage` holds the parsed values so tests can read them directly.
    localStorage: {
      getItem: (k) => {
        const key = k.replace(/^llmJudge:/, "");
        return key in storage ? JSON.stringify(storage[key]) : null;
      },
      setItem: (k, v) => { storage[k.replace(/^llmJudge:/, "")] = JSON.parse(v); },
    },
    // ui.js keeps per-tab settings (the fresh-session mark) in sessionStorage.
    sessionStorage: { getItem: () => null, setItem: () => {} },
  };
  context.window.addEventListener = () => {};
  document.addEventListener = () => {};
  vm.createContext(context);
  for (const file of SCRIPTS) {
    vm.runInContext(fs.readFileSync(path.join(EXT, file), "utf8"), context, { filename: file });
  }
  const status = document.getElementById("report-status");
  for (let i = 0; i < 200 && (status.textContent === "" || /Building/.test(status.textContent)); i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  return { context, document, storage, printed, report: document.getElementById("report"),
    options: document.getElementById("report-options"), status };
}

function testCaseRows(report) {
  const table = byClass(report, "cases")[0];
  assert.ok(table, "test case table is rendered");
  return byTag(byTag(table, "tbody")[0], "tr");
}

// --- tests ---------------------------------------------------------------------
test("report loads without printing on its own and says how to save", async () => {
  const page = await loadReport();
  assert.match(page.status.textContent, /Save as PDF/);
  assert.equal(page.document.getElementById("print-button").disabled, false);
  assert.equal(page.printed.length, 0);
  assert.equal(page.document.title, "Evaluation report · Shop bot");
  assert.match(page.report.textContent, /https:\/\/bot\.example\/chat/);
  assert.doesNotMatch(page.report.textContent, /token=secret/, "chatbot URL query string is not printed");
});

test("the same golden question is one test case across metrics, even when sent with facts", async () => {
  const { report } = await loadReport();
  const text = report.textContent;
  const rows = testCaseRows(report);
  // TC-01 = refund question (Answer Relevancy + Hallucination), TC-02 = shipping, TC-03 = conversation.
  assert.equal(rows.length, 4);
  assert.match(rows[0].textContent, /TC-01/);
  assert.match(rows[0].textContent, /Answer Relevancy/);
  assert.match(rows[1].textContent, /Hallucination/);
  assert.doesNotMatch(rows[1].textContent, /TC-0/, "second metric of TC-01 shares its row group");
  assert.match(rows[2].textContent, /TC-02/);
  assert.match(rows[3].textContent, /TC-03.*Returns scenario/s);
  assert.doesNotMatch(text, /TC-05/);
});

test("test case table shows score detail, status and success rates", async () => {
  const { report } = await loadReport();
  const [refundRelevancy, refundHallucination, shipping] = testCaseRows(report);
  assert.match(refundRelevancy.textContent, /0\.40 \(threshold ≥ 0\.70, evaluation model=judge-x/);
  assert.match(refundRelevancy.textContent, /FAILED/);
  assert.match(refundRelevancy.textContent, /How to improve: Answer the question first\./);
  assert.match(refundRelevancy.textContent, /50%/, "TC-01 passed 1 of its 2 checks");
  assert.match(refundHallucination.textContent, /threshold ≤ 0\.50/, "lower-is-better threshold is a maximum");
  assert.match(refundHallucination.textContent, /PASSED/);
  assert.match(shipping.textContent, /100%/);
  const footer = byTag(byClass(report, "cases")[0], "tfoot")[0].textContent;
  assert.match(footer, /Overall success rate: 75% \(3 of 4 metric checks passed across 3 test cases\)/);
  assert.match(report.textContent, /1 metric run had no saved case details/, "Toxicity ran without details");
  assert.match(byClass(report, "headline")[0].textContent, /75%/);
});

test("judge text is rendered as text, never as markup", async () => {
  const { report } = await loadReport();
  assert.equal(byTag(report, "img").length, 0);
  assert.match(report.textContent, /<img src=x onerror=alert\(1\)>/);
});

test("how to improve covers failed metrics and passed metrics with failed cases", async () => {
  const { report } = await loadReport();
  const plan = byClass(report, "action-plan")[0];
  assert.ok(plan);
  const items = byClass(plan, "plan-item");
  assert.equal(items.length, 1, "only Answer Relevancy failed a case");
  assert.match(items[0].textContent, /Answer Relevancy/);
  assert.match(items[0].textContent, /Failed cases1 of 2: TC-01/);
  assert.match(items[0].textContent, /If they are fixedaverage 0\.90 ≥ 0\.70 → pass/);
  assert.match(items[0].textContent, /Fix the chatbot/);
});

test("passed case cards are opt-in: off by default, listed when ticked", async () => {
  const { report } = await loadReport();
  assert.equal(byClass(report, "case").length, 1, "only the failed case gets a card");
  assert.match(report.textContent, /passed cases? not listed/);
});

test("case cards: bold diff for single-turn cases, none for conversations", async () => {
  const { report } = await loadReport({ saved: { reportOptions: { passed: true } } });
  const cases = byClass(report, "case");
  const relevancy = cases.find((c) => /TC-01 · ✕ fail/.test(c.textContent));
  assert.deepEqual(byClass(relevancy, "diff").map((b) => b.textContent), ["5", "7"]);
  const grounded = cases.find((c) => /Sent to the chatbot/.test(c.textContent));
  assert.match(grounded.textContent, /Facts: 7 days\./, "the prompt actually sent is shown when it differs");
  const conversation = cases.find((c) => /Returns scenario/.test(c.textContent));
  assert.match(conversation.textContent, /Scenario/);
  assert.equal(byClass(conversation, "diff").length, 0);
  assert.match(relevancy.textContent, /How to improve/);
});

test("appendix names the lower-is-better metrics from the catalog", async () => {
  const { report } = await loadReport();
  const appendix = byClass(report, "appendix")[0].textContent;
  assert.match(appendix, /Hallucination, Toxicity are violation rates/);
  assert.doesNotMatch(appendix, /Bias/);
});

test("saved options pick sections and metrics at runtime", async () => {
  const page = await loadReport({ saved: { reportOptions: {
    metrics: ["answer_relevancy"], diff: false, passed: false, improve: false, appendix: false,
  } } });
  const rows = testCaseRows(page.report);
  assert.equal(rows.length, 2);
  assert.ok(rows.every((r) => /Answer Relevancy/.test(r.textContent)));
  assert.doesNotMatch(page.report.textContent, /How to improve/);
  assert.equal(byClass(page.report, "diff").length, 0);
  assert.match(page.report.textContent, /1 passed case not listed/);
  assert.equal(byClass(page.report, "appendix").length, 0);
  assert.doesNotMatch(byClass(page.report, "summary")[0].textContent, /Role Violation/, "unpicked metrics leave the summary");
});

test("damaged saved options fall back to defaults", async () => {
  const page = await loadReport({ saved: { reportOptions: { cases: "yes", metrics: [1, null, "answer_relevancy"], evil: true } } });
  const options = page.context.readOptions({ cases: "yes", metrics: [1, null, "x"], evil: true });
  assert.equal(options.cases, true);
  assert.deepEqual([...options.metrics], ["x"]);
  assert.equal("evil" in options, false);
  assert.equal(testCaseRows(page.report).length, 2);
  assert.equal(page.context.readOptions("junk").metrics, null);
});

test("ticking an option re-renders the report and remembers the choice", async () => {
  const page = await loadReport();
  const casesBox = page.document.getElementById("opt-cases");
  assert.ok(casesBox);
  casesBox.fire("change", { target: { checked: false } });
  assert.equal(byClass(page.report, "cases").length, 0);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(page.storage.reportOptions.cases, false);

  page.document.getElementById("opt-failed").fire("click", {});
  assert.deepEqual([...page.storage.reportOptions.metrics], ["answer_relevancy"]);
  page.document.getElementById("opt-none").fire("click", {});
  assert.match(page.report.textContent, /No metric is selected/);
  page.document.getElementById("opt-all").fire("click", {});
  assert.equal(page.storage.reportOptions.metrics, null, "All goes back to every metric, including future ones");
});

test("a chatbot with no runs still renders a summary", async () => {
  const page = await loadReport({ runs: [] });
  assert.match(page.status.textContent, /No metric has been run/);
  assert.equal(byClass(page.report, "cases").length, 0);
  assert.ok(byClass(page.report, "summary").length);
});

// runSummary lives in lib/ui.js (the side panel's run banner); ui.js is loaded here too.
test("run banner: progress while running, then a clear finish", async () => {
  const { context } = await loadReport();
  const pass = { key: "a", status: "pass" };
  const fail = { key: "b", status: "fail" };
  const error = { key: "c", status: "error" };

  const running = context.runSummary({ status: "running", results: [pass], count: 3, current: "Toxicity", elapsedMs: 65_000 });
  assert.equal(running.tone, "running");
  assert.equal(running.text, "Running metric 2 of 3: Toxicity · 1m 05s");
  assert.match(context.runSummary({ status: "queued", elapsedMs: 4000 }).text, /^Waiting for the run before this one/);

  const allPass = context.runSummary({ status: "done", results: [pass], count: 1, elapsedMs: 9000, at: "17:42" });
  assert.deepEqual({ ...allPass }, { tone: "pass", text: "✓ Run complete at 17:42 · 1 passed · took 9s" });
  const mixed = context.runSummary({ status: "done", results: [pass, fail, error], count: 3, elapsedMs: 1000 });
  assert.equal(mixed.tone, "fail");
  assert.equal(mixed.text, "✕ Run complete · 1 passed · 1 failed · 1 could not run · took 1s");
  assert.equal(context.runSummary({ status: "done", results: [error], count: 1 }).tone, "error");

  const stopped = context.runSummary({ status: "cancelled", results: [pass, { key: "b", status: "cancelled" }], count: 4, at: "9:05" });
  assert.equal(stopped.text, "■ Run stopped at 9:05 · 1 of 4 finished (1 passed)");
  assert.equal(context.runSummary({ status: "interrupted", error: "backend restarted" }).text, "✕ Run interrupted: backend restarted");
  assert.match(context.runSummary({ status: "empty" }).text, /^Nothing ran/);
});

test("wordDiff ignores case and punctuation and gives up on huge answers", async () => {
  const { context } = await loadReport();
  const same = context.wordDiff("Refunds, take 7 DAYS.", "refunds take 7 days");
  assert.ok(same.actual.every((t) => typeof t === "string"));
  const huge = Array.from({ length: 600 }, (_, i) => `w${i}`).join(" ");
  assert.equal(context.wordDiff(huge, huge), null);
});

test("wordDiff names each difference: missing, changed or added", async () => {
  const { context } = await loadReport();
  const diff = context.wordDiff(
    "Refunds take 5 days to the original card. Thanks!",
    "Refunds take 7 business days to the original payment method.",
  );
  const kinds = JSON.parse(JSON.stringify(diff.changes.map((c) => [c.kind, c.actual.trim(), c.expected.trim()])));
  assert.deepEqual(kinds, [
    ["changed", "5", "7 business"],
    ["changed", "card. Thanks!", "payment method."],
  ]);
  const onlyMissing = context.wordDiff("Refunds take 7 days.", "Refunds take 7 business days.");
  assert.deepEqual(JSON.parse(JSON.stringify(onlyMissing.changes.map((c) => [c.kind, c.expected.trim()]))), [["missing", "business"]]);
  const onlyAdded = context.wordDiff("Refunds take 7 days. Happy shopping!", "Refunds take 7 days.");
  assert.deepEqual(JSON.parse(JSON.stringify(onlyAdded.changes.map((c) => [c.kind, c.actual.trim()]))), [["added", "Happy shopping!"]]);
  // Each side marks its own words with the change's kind.
  const marked = (nodes) => JSON.parse(JSON.stringify(nodes.filter((n) => typeof n !== "string").map((n) => [n.className, n.textContent])));
  assert.deepEqual(marked(onlyMissing.expected), [["diff missing", "business"]]);
  assert.deepEqual(marked(onlyAdded.actual), [["diff added", "Happy"], ["diff added", "shopping!"]]);
});

test("failed case cards list the differences and how to pass the way DeepEval decides", async () => {
  const { report } = await loadReport();
  const card = byClass(report, "case").find((c) => /TC-01 · ✕ fail/.test(c.textContent));
  const differences = byClass(card, "diff-list")[0];
  assert.match(differences.textContent, /Changed: the reply says “5” where the expected answer says “7”/);
  assert.match(card.textContent, /Answer Relevancy does not compare the reply with the expected answer/);
  const pass = byClass(card, "how-to-pass")[0].textContent;
  assert.match(pass, /Pass rule: DeepEval passes this case when the Answer Relevancy score is ≥ 0\.70; it scored 0\.40, 0\.30 short\./);
  assert.match(pass, /Fix the chatbot: Answer the question first\./);
});
