"use strict";

const transport = window.RuminateTransport;
let appReady = false;
let initialViewApplied = false;
const icons = {
  library: '<path d="M4 5h4v15H4zM10 3h4v17h-4zM16 6l4-1 3 14-4 1z"/>',
  review: '<path d="M20 7a8 8 0 1 0 1 8M20 3v5h-5"/><path d="M12 8v5l3 2"/>',
  research: '<circle cx="5" cy="12" r="3"/><circle cx="18" cy="5" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8 10 7-4M8 14l7 4M18 8v8"/>',
  outputs: '<path d="M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  external: '<path d="M14 4h6v6m0-6-9 9M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  web: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c6 5 6 13 0 18M12 3c-6 5-6 13 0 18"/>',
  file: '<path d="M6 3h8l5 5v13H6zM14 3v6h5M9 13h7M9 17h5"/>',
  book: '<path d="M12 5c-4-2-7-2-10-1v15c3-1 6-1 10 1 4-2 7-2 10-1V4c-3-1-6-1-10 1zM12 5v15"/>',
  spark: '<path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3L12 3zM20 2v4M18 4h4"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  leaf: '<path d="M20 3C9 2 2 7 5 15c5 7 16 1 15-12ZM4 21 16 8"/>',
  quote: '<path d="M4 7h6v6H4V7Zm10 0h6v6h-6V7ZM10 13c0 4-2 5-5 5m15-5c0 4-2 5-5 5"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3M12 17h.01"/>',
  back: '<path d="M19 12H5m5-5-5 5 5 5"/>',
  chat: '<path d="M21 11a8 8 0 0 1-8 8H5l-3 3V7a5 5 0 0 1 5-5h6a8 8 0 0 1 8 9ZM7 8h9M7 12h6"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
};
const kindLabels = { source_assertion: "资料观点", model_inference: "推测", hypothesis: "假设", question: "问题" };
const projectLabels = { research_note: "研究笔记", literature_review: "文献综述", research_proposal: "研究提纲", product_spec: "产品方案", article: "文章" };
const outlines = {
  research_note: ["研究问题", "已有证据", "值得继续想的事", "下一步阅读"],
  literature_review: ["材料与范围", "主要观点", "证据分歧", "研究缺口"],
  research_proposal: ["研究问题", "已有依据", "待验证假设", "研究设计", "预期产出"],
  product_spec: ["目标问题", "已有依据", "需求假设", "验证计划", "验收条件"],
  article: ["文章主题", "论证依据", "争议与边界", "待补充材料"],
};
const state = { view: "thoughts", topics: [], sources: [], projects: [], topic: "", query: "", sort: "recent", status: "all", offset: 0, loadingId: 0, health: {}, dirty: false };
const detailStack = [];
const viewLabels = { thoughts: "想法", library: "资料", outputs: "输出" };
const viewKeys = { library: "sources", thoughts: "thoughts", outputs: "outputs" };
let toastTimer;
let currentSelection = null, selectionKey = "", suppressedSelection = "", selectionTimer, selectionRevision = 0;
function setSelection(data, title) {
  const next = { data, title };
  const key = JSON.stringify(next);
  currentSelection = next;
  if (key === selectionKey) return;
  selectionKey = key; const revision = ++selectionRevision;
  clearTimeout(selectionTimer);
  if (!transport.canUpdateContext || key === suppressedSelection) return;
  selectionTimer = setTimeout(async () => {
    try {
      const result = await transport.updateContext(data, title);
      if (revision === selectionRevision && result.supported) showContextHint("右下角对话已关联当前内容");
    } catch { if (revision === selectionRevision) showContextHint("可点“继续聊”把内容带入对话"); }
  }, 80);
}
function showContextHint(text) {
  document.querySelectorAll(".context-hint").forEach(node => { node.textContent = text; });
}
function contextHint() { const hint = el("p", "context-hint"); hint.setAttribute("aria-live", "polite"); return hint; }
window.addEventListener("ruminate:model-context", event => {
  if (event.detail === null) {
    suppressedSelection = selectionKey; ++selectionRevision; clearTimeout(selectionTimer);
    showContextHint("对话关联已移除；点“继续聊”可重新加入");
  }
});
function claimSelection(claim, reviewId) {
  const evidence = (claim.evidence || []).slice(0, 5).map(ref => ({
    evidence_id: ref.evidence_id, source_id: ref.source_id, version_id: ref.version_id,
    block_id: ref.block_id, title: ref.title, page: ref.page, locator: ref.locator,
    span: ref.span, quote: (ref.quote || "").slice(0, 1200), quote_truncated: (ref.quote || "").length > 1200, relation: ref.relation,
  }));
  return { source: "ruminate", kind: "claim", claim_id: claim.claim_id, topic_id: claim.topic_id,
    statement: claim.statement, claim_kind: claim.claim_kind, origin: claim.origin,
    evidence_status: claim.evidence_status, evidence_count: claim.evidence_count, evidence,
    evidence_truncated: claim.evidence_count > evidence.length, ...(reviewId ? { review_id: reviewId } : {}) };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function append(node, ...children) { children.flat().filter(Boolean).forEach((child) => node.append(child)); return node; }
function icon(name) {
  const wrap = el("span");
  // Icons are application-owned constants; source content is always inserted as text.
  wrap.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
  return wrap.firstElementChild;
}
function button(label, style = "secondary", name, handler) {
  const b = el("button", `button ${style}`);
  b.type = "button";
  append(b, name ? icon(name) : null, el("span", "", label));
  if (handler) b.addEventListener("click", handler);
  return b;
}
function badge(text, variant = "") { return el("span", `badge ${variant}`, text); }
function formatDate(value) {
  if (!value) return "刚刚收集";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value).slice(0, 10) : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(d);
}
function domain(value) { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.hostname.replace(/^www\./, "") : "本地文件"; } catch { return "本地文件"; } }
function safeURL(value) { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; } catch { return null; } }
function topicName(id) { return state.topics.find((t) => t.topic_id === id || t.slug === id)?.name || "研究主题"; }
function classifySource(source) {
  const mime = source.mime_type || source.metadata?.mime_type || "";
  if (mime === "application/pdf") return { type: "pdf", label: "PDF", full: "PDF 文档", icon: "file", style: "" };
  if (mime.includes("markdown") || mime === "text/plain") return { type: "note", label: "MD", full: "笔记", icon: "book", style: "note" };
  return { type: "web", label: "WEB", full: "网页", icon: "web", style: "web" };
}
function locationLabel(evidence) {
  if (evidence.page) return `第 ${evidence.page} 页`;
  if (evidence.locator?.line_start) return `第 ${evidence.locator.line_start} 行`;
  if (evidence.section && evidence.section !== evidence.title) return evidence.section;
  return "原文片段";
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  const region = document.querySelector("#toast-region");
  region.replaceChildren(el("div", `toast${error ? " error" : ""}`, message));
  toastTimer = setTimeout(() => region.replaceChildren(), 4300);
}
async function api(path, body) {
  return transport.request(path, body);
}
function errorMessage(error) { return error.message || (transport.embedded ? "连接资料库失败，请重试。" : "连接资料库失败，请检查本地服务。"); }
async function busy(b, work, label = "正在处理…") {
  if (b.disabled) return;
  const previous = [...b.childNodes];
  b.disabled = true;
  b.replaceChildren(el("span", "", label));
  try { return await work(); } catch (error) { toast(errorMessage(error), true); }
  finally { b.disabled = false; b.replaceChildren(...previous); }
}
function heading(title, description, eyebrow, action) {
  const head = el("section", "page-head");
  append(head, append(el("div"), el("div", "eyebrow", eyebrow), el("h1", "", title), el("p", "", description)));
  if (action) head.append(append(el("div", "header-actions"), action));
  return head;
}
function sectionTitle(title, count, trailing) {
  const h = el("h2", "", title);
  if (count !== undefined) h.append(el("span", "", String(count).padStart(2, "0")));
  return append(el("div", "section-title"), h, trailing);
}
function empty(title, description, name = "leaf", action) {
  return append(el("div", "empty-state"), append(el("div", "empty-icon"), icon(name)), el("h3", "", title), el("p", "", description), action);
}
function loading() { return append(el("div", "loading-page"), el("span", "spinner"), el("p", "", "正在整理…")); }
function topicSelect(value, all = false) {
  const select = el("select", "select");
  select.setAttribute("aria-label", "研究主题");
  if (all) { const option = el("option", "", "全部主题"); option.value = ""; select.append(option); }
  state.topics.forEach((topic) => { const option = el("option", "", topic.name); option.value = topic.topic_id; select.append(option); });
  select.value = value || (all ? "" : state.topics[0]?.topic_id || "");
  return select;
}
function params(values) { return new URLSearchParams(Object.entries(values).filter(([, v]) => v !== "" && v !== undefined && v !== null)).toString(); }

