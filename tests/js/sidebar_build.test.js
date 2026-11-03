// Tests for the side panel's automatic test-data build, the detected kind and
// the leftovers list (ChatbotExtension/sidebar/sidebar.js). The real api.js,
// announce.js, ui.js and sidebar.js run in a VM against a small fake DOM seeded
// from sidebar.html (an id the page lacks fails here as it would in the
// browser) and a fake backend speaking the targets / mcp/tools / prepare-all
// API, like report.test.js: `node --test tests/js`. tests/test_report_js.py runs
// every tests/js/*.test.js with pytest.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXT = path.join(__dirname, "..", "..", "ChatbotExtension");
const SCRIPTS = ["lib/api.js", "lib/announce.js", "lib/ui.js", "sidebar/sidebar.js"];
const ORIGIN = "http://127.0.0.1:8765";
const HTML = fs.readFileSync(path.join(EXT, "sidebar", "sidebar.html"), "utf8");

// --- fake DOM ------------------------------------------------------------------
class FakeNode {
  constructor(tagName, document) {
    this.tagName = tagName;
    this.ownerDocument = document;
    this.parentNode = null;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.id = "";
    this.style = {};
    this.dataset = {};
    this.files = [];
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.hidden = false;
    this.selected = false;
    this.indeterminate = false;
    this.placeholder = "";
    this.title = "";
    this.rawValue = "";
  }

  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    return {
      contains: (name) => names().includes(name),
      add: (...add) => { this.className = [...new Set([...names(), ...add])].join(" "); },
      remove: (...drop) => { this.className = names().filter((n) => !drop.includes(n)).join(" "); },
      toggle: (name, force) => {
        const on = force === undefined ? !names().includes(name) : Boolean(force);
        if (on) this.classList.add(name); else this.classList.remove(name);
        return on;
      },
    };
  }

  // A <select> reads its value from its options; other fields keep their own.
  get options() { return this.children.filter((c) => c.tagName === "option"); }
  get selectedOptions() { return this.options.filter((o) => o.selected); }
  get value() {
    const options = this.options;
    if (!options.length) return this.rawValue;
    const chosen = options.find((o) => o.selected) || options[0];
    return chosen.attributes.value ?? chosen.textContent;
  }
  set value(value) {
    const options = this.options;
    if (!options.length) this.rawValue = String(value);
    for (const o of options) o.selected = String(o.attributes.value ?? o.textContent) === String(value);
  }

  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(text) { this.replaceChildren(String(text)); }

  append(...children) {
    for (const child of children) {
      const node = child instanceof FakeNode ? child : new FakeText(String(child), this.ownerDocument);
      if (node.parentNode) node.remove();
      node.parentNode = this;
      this.children.push(node);
    }
  }
  replaceChildren(...children) {
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...children);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
  }
  after(node) {
    const parent = this.parentNode;
    if (node.parentNode) node.remove();
    node.parentNode = parent;
    parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
  }
  contains(node) {
    for (let up = node; up; up = up.parentNode) if (up === this) return true;
    return false;
  }
  closest(selector) {
    for (let up = this; up; up = up.parentNode) if (matches(up, selector)) return up;
    return null;
  }
  querySelectorAll(selector) { return descendants(this).filter((n) => matches(n, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "id") this.id = String(value);
    if (name === "value" && this.tagName !== "option") this.rawValue = String(value);
  }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  // Runs the listeners and resolves when the async ones have finished.
  fire(type, extra = {}) {
    const event = { type, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...extra };
    return Promise.all((this.listeners[type] || []).map((fn) => fn(event)));
  }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
  reset() {}
}

class FakeText extends FakeNode {
  constructor(data, document) {
    super("#text", document);
    this.data = data;
  }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}

function descendants(node) {
  return node.children.filter((c) => c.tagName !== "#text").flatMap((c) => [c, ...descendants(c)]);
}

// Enough of CSS selectors for the panel: "tag.class.class:checked", descendant
// chains and comma lists.
function matchesCompound(node, compound) {
  const m = compound.match(/^([a-z0-9-]*)((?:\.[\w-]+)*)(:checked)?$/i);
  if (!m || node.tagName === "#text") return false;
  const [, tag, classes, checked] = m;
  if (tag && node.tagName !== tag.toLowerCase()) return false;
  const names = node.className.split(/\s+/);
  if (classes.split(".").filter(Boolean).some((cls) => !names.includes(cls))) return false;
  return !checked || node.checked;
}

