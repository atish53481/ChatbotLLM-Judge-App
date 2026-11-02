// Tests for the side panel's MCP server form and its "Build test data for every
// metric" flow (ChatbotExtension/sidebar/sidebar.js). The real api.js,
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
  noPrepareAll = false, noJudgeKey = false } = {}) {
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

// --- tests ---------------------------------------------------------------------------
test("every element the side panel script looks up is in sidebar.html", () => {
  const ids = new Set([...HTML.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const script = fs.readFileSync(path.join(EXT, "sidebar", "sidebar.js"), "utf8");
  const missing = [...new Set([...script.matchAll(/\$\("([^"]+)"\)/g)].map((m) => m[1]))].filter((id) => !ids.has(id));
  assert.deepEqual(missing, []);
});

test("MCP server: the choice swaps the forms; Test & list tools sends the stdio config and lists the tools as text", async (t) => {
  const page = await loadPanel({ tools: { server: { name: "files", version: "1.2.0", instructions: "Paths are relative to /data." }, tools: [
    { name: "read_file", description: "Read a file <img src=x onerror=alert(1)>", input_schema: {} },
    { name: "list_dir", description: "", input_schema: {} },
  ] } });
  t.after(page.close);
  assert.ok(page.visible("target-form"), "the website chatbot form shows first");
  assert.ok(!page.visible("mcp-form"));
  assert.equal(page.$("add-target").open, true, "no chatbot yet: the add form is open");

  await page.choose("kind-mcp");
  assert.ok(!page.visible("target-form"));
  assert.ok(page.visible("mcp-form"));
  assert.ok(page.visible("mcp-stdio"));
  assert.ok(!page.visible("mcp-http"));

  page.type({
    "mcp-command": " npx ", "mcp-args": "-y\n\n @mcp/files \n",
    "mcp-env": "API_KEY=abc\nexport MODE=\"fast\"\n# a comment", "mcp-persona": "File helper",
    "mcp-agent-model": "openai/gpt-oss-20b",
  });
  await page.click("mcp-test");
  assert.deepEqual(page.last("POST", "/api/targets/mcp/tools").body, { config: {
    transport: "stdio", persona: "File helper", agent_model: "openai/gpt-oss-20b", command: "npx",
    args: ["-y", "@mcp/files"], url: "", headers: {}, env: { API_KEY: "abc", MODE: "fast" },
  } });
  assert.equal(page.text("mcp-test-status"), "Connected to files 1.2.0: 2 tools.");
  assert.equal(page.text("mcp-server-instructions"), "Server instructions: Paths are relative to /data.");
  assert.ok(page.visible("mcp-server-instructions"));
  assert.match(page.$("mcp-test-status").className, /status-pass/);
  assert.ok(page.visible("mcp-tool-list"));
  const items = byTag(page.$("mcp-tool-list"), "li").map((li) => li.textContent);
  assert.deepEqual(items, ["read_fileRead a file <img src=x onerror=alert(1)>", "list_dir(no description)"]);
  assert.equal(byTag(page.$("mcp-tool-list"), "img").length, 0, "descriptions are text, never markup");
  assert.equal(page.$("mcp-test").disabled, false);

  // Another transport: the listed tools were the other server's.
  await page.choose("mcp-transport-http");
  assert.ok(page.visible("mcp-http"));
  assert.ok(!page.visible("mcp-stdio"));
  assert.ok(!page.visible("mcp-tool-list"));
  assert.equal(page.text("mcp-test-status"), "");
});

test("Test & list tools checks the URL first and shows the backend's message on a 400", async (t) => {
  const page = await loadPanel({ toolsError: "The MCP server did not answer at this URL." });
  t.after(page.close);
  await page.choose("kind-mcp");
  await page.choose("mcp-transport-http");
  page.type({ "mcp-url": "ftp://files.example" });
  await page.click("mcp-test");
  assert.match(page.text("mcp-test-status"), /^URL must start with http:\/\/ or https:\/\//);
  assert.equal(page.count("POST", "/api/targets/mcp/tools"), 0, "nothing is sent for a bad form");

  page.type({ "mcp-url": "http://localhost:3000/mcp", "mcp-headers": "Authorization: Bearer t0k" });
  await page.click("mcp-test");
  assert.equal(page.text("mcp-test-status"), "The MCP server did not answer at this URL.", "no '400:' prefix");
  assert.match(page.$("mcp-test-status").className, /status-error/);
  assert.deepEqual(page.last("POST", "/api/targets/mcp/tools").body.config, {
    transport: "http", persona: "", agent_model: "", url: "http://localhost:3000/mcp", command: "", args: [], env: {},
    headers: { Authorization: "Bearer t0k" },
  });

  page.type({ "mcp-headers": "Authorization: ***" });
  await page.click("mcp-test");
  assert.match(page.text("mcp-test-status"), /Type the real value of header Authorization/);
});

test("saving an MCP server asks to build test data for every metric; Yes follows the build to its summary", async (t) => {
  const page = await loadPanel();
  t.after(page.close);
  await page.choose("kind-mcp");
  page.type({ "mcp-name": "Files", "mcp-command": "npx", "mcp-args": "@mcp/files", "mcp-env": "TOKEN=s3cret" });
  await page.submit("mcp-form");
  await page.idle();

  const created = page.last("POST", "/api/targets");
  assert.equal(created.body.type, "mcp");
  assert.equal(created.body.name, "Files");
  assert.deepEqual(created.body.config.env, { TOKEN: "s3cret" });
  assert.deepEqual(page.last("POST", "/api/targets/10/prepare-goldens").body, { help_url: "", generate: false });
  assert.equal(page.$("add-target").open, false);
  assert.equal(page.$("target-select").value, "10");
  assert.equal(page.$("target-select").children[0].textContent, "Files (MCP)");
  assert.equal(page.text("target-theme"), "MCP server · Golden set: files");

  // The question, before anything spends judge tokens.
  assert.ok(page.visible("testdata"));
  assert.ok(page.visible("testdata-question"));
  // Static text in the page (the fake DOM keeps ids and classes, not text).
  assert.match(HTML, /<p id="testdata-question"[^>]*>Build test data for every metric with AI\? It interviews the chatbot, reads its help pages, lists its tools and writes goldens, scenarios, tool tests and security probes fitted to it\. Uses judge tokens\.<\/p>/);
  assert.ok(page.visible("testdata-ask"));
  assert.ok(!page.visible("testdata-needs-judge"));
  assert.equal(page.document.activeElement, page.$("testdata-question"), "focus lands on the question");
  assert.equal(page.count("POST", "/api/targets/10/prepare-all"), 0);

  page.backend.prepareAll[10] = [RUNNING];
  const goldenLoads = page.count("GET", "/api/goldens");
  await page.click("testdata-yes");
  await page.settle(() => page.steps().length === 4);
  assert.deepEqual(page.last("POST", "/api/targets/10/prepare-all").body, { generate: true });
  assert.equal(page.text("testdata-status"),
    "Building test data for Files: Tool tests… It runs on the backend; you can keep working.");
  assert.ok(!page.visible("testdata-ask"), "the question gives way to the progress");
  assert.ok(!page.visible("testdata-close"), "a running build cannot be dismissed");
  assert.equal(page.$("build-testdata").disabled, true);
  assert.equal(page.$("build-testdata").textContent, "Building…");
  assert.deepEqual(page.steps(), [
    "✓Goldens and scenarios · done6 goldens, 2 scenarios",
    "–Context goldens · skippedNo help pages found",
    "◔Tool tests · working…",
    "○Security probes · waiting",
  ]);
  assert.equal(page.$("testdata-metrics").children.length, 0, "the summary waits for the end");

  page.backend.prepareAll[10] = [DONE];
  await page.settle(() => typeof page.storage.metricsChangedAt === "number");
  assert.equal(page.text("testdata-status"), "Test data built for Files; 1 step failed (see below).");
  assert.ok(page.steps().includes("!Security probes · failedJudge rate limit"));
  assert.ok(page.steps().includes("✓Tool tests · done8 tool tests <b>written</b>"), "details are text");
  const summary = page.$("testdata-metrics");
  assert.match(summary.textContent, /^31 metrics have test cases; these still have none:/);
  assert.deepEqual(byTag(summary, "li").map((li) => li.textContent), [
    "Contextual Recall: No golden answer with Context: the server has no help pages.Add one",
    "Turn Faithfulness: Needs retrieved context, which an MCP server does not return.",
  ], "a metric the catalog does not list gets no Add button");
  assert.ok(page.visible("testdata-close"));
  assert.equal(page.$("build-testdata").disabled, false);
  assert.equal(page.$("build-testdata").textContent, "Build all test data");
  await page.idle();
  assert.ok(page.count("GET", "/api/goldens") > goldenLoads, "goldens reload after the build");
  assert.ok(page.count("GET", "/api/conversations") > 0);

  // "Add one" opens that metric's Add form under Metrics to run.
  await byClass(summary, "testdata-add")[0].fire("click");
  await page.settle(() => page.document.activeElement && page.document.activeElement.textContent === "Add");
  assert.deepEqual({ ...page.state().openCases }, { key: "contextual_recall", mode: "add" });
  assert.equal(page.$("threshold-details").open, true);
  assert.equal(page.document.activeElement.textContent, "Add");
});

test("a website chatbot saves as before; No hides the question and opens the golden form", async (t) => {
  const page = await loadPanel();
  t.after(page.close);
  page.type({ "target-name": "Shop bot", "target-method": "POST", "target-url": "https://bot.example/chat",
    "target-body": '{"q": "{{message}}"}' });
  await page.submit("target-form");
  await page.idle();
  const created = page.last("POST", "/api/targets");
  assert.equal(created.body.type, "http");
  assert.equal(created.body.config.url, "https://bot.example/chat");
  assert.equal(page.text("target-form-error"), "");
  assert.equal(page.text("target-theme"), "Golden set: shop_bot");
  assert.ok(page.visible("testdata-question"));

  await page.click("testdata-no");
  assert.ok(!page.visible("testdata"));
  assert.equal(page.$("add-golden").open, true);
  assert.equal(page.document.activeElement, page.$("golden-question"));
  assert.equal(page.count("POST", "/api/targets/10/prepare-all"), 0, "No spends no judge tokens");

  // The question does not come back for this chatbot.
  await page.$("target-select").fire("change");
  await page.idle();
  assert.ok(!page.visible("testdata"));
});

test("editing an MCP server fills its form; *** keeps a saved secret and hidden settings stay", async (t) => {
  const page = await loadPanel({ targets: [MCP_FILES] });
  t.after(page.close);
  assert.equal(page.$("target-select").children[0].textContent, "Files (MCP)");
  assert.ok(!page.visible("testdata"), "a chatbot saved earlier is not asked again");

  await page.click("edit-target");
  assert.ok(page.visible("mcp-form"));
  assert.ok(!page.visible("target-form"));
  assert.ok(!page.visible("target-kind"), "a saved target keeps its kind");
  assert.equal(page.text("target-form-summary"), "Edit MCP server");
  assert.equal(page.$("mcp-name").value, "Files");
  assert.equal(page.$("mcp-command").value, "npx");
  assert.equal(page.$("mcp-args").value, "-y\n@mcp/files");
  assert.equal(page.$("mcp-env").value, "API_KEY=***\nREGION=***", "saved values show as ***");
  assert.ok(page.visible("mcp-env-hint"));
  assert.equal(page.$("mcp-persona").value, "File helper");
  assert.equal(page.$("mcp-agent-model").value, "m-1");
  assert.equal(page.text("mcp-submit"), "Save changes");
  assert.ok(page.visible("mcp-cancel"));

  // Test while editing: the backend may use the saved environment.
  await page.click("mcp-test");
  const listed = page.last("POST", "/api/targets/mcp/tools").body;
  assert.equal(listed.target_id, 3);
  assert.deepEqual(listed.config.env, { API_KEY: "***", REGION: "***" });

  // *** keeps API_KEY, the deleted line drops REGION, a new line adds MODE.
  page.type({ "mcp-env": "API_KEY=***\nMODE=fast", "mcp-persona": "Docs helper" });
  await page.submit("mcp-form");
  await page.idle();
  assert.equal(page.text("mcp-form-error"), "");
  assert.deepEqual(page.last("PUT", "/api/targets/3").body, { name: "Files", type: "mcp", config: {
    transport: "stdio", command: "npx", args: ["-y", "@mcp/files"], url: "", headers: {}, persona: "Docs helper",
    agent_model: "m-1", theme: "files", help_url: "https://files.example/help", probe_set: "target_3",
    env: { API_KEY: "***", MODE: "fast" },
  } });
  assert.deepEqual(page.backend.targets[0].config.env, { API_KEY: "real-key", MODE: "fast" });
  assert.equal(page.count("POST", "/api/targets/3/prepare-goldens"), 0, "an edit asks nothing");
  assert.ok(!page.visible("testdata"));
  assert.ok(page.visible("target-kind"), "the form is back to Add");
  assert.equal(page.text("target-form-summary"), "Add a chatbot or MCP server");
});

test("choosing MCP server drops a pasted cURL's update of a saved chatbot", async (t) => {
  const page = await loadPanel({ targets: [SHOP_BOT] });
  t.after(page.close);
  vm.runInContext("offerUpdate({ existing: { id: 4 } })", page.context);
  assert.equal(page.state().editingTarget, 4);
  assert.ok(page.visible("target-existing"));
  await page.choose("kind-mcp");
  assert.equal(page.state().editingTarget, null);
  assert.ok(!page.visible("target-existing"));
  assert.equal(page.text("target-submit"), "Save chatbot");
});

test("a build running when the panel loads is followed; Build all test data runs it again, and a stop is shown", async (t) => {
  const page = await loadPanel({ targets: [SHOP_BOT], prepareAll: { 4: [RUNNING] } });
  t.after(page.close);
  await page.settle(() => page.steps().length === 4);
  assert.ok(page.visible("testdata"));
  assert.ok(!page.visible("testdata-question"), "no question for a chatbot saved earlier");
  assert.equal(page.text("build-testdata"), "Building…");
  assert.equal(page.$("build-testdata").disabled, true);

  page.backend.prepareAll[4] = [DONE];
  await page.settle(() => page.$("build-testdata").disabled === false);
  assert.match(page.text("testdata-status"), /^Test data built for Shop bot/);
  await page.click("testdata-close");
  assert.ok(!page.visible("testdata"));
  assert.equal(page.count("POST", "/api/targets/4/prepare-all"), 0, "following a build starts none");

  page.backend.prepareAll[4] = [RUNNING, { ...DONE, steps: DONE.steps.slice(0, 3) }];
  await page.click("build-testdata");
  await page.settle(() => /^Test data built/.test(page.text("testdata-status")));
  assert.deepEqual(page.last("POST", "/api/targets/4/prepare-all").body, { generate: true });
  assert.equal(page.text("testdata-status"), "Test data built for Shop bot.");
  assert.match(page.$("testdata-status").className, /status-pass/);

  page.backend.prepareAll[4] = [{ status: "error", error: "No judge is configured.", steps: [], metrics: null }];
  await page.click("build-testdata");
  await page.settle(() => /^The build stopped/.test(page.text("testdata-status")));
  assert.equal(page.text("testdata-status"), "The build stopped: No judge is configured.");
  assert.match(page.$("testdata-status").className, /status-error/);
  assert.equal(page.$("build-testdata").disabled, false);
});

test("without a judge key the build waits: Yes and Build all test data are off and the question says why", async (t) => {
  const page = await loadPanel({ judgeUp: false });
  t.after(page.close);
  await page.choose("kind-mcp");
  page.type({ "mcp-name": "Files", "mcp-command": "npx" });
  await page.submit("mcp-form");
  await page.idle();
  assert.ok(page.visible("testdata-question"));
  assert.ok(page.visible("testdata-needs-judge"));
  assert.equal(page.$("testdata-yes").disabled, true);
  assert.equal(page.$("build-testdata").disabled, true);
  assert.match(page.$("build-testdata").title, /Judge not configured/);
});

test("a backend without the build says to restart it", async (t) => {
  const page = await loadPanel({ targets: [SHOP_BOT], noPrepareAll: true });
  t.after(page.close);
  assert.ok(!page.visible("testdata"));
  await page.click("build-testdata");
  await page.settle(() => /restart it/.test(page.text("testdata-status")));
  assert.match(page.text("testdata-status"), /^This backend cannot build test data yet: restart it \(run-backend\.bat\)/);
  assert.match(page.$("testdata-status").className, /status-error/);
  assert.ok(page.visible("testdata-close"));
  assert.equal(page.$("build-testdata").disabled, false);
});

test("a saved chatbot's own probe set is offered and shown selected; adding a new one drops it", async (t) => {
  const bot = { ...SHOP_BOT, config: { ...SHOP_BOT.config, probe_set: "target_4" } };
  const page = await loadPanel({ targets: [bot] });
  t.after(page.close);
  await page.click("edit-target");
  const select = page.$("target-probe-set");
  const own = select.options.find((o) => o.getAttribute("value") === "target_4");
  assert.ok(own, "the chatbot's own set is an option");
  assert.equal(own.textContent, "This chatbot's own (built with AI)");
  assert.equal(select.value, "target_4");
  await page.click("target-cancel");
  assert.equal(select.options.some((o) => /^target_/.test(o.getAttribute("value"))), false);
});

test("starting a build without a judge key shows the backend's 503 message", async (t) => {
  const page = await loadPanel({ targets: [SHOP_BOT], noJudgeKey: true });
  t.after(page.close);
  await page.click("build-testdata");
  await page.settle(() => /Could not start/.test(page.text("testdata-status")));
  assert.equal(page.text("testdata-status"), "Could not start the build: No judge key: add it in Judge settings.");
  assert.equal(page.$("build-testdata").disabled, false);
});