async function refreshState(initial) {
  const data = initial?.topics && initial?.sources ? initial : await api("/api/state");
  Object.assign(state, { topics: data.topics, sources: data.sources, projects: data.projects, health: data.health });
  renderNavigation();
}
function renderNavigation() {
  document.querySelector("#main-nav").replaceChildren(...Object.entries(viewLabels).map(([key, label]) => {
    const b = button(label, `nav-item${state.view === key ? " active" : ""}`, null, () => navigate(key));
    if (state.view === key) b.setAttribute("aria-current", "page");
    return b;
  }));
  const action = state.view === "library" ? ["添加资料", openCapture] : state.view === "thoughts" ? ["记下想法", openThought] : ["新建输出", openNewProject];
  document.querySelector("#primary-action").replaceChildren(button(action[0], "primary", "plus", () => action[1]()));
}
function clearDetails() {
  while (detailStack.length) {
    const entry = detailStack.pop();
    entry.node.dispatchEvent(new Event("close"));
  }
}
function listSignature(data) { return JSON.stringify([data.items, data.total, data.pending_count]); }
async function navigate(view, options = {}) {
  const aliases = { sources: "library", research: "thoughts", review: "thoughts" };
  if (view === "review") state.status = "pending";
  const next = aliases[view] || (viewLabels[view] ? view : "thoughts");
  if (next !== state.view) { state.query = ""; state.offset = 0; }
  state.view = next;
  clearDetails();
  const previous = currentSelection?.data;
  if (previous?.kind !== "list" || previous.view !== viewKeys[state.view] || previous.topic_id !== (state.topic || null) || previous.query !== state.query || previous.status !== state.status || previous.offset !== state.offset) {
    setSelection({ source: "ruminate", kind: "loading", view: viewKeys[state.view] }, `Ruminate · ${viewLabels[state.view]}`);
  }
  renderNavigation();
  if (!transport.embedded) history.replaceState(null, "", `#${state.view}`);
  const main = document.querySelector("#main");
  const generation = ++state.loadingId;
  const toolbar = el("div", "list-toolbar");
  const search = el("form", "search-box");
  const searchInput = input(state.view === "library" ? "搜索资料标题或原文" : state.view === "thoughts" ? "搜索想法" : "搜索输出标题或正文", false, "search");
  searchInput.value = state.query; searchInput.maxLength = 512; searchInput.setAttribute("aria-label", `搜索${viewLabels[state.view]}`);
  const searchButton = button("搜索", "search-submit", "search"); searchButton.type = "submit";
  append(search, searchButton, searchInput);
  let searchTimer;
  const runSearch = () => { clearTimeout(searchTimer); if (generation !== state.loadingId || detailStack.length) return; state.query = searchInput.value.trim(); state.offset = 0; navigate(state.view, { focusSearch: true }); };
  search.addEventListener("submit", e => { e.preventDefault(); runSearch(); });
  searchInput.addEventListener("input", e => { if (!e.isComposing) { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 350); } });
  const topic = topicSelect(state.topic, true); topic.setAttribute("aria-label", "筛选主题");
  const newTopic = el("option", "", "+ 新建主题"); newTopic.value = "__new__"; topic.append(newTopic);
  topic.addEventListener("change", () => { if (topic.value === "__new__") { topic.value = state.topic; openTopic(); } else { state.topic = topic.value; state.offset = 0; navigate(state.view); } });
  const sort = el("select", "select sort-select"); sort.setAttribute("aria-label", "排序");
  [["recent", "最近保存"], ["oldest", "最早保存"], ["title", "按名称"]].forEach(([value, label]) => { const o = el("option", "", label); o.value = value; sort.append(o); });
  sort.value = state.sort; sort.addEventListener("change", () => { state.sort = sort.value; state.offset = 0; navigate(state.view); });
  const refresh = button("刷新", "icon-button refresh", "review", () => navigate(state.view)); refresh.setAttribute("aria-label", "刷新列表"); refresh.title = "刷新列表";
  append(toolbar, search, topic, sort, refresh);
  const filters = el("div", "thought-filters");
  if (state.view === "thoughts") {
    [["all", "全部想法"], ["pending", "待回看"], ["later", "稍后"], ["processed", "已处理"]].forEach(([key, label]) => {
      const b = button(label, `filter-button${state.status === key ? " active" : ""}`, null, () => { state.status = key; state.offset = 0; navigate("thoughts"); });
      b.dataset.filter = key; b.setAttribute("aria-pressed", state.status === key ? "true" : "false"); filters.append(b);
    });
  }
  const list = el("section", "management-list"); list.setAttribute("aria-label", `${viewLabels[state.view]}列表`); list.setAttribute("aria-busy", "true"); list.append(loading());
  main.replaceChildren(toolbar, ...(state.view === "thoughts" ? [filters] : []), list);
  main.scrollTop = options.scroll || 0;
  if (options.focusSearch) { searchInput.focus(); }
  try {
    const result = await api(`/api/management?${params({ view: viewKeys[state.view], topic: state.topic, query: state.query, sort: state.sort, status: state.view === "thoughts" ? state.status : "all", offset: state.offset, limit: 25 })}`);
    if (generation !== state.loadingId || detailStack.length) return;
    if (!result.items.length && state.offset > 0) { state.offset = Math.max(0, state.offset - 25); return navigate(state.view, options); }
    state.signature = listSignature(result);
    setSelection({ source: "ruminate", kind: "list", view: viewKeys[state.view], topic_id: state.topic || null,
      query: state.query, status: state.status, total: result.total, offset: state.offset,
      visible_items: result.items.slice(0, 10).map(item => ({ id: item.id, title: item.title.slice(0, 220), row_kind: item.row_kind })) }, `Ruminate · ${viewLabels[state.view]}`);
    const pending = filters.querySelector('[data-filter="pending"]');
    if (pending) pending.append(el("span", "filter-count", result.pending_count));
    list.replaceChildren(); list.setAttribute("aria-busy", "false");
    const count = el("div", "list-summary", `${result.total} ${state.view === "thoughts" ? "条想法" : "份" + viewLabels[state.view]}${state.query ? " · 搜索结果" : ""}`); count.setAttribute("aria-live", "polite");
    list.append(count);
    if (result.items.length) {
      list.append(append(el("div", "list-columns"), el("span", "", state.view === "thoughts" ? "想法与依据" : "名称"), el("span", "", "主题"), el("span", "", state.view === "thoughts" && state.status === "later" ? "再次回看" : "保存时间"), el("span")));
      result.items.forEach(item => list.append(managementRow(item)));
    } else {
      const filtered = state.query || state.topic;
      list.append(empty(filtered ? "没有找到匹配的内容" : state.view === "thoughts" && state.status === "pending" ? "暂时没有待回看的想法" : `还没有${viewLabels[state.view]}`, filtered ? "试试其他关键词或主题。" : state.view === "thoughts" && state.status === "pending" ? "新建议和到期的问题会出现在这里。" : "从右上角添加第一条内容。", state.view === "thoughts" ? "spark" : "book"));
    }
    if (result.total > 25) {
      const previous = button("上一页", "secondary small", "back", () => { state.offset -= 25; navigate(state.view); }); previous.disabled = state.offset === 0;
      const next = button("下一页", "secondary small", "arrow", () => { state.offset = result.next_offset; navigate(state.view); }); next.disabled = result.next_offset === null;
      list.append(append(el("div", "list-pagination"), previous, el("span", "", `${state.offset + 1}–${Math.min(state.offset + 25, result.total)} / ${result.total}`), next));
    }
    state.dirty = false;
    if (options.focusId) list.querySelector(`[data-row-id="${CSS.escape(options.focusId)}"]`)?.focus({ preventScroll: true });
    main.scrollTop = options.scroll || 0;
  } catch (error) {
    if (generation === state.loadingId) { list.setAttribute("aria-busy", "false"); list.replaceChildren(empty("没能加载列表", errorMessage(error), "question", button("重试", "secondary", "review", () => navigate(state.view)))); }
  }
}
function thoughtParts(statement) {
  const parts = statement.split(/\n\s*\n/);
  const reason = parts.find(p => /^为什么值得回看[:：]/.test(p));
  const next = parts.find(p => /^下一步[:：]/.test(p));
  // Only split the automation's explicit format. Preserve free-form notes in full.
  return reason || next ? { title: parts[0], reason: reason?.replace(/^为什么值得回看[:：]\s*/, ""), next: next?.replace(/^下一步[:：]\s*/, ""), rest: parts.slice(1).filter(p => p !== reason && p !== next).join("\n\n") } : { title: statement };
}
function managementRow(item) {
  const row = el("button", "content-row"); row.type = "button"; row.dataset.rowId = item.id;
  const isThought = state.view === "thoughts", isSource = state.view === "library";
  const isBelief = item.row_kind === "belief";
  const parts = isThought && !isBelief ? thoughtParts(item.title) : { title: item.title };
  const sourceType = isSource ? classifySource(item) : null;
  const name = isBelief ? "quote" : isThought ? "question" : sourceType?.icon || "outputs";
  const mark = append(el("span", `row-symbol ${isThought ? "thought-symbol" : ""}`), icon(name));
  const subtitle = isBelief ? "我的观点 · 已由你确认" : isSource ? `${domain(item.canonical_uri)} · ${sourceType.full}` : isThought ? `${kindLabels[item.claim_kind]} · ${item.origin === "host_model" ? "AI 提出" : item.origin === "human" ? "手动记录" : "本地整理"} · ${item.evidence_count} 处引用` : `${projectLabels[item.kind] || "研究输出"} · ${item.version ? "版本 " + item.version : "尚未生成"}`;
  const text = append(el("span", "row-copy"), el("span", "row-title", parts.title), el("span", "row-subtitle", subtitle));
  if (isThought && parts.reason) text.append(el("span", "row-reason", parts.reason));
  const topics = isSource ? item.topics.map(t => t.name) : [item.topic_name];
  const topic = el("span", "row-topic", topics.join(" · ") || "未归入主题");
  const dateValue = isThought && state.status === "later" ? item.due_at : item.saved_at;
  const date = el("time", "row-date", formatDate(dateValue)); date.dateTime = dateValue; date.title = new Date(dateValue).toLocaleString("zh-CN");
  append(row, append(el("span", "row-name"), mark, text), topic, date, icon("arrow"));
  row.addEventListener("click", () => isBelief ? openBelief(item) : isSource ? openSource(item.source_id) : isThought ? openClaim(item) : openProject(item.project_id));
  return row;
}
function detail(title, extra) {
  const main = document.querySelector("#main");
  const node = el("article", `detail-page ${extra}`); node.setAttribute("aria-label", title);
  const entry = { node, children: [...main.childNodes], scroll: main.scrollTop, focus: document.activeElement, focusId: document.activeElement?.dataset.rowId, selection: currentSelection };
  detailStack.push(entry);
  setSelection({ source: "ruminate", kind: "loading", title }, "Ruminate · 正在打开");
  const close = () => {
    if (detailStack.at(-1) !== entry) return;
    detailStack.pop(); node.dispatchEvent(new Event("close"));
    main.replaceChildren(...entry.children); main.scrollTop = entry.scroll;
    if (!detailStack.length && state.dirty) navigate(state.view, { scroll: entry.scroll, focusId: entry.focusId });
    else { if (entry.selection) setSelection(entry.selection.data, entry.selection.title); entry.focus?.focus?.({ preventScroll: true }); }
  };
  const back = button("返回", "ghost small", "back", close);
  const titleNode = el("h1", "detail-title", title); titleNode.tabIndex = -1;
  const body = el("div", "detail-body"); append(node, back, titleNode, body); main.replaceChildren(node); main.scrollTop = 0; titleNode.focus({ preventScroll: true });
  return { dialog: node, body, title: titleNode, close };
}
async function openBelief(item) {
  const pane = detail("正在打开观点…", "belief-reader"); pane.body.append(loading());
  try {
    const belief = await api(`/api/beliefs/${item.id}`);
    if (!pane.dialog.isConnected) return;
    pane.title.textContent = belief.text;
    setSelection({ source: "ruminate", kind: "belief", belief_id: item.id, text: belief.text, origin: belief.origin, status: belief.status, user_confirmed_at: belief.user_confirmed_at, provenance: belief.provenance }, belief.text);
    const confirmed = belief.status === "confirmed" && belief.origin === "human" && belief.user_confirmed_at;
    const meta = append(el("div", "reader-meta"), badge(confirmed ? "你已确认的观点" : "待确认观点", "green"), el("span", "", item.topic_name));
    if (confirmed) meta.append(el("span", "", `确认于 ${formatDate(belief.user_confirmed_at)}`));
    pane.body.replaceChildren(meta, append(el("section", "thought-section"), el("h2", "", "记录来源"), el("p", "", belief.provenance)));
    const links = append(el("section", "thought-section"), el("h2", "", `相关想法 · ${belief.related_count}`));
    const draw = rows => rows.forEach(claim => {
      const card = el("div", "belief-link");
      const relation = { supports: "支持这条观点", challenges: "提出不同看法", related: "相关想法" }[claim.relation] || "相关想法";
      card.append(el("span", "muted", relation));
      card.append(button(thoughtParts(claim.statement).title, "belief-claim-button", "arrow", () => openClaim({ ...claim, topic_name: item.topic_name, saved_at: claim.created_at })));
      if (claim.justification) card.append(el("p", "muted", claim.justification));
      links.append(card);
    });
    draw(belief.related_claims);
    if (belief.next_offset !== null) {
      let offset = belief.next_offset;
      const more = button("更多相关想法", "secondary small");
      more.addEventListener("click", () => busy(more, async () => {
        const next = await api(`/api/beliefs/${item.id}?${params({ offset })}`); draw(next.related_claims); offset = next.next_offset;
        if (offset === null) more.remove(); else links.append(more);
      })); links.append(more);
    }
    pane.body.append(links);
  } catch (error) { pane.body.replaceChildren(empty("没能打开观点", errorMessage(error), "quote")); }
}
async function openClaim(item) {
  const pane = detail("正在打开想法…", "thought-reader"); pane.body.append(loading());
  try {
    const claim = await api(`/api/claims/${item.claim_id}`);
    if (!pane.dialog.isConnected) return;
    const parts = thoughtParts(claim.statement); pane.title.textContent = parts.title;
    const origin = claim.origin === "host_model" ? "AI 提出" : claim.origin === "human" ? "手动记录" : "本地整理";
    const meta = append(el("div", "reader-meta"), badge(kindLabels[claim.claim_kind], "green"), el("span", "", origin), el("span", "", item.topic_name), el("span", "", formatDate(item.saved_at)));
    pane.body.replaceChildren(meta);
    if (parts.reason) pane.body.append(append(el("section", "thought-section"), el("h2", "", "为什么值得回看"), el("p", "", parts.reason)));
    if (parts.next) pane.body.append(append(el("section", "thought-section"), el("h2", "", "下一步"), el("p", "", parts.next)));
    if (parts.rest) pane.body.append(el("p", "thought-text", parts.rest));
    if (item.review_id && item.review_question && (!parts.reason || item.review_reason !== "evidence_gap") && item.review_question !== claim.statement && item.review_question !== parts.title) pane.body.append(append(el("section", "thought-section"), el("h2", "", "这次回看的问题"), el("p", "", item.review_question)));
    const evidence = append(el("section", "thought-section"), el("h2", "", `原文依据 · ${claim.evidence_count}`));
    (claim.evidence || []).forEach(ref => evidence.append(evidenceQuote(ref)));
    if (!claim.evidence_count) evidence.append(el("p", "muted", "这条想法还没有关联原文。"));
    if (claim.evidence_truncated) evidence.append(el("p", "muted", "当前显示前 20 处引用。"));
    pane.body.append(evidence);
    const actions = el("div", "thought-actions");
    if (item.review_id && ["queued", "postponed"].includes(item.review_status)) {
      const feedback = async (action, clicked) => {
        const controls = [...actions.querySelectorAll("button")]; controls.forEach(b => { b.disabled = true; });
        try { await api(`/api/reviews/${item.review_id}/feedback`, { action, postpone_days: 7 }); state.dirty = true; toast(action === "useful" ? "已记下这次反馈" : action === "postpone" ? "7 天后再回看" : "已略过这条建议"); pane.close(); }
        catch (error) { controls.forEach(b => { b.disabled = false; }); toast(errorMessage(error), true); clicked.focus(); }
      };
      [["有启发", "useful", "check"], ["稍后再看", "postpone", "clock"], ["略过", "dismiss", "close"]].forEach(([label, action, name]) => { const b = button(label, action === "useful" ? "primary" : "secondary", name); b.addEventListener("click", () => feedback(action, b)); actions.append(b); });
    } else if (item.review_status) actions.append(badge(item.review_status === "resolved" ? "已回看 · 有启发" : "已略过", "gray"));
    if (transport.canDiscuss) actions.append(discussionButton(claimSelection(claim, item.review_id), parts.title));
    pane.body.append(actions, contextHint());
    setSelection(claimSelection(claim, item.review_id), parts.title);
  } catch (error) { pane.body.replaceChildren(empty("没能打开想法", errorMessage(error), "question")); }
}

