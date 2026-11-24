/* Deterministic protocol tests. The real iframe/stdio host smoke is separate. */
"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { webcrypto } = require("node:crypto");
const vm = require("node:vm");
const test = require("node:test");

const source = readFileSync(join(__dirname, "../ruminate/ui/transport.js"), "utf8");
const normalized = (value) => JSON.parse(JSON.stringify(value));
const pause = () => new Promise(setImmediate);

function harness(options = {}) {
  const listeners = new Map();
  const sent = [];
  const network = [];
  const styles = new Map();
  const root = {
    dataset: {}, clientWidth: 800,
    style: { setProperty: (key, value) => styles.set(key, value), removeProperty: (key) => styles.delete(key) },
  };
  const host = {
    protocolVersion: "2026-01-26", hostInfo: { name: "Test host", version: "1" },
    hostCapabilities: { serverTools: {}, message: { text: {} }, updateModelContext: { text: {}, structuredContent: {} }, openLinks: {}, ...options.capabilities },
    hostContext: { theme: "light", displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"], containerDimensions: { maxHeight: 600 } },
    ...options.host,
  };
  const window = {
    RUMINATE_EMBEDDED: true,
    openai: options.openai,
    addEventListener: (type, listener) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener: (type, listener) => listeners.get(type)?.delete(listener),
    dispatchEvent: (event) => { for (const listener of listeners.get(event.type) || []) listener(event); },
  };
  const parent = {
    postMessage(message, origin) {
      assert.equal(origin, "*");
      sent.push(normalized(message));
      if (!Object.hasOwn(message, "id") || !message.method) return;
      queueMicrotask(() => {
        if (options.respond) {
          const answer = options.respond(message, host);
          if (answer === undefined) return;
          reply(message.id, answer);
        } else if (message.method === "ui/initialize") reply(message.id, { result: host });
        else if (message.method === "tools/call") reply(message.id, { result: { structuredContent: { data: { ok: true } }, content: [] } });
        else if (message.method === "ui/request-display-mode") reply(message.id, { result: { mode: message.params.mode } });
        else reply(message.id, { result: {} });
      });
    },
  };
  window.parent = parent;
  const document = { documentElement: root, body: { getBoundingClientRect: () => ({ height: 720 }) } };
  function deliver(message, eventSource = parent) {
    window.dispatchEvent({ type: "message", source: eventSource, origin: "null", data: message });
  }
  function reply(id, answer) { deliver({ jsonrpc: "2.0", id, ...answer }); }
  vm.runInNewContext(source, {
    window, document, URL, URLSearchParams, Object, Number, Blob, TextEncoder, crypto: webcrypto,
    setTimeout: (callback, delay) => setTimeout(callback, delay === 120000 && options.rpcTimeout ? options.rpcTimeout : delay), clearTimeout, console,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    fetch: (...args) => { network.push(args); throw new Error("Embedded UI attempted HTTP"); },
  });
  return { transport: window.RuminateTransport, sent, network, deliver, reply, parent, root, styles };
}

test("MCP handshake, notifications, theme merge and size events use the stable schema", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  await h.transport.connect();
  const init = h.sent.find((entry) => entry.method === "ui/initialize");
  assert.equal(init.params.protocolVersion, "2026-01-26");
  assert.deepEqual(init.params.appCapabilities.availableDisplayModes, ["inline", "fullscreen"]);
  assert.equal(init.params.appInfo.name, "Ruminate");
  assert(h.sent.some((entry) => entry.method === "ui/notifications/initialized" && !Object.hasOwn(entry, "id")));
  h.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: { view: "research", topic_id: "top_1" } } });
  h.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: { sources: [] } } });
  assert.equal(h.transport.input.topic_id, "top_1");
  assert.deepEqual(normalized(h.transport.result), { structuredContent: { sources: [] } });
  h.deliver({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: "dark", styles: { variables: { "--color-background-primary": "#222", "--evil": "url(https://example.com)" } } } });
  assert.equal(h.root.dataset.hostTheme, "dark");
  assert.equal(h.transport.hostContext.displayMode, "inline");
  assert.equal(h.styles.get("--color-background-primary"), "#222");
  assert.equal(h.styles.has("--evil"), false);
  await new Promise((resolve) => setTimeout(resolve, 70));
  const size = h.sent.find((entry) => entry.method === "ui/notifications/size-changed");
  assert.deepEqual(size.params, { width: 800, height: 600 });
  assert.equal(h.network.length, 0);
});

