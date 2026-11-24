/* MCP Apps 2026-01-26 bridge. The preview adapter is used only outside an iframe.
 * Protocol source: modelcontextprotocol/ext-apps/src/spec.types.ts.
 * The embedded app never opens a localhost connection or reads host credentials.
 */
"use strict";

(() => {
  const PROTOCOL_VERSION = "2026-01-26";
  const embedded = window.RUMINATE_EMBEDDED === true || document.documentElement.dataset.ruminateMode === "mcp"
    || window.parent !== window || typeof window.openai?.callTool === "function";
  const pending = new Map();
  const unresolvedMutations = new Map();
  let sequence = 0;
  let connection;
  let previewToken = "";
  let connected = false;
  let legacy = false;
  let closed = false;
  let context = {};
  let capabilities = {};
  let toolInput = {};
  let toolResult;
  let resizeObserver;
  let resizeTimer;
  let previousSize = "";
  let contextQueue = Promise.resolve();

  function requestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function protocolError(message, code) {
    const error = new Error(message);
    if (code !== undefined) error.code = code;
    return error;
  }

  function emit(name, detail) {
    window.dispatchEvent(new CustomEvent(`ruminate:${name}`, { detail }));
  }

  function send(message) {
    // Opaque sandbox origins cannot be addressed by origin string. Only the
    // captured parent WindowProxy may send replies, checked in onMessage.
    window.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
  }

  function rpc(method, params, timeout = 120000) {
    if (closed) return Promise.reject(protocolError("这个工作区已关闭，请重新打开。"));
    const id = `ruminate:${++sequence}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(protocolError("连接超时，请重试。", "host_timeout"));
      }, timeout);
      pending.set(id, { resolve, reject, timer });
      send({ id, method, params });
    });
  }

  function notify(method, params = {}) {
    if (connected && !legacy && !closed) send({ method, params });
  }

  function onMessage(event) {
    if (event.source !== window.parent || closed) return;
    const message = event.data;
    if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") return;
    if (Object.hasOwn(message, "id") && !message.method) {
      const request = pending.get(message.id);
      if (!request) return;
      const hasResult = Object.hasOwn(message, "result");
      const hasError = Object.hasOwn(message, "error");
      if (hasResult === hasError) return;
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (hasError) request.reject(protocolError(message.error?.message || "操作未完成，请重试。", message.error?.code));
      else request.resolve(message.result);
      return;
    }
    if (typeof message.method !== "string") return;
    if (Object.hasOwn(message, "id")) {
      if (message.method === "ping") send({ id: message.id, result: {} });
      else if (message.method === "ui/resource-teardown") {
        send({ id: message.id, result: {} });
        dispose();
      } else send({ id: message.id, error: { code: -32601, message: "Method not supported" } });
      return;
    }
    if (!connected) return;
    const data = message.params || {};
    switch (message.method) {
      case "ui/notifications/tool-input":
        toolInput = data.arguments && typeof data.arguments === "object" ? data.arguments : {};
        emit("tool-input", toolInput);
        break;
      case "ui/notifications/tool-result":
        toolResult = data;
        emit("tool-result", data);
        break;
      case "ui/notifications/tool-cancelled":
        emit("tool-cancelled", { reason: data.reason || "这次操作已取消。" });
        break;
      case "ui/notifications/host-context-changed":
        applyContext(data);
        break;
    }
  }

  function applyContext(update) {
    if (!update || typeof update !== "object") return;
    context = {
      ...context, ...update,
      styles: {
        ...context.styles, ...update.styles,
        variables: { ...context.styles?.variables, ...update.styles?.variables },
      },
    };
    const root = document.documentElement;
    root.dataset.hostTheme = context.theme === "dark" ? "dark" : "light";
    root.dataset.displayMode = ["inline", "fullscreen", "pip"].includes(context.displayMode)
      ? context.displayMode : "inline";
    for (const [key, value] of Object.entries(context.styles?.variables || {})) {
      if (!/^--(?:color|font|border|shadow)-[a-z0-9-]+$/.test(key)) continue;
      if (value == null) root.style.removeProperty(key);
      else if (typeof value === "string" && value.length <= 400 && !/[{};]|url\s*\(/i.test(value)) root.style.setProperty(key, value);
    }
    const dimensions = context.containerDimensions || {};
    const height = dimensions.height ?? dimensions.maxHeight;
    if (typeof height === "number" && Number.isFinite(height) && height > 0) {
      root.style.setProperty("--ruminate-host-height", `${Math.max(240, Math.min(height, 1800))}px`);
    } else root.style.removeProperty("--ruminate-host-height");
    emit("host-context", context);
    if (Object.hasOwn(update, "openai/modelContext")) emit("model-context", update["openai/modelContext"]);
    scheduleSize();
  }

  function scheduleSize() {
    if (!connected || !embedded || legacy || closed) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const dimensions = context.containerDimensions || {};
      const measured = Math.ceil(document.body.getBoundingClientRect().height);
      const height = Math.max(1, Math.min(measured, dimensions.height || dimensions.maxHeight || 1800));
      const width = Math.max(1, Math.ceil(document.documentElement.clientWidth));
      const size = `${width}:${height}`;
      if (size === previousSize) return;
      previousSize = size;
      notify("ui/notifications/size-changed", { width, height });
    }, 50);
  }

  async function connect() {
    if (connection) return connection;
    connection = (async () => {
      if (!embedded) {
        const session = await previewRequest("/api/session");
        previewToken = session.token;
        connected = true;
        return { mode: "preview" };
      }
      document.documentElement.dataset.ruminateEmbedded = "true";
      window.addEventListener("message", onMessage);
      try {
        const result = await rpc("ui/initialize", {
          appInfo: { name: "Ruminate", version: "0.7.0" },
          appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
          protocolVersion: PROTOCOL_VERSION,
        }, 10000);
        if (result?.protocolVersion !== PROTOCOL_VERSION) throw protocolError("当前客户端的界面协议版本不兼容，请更新客户端。", "unsupported_protocol");
        if (!result.hostCapabilities || typeof result.hostCapabilities !== "object") throw protocolError("连接信息不完整，请重新打开工作台。", "invalid_initialize");
        capabilities = result.hostCapabilities;
        connected = true;
        applyContext(result.hostContext || {});
        notify("ui/notifications/initialized");
      } catch (error) {
        // Compatibility is feature-detected, never a substitute localhost iframe.
        if (!["host_timeout", -32601].includes(error.code) || typeof window.openai?.callTool !== "function") throw error;
        legacy = true;
        connected = true;
        capabilities = {
          serverTools: {},
          ...(window.openai.sendFollowUpMessage ? { message: { text: {} } } : {}),
          ...(window.openai.openExternal ? { openLinks: {} } : {}),
        };
        toolInput = window.openai.toolInput || {};
        toolResult = window.openai.toolOutput;
        applyContext({ theme: window.openai.theme, displayMode: window.openai.displayMode });
      }
      if (typeof ResizeObserver === "function") {
        resizeObserver = new ResizeObserver(scheduleSize);
        resizeObserver.observe(document.body);
      }
      window.addEventListener("resize", scheduleSize);
      scheduleSize();
      return { mode: legacy ? "openai-compatibility" : "mcp", capabilities, hostContext: context };
    })();
    return connection;
  }

  function checkedPath(path) {
    if (typeof path !== "string" || !path.startsWith("/api/") || path.includes("\\")) throw protocolError("不支持这个资料操作。");
    const url = new URL(path, "https://ruminate.invalid");
    if (url.origin !== "https://ruminate.invalid") throw protocolError("不支持这个资料操作。");
    const query = Object.fromEntries(url.searchParams);
    for (const name of ["limit", "offset", "start", "page", "revision"]) {
      if (query[name] !== undefined) {
        if (!/^\d+$/.test(query[name])) throw protocolError("请检查页码或版本。");
        query[name] = Number(query[name]);
      }
    }
    return { path: url.pathname, query };
  }

  function route(path, body) {
    const { path: pathname, query } = checkedPath(path);
    let match;
    if (body !== undefined) {
      const actions = {
        "/api/reflection-notes": "save_reflection_note", "/api/topic-suggestions/decide": "decide_topic_suggestion",
        "/api/topics": "create_topic", "/api/ingest/url": "capture_url",
        "/api/ingest/file": "upload_file", "/api/claims": "record_claim",
        "/api/reviews/generate": "generate_reviews", "/api/reviews/present": "present_reviews",
        "/api/projects": "create_project",
      };
      if (actions[pathname]) return { name: "ui_write", arguments: { action: actions[pathname], params: body } };
      if ((match = pathname.match(/^\/api\/reviews\/([a-z0-9_]+)\/feedback$/))) return { name: "ui_write", arguments: { action: "review_feedback", params: { ...body, review_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/projects\/([a-z0-9_]+)\/draft$/))) return { name: "ui_write", arguments: { action: "create_draft", params: { ...body, project_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/sources\/([a-z0-9_]+)\/edit$/))) return { name: "ui_write", arguments: { action: "update_source", params: { ...body, source_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/topics\/([a-z0-9_-]+)\/edit$/))) return { name: "ui_write", arguments: { action: "update_topic", params: { ...body, topic_id: match[1] } } };
    } else {
      const actions = { "/api/thinking": "thinking", "/api/activity": "activity", "/api/topic-suggestions": "topic_suggestions", "/api/management": "management", "/api/state": "state", "/api/topics": "topics", "/api/sources": "sources", "/api/reviews": "reviews", "/api/projects": "projects" };
      if (actions[pathname]) return { name: "ui_read", arguments: { action: actions[pathname], params: query } };
      if ((match = pathname.match(/^\/api\/sources\/([a-z0-9_]+)(?:\/(page|observations))?$/))) {
        return { name: "ui_read", arguments: { action: { page: "source_page", observations: "observations" }[match[2]] || "source", params: { ...query, source_id: match[1] } } };
      }
      if ((match = pathname.match(/^\/api\/topics\/([a-z0-9_-]+)\/context$/))) return { name: "ui_read", arguments: { action: "topic_context", params: { ...query, topic_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/claims\/([a-z0-9_]+)$/))) return { name: "ui_read", arguments: { action: "claim", params: { claim_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/beliefs\/([a-z0-9_]+)$/))) return { name: "ui_read", arguments: { action: "belief", params: { ...query, belief_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/projects\/([a-z0-9_]+)(?:\/(revision|impact))?$/))) return { name: "ui_read", arguments: { action: match[2] || "project", params: { ...query, project_id: match[1] } } };
      if ((match = pathname.match(/^\/api\/jobs\/([a-z0-9_]+)$/))) return { name: "ui_read", arguments: { action: "job", params: { job_id: match[1] } } };
    }
    throw protocolError("当前版本不支持这项操作。", "unsupported_action");
  }

  function unwrap(result) {
    if (result?.isError) {
      const text = result.content?.find((item) => item.type === "text")?.text;
      let error = result.structuredContent?.error;
      if (!error && text) {
        try { error = JSON.parse(text); } catch { /* Text-only MCP error. */ }
      }
      const labels = {
        no_claims: "先在这个主题下记下几个观点，再生成提纲。",
        invalid_span: "选一段与原文一致的引用，最多 1,000 字。",
        topic_not_found: "没有找到这个研究主题。",
        source_not_found: "没有找到这份资料。",
        claim_not_found: "没有找到这条想法。",
        invalid_page: "这个页码不在资料中。",
        network_denied: "当前资料库关闭了联网导入。",
        network_disabled: "当前资料库关闭了联网导入。",
        idempotency_conflict: "这次提交的内容有变化，请重新提交。",
      };
      throw protocolError(labels[error?.code] || error?.message || text?.slice(0, 1000) || "操作未完成，请重试。", error?.code || "tool_error");
    }
    if (result?.structuredContent && Object.hasOwn(result.structuredContent, "data")) return result.structuredContent.data;
    if (result?.structuredContent) return result.structuredContent;
    const text = result?.content?.find((item) => item.type === "text")?.text;
    if (text) {
      try { const data = JSON.parse(text); return Object.hasOwn(data, "data") ? data.data : data; }
      catch { throw protocolError("返回内容格式有误，请重试。", "invalid_tool_result"); }
    }
    // Some legacy OpenAI bridges already unwrap structuredContent.
    if (legacy && result && typeof result === "object") return Object.hasOwn(result, "data") ? result.data : result;
    throw protocolError("未收到有效内容，请重试。", "invalid_tool_result");
  }

  async function toolCall(call) {
    await connect();
    if (!capabilities.serverTools) throw protocolError("当前客户端未连接资料工具，请重新打开工作台。", "tools_unavailable");
    if (legacy) return window.openai.callTool(call.name, call.arguments);
    return rpc("tools/call", call);
  }

  async function previewRequest(path, body) {
    if (embedded) throw protocolError("请通过工作台的资料工具访问内容。", "preview_unavailable");
    checkedPath(path);
    const options = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Ruminate-Token"] = previewToken;
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw protocolError(result.error?.message || "这次没有完成，请再试一次。", result.error?.code);
    return result;
  }

  async function request(path, body) {
    await connect();
    let mutationKey;
    let mutation;
    if (body !== undefined && !body.request_id) {
      // Retain only a digest and ID: uploads can contain many megabytes of base64.
      // A lost host reply must not turn the next submit into a second mutation.
      const canonical = JSON.stringify([path, body], (_key, value) => value && typeof value === "object" && !Array.isArray(value)
        ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, value[key]])) : value);
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical));
      mutationKey = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
      mutation = unresolvedMutations.get(mutationKey);
      if (!mutation) {
        // Never silently evict an unresolved ID: doing so could duplicate a write.
        if (unresolvedMutations.size >= 128) throw protocolError("还有较多未完成的操作，请先重试之前的提交。", "too_many_pending_writes");
        mutation = { requestId: requestId() };
        unresolvedMutations.set(mutationKey, mutation);
      }
      body = { ...body, request_id: mutation.requestId };
    }
    const result = !embedded ? await previewRequest(path, body) : unwrap(await toolCall(route(path, body)));
    // Only an acknowledged success completes this operation. A later intentional
    // submission of identical content gets a new ID; failed/timeout calls keep it.
    if (mutationKey && unresolvedMutations.get(mutationKey) === mutation) unresolvedMutations.delete(mutationKey);
    return result;
  }

  async function pageImage(path) {
    await connect();
    if (!embedded) {
      checkedPath(path);
      const response = await fetch(path, { credentials: "same-origin" });
      if (!response.ok) {
        const data = await response.json();
        throw protocolError(data.error?.message || "没能打开这一页。", data.error?.code);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      return { url, metadata: { page_count: Number(response.headers.get("X-Ruminate-Page-Count")) || null }, release: () => URL.revokeObjectURL(url) };
    }
    const result = await toolCall(route(path));
    const metadata = unwrap(result);
    const image = result.content?.find((item) => item.type === "image" && item.mimeType === "image/png");
    if (!image || typeof image.data !== "string" || image.data.length > 12 * 1024 * 1024 || !/^[A-Za-z0-9+/=]+$/.test(image.data)) throw protocolError("资料工具没有返回有效的页面图像。", "invalid_image");
    return { url: `data:image/png;base64,${image.data}`, metadata, release: () => {} };
  }

  async function openLink(url) {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) throw protocolError("只能打开网页链接。");
    await connect();
    if (!embedded) { window.open(parsed.href, "_blank", "noopener,noreferrer"); return; }
    if (!capabilities.openLinks) throw protocolError("请复制原文地址，在浏览器中打开。", "open_link_unavailable");
    if (legacy) return window.openai.openExternal({ href: parsed.href });
    const result = await rpc("ui/open-link", { url: parsed.href });
    if (result?.isError) throw protocolError("链接打开失败，请重试。");
  }

  async function downloadMarkdown(filename, markdown) {
    await connect();
    const safeName = filename.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").slice(0, 160) || "ruminate-draft.md";
    if (embedded) {
      if (!capabilities.downloadFile || legacy) return { supported: false };
      const result = await rpc("ui/download-file", {
        contents: [{ type: "resource", resource: { uri: `ruminate-export:///${encodeURIComponent(safeName)}`, mimeType: "text/markdown", text: markdown } }],
      });
      if (result?.isError) throw protocolError("这次没有下载文件，可以稍后重试。");
      return { supported: true, method: "host" };
    }
    const url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = safeName;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return { supported: true, method: "browser" };
  }

  function updateContext(selection, title = "Ruminate 当前内容") {
    // Serialize replacements so a slow older selection cannot become the last attachment.
    const update = contextQueue.catch(() => {}).then(async () => {
      await connect();
      if (!embedded || legacy || !(capabilities.updateModelContext?.text || capabilities.updateModelContext?.structuredContent)) return { supported: false };
      const serialized = JSON.stringify(selection);
      if (new TextEncoder().encode(serialized).length > 24576) throw protocolError("选中的内容太长，请缩小阅读范围。");
      const params = {};
      if (capabilities.updateModelContext.text) {
        const block = { type: "text", text: `Ruminate 当前阅读内容：\n${serialized}` };
        if (capabilities.experimental?.["openai/modelContext"]) block._meta = { "openai/title": title.slice(0, 120), "openai/group": { title: "Ruminate" } };
        params.content = [block];
      }
      if (capabilities.updateModelContext.structuredContent) params.structuredContent = selection;
      const result = await rpc("ui/update-model-context", params);
      if (result?.isError) throw protocolError("当前内容未能关联到对话，请重试。");
      return { supported: true };
    });
    contextQueue = update;
    return update;
  }

  async function discuss(text, selection, title) {
    await connect();
    if (!embedded || !capabilities.message?.text) throw protocolError("当前客户端不支持从工作台发起对话。", "message_unavailable");
    if (legacy) { await window.openai.sendFollowUpMessage({ prompt: `${text}\n\n${JSON.stringify(selection)}` }); return { draft: false }; }
    const contextResult = await updateContext(selection, title);
    const draft = !!capabilities.experimental?.["openai/message"];
    const params = { role: "user", content: [{ type: "text", text: contextResult.supported ? text : `${text}\n\n选中的研究内容：\n${JSON.stringify(selection)}` }] };
    if (draft) params._meta = { "openai/message": { target: "active", send: false } };
    const result = await rpc("ui/message", params);
    if (result?.isError) throw protocolError("消息没有送到对话，请再试一次。");
    return { draft };
  }

  async function requestDisplayMode(mode) {
    await connect();
    if (!embedded || legacy || !context.availableDisplayModes?.includes(mode)) return context.displayMode || "inline";
    const result = await rpc("ui/request-display-mode", { mode });
    applyContext({ displayMode: result.mode });
    return result.mode;
  }

  function dispose() {
    if (closed) return;
    closed = true;
    clearTimeout(resizeTimer);
    resizeObserver?.disconnect();
    window.removeEventListener("message", onMessage);
    window.removeEventListener("resize", scheduleSize);
    for (const item of pending.values()) {
      clearTimeout(item.timer);
      item.reject(protocolError("这个工作区已关闭。", "closed"));
    }
    pending.clear();
    unresolvedMutations.clear();
    emit("teardown", {});
  }

  window.RuminateTransport = Object.freeze({
    embedded, connect, request, pageImage, openLink, downloadMarkdown, discuss, updateContext,
    requestDisplayMode, requestId, dispose,
    get canUpdateContext() { return embedded && !legacy && !!(capabilities.updateModelContext?.text || capabilities.updateModelContext?.structuredContent); },
    get canDraft() { return embedded && !legacy && !!capabilities.experimental?.["openai/message"]; },
    get canDiscuss() { return embedded && !!capabilities.message?.text; },
    get canFullscreen() { return embedded && !legacy && !!context.availableDisplayModes?.includes("fullscreen"); },
    get hostContext() { return context; },
    get input() { return toolInput; },
    get result() { return toolResult; },
    get mode() { return embedded ? legacy ? "openai-compatibility" : "mcp" : "preview"; },
  });
})();