function evidenceQuote(ref) {
  const quote = el("blockquote", "evidence-quote");
  const relations = { supports: "支持", challenges: "不同观点", mentions: "相关背景", unclear: "关系待梳理" };
  if (relations[ref.relation]) quote.append(badge(relations[ref.relation], ref.relation === "challenges" ? "orange" : "gray"));
  quote.append(el("p", "", ref.quote || ""));
  const source = state.sources.find((s) => s.source_id === ref.source_id);
  const b = append(el("button"), icon("external"), document.createTextNode(`${ref.title || source?.title || "打开来源"} · ${locationLabel(ref)}`));
  b.addEventListener("click", () => openSource(ref.source_id, { version: ref.version_id, blockId: ref.block_id, span: ref.span || ref.evidence_span })); quote.append(b); return quote;
}

function discussionButton(selection, title) {
  const b = button(transport.canDraft ? "继续聊这条" : "在对话中继续", "soft small", "chat");
  b.addEventListener("click", () => busy(b, async () => {
    suppressedSelection = "";
    const text = `请结合 Ruminate 中“${title}”的原文依据，帮我找出值得追问的问题和可能的反例。`;
    const result = await transport.discuss(text, selection, title);
    if (transport.canUpdateContext) showContextHint("右下角对话已关联当前内容");
    toast(result.draft ? "问题已放入右下角对话，可修改后发送" : "已把想法和引用带回对话");
  }, "正在带入对话…"));
  return b;
}