test("UI paths map to bounded actions and preserve request idempotency", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  await h.transport.request("/api/sources/src_1?version=ver_1&start=20&limit=10");
  await h.transport.request("/api/reviews/rev_1/feedback", { action: "useful", request_id: "my-request" });
  const calls = h.sent.filter((entry) => entry.method === "tools/call");
  assert.deepEqual(calls[0].params, { name: "ui_read", arguments: { action: "source", params: { version: "ver_1", start: 20, limit: 10, source_id: "src_1" } } });
  assert.deepEqual(calls[1].params, { name: "ui_write", arguments: { action: "review_feedback", params: { action: "useful", request_id: "my-request", review_id: "rev_1" } } });
  await assert.rejects(h.transport.request("http://127.0.0.1:8765/api/state"), /不支持/);
  await assert.rejects(h.transport.request("/api/not-a-tool"), /不支持这项操作/);
  assert.equal(h.network.length, 0);
});

test("organization edits use MCP actions and the route owns the record ID", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  await h.transport.request("/api/sources/src_1/edit", { source_id: "src_2", title: "My label", topic_ids: [], request_id: "source-edit" });
  await h.transport.request("/api/topics/top_1/edit", { name: "Research", request_id: "topic-edit" });
  const calls = h.sent.filter(entry => entry.method === "tools/call");
  assert.deepEqual(calls[0].params, { name: "ui_write", arguments: { action: "update_source", params: { source_id: "src_1", title: "My label", topic_ids: [], request_id: "source-edit" } } });
  assert.deepEqual(calls[1].params, { name: "ui_write", arguments: { action: "update_topic", params: { topic_id: "top_1", name: "Research", request_id: "topic-edit" } } });
  assert.equal(h.network.length, 0);
});

test("a committed write with a lost reply reuses its ID, then a new intentional submit gets a new ID", async (t) => {
  const committed = new Map();
  let loseReply = true;
  const h = harness({
    rpcTimeout: 20,
    respond: (message, host) => {
      if (message.method === "ui/initialize") return { result: host };
      const { request_id: requestId } = message.params.arguments.params;
      if (!committed.has(requestId)) committed.set(requestId, { topic_id: `top_${committed.size + 1}` });
      if (loseReply) { loseReply = false; return undefined; }
      return { result: { structuredContent: { data: committed.get(requestId) }, content: [] } };
    },
  });
  t.after(() => h.transport.dispose());
  await assert.rejects(h.transport.request("/api/topics", { name: "研究", description: "原文" }), (error) => error.code === "host_timeout");
  const recovered = await h.transport.request("/api/topics", { description: "原文", name: "研究" });
  assert.equal(recovered.topic_id, "top_1");
  assert.equal(committed.size, 1, "retry must return the committed topic instead of creating a duplicate");
  const later = await h.transport.request("/api/topics", { name: "研究", description: "原文" });
  assert.equal(later.topic_id, "top_2");
  assert.equal(committed.size, 2, "an acknowledged operation must not absorb a later intentional submit");
  const ids = h.sent.filter((entry) => entry.method === "tools/call").map((entry) => entry.params.arguments.params.request_id);
  assert.equal(ids[0], ids[1]);
  assert.notEqual(ids[1], ids[2]);
  assert.equal(h.network.length, 0);
});

test("changed pending content receives a fresh ID and explicit IDs remain caller-owned", async (t) => {
  let loseReply = true;
  const h = harness({
    rpcTimeout: 20,
    respond: (message, host) => {
      if (message.method === "ui/initialize") return { result: host };
      if (loseReply) { loseReply = false; return undefined; }
      return { result: { structuredContent: { data: { ok: true } }, content: [] } };
    },
  });
  t.after(() => h.transport.dispose());
  await assert.rejects(h.transport.request("/api/claims", { statement: "第一版" }), (error) => error.code === "host_timeout");
  await h.transport.request("/api/claims", { statement: "修改后的内容" });
  await h.transport.request("/api/claims", { statement: "明确指定", request_id: "explicit-write" });
  await h.transport.request("/api/claims", { statement: "明确指定", request_id: "explicit-write" });
  const ids = h.sent.filter((entry) => entry.method === "tools/call").map((entry) => entry.params.arguments.params.request_id);
  assert.notEqual(ids[0], ids[1]);
  assert.deepEqual(ids.slice(2), ["explicit-write", "explicit-write"]);
});

test("matching id from another window is ignored and cannot replace tool data", async (t) => {
  const h = harness({ respond: (message, host) => message.method === "ui/initialize" ? { result: host } : undefined });
  t.after(() => h.transport.dispose());
  await h.transport.connect();
  let resolved = false;
  const promise = h.transport.request("/api/claims/clm_1").then((data) => { resolved = true; return data; });
  await pause();
  const call = h.sent.find((entry) => entry.method === "tools/call");
  h.deliver({ jsonrpc: "2.0", id: call.id, result: { structuredContent: { data: "spoofed" } } }, {});
  await pause();
  assert.equal(resolved, false);
  h.reply(call.id, { result: { structuredContent: { data: { claim_id: "clm_1" } }, content: [] } });
  assert.deepEqual(normalized(await promise), { claim_id: "clm_1" });
});