function matches(node, selector) {
  return selector.split(",").some((part) => {
    const chain = part.trim().split(/\s+/);
    if (!matchesCompound(node, chain[chain.length - 1])) return false;
    let i = chain.length - 2;
    for (let up = node.parentNode; up && i >= 0; up = up.parentNode) if (matchesCompound(up, chain[i])) i -= 1;
    return i < 0;
  });
}

function parseAttrs(text) {
  const attrs = {};
  for (const m of text.matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?/g)) attrs[m[1]] = m[2] ?? m[3] ?? "";
  return attrs;
}

// Every element of sidebar.html with an id, with its tag, classes and flags;
// getElementById finds nothing else, as in the browser.
function makeDocument() {
  const nodes = new Map();
  const document = {
    title: "LLM Judge",
    hidden: false,
    activeElement: null,
    createElement: (tag) => new FakeNode(String(tag).toLowerCase(), document),
    getElementById: (id) => nodes.get(id) || null,
    querySelectorAll: (selector) => [...nodes.values()].filter((n) => !n.parentNode)
      .flatMap((root) => [root, ...descendants(root)]).filter((n) => matches(n, selector)),
    querySelector: (selector) => document.querySelectorAll(selector)[0] || null,
    addEventListener: () => {},
    nodes,
  };
  for (const m of HTML.matchAll(/<([a-zA-Z][\w-]*)\s([^>]*)>/g)) {
    const attrs = parseAttrs(m[2]);
    if (!attrs.id) continue;
    const node = document.createElement(m[1]);
    node.id = attrs.id;
    node.attributes = { ...attrs };
    node.className = attrs.class || "";
    for (const flag of ["checked", "open", "hidden", "disabled"]) node[flag] = flag in attrs;
    node.placeholder = attrs.placeholder || "";
    node.title = attrs.title || "";
    if ("value" in attrs) node.rawValue = attrs.value;
    nodes.set(attrs.id, node);
  }
  return document;
}

const byClass = (root, cls) => descendants(root).filter((n) => n.className.split(/\s+/).includes(cls));
const byTag = (root, tag) => descendants(root).filter((n) => n.tagName === tag);

// --- fake backend ----------------------------------------------------------------
const METRICS = [
  { key: "answer_relevancy", title: "Answer Relevancy", direction: "higher", threshold: 0.7,
    ui_category: "quality", ui_category_label: "Quality", cases_available: 3 },
  { key: "contextual_recall", title: "Contextual Recall", direction: "higher", threshold: 0.7,
    ui_category: "retrieval", ui_category_label: "Retrieval", cases_available: 0 },
  { key: "tool_correctness", title: "Tool Correctness", direction: "higher", threshold: 0.7,
    ui_category: "agent", ui_category_label: "Agent", cases_available: 0 },
];

const IDLE = { status: "idle", steps: [], metrics: null };
const RUNNING = { status: "running", metrics: { ready: 20, missing: [] }, steps: [
  { key: "goldens", label: "Goldens and scenarios", status: "done", detail: "6 goldens, 2 scenarios" },
  { key: "context", label: "Context goldens", status: "skipped", detail: "No help pages found" },
  { key: "tools", label: "Tool tests", status: "running", detail: "" },
  { key: "probes", label: "Security probes", status: "pending", detail: "" },
] };
const DONE = { status: "done", steps: [
  { key: "goldens", label: "Goldens and scenarios", status: "done", detail: "6 goldens, 2 scenarios" },
  { key: "context", label: "Context goldens", status: "skipped", detail: "No help pages found" },
  { key: "tools", label: "Tool tests", status: "done", detail: "8 tool tests <b>written</b>" },
  { key: "probes", label: "Security probes", status: "error", detail: "Judge rate limit" },
], metrics: { ready: 31, missing: [
  { key: "contextual_recall", title: "Contextual Recall", reason: "No golden answer with Context: the server has no help pages." },
  { key: "turn_faithfulness", title: "Turn Faithfulness", reason: "Needs retrieved context, which an MCP server does not return." },
] } };