async function openProject(id) {
  const modal = dialog("研究输出", "", "reader output-reader"); modal.body.append(loading());
  let project, loadSequence = 0;
  async function load(version) {
    const sequence = ++loadSequence;
    modal.body.replaceChildren(loading());
    try {
      const loadedProject = await api(`/api/projects/${id}`);
      if (sequence !== loadSequence || !modal.dialog.isConnected) return;
      project = loadedProject; modal.title.textContent = project.title;
      const actions = el("div", "document-actions");
      const versions = el("select", "select"); versions.setAttribute("aria-label", "输出版本");
      project.revisions.forEach((rev) => { const o = el("option", "", `版本 ${rev.version} · ${formatDate(rev.created_at)}`); o.value = rev.version; versions.append(o); });
      const draft = button(project.revisions.length ? "生成新版本" : "生成提纲", "primary small", "spark");
      draft.addEventListener("click", () => busy(draft, async () => { const revision = await api(`/api/projects/${id}/draft`, {}); state.dirty = true; toast("提纲已生成"); await load(revision.version); }, "正在整理…"));
      if (!project.revisions.length) { modal.body.replaceChildren(actions, empty("生成第一份提纲", project.goal || "根据主题中的想法和引用生成提纲。", "outputs", draft)); return; }
      const selected = version || project.revisions[0].version; versions.value = String(selected); versions.addEventListener("change", () => load(Number(versions.value)));
      const revision = await api(`/api/projects/${id}/revision?${params({ revision: selected })}`);
      if (sequence !== loadSequence || !modal.dialog.isConnected) return;
      const download = button("导出 Markdown", "secondary small", "download");
      download.addEventListener("click", () => busy(download, async () => {
        const filename = `${project.title}-v${revision.version}.md`;
        const result = await transport.downloadMarkdown(filename, revision.markdown);
        if (!result.supported) showMarkdownExport(filename, revision.markdown);
        else toast(result.method === "host" ? "已提交下载" : "已开始下载");
      }, "正在导出…"));
      append(actions, versions, download, draft);
      const snapshot = revision.snapshot;
      const cover = append(el("section", "document-cover"), el("div", "eyebrow", `${projectLabels[project.kind] || "研究输出"} / 版本 ${revision.version}`), el("h1", "", snapshot.project.title), el("p", "", snapshot.project.goal || "根据已保存的想法与引用整理。"));
      const claims = snapshot.claims || []; const count = claims.reduce((total, c) => total + c.evidence.length, 0);
      const meta = append(el("div", "document-meta"), badge("草稿", "orange"), el("span", "", `${claims.length} 条观点与问题`), el("span", "", `${count} 处引用`), el("span", "", formatDate(revision.created_at)));
      const outline = append(el("section", "document-section"), el("h2", "", "提纲")); const ol = el("ol", "document-outline"); (outlines[project.kind] || outlines.research_note).forEach((text) => ol.append(el("li", "", text))); outline.append(ol);
      const content = append(el("section", "document-section"), el("h2", "", "观点与证据"));
      let citation = 0;
      claims.forEach((claim) => {
        const item = el("article", "document-claim"); const paragraph = el("p");
        append(paragraph, badge(kindLabels[claim.claim_kind] || "观点", claim.claim_kind === "hypothesis" ? "orange" : claim.claim_kind === "model_inference" ? "purple" : "gray"));
        if (claim.classification === "contested") paragraph.append(badge("有分歧", "orange"));
        append(paragraph, document.createTextNode(claim.statement)); item.append(paragraph);
        claim.evidence.forEach((ref) => {
          citation += 1;
          const quote = append(el("div", "document-evidence"), el("p", "", `“${ref.quote}”`));
          const relation = { supports: "支持", challenges: "不同观点", mentions: "相关背景", unclear: "关系待梳理" }[ref.relation] || "引用";
          const b = el("button", "", `[${citation}] ${relation} · ${ref.title} · ${locationLabel(ref)}`);
          b.addEventListener("click", () => openSource(ref.source_id, { version: ref.version_id, blockId: ref.block_id, span: { start: ref.start_char, end: ref.end_char } })); quote.append(b); item.append(quote);
        }); content.append(item);
      });
      const details = el("details", "technical-details"); append(details, el("summary", "", "查看 Markdown 原文与版本信息"), el("pre", "markdown-source", revision.markdown));
      details.append(technical({ "输出编号": project.project_id, "版本编号": revision.revision_id, "生成时间": revision.created_at, "整理方式": revision.provider === "none" ? "本地证据整理" : revision.provider }));
      modal.body.replaceChildren(actions, cover, meta, outline, content, details, contextHint());
      setSelection({ source: "ruminate", kind: "output", project_id: id, revision_id: revision.revision_id, version: revision.version, title: project.title,
        claims: claims.slice(0, 8).map(claim => ({ claim_id: claim.claim_id, statement: claim.statement.slice(0, 1000), claim_kind: claim.claim_kind })) }, project.title);
    } catch (error) { if (sequence === loadSequence) modal.body.replaceChildren(empty("暂时没能打开输出", errorMessage(error), "outputs", button("再试一次", "primary", "review", () => load(version)))); }
  }
  await load();
}