test("image blocks stay local as data images and preserve page metadata", async (t) => {
  const h = harness({ respond: (message, host) => message.method === "ui/initialize" ? { result: host } : {
    result: { structuredContent: { data: { page: 2, page_count: 5 } }, content: [{ type: "image", mimeType: "image/png", data: "aGVsbG8=" }] },
  } });
  t.after(() => h.transport.dispose());
  const result = await h.transport.pageImage("/api/sources/src_1/page?version=ver_1&page=2");
  assert.equal(result.url, "data:image/png;base64,aGVsbG8=");
  assert.equal(result.metadata.page_count, 5);
  assert.equal(h.sent.find((entry) => entry.method === "tools/call").params.arguments.action, "source_page");
  result.release();
  assert.equal(h.network.length, 0);
});

test("selected claim reaches model context before a user-message follow-up", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  const selected = { claim_id: "clm_1", evidence: [{ version_id: "ver_1", block_id: "blk_1", quote: "原文" }] };
  await h.transport.discuss("继续这个问题", selected);
  const context = h.sent.find((entry) => entry.method === "ui/update-model-context");
  const message = h.sent.find((entry) => entry.method === "ui/message");
  assert.deepEqual(context.params.structuredContent, selected);
  assert.deepEqual(message.params, { role: "user", content: [{ type: "text", text: "继续这个问题" }] });
  assert(h.sent.indexOf(context) < h.sent.indexOf(message));
  assert.equal(h.network.length, 0);
});

test("host export capability is required; absent download falls back without HTTP", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  assert.deepEqual(normalized(await h.transport.downloadMarkdown("draft.md", "# draft")), { supported: false });
  assert.equal(h.sent.some((entry) => entry.method === "ui/download-file"), false);
  const supported = harness({ capabilities: { downloadFile: {} } }); t.after(() => supported.transport.dispose());
  await supported.transport.downloadMarkdown("draft.md", "# draft");
  const download = supported.sent.find((entry) => entry.method === "ui/download-file");
  assert.deepEqual(download.params.contents, [{ type: "resource", resource: { uri: "ruminate-export:///draft.md", mimeType: "text/markdown", text: "# draft" } }]);
  assert.equal(h.network.length + supported.network.length, 0);
});

test("tool and protocol failures surface instead of silently using preview HTTP", async (t) => {
  const failed = harness({ respond: (message, host) => message.method === "ui/initialize" ? { result: host } : { result: { isError: true, content: [{ type: "text", text: "not allowed" }] } } });
  t.after(() => failed.transport.dispose());
  await assert.rejects(failed.transport.request("/api/state"), /not allowed/);
  const incompatible = harness({ host: { protocolVersion: "1900-01-01" } }); t.after(() => incompatible.transport.dispose());
  await assert.rejects(incompatible.transport.connect(), /协议版本不兼容/);
  assert.equal(failed.network.length + incompatible.network.length, 0);
});

test("standard external links and fullscreen use host methods", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  await h.transport.openLink("https://example.com/paper");
  assert.deepEqual(h.sent.find((entry) => entry.method === "ui/open-link").params, { url: "https://example.com/paper" });
  await assert.rejects(h.transport.openLink("javascript:alert(1)"), /网页链接/);
  assert.equal(await h.transport.requestDisplayMode("fullscreen"), "fullscreen");
  assert.equal(h.transport.hostContext.displayMode, "fullscreen");
  assert.equal(h.network.length, 0);
});

test("resource teardown acknowledges the host and rejects pending calls", async (t) => {
  const h = harness({ respond: (message, host) => message.method === "ui/initialize" ? { result: host } : undefined });
  t.after(() => h.transport.dispose());
  await h.transport.connect();
  const pending = h.transport.request("/api/state");
  const rejection = assert.rejects(pending, /工作区已关闭/);
  await pause();
  h.deliver({ jsonrpc: "2.0", id: "host-teardown", method: "ui/resource-teardown", params: {} });
  await rejection;
  assert.deepEqual(h.sent.find((entry) => entry.id === "host-teardown"), { jsonrpc: "2.0", id: "host-teardown", result: {} });
});

test("legacy OpenAI bridge is only used after unsupported standard handshake", async (t) => {
  const calls = [];
  const h = harness({
    openai: { callTool: async (name, args) => { calls.push({ name, args }); return { data: { ok: true } }; } },
    respond: () => ({ error: { code: -32601, message: "not implemented" } }),
  });
  t.after(() => h.transport.dispose());
  assert.deepEqual(normalized(await h.transport.request("/api/state")), { ok: true });
  assert.equal(h.transport.mode, "openai-compatibility");
  assert.equal(calls[0].name, "ui_read");
  assert.equal(h.network.length, 0);
});