const STATUS_TEXT = { 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict" };

function makeBackend({ targets = [], judgeUp = true, tools = null, toolsError = "", prepareAll = {},
  noPrepareAll = false, noJudgeKey = false, autoBuild = true, settingsFail = false } = {}) {
  const backend = {
    calls: [], pending: 0, closed: false, nextId: 10,
    targets: JSON.parse(JSON.stringify(targets)),
    prepareAll: { ...prepareAll },
  };
  // As app._public: secret values never leave the backend.
  const masked = (target) => {
    const config = { ...target.config };
    for (const key of ["env", "headers"]) {
      if (config[key]) config[key] = Object.fromEntries(Object.keys(config[key]).map((k) => [k, "***"]));
    }
    return { ...target, config };
  };
  const find = (id) => backend.targets.find((t) => t.id === Number(id));
  function respond(method, p, body) {
    const route = `${method} ${p}`;
    let m;
    if (route === "GET /api/status") return [200, { judge: { up: judgeUp, model: "judge-x" } }];
    if (route === "POST /api/goldens/reset") return [200, { reset: true }];
    if (route === "POST /api/session/start") return [200, { since_id: 0 }];
    if (route === "GET /api/judge/settings") {
      return [200, { model: "judge-x", base_url: "https://judge.example/v1", has_key: judgeUp, key_hint: "••••abcd", key_source: "env" }];
    }
    if (route === "GET /api/confident/settings") return [200, { has_key: false, auto: false, key_source: null, project: "" }];
    if (route === "GET /api/langfuse/settings") return [200, { has_keys: false, auto: false, key_source: null, host: "" }];
    if (route === "GET /api/build/settings") {
      return settingsFail ? [500, { detail: "boom" }] : [200, { auto_build: autoBuild }];
    }
    if (route === "GET /api/metrics") return [200, METRICS];
    if (route === "GET /api/targets") return [200, backend.targets.map(masked)];
    if (route === "POST /api/targets") {
      const target = { id: backend.nextId++, name: body.name, type: body.type, config: body.config };
      backend.targets.push(target);
      return [200, masked(target)];
    }
    if (route === "POST /api/targets/mcp/tools") {
      return toolsError ? [400, { detail: toolsError }] : [200, tools || { tools: [], server: {} }];
    }
    if ((m = p.match(/^\/api\/targets\/(\d+)$/)) && method === "PUT") {
      const target = find(m[1]);
      const config = { ...body.config };
      // A PUT without env / headers keeps the stored ones; a value of *** keeps that one.
      for (const key of ["env", "headers"]) {
        if (!(key in config)) {
          if (target.config[key]) config[key] = target.config[key];
        } else {
          config[key] = Object.fromEntries(Object.entries(config[key])
            .map(([k, v]) => [k, v === "***" ? (target.config[key] || {})[k] : v]));
        }
      }
      Object.assign(target, { name: body.name, type: body.type, config });
      return [200, masked(target)];
    }
    if ((m = p.match(/^\/api\/targets\/(\d+)\/prepare-goldens$/)) && method === "POST") {
      const target = find(m[1]);
      if (!target.config.theme || target.config.theme === "generic") {
        target.config.theme = target.name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      }
      return [200, { theme: target.config.theme, seeded: 0, documents: [], errors: [], can_generate: ["chatbot"],
        needs_context: false, message: "AI can build this chatbot's test data. Generate them?" }];
    }
    if ((m = p.match(/^\/api\/targets\/(\d+)\/prepare-all\/cancel$/)) && method === "POST") {
      // Stop: the next polls see the build stopped.
      const stopped = { status: "stopped", kind: "", leftovers: [], steps: [], metrics: { ready: 1, missing: [] } };
      backend.prepareAll[m[1]] = [stopped];
      return [200, { ...stopped, status: "running" }];
    }
    if ((m = p.match(/^\/api\/targets\/(\d+)\/prepare-all$/))) {
      if (noPrepareAll) return [404, { detail: "Not Found" }];
      const queue = backend.prepareAll[m[1]] || [];
      if (method === "POST") {
        if (noJudgeKey) return [503, { detail: "No judge key: add it in Judge settings." }];
        return [200, { status: "running", steps: [], metrics: null }];
      }
      // Each poll takes the next status; the last one stays.
      return [200, queue.length > 1 ? queue.shift() : queue[0] || IDLE];
    }
    if (route === "GET /api/goldens/health") return [200, []];
    if (["GET /api/goldens", "GET /api/conversations", "GET /api/runs/latest", "GET /api/documents", "GET /api/jobs"]
      .includes(route)) return [200, []];
    return [404, { detail: "Not Found" }];
  }
  backend.fetch = async (url, init = {}) => {
    // After the test: polling loops wait forever instead of keeping Node alive.
    if (backend.closed) return new Promise(() => {});
    const method = (init.method || "GET").toUpperCase();
    const parsed = new URL(url);
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    backend.calls.push({ method, path: parsed.pathname, query: parsed.search, body });
    // Build polls repeat on their own; idle() waits for everything else.
    const counted = !(method === "GET" && parsed.pathname.endsWith("/prepare-all"));
    if (counted) backend.pending += 1;
    const [status, data] = respond(method, parsed.pathname, body);
    if (counted) setTimeout(() => { backend.pending -= 1; }, 1);
    return { ok: status < 400, status, statusText: STATUS_TEXT[status] || "OK", text: async () => JSON.stringify(data) };
  };
  return backend;
}

// --- loading the panel ---------------------------------------------------------------
async function settle(check, ms = 3000) {
  const end = Date.now() + ms;
  while (!check() && Date.now() < end) await new Promise((resolve) => setTimeout(resolve, 2));
  return check();
}

// Waits until no request (other than build polls) has been in flight for a while.
async function idle(backend) {
  const end = Date.now() + 3000;
  let quiet = 0;
  while (quiet < 6 && Date.now() < end) {
    await new Promise((resolve) => setTimeout(resolve, 2));
    quiet = backend.pending === 0 ? quiet + 1 : 0;
  }
}

async function loadPanel(options = {}) {
  const backend = makeBackend(options);
  const storage = {};
  const tabStorage = {};
  const document = makeDocument();
  const store = (data) => ({
    getItem: (k) => {
      const key = k.replace(/^llmJudge:/, "");
      return key in data ? JSON.stringify(data[key]) : null;
    },
    setItem: (k, v) => { data[k.replace(/^llmJudge:/, "")] = JSON.parse(v); },
    removeItem: (k) => { delete data[k.replace(/^llmJudge:/, "")]; },
  });
  const context = {
    console, URL, URLSearchParams, Promise, Date, Map, Set, Math, JSON, Uint16Array,
    Node: FakeNode,
    document,
    location: { protocol: "http:", origin: ORIGIN, host: "127.0.0.1:8765", href: `${ORIGIN}/app/sidebar/sidebar.html`, search: "" },
    window: { addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }), open: () => null, parent: null },
    fetch: backend.fetch,
    localStorage: store(storage),
    sessionStorage: store(tabStorage),
    // Waits (the 2 s build poll, 3 s confirm resets) shrink to a few ms; the
    // panel's 10 s status loop never runs.
    setTimeout: (fn, ms, ...args) => setTimeout(fn, Math.min(Number(ms) || 0, 5), ...args),
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
  };
  vm.createContext(context);
  for (const file of SCRIPTS) {
    vm.runInContext(fs.readFileSync(path.join(EXT, file), "utf8"), context, { filename: file });
  }
  const $ = (id) => {
    const node = document.getElementById(id);
    assert.ok(node, `#${id} is in sidebar.html`);
    return node;
  };
  const page = {
    context, document, backend, storage, $,
    text: (id) => $(id).textContent,
    visible: (id) => !$(id).classList.contains("hidden"),
    type(values) {
      for (const [id, value] of Object.entries(values)) $(id).value = value;
    },
    // A radio: the others of its group go off, as in the browser.
    choose(id) {
      const radio = $(id);
      for (const node of document.nodes.values()) {
        if (node.attributes.name && node.attributes.name === radio.attributes.name) node.checked = false;
      }
      radio.checked = true;
      return radio.fire("change");
    },
    click: (id) => $(id).fire("click"),
    submit: (id) => $(id).fire("submit"),
    calls: (method, p) => backend.calls.filter((c) => c.method === method && c.path === p),
    count: (method, p) => page.calls(method, p).length,
    last(method, p) {
      const found = page.calls(method, p);
      assert.ok(found.length, `${method} ${p} was called`);
      return found[found.length - 1];
    },
    state: () => vm.runInContext("state", context),
    steps: () => byClass($("testdata-steps"), "build-step").map((li) => li.textContent),
    settle,
    idle: () => idle(backend),
    close: () => { backend.closed = true; },
  };
  await settle(() => backend.calls.some((c) => c.method === "GET" && c.path === "/api/targets"));
  await page.idle();
  return page;
}