function showMarkdownExport(filename, markdown) {
  const modal = dialog("导出 Markdown", "可以复制下面的完整 Markdown 内容。");
  const text = el("textarea", "export-text"); text.value = markdown; text.readOnly = true;
  text.setAttribute("aria-label", "完整 Markdown 内容");
  const copy = button("复制 Markdown", "primary", "outputs", async () => {
    try { await navigator.clipboard.writeText(markdown); toast("Markdown 已复制"); }
    catch { text.focus(); text.select(); toast("已选中全文，按复制快捷键即可"); }
  });
  append(modal.body, el("p", "export-filename", filename), text, append(el("div", "form-actions"), copy));
}

function dialog(title, description = "", extra = "") {
  if (extra.split(" ").includes("reader")) return detail(title, extra);
  const d = el("dialog", `dialog ${extra}`); d.setAttribute("aria-label", title);
  const titleNode = el("h2", "", title); const intro = append(el("div"), titleNode); if (description) intro.append(el("p", "", description));
  const close = append(el("button", "icon-button"), icon("close")); close.type = "button"; close.setAttribute("aria-label", "关闭"); close.addEventListener("click", () => d.close());
  const header = append(el("header", "dialog-head"), intro, close); const body = el("div", "dialog-body"); append(d, header, body);
  document.body.append(d); d.addEventListener("close", () => d.remove());
  d.addEventListener("click", (event) => { if (event.target === d) { const r = d.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) d.close(); } });
  d.showModal(); return { dialog: d, body, title: titleNode, close: () => d.close() };
}
function field(label, control, help) { const wrap = el("label", "field"); append(wrap, el("span", "", label), control); if (help) wrap.append(el("span", "field-help", help)); return wrap; }
function input(placeholder, required = true, type = "text") { const i = el("input"); i.type = type; i.placeholder = placeholder; i.required = required; return i; }
function textarea(placeholder, required = true) { const i = el("textarea"); i.placeholder = placeholder; i.required = required; i.maxLength = 2000; return i; }
function formError(form, error) {
  form.querySelector(".form-error")?.remove();
  const node = el("div", "form-error", errorMessage(error)); node.setAttribute("role", "alert"); form.prepend(node);
}
function submitButton(label) { const b = button(label, "primary"); b.type = "submit"; return b; }
function bindSubmit(form, submit, work, working) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (submit.disabled) return; const label = submit.textContent; submit.disabled = true; submit.textContent = working || "正在保存…";
    form.querySelector(".form-error")?.remove();
    try { await work(); } catch (error) { formError(form, error); }
    finally { submit.disabled = false; submit.textContent = label; }
  });
}
function openTopic() {
  const modal = dialog("新建研究主题", "用主题归类资料、想法和输出。"); const form = el("form", "dialog-form");
  const name = input("例如：电商 Agent 的真实价值"); name.maxLength = 200;
  const description = textarea("填写研究问题或范围（可选）", false);
  const submit = submitButton("创建主题");
  append(form, field("主题名称", name), field("想探索的问题", description), append(el("div", "form-actions"), button("取消", "ghost", null, modal.close), submit)); modal.body.append(form); name.focus();
  bindSubmit(form, submit, async () => { const topic = await api("/api/topics", { name: name.value.trim(), description: description.value.trim() }); state.topic = topic.topic_id; await refreshState(); modal.close(); toast("研究主题已创建"); await navigate("research"); });
}
function openCapture() {
  const modal = dialog("添加一份资料", "保存网页、PDF 或研究笔记。");
  const tabs = el("div", "segmented"); const form = el("form", "dialog-form"); const modeArea = el("div"); const topic = topicSelect(state.topic, true);
  topic.options[0].textContent = "暂不归入主题";
  const url = input("https://…", true, "url"); const file = input("", false, "file"); file.accept = ".pdf,.md,.markdown,.txt,.html,.htm,application/pdf,text/markdown,text/plain,text/html";
  let mode = "url"; const submit = submitButton("收集资料");
  const urlTab = el("button", "active", "网页链接"); urlTab.type = "button"; const fileTab = el("button", "", "本地文件"); fileTab.type = "button";
  function switchMode(next) {
    mode = next; urlTab.classList.toggle("active", mode === "url"); fileTab.classList.toggle("active", mode === "file"); url.required = mode === "url"; file.required = mode === "file";
    if (mode === "url") modeArea.replaceChildren(field("网页地址", url, "点击收集后，会读取这个链接并把原文保存到本地。"));
    else {
      file.className = "file-input"; file.tabIndex = -1; file.setAttribute("aria-label", "选择本地文件");
      const selected = el("strong", "upload-filename", file.files[0]?.name || "选择一份资料");
      const choose = button(file.files[0] ? "更换文件" : "选择文件", "secondary", "plus", () => file.click());
      file.onchange = () => { selected.textContent = file.files[0]?.name || "选择一份资料"; choose.querySelector("span").textContent = file.files[0] ? "更换文件" : "选择文件"; };
      const zone = append(el("div", "upload-zone"), icon("upload"), selected, el("span", "", "PDF、Markdown、纯文本或 HTML · 最大 20 MB"), choose, file);
      modeArea.replaceChildren(zone);
    }
  }
  urlTab.addEventListener("click", () => switchMode("url")); fileTab.addEventListener("click", () => switchMode("file")); append(tabs, urlTab, fileTab); switchMode(mode);
  append(form, modeArea, field("放进主题", topic, ""), append(el("div", "form-actions"), button("取消", "ghost", null, modal.close), submit)); append(modal.body, tabs, form); url.focus();
  bindSubmit(form, submit, async () => {
    let result;
    if (mode === "url") result = await api("/api/ingest/url", { url: url.value.trim(), topic: topic.value || null });
    else {
      const selected = file.files[0]; if (!selected) throw new Error("先选择一份文件。"); if (selected.size > 20 * 1024 * 1024) throw new Error("文件超过 20 MB，请选择较小的文件。");
      const content = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("没能读到这份文件，请重新选择。")); reader.readAsDataURL(selected); });
      result = await api("/api/ingest/file", { filename: selected.name, content_base64: content, topic: topic.value || null });
    }
    if (["needs_attention", "failed", "needs_browser"].includes(result.status) && !result.source_id) throw new Error(result.message || result.error?.message || "这次还没有读出正文。可以尝试下载文件后导入。");
    await refreshState(); modal.close(); state.query = ""; state.type = "all"; state.topic = topic.value; toast(["needs_ocr", "needs_attention"].includes(result.status) ? "文件已收集，可以先查看页面图像" : "资料已保存"); await navigate("library");
    if (result.source_id) openSource(result.source_id);
  }, "正在收集…");
}
function openThought({ topic, block, source } = {}) {
  if (!state.topics.length) { openTopic(); return; }
  const selection = window.getSelection(); const selectedText = selection?.toString() || "";
  const modal = dialog(block ? "记录带引用的想法" : "记下想法", "一个观点、一个猜想，或一个还想追问的问题。"); const form = el("form", "dialog-form");
  const select = topicSelect(topic || state.topic || source?.topics?.[0]?.topic_id);
  const kind = el("select"); Object.entries(kindLabels).forEach(([value, label]) => { const o = el("option", "", label); o.value = value; kind.append(o); }); kind.value = block ? "source_assertion" : "question";
  const statement = textarea("用自己的话记下来…"); const quote = textarea("保留要引用的原文"); quote.maxLength = 2000;
  const relation = el("select");
  [["supports", "支持这个想法"], ["challenges", "提出不同观点"], ["mentions", "提供相关背景"]].forEach(([value, label]) => { const option = el("option", "", label); option.value = value; relation.append(option); });
  kind.addEventListener("change", () => { relation.value = kind.value === "question" ? "mentions" : "supports"; });
  relation.value = kind.value === "question" ? "mentions" : "supports";
  if (block) quote.value = selectedText && block.text.includes(selectedText) ? selectedText : Array.from(block.text).slice(0, 1000).join("");
  append(form, field("研究主题", select), field("这条想法是", kind), field("想法", statement));
  if (block) append(form, field("引用片段", quote, "可以缩短片段，文字需保持与原文一致（最多 1,000 字）。"), field("这段原文", relation));
  const submit = submitButton("记下想法"); append(form, append(el("div", "form-actions"), button("取消", "ghost", null, modal.close), submit)); modal.body.append(form); statement.focus();
  bindSubmit(form, submit, async () => {
    const payload = { topic_id: select.value, statement: statement.value.trim(), claim_kind: kind.value };
    if (block) { const q = quote.value; const start = block.text.indexOf(q); if (!q || start < 0) throw new Error("引用片段需要与原文一致，可以直接从原文复制一小段。"); if (Array.from(q).length > 1000) throw new Error("引用最多 1,000 字，请缩短选段。"); Object.assign(payload, { block_id: block.block_id, relation: relation.value, start_char: Array.from(block.text.slice(0, start)).length, end_char: Array.from(block.text.slice(0, start + q.length)).length }); }
    await api("/api/claims", payload); modal.close(); toast("想法已保存"); state.dirty = true; if (!detailStack.length) await navigate(state.view);
  });
}
function openNewProject() {
  if (!state.topics.length) { openTopic(); return; }
  const modal = dialog("新建输出", "选择主题和输出类型。"); const form = el("form", "dialog-form");
  const topic = topicSelect(state.topic); const title = input("填写输出标题"); title.maxLength = 200;
  const kind = el("select"); Object.entries(projectLabels).forEach(([value, label]) => { const o = el("option", "", label); o.value = value; kind.append(o); }); kind.value = "research_proposal";
  const goal = textarea("填写写作目标或读者（可选）", false); const submit = submitButton("创建输出");
  append(form, field("研究主题", topic), field("标题", title), field("输出类型", kind), field("写作目标", goal), append(el("div", "form-actions"), button("取消", "ghost", null, modal.close), submit)); modal.body.append(form); title.focus();
  bindSubmit(form, submit, async () => { const p = await api("/api/projects", { topic_id: topic.value, title: title.value.trim(), kind: kind.value, goal: goal.value.trim() }); await refreshState(); modal.close(); toast("输出项目已创建"); await navigate("outputs"); await openProject(p.project_id); });
}
function technical(items) { const details = el("details", "technical-details"); const dl = el("dl"); Object.entries(items).forEach(([key, value]) => append(dl, el("dt", "", key), el("dd", "", typeof value === "object" ? JSON.stringify(value) : value))); append(details, el("summary", "", "来源与保存信息"), dl); return details; }