test("management routes preserve scoped pagination and review filters", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  await h.transport.connect();
  await h.transport.request("/api/management?view=thoughts&status=pending&topic=top_1&query=AI&sort=oldest&limit=25&offset=25");
  const call = h.sent.find(entry => entry.method === "tools/call");
  assert.deepEqual(call.params, { name: "ui_read", arguments: { action: "management", params: { view: "thoughts", status: "pending", topic: "top_1", query: "AI", sort: "oldest", limit: 25, offset: 25 } } });
  assert.deepEqual(h.network, []);
});

test("reading context is bounded and never sends a chat; titled drafts require advertised extensions", async (t) => {
  const h = harness({ capabilities: { experimental: { "openai/modelContext": {}, "openai/message": {} } } });
  t.after(() => h.transport.dispose());
  const selected = { kind: "source", source_id: "src_1", version_id: "ver_1" };
  await h.transport.updateContext(selected, "正在读的论文");
  assert.equal(h.sent.some(item => item.method === "ui/message"), false);
  assert.equal(h.sent.at(-1).params.content[0]._meta["openai/title"], "正在读的论文");
  assert.equal(h.transport.canDraft, true);
  assert.deepEqual(normalized(await h.transport.discuss("有哪些反例？", selected)), { draft: true });
  const message = h.sent.find(item => item.method === "ui/message");
  assert.deepEqual(message.params._meta, { "openai/message": { target: "active", send: false } });
  const count = h.sent.filter(item => item.method === "ui/update-model-context").length;
  await assert.rejects(h.transport.updateContext({ text: "字".repeat(9000) }), /内容太长/);
  assert.equal(h.sent.filter(item => item.method === "ui/update-model-context").length, count);
});

test("context replacements stay ordered and recover after a failed update", async (t) => {
  const h = harness({ respond: (message, host) => message.method === "ui/initialize" ? { result: host } : undefined });
  t.after(() => h.transport.dispose());
  await h.transport.connect();
  const first = h.transport.updateContext({ id: "old" });
  const failure = assert.rejects(first, /未能关联到对话/);
  const second = h.transport.updateContext({ id: "new" });
  await pause();
  let calls = h.sent.filter(item => item.method === "ui/update-model-context");
  assert.equal(calls.length, 1);
  h.reply(calls[0].id, { result: { isError: true } });
  await failure; await pause();
  calls = h.sent.filter(item => item.method === "ui/update-model-context");
  assert.equal(calls.length, 2);
  assert.equal(calls[1].params.structuredContent.id, "new");
  h.reply(calls[1].id, { result: {} });
  await second;
});

test("hosts without context support get explicit discussion content only after a click", async (t) => {
  const h = harness({ capabilities: { updateModelContext: undefined } });
  t.after(() => h.transport.dispose());
  assert.deepEqual(normalized(await h.transport.updateContext({ id: "chosen" })), { supported: false });
  assert.equal(h.sent.some(item => item.method === "ui/message"), false);
  await h.transport.discuss("继续", { id: "chosen" });
  assert.equal(h.sent.some(item => item.method === "ui/update-model-context"), false);
  assert.match(h.sent.find(item => item.method === "ui/message").params.content[0].text, /chosen/);
});

test("thinking reads and notes use the bounded MCP adapter without network", async (t) => {
  const h = harness(); t.after(() => h.transport.dispose());
  await h.transport.request("/api/thinking?claim_id=clm_1&offset=20");
  await h.transport.request("/api/activity?timezone=Asia%2FShanghai&day=2026-11-17");
  await h.transport.request("/api/topic-suggestions?offset=25");
  await h.transport.request("/api/reflection-notes", { claim_id: "clm_1", text: "My thought", request_id: "note-1" });
  await h.transport.request("/api/topic-suggestions/decide", { suggestion_id: "tag_1", decision: "revert", request_id: "undo-1" });
  const calls = h.sent.filter(e => e.method === "tools/call").map(e => e.params.arguments);
  assert.deepEqual(calls.map(c => c.action), ["thinking", "activity", "topic_suggestions", "save_reflection_note", "decide_topic_suggestion"]);
  assert.equal(calls[0].params.claim_id, "clm_1");
  assert.equal(calls[0].params.offset, 20);
  assert.equal(calls[1].params.timezone, "Asia/Shanghai");
  assert.equal(calls[3].params.request_id, "note-1");
  assert.equal(h.network.length, 0);
});