const MCP_FILES = { id: 3, name: "Files", type: "mcp", config: {
  transport: "stdio", command: "npx", args: ["-y", "@mcp/files"], env: { API_KEY: "real-key", REGION: "eu" },
  persona: "File helper", agent_model: "m-1", theme: "files", help_url: "https://files.example/help", probe_set: "target_3",
} };
const SHOP_BOT = { id: 4, name: "Shop bot", type: "http", config: { url: "https://bot.example/chat", theme: "shop" } };

const HINT = "Add a help page URL in the chatbot's settings.";
const LEFTOVERS = [
  { key: "contextual_recall", title: "Contextual Recall", reason: "No help pages found", hint: HINT },
  { key: "tool_correctness", title: "Tool Correctness", reason: "Does not apply to this target", hint: "" },
];
const BUSY = { status: "running", kind: "agent", leftovers: [], steps: [], metrics: null };
const finished = (extra = {}) => ({ status: "done", kind: "agent", leftovers: LEFTOVERS, steps: [],
  metrics: { ready: 3, missing: [] }, ...extra });
const EXISTING = (config = {}) => ({ id: 4, name: "Shop bot", type: "http",
  config: { url: "https://bot.example/chat", theme: "shop", ...config } });

// --- tests ---------------------------------------------------------------------------
test("a new chatbot with auto build on starts the build at once and never shows the question", async (t) => {
  const page = await loadPanel({ prepareAll: { 10: [BUSY] } });
  t.after(page.close);
  await page.choose("kind-mcp");
  page.type({ "mcp-name": "Files", "mcp-command": "npx", "mcp-args": "@mcp/files" });
  await page.submit("mcp-form");
  await page.idle();
  assert.deepEqual(page.last("POST", "/api/targets/10/prepare-all").body, { generate: true });
  assert.equal(page.count("POST", "/api/targets/10/prepare-all"), 1);
  assert.ok(!page.visible("testdata-question"));
  assert.ok(!page.visible("testdata-ask"));
  assert.ok(page.visible("testdata"));
});