async function openSource(id, options = {}) {
  const modal = dialog("正在打开资料…", "", "reader source-reader"); modal.body.append(loading());
  let source, mode = "text", page = options.page || 1, pageCount = null, releaseImage = null, fetchSequence = 0, sourceSequence = 0;
  modal.dialog.addEventListener("close", () => { ++sourceSequence; ++fetchSequence; releaseImage?.(); });
  async function fetchSource(version = options.version) {
    const sequence = ++sourceSequence;
    const loadedSource = await api(`/api/sources/${id}?${params({ version, limit: 20 })}`);
    if (options.blockId) {
      while (!loadedSource.blocks.some((b) => b.block_id === options.blockId) && loadedSource.next_block_start !== null) {
        if (sequence !== sourceSequence || !modal.dialog.isConnected) return;
        const more = await api(`/api/sources/${id}?${params({ version: loadedSource.version_id, start: loadedSource.next_block_start, limit: 20 })}`);
        loadedSource.blocks.push(...more.blocks); loadedSource.next_block_start = more.next_block_start;
        if (!more.blocks.length) break;
      }
    }
    if (sequence !== sourceSequence || !modal.dialog.isConnected) return;
    source = loadedSource;
    if (source.blocks.find((b) => b.block_id === options.blockId)?.page) page = source.blocks.find((b) => b.block_id === options.blockId).page;
    render();
  }
  function render() {
    if (!modal.dialog.isConnected) return;
    ++fetchSequence; releaseImage?.(); releaseImage = null;
    modal.title.textContent = source.title;
    const meta = append(el("div", "reader-meta"), badge(classifySource(source).full, source.mime_type === "application/pdf" ? "purple" : ""), el("span", "", `${source.block_count} 个片段`), el("span", "", `收集于 ${formatDate(source.retrieved_at)}`));
    const url = safeURL(source.canonical_uri); if (url) { const link = append(el("a"), document.createTextNode("访问原网页"), icon("external")); link.href = transport.embedded ? "#source" : url; link.title = url; if (!transport.embedded) { link.target = "_blank"; link.rel = "noopener noreferrer"; } link.addEventListener("click", (event) => { if (transport.embedded) { event.preventDefault(); transport.openLink(url).catch((error) => toast(errorMessage(error), true)); } }); meta.append(link); }
    const tabs = el("div", "reader-tabs"); const textButton = button("原文片段", `secondary small${mode === "text" ? " active" : ""}`, "book", () => { mode = "text"; render(); }); tabs.append(textButton);
    if (source.mime_type === "application/pdf") tabs.append(button("页面图像", `secondary small${mode === "image" ? " active" : ""}`, "file", () => { mode = "image"; render(); }));
    const version = el("select", "select reader-version"); version.setAttribute("aria-label", "资料版本");
    source.versions.forEach((v, index) => { const o = el("option", "", `${index === 0 ? "最新保存" : "历史保存"} · ${formatDate(v.retrieved_at)}`); o.value = v.version_id; version.append(o); }); version.value = source.version_id;
    version.addEventListener("change", () => { options.blockId = null; page = 1; pageCount = null; modal.body.replaceChildren(loading()); fetchSource(version.value).catch((e) => toast(errorMessage(e), true)); }); tabs.append(version);
    const content = el("div", "reader-content"); modal.body.replaceChildren(meta, tabs, content);
    if (mode === "image") renderPDF(content); else renderBlocks(content);
    syncSourceContext();
    modal.body.append(technical({ "资料编号": source.source_id, "保存版本": source.version_id, "保存时间": source.retrieved_at, "原始来源": source.canonical_uri, "文件校验": source.sha256 }));
  }
  function syncSourceContext() {
    const selected = mode === "image" ? source.blocks.filter(block => block.page === page).slice(0, 3) : options.blockId ? source.blocks.filter(block => block.block_id === options.blockId) : source.blocks.slice(0, 3);
    const quote = options.span && options.blockId ? Array.from(selected[0]?.text || "").slice(options.span.start, options.span.end).join("") : "";
    setSelection({ source: "ruminate", kind: "source", source_id: source.source_id, version_id: source.version_id, title: source.title, uri: source.canonical_uri,
      reading_mode: mode, ...(mode === "image" ? { page } : {}), selected_block_id: options.blockId || null, selected_span: options.span || null,
      selected_quote: quote.slice(0, 1200), quote_truncated: quote.length > 1200,
      blocks: selected.map(block => ({ block_id: block.block_id, page: block.page, text: block.text.slice(0, 1600), text_truncated: block.text.length > 1600 })) }, source.title);
  }
  function renderBlocks(content) {
    source.blocks.forEach((block) => {
      const node = el("article", `source-block${block.block_id === options.blockId ? " highlighted" : ""}`); node.dataset.blockId = block.block_id;
      const p = el("p", "block-text", block.text);
      append(node, el("span", "block-number", String(block.ordinal + 1).padStart(2, "0")), p);
      if (block.block_id === options.blockId && options.span) {
        const chars = Array.from(block.text); const start = options.span.start; const end = options.span.end;
        if (Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end <= chars.length) p.replaceChildren(document.createTextNode(chars.slice(0, start).join("")), el("mark", "", chars.slice(start, end).join("")), document.createTextNode(chars.slice(end).join("")));
      }
      const note = append(el("button"), icon("plus"), document.createTextNode("记下想法")); note.addEventListener("click", () => openThought({ block, source }));
      append(node, append(el("div", "block-meta"), el("span", "", block.page ? `第 ${block.page} 页` : block.section || `片段 ${block.ordinal + 1}`), note)); content.append(node);
    });
    if (!source.blocks.length) content.append(empty("暂无可提取的文字", "PDF 可切换到“页面图像”阅读。", "file"));
    if (source.next_block_start !== null) {
      const more = button("继续阅读", "secondary small", "arrow"); const footer = append(el("div", "reader-pagination"), more); content.append(footer);
      more.addEventListener("click", () => busy(more, async () => { const next = await api(`/api/sources/${id}?${params({ version: source.version_id, start: source.next_block_start, limit: 20 })}`); source.blocks.push(...next.blocks); source.next_block_start = next.next_block_start; const scroll = modal.body.scrollTop; render(); modal.body.scrollTop = scroll; }, "正在读取…"));
    }
    if (options.blockId) requestAnimationFrame(() => content.querySelector(`[data-block-id="${CSS.escape(options.blockId)}"]`)?.scrollIntoView({ block: "center" }));
  }
  async function renderPDF(content) {
    const current = ++fetchSequence;
    const previous = button("上一页", "secondary small", "back"); const next = button("下一页", "secondary small", "arrow"); const input = el("input", "page-number"); input.type = "number"; input.min = "1"; input.value = page; input.setAttribute("aria-label", "PDF 页码");
    const total = el("span", "", pageCount ? `/ ${pageCount}` : "页"); const go = button("跳转", "ghost small");
    const toolbar = append(el("form", "pdf-toolbar"), previous, input, total, go, next); go.type = "submit";
    const wrap = append(el("div", "pdf-image-wrap"), loading()); append(content, toolbar, wrap, el("p", "pdf-help", "页面来自保存的原始 PDF，可查看图表、公式和版式。"));
    previous.disabled = page <= 1; next.disabled = pageCount !== null && page >= pageCount;
    const change = (target) => { if (!Number.isInteger(target) || target < 1 || (pageCount && target > pageCount)) { toast("请输入有效页码", true); return; } page = target; input.value = page; content.replaceChildren(); renderPDF(content); syncSourceContext(); };
    previous.addEventListener("click", () => change(page - 1)); next.addEventListener("click", () => change(page + 1)); toolbar.addEventListener("submit", (e) => { e.preventDefault(); change(Number(input.value)); });
    try {
      const result = await transport.pageImage(`/api/sources/${id}/page?${params({ version: source.version_id, page })}`);
      if (current !== fetchSequence || !modal.dialog.isConnected) { result.release(); return; }
      pageCount = result.metadata.page_count || null; total.textContent = pageCount ? `/ ${pageCount}` : "页"; input.max = pageCount || "500"; next.disabled = pageCount !== null && page >= pageCount;
      releaseImage?.(); releaseImage = result.release; const image = el("img", "pdf-page"); image.alt = `${source.title}，第 ${page} 页`; image.src = result.url; wrap.replaceChildren(image);
    } catch (error) { if (current === fetchSequence) wrap.replaceChildren(empty("这一页没能展开", errorMessage(error), "file")); }
  }
  try { await fetchSource(); } catch (error) { modal.body.replaceChildren(empty("暂时没能打开资料", errorMessage(error), "file")); }
}