test("a new chatbot with auto build off shows the question and sends no build", async (t) => {
  const page = await loadPanel({ autoBuild: false });
  t.after(page.close);
  await page.choose("kind-mcp");
  page.type({ "mcp-name": "Files", "mcp-command": "npx", "mcp-args": "@mcp/files" });
  await page.submit("mcp-form");
  await page.idle();
  assert.ok(page.visible("testdata-question"));
  assert.equal(page.count("POST", "/api/targets/10/prepare-all"), 0);
});

test("when the settings request fails the question is shown (safe default)", async (t) => {
  const page = await loadPanel({ settingsFail: true });
  t.after(page.close);
  await page.choose("kind-mcp");
  page.type({ "mcp-name": "Files", "mcp-command": "npx", "mcp-args": "@mcp/files" });
  await page.submit("mcp-form");
  await page.idle();
  assert.ok(page.visible("testdata-question"));
  assert.equal(page.count("POST", "/api/targets/10/prepare-all"), 0);
});

test("an existing chatbot never built automatically and with a hinted leftover starts one build", async (t) => {
  const page = await loadPanel({ targets: [EXISTING()], prepareAll: { 4: [finished()] } });
  t.after(page.close);
  await page.settle(() => page.count("POST", "/api/targets/4/prepare-all") > 0);
  await page.idle();
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 1);
  assert.deepEqual(page.last("POST", "/api/targets/4/prepare-all").body, { generate: true });
});

test("a chatbot already built automatically is not built again", async (t) => {
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [finished()] } });
  t.after(page.close);
  await page.idle();
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 0);
});

test("leftovers without a hint do not start a build; auto build off does not either", async (t) => {
  const none = { ...finished(), leftovers: [LEFTOVERS[1]] };
  const a = await loadPanel({ targets: [EXISTING()], prepareAll: { 4: [none] } });
  t.after(a.close);
  await a.idle();
  assert.equal(a.count("POST", "/api/targets/4/prepare-all"), 0);
  const b = await loadPanel({ targets: [EXISTING()], prepareAll: { 4: [finished()] }, autoBuild: false });
  t.after(b.close);
  await b.idle();
  assert.equal(b.count("POST", "/api/targets/4/prepare-all"), 0);
});