// One keyboard-accessible select control for filters, forms and version pickers.
let openSelect = null, controlSerial = 0;
function enhanceSelect(select) {
  if (!select.isConnected || select.multiple) return;
  const label = select.closest(".field")?.firstElementChild?.textContent || select.getAttribute("aria-label") || "选择选项";
  const wrap = el("div", `select-control ${select.className}`);
  const trigger = el("button", "select-trigger"); trigger.type = "button";
  trigger.setAttribute("role", "combobox"); trigger.setAttribute("aria-haspopup", "listbox"); trigger.setAttribute("aria-expanded", "false"); trigger.setAttribute("aria-label", label);
  const text = el("span", "select-value"); const arrow = icon("chevron"); append(trigger, text, arrow);
  const list = el("div", "select-menu"); list.id = `select-menu-${++controlSerial}`; list.setAttribute("role", "listbox"); list.setAttribute("aria-label", `${label}选项`);
  trigger.setAttribute("aria-controls", list.id);
  const nativePopover = typeof list.showPopover === "function";
  if (nativePopover) list.setAttribute("popover", "manual"); else list.hidden = true;
  let opened = false, active = -1, typed = "", typedAt = 0;
  const options = () => [...select.options].filter(option => !option.hidden);
  function sync() {
    text.textContent = select.selectedOptions[0]?.textContent || "请选择";
    trigger.dataset.value = select.value; trigger.disabled = select.disabled;
  }
  function position() {
    const rect = trigger.getBoundingClientRect();
    const below = innerHeight - rect.bottom - 12, above = rect.top - 12;
    const up = below < Math.min(list.scrollHeight, 200) && above > below;
    const available = Math.max(64, up ? above : below);
    list.style.maxHeight = `${Math.min(288, available)}px`;
    list.style.width = `${Math.min(Math.max(rect.width, 180), innerWidth - 24)}px`;
    list.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - list.getBoundingClientRect().width - 12))}px`;
    list.style.top = `${up ? Math.max(8, rect.top - list.getBoundingClientRect().height - 6) : rect.bottom + 6}px`;
  }
  function highlight(index) {
    const values = options(); if (!values.length) return;
    active = Math.max(0, Math.min(index, values.length - 1));
    const nodes = [...list.children];
    nodes.forEach((node, i) => node.classList.toggle("focused", i === active));
    const node = nodes[active]; trigger.setAttribute("aria-activedescendant", node.id);
    if (node.offsetTop < list.scrollTop) list.scrollTop = node.offsetTop;
    else if (node.offsetTop + node.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = node.offsetTop + node.offsetHeight - list.clientHeight;
  }
  function close(restore = false) {
    if (!opened) return;
    opened = false;
    if (nativePopover) { if (list.matches(":popover-open")) list.hidePopover(); } else list.hidden = true;
    trigger.setAttribute("aria-expanded", "false"); trigger.removeAttribute("aria-activedescendant");
    if (openSelect?.trigger === trigger) openSelect = null;
    if (restore && trigger.isConnected) trigger.focus({ preventScroll: true });
  }
  function choose(index) {
    const option = options()[index]; if (!option || option.disabled) return;
    select.value = option.value; sync(); close(true);
    select.dispatchEvent(new Event("change", { bubbles: true })); sync();
  }
  function open() {
    if (opened || trigger.disabled) return;
    openSelect?.close();
    list.replaceChildren(...options().map((option, index) => {
      const node = el("div", `select-option${option.disabled ? " disabled" : ""}`);
      node.id = `${list.id}-${index}`; node.setAttribute("role", "option"); node.setAttribute("aria-selected", option.selected ? "true" : "false");
      if (option.disabled) node.setAttribute("aria-disabled", "true");
      append(node, el("span", "", option.textContent), option.selected ? icon("check") : null);
      node.addEventListener("pointerdown", event => event.preventDefault());
      node.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); choose(index); });
      node.addEventListener("pointermove", () => highlight(index));
      return node;
    }));
    opened = true; openSelect = { trigger, list, close, position };
    if (nativePopover) list.showPopover(); else list.hidden = false;
    trigger.setAttribute("aria-expanded", "true"); position();
    highlight(Math.max(0, options().findIndex(option => option.selected)));
  }
  trigger.addEventListener("click", event => { event.preventDefault(); opened ? close() : open(); });
  trigger.addEventListener("keydown", event => {
    const values = options();
    if (event.key === "Escape" && opened) { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (event.key === "Tab") { close(); return; }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault(); const wasOpen = opened; open();
      let next = event.key === "Home" ? 0 : event.key === "End" ? values.length - 1 : wasOpen ? active + (event.key === "ArrowDown" ? 1 : -1) : active;
      const step = event.key === "ArrowUp" || event.key === "End" ? -1 : 1;
      while (next >= 0 && next < values.length && values[next].disabled) next += step;
      highlight(next); return;
    }
    if (["Enter", " "].includes(event.key)) { event.preventDefault(); opened ? choose(active) : open(); return; }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing) {
      const now = Date.now(); typed = now - typedAt < 700 ? typed + event.key : event.key; typedAt = now;
      const index = values.findIndex(option => !option.disabled && option.textContent.toLocaleLowerCase().startsWith(typed.toLocaleLowerCase()));
      if (index >= 0) { event.preventDefault(); open(); highlight(index); }
    }
  });
  select.addEventListener("change", sync);
  append(wrap, trigger, list); select.replaceWith(wrap); sync();
}
const controlsObserver = new MutationObserver(() => {
  if (openSelect && !openSelect.trigger.isConnected) openSelect.close();
  document.querySelectorAll("select").forEach(enhanceSelect);
});
controlsObserver.observe(document.body, { childList: true, subtree: true });
document.addEventListener("pointerdown", event => {
  if (openSelect && !openSelect.trigger.contains(event.target) && !openSelect.list.contains(event.target)) openSelect.close();
}, true);
document.addEventListener("scroll", event => {
  if (openSelect && !openSelect.list.contains(event.target)) openSelect.close();
}, true);
window.addEventListener("resize", () => openSelect?.close());


document.querySelector(".brand").addEventListener("click", event => { event.preventDefault(); navigate("thoughts"); });
if (!transport.embedded) window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
window.addEventListener("ruminate:tool-input", event => {
  if (!appReady || initialViewApplied) return;
  initialViewApplied = true;
  if (event.detail.topic_id) state.topic = event.detail.topic_id;
  if (event.detail.view) navigate(event.detail.view);
});
window.addEventListener("ruminate:tool-result", event => {
  if (!appReady) return;
  const data = event.detail.structuredContent;
  if (data?.topics && data?.sources && data?.projects) {
    refreshState(data).then(() => { if (!detailStack.length) navigate(state.view); }).catch(error => toast(errorMessage(error), true));
  }
});
window.addEventListener("ruminate:tool-cancelled", event => toast(event.detail.reason, true));
window.addEventListener("ruminate:teardown", () => {
  appReady = false; clearTimeout(selectionTimer); ++selectionRevision; ++state.loadingId; clearInterval(refreshTimer); clearDetails();
  document.querySelectorAll("dialog[open]").forEach(node => node.close());
  document.querySelector("#main").replaceChildren(empty("工作区已关闭", "可以从插件入口重新打开。", "leaf"));
});
let refreshInFlight = false;
async function refreshVisible() {
  if (!appReady || document.hidden || detailStack.length || refreshInFlight || openSelect || document.querySelector("dialog[open]") || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  refreshInFlight = true;
  const generation = state.loadingId;
  try {
    const data = await api(`/api/management?${params({ view: viewKeys[state.view], topic: state.topic, query: state.query, sort: state.sort, status: state.view === "thoughts" ? state.status : "all", offset: state.offset, limit: 25 })}`);
    if (generation === state.loadingId && !detailStack.length && listSignature(data) !== state.signature) {
      await navigate(state.view, { scroll: document.querySelector("#main").scrollTop, focusId: document.activeElement?.dataset.rowId });
    }
  } catch { /* Keep the current list readable; explicit refresh reports connection errors. */ }
  finally { refreshInFlight = false; }
}
const refreshTimer = setInterval(refreshVisible, 60000);
window.addEventListener("focus", refreshVisible);
document.addEventListener("visibilitychange", refreshVisible);
(async () => {
  try {
    await transport.connect();
    await refreshState(transport.result?.structuredContent);
    if (transport.input.topic_id) state.topic = transport.input.topic_id;
    initialViewApplied = Object.keys(transport.input).length > 0;
    await navigate(transport.input.view || (!transport.embedded && location.hash.slice(1)) || "thoughts");
    appReady = true;
  } catch (error) {
    document.querySelector("#main").replaceChildren(empty("还没连上资料库", errorMessage(error), "question", button("重新连接", "primary", "review", () => location.reload())));
  }
})();