test("a running build is followed, not restarted", async (t) => {
  const page = await loadPanel({ targets: [EXISTING()], prepareAll: { 4: [BUSY, finished({ leftovers: [] })] } });
  t.after(page.close);
  await page.idle();
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 0);
});

test("kind and leftovers render as text, N/A ones under Does not apply", async (t) => {
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [BUSY, finished({
    leftovers: [{ ...LEFTOVERS[0], hint: "Do <b>this</b>" }, LEFTOVERS[1]] })] } });
  t.after(page.close);
  await page.settle(() => page.text("testdata-leftovers").includes("Tool Correctness"));
  assert.equal(page.text("testdata-kind"), "Detected: Agent (calls tools)");
  assert.ok(page.visible("testdata-kind"));
  const items = byTag(page.$("testdata-leftovers"), "li").map((li) => li.textContent);
  assert.deepEqual(items, [
    "Contextual Recall: No help pages found. Do <b>this</b>",
    "Tool Correctness: Does not apply to this target.",
  ]);
  assert.match(page.text("testdata-leftovers"), /Does not apply/);
  assert.equal(byTag(page.$("testdata-leftovers"), "b").length, 0);
});

test("each kind has its label and an empty kind hides the line", async (t) => {
  const labels = { rag: "Detected: RAG bot (returns sources)", chat: "Detected: Chatbot", mcp: "Detected: MCP server" };
  for (const [kind, label] of Object.entries(labels)) {
    const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [BUSY, finished({ kind })] } });
    t.after(page.close);
    await page.settle(() => page.text("testdata-kind") === label);
    assert.equal(page.text("testdata-kind"), label);
  }
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [BUSY, finished({ kind: "" })] } });
  t.after(page.close);
  await page.idle();
  await page.settle(() => page.text("testdata-status").startsWith("Test data built"));
  assert.ok(!page.visible("testdata-kind"));
});

test("selecting an already built chatbot shows its kind and leftovers without a build", async (t) => {
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [finished({ kind: "rag" })] } });
  t.after(page.close);
  await page.idle();
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 0);
  assert.ok(page.visible("testdata"));
  assert.equal(page.text("testdata-kind"), "Detected: RAG bot (returns sources)");
  assert.equal(byTag(page.$("testdata-leftovers"), "li").length, 2);
});

test("a reason ending in a period gets no double period", async (t) => {
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [finished({
    leftovers: [{ ...LEFTOVERS[0], reason: "No help pages found." }] })] } });
  t.after(page.close);
  await page.idle();
  const items = byTag(page.$("testdata-leftovers"), "li").map((li) => li.textContent);
  assert.deepEqual(items, [`Contextual Recall: No help pages found. ${HINT}`]);
});

test("a leftover is not applicable by its reason, even when it carries a hint", async (t) => {
  const odd = { key: "x", title: "X", reason: "Does not apply to this target", hint: "ignored?" };
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [finished({ leftovers: [odd] })] } });
  t.after(page.close);
  await page.idle();
  assert.match(page.text("testdata-leftovers"), /Does not apply/);
  assert.equal(byTag(page.$("testdata-leftovers"), "ul").length, 1);
});

test("a failed automatic start is not retried when the chatbot is selected again", async (t) => {
  const page = await loadPanel({ targets: [EXISTING()], prepareAll: { 4: [finished()] }, noJudgeKey: true });
  t.after(page.close);
  await page.idle();
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 1);
  await page.context.onTargetChanged();
  await page.idle();
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 1);
});

test("Stop shows while a build runs, posts the cancel and the build ends stopped", async (t) => {
  const page = await loadPanel({ targets: [EXISTING({ auto_built: true })], prepareAll: { 4: [BUSY] } });
  t.after(page.close);
  await page.settle(() => page.visible("testdata-stop"));
  assert.ok(page.visible("testdata-stop"));
  await page.click("testdata-stop");
  assert.equal(page.count("POST", "/api/targets/4/prepare-all/cancel"), 1);
  await page.settle(() => /stopped/i.test(page.text("testdata-status")));
  assert.match(page.text("testdata-status"), /stopped/i);
  assert.ok(!page.visible("testdata-stop"));
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 0);
});
