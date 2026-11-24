# Ruminate 本地认知内核架构

当前版本 0.7.0，schema 7。主路径为：

`MCP Apps 管理页 / CLI / MCP → Application → 导入、知识、反刍、输出与备份服务 → SQLite + CAS blobs`。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `config.py` | 平台数据目录、固定文件/目录授权范围、网络模式与资源上限；读取配置不创建存储。 |
| `application.py` | GUI、CLI 与 MCP 共用入口，初始化、健康检查、资料与视觉访问；提供 knowledge / reviews / outputs / enrichment 服务。 |
| `ingestion/files.py` | POSIX 目录句柄遍历、授权范围验证、限量文件读取、SHA256、原子 blob 写入及读回校验。 |
| `ingestion/fetch.py` | 显式网络授权、URL 规范化、每跳 SSRF 校验、固定 IP socket、TLS hostname 验证、下载及时间上限。 |
| `ingestion/parsers.py` / `pdf_worker.py` | 统一 ParsedDocument / ParsedBlock；文本行段、受限 PDF/HTML 解析、真实 locator、降级 warnings。 |
| `ingestion/vision.py` / `vision_worker.py` | 受限子进程将指定 PDF 页渲染成 PNG；不调用模型、不执行 OCR、不改写原生块。 |
| `ingestion/service.py` | 同步执行但持久化的导入任务、字节快照、租约、重试、失败码。 |
| `persistence/sources.py` | 来源身份、不可变版本和块、主题关联、FTS 索引、范围受限的查询。 |
| `persistence/observations.py` | 带明确版本/页码的宿主转写或解读；始终 unverified，幂等写入与审计。 |
| `persistence/database.py` | WAL、数据库身份与 schema 校验、事务化迁移及 checksum。 |
| `persistence/common.py` | 认知服务共享事务、参数校验、审计、主题解析与请求幂等。 |
| `knowledge.py` | Topic / Claim / ClaimEvidence / UserBelief / BeliefRelation 与有范围限制的研究上下文。 |
| `management.py` | 资料显示名称与主题分类的幂等编辑；同一快照内查询资料、想法及确认观点、输出，提供稳定分页与真实计数。到期回看不受主动推送额度限制。 |
| `thinking.py` | 有引用的引导修订、个人思考、双向关联及真实用户活动聚合。 |
| `organization.py` | 宿主分类建议、追加归类与撤回；保留人工分类归属。 |
| `review.py` | 确定性候选、可解释评分、只读队列预览、显式曝光及人工反馈。 |
| `outputs.py` | 五种模板、不可变修订、原文引用校验、新证据影响分析、Markdown / JSON 导出。 |
| `providers.py` / `enrichment.py` | NoLLM / Mock 提议接口、快照输入、协作超时、租约和可恢复任务；不自动写入认知结论。 |
| `backup.py` | SQLite 一致快照、引用 blobs、结构化记录与已存修订导出；校验后恢复到新目录。 |
| `cli.py` / `cognition_cli.py` / `mcp_server.py` | 参数适配；MCP 默认九个只读工具，显式启用写入后增加工作流工具。 |
| `web_server.py` / `ui/` | 127.0.0.1 HTTP 适配器与无构建步骤的本地页面；会话、同源检查、文件上传及阅读/反刍/研究/输出交互。 |
| `workspace.py` / `mcp_ui.py` / `ui/transport.js` | MCP Apps 资源与桥接；命名操作枚举调用共享 Application，HTTP 预览复用同一适配器。 |

## 存储与事务

migration 001 保存应用身份、任务和审计表；002 增加来源、版本、块、索引与导入快照；003 增加独立视觉记录；004 增加认知关系、反刍与输出修订、幂等请求；005 增加 enrichment 输入与结果快照；006 增加 `sources.title_is_custom`，区分用户资料名与随版本更新的原始标题。不要修改已使用的迁移文件；旧版本通过新增迁移演进。

migration 007 追加反刍引导、个人思考、分类建议和想法的次级主题，并记录自动添加的资料分类归属，便于撤回时保留手动选择。

原始字节存入 `blobs/<sha256-prefix>/<sha256>`，临时文件写完并 fsync 后原子替换。读取校验路径形状与内容 hash。数据库事务共同提交版本、块、FTS、主题、任务结果和审计记录；失败不留下半套已入库证据。

导入先保存 Job，再获取授权来源、保存 blob 与 snapshot，解析后提交索引。租约防止同一任务同时提交；中断后的任务在租约过期后可重试。已有快照重试沿用当时字节和抓取时间，不重新读取已经改变的源文件或页面。这里没有常驻队列服务或自动后台调度器。

## 身份、引用与检索

文件按 SHA256 强去重；相同路径内容变化产生新版本。URL 规范化只移除明确追踪参数，保留语义查询串，记录重定向别名；页面的 canonical 作为提示元数据，不直接决定合并。旧版本与旧 block_id 不会被新内容覆盖。

每个块保存原文、ordinal、page / section、locator 和解析质量。PDF locator 为真实页码及该页提取文本范围；Markdown/TXT 为行范围及解码文本范围；HTML 优先使用保存快照的 XPath，退化时明确使用 `html_extracted_text` 提取基准。字符偏移按 Python Unicode 字符计数，不是 UTF-8 字节或 UTF-16 偏移。parser_version 记录 Ruminate 版本，实际依赖由锁文件固定；长期引用主要由已保存版本与块复现，不保证未来不同解析器版本重跑字节仍得到完全一致文本。

FTS5 的索引文本与原文分离：英文词项加中文单字/双字 n-gram；原文不做归一化替换。查询是字面词项的 AND 匹配，排名用 BM25，不含向量/语义搜索或作者/日期高级筛选。检索默认只覆盖当前版本，可按主题过滤；旧版通过 source_id + version_id 读取。默认每次 10 条检索、800 字符片段；读取块分页并有总字符预算。

只读操作不修改业务记录或原始文件；SQLite WAL 可能创建协调文件。禁止为了隐藏文件变化而使用忽略活跃 WAL 的 immutable 连接或手工删除 WAL。

## 宿主视觉路径

`Application.get_source_page_image` 按 source_id、明确或当前 version_id 和一基页码取出已保存 PDF，校验 blob 后交给 pypdfium2 子进程。每次仅渲染一页，最长边 1800 像素、PNG 最多 8 MiB，默认 wall timeout 10 秒。返回图片、来源 SHA256、图片 SHA256、renderer 版本与真实页级 locator；不生成猜测的文字坐标。

MCP 返回 TextContent 元数据与 ImageContent，供当前 Codex 会话识别；CLI 可将同一 PNG 导出到显式指定的新文件。渲染器只处理字节与像素，模型识别发生在宿主上下文中，不依赖 Ruminate 的模型凭据，也没有后台模型调用。

宿主文字可经 Application / CLI 保存为 `visual_transcription` 或 `visual_interpretation`，记录 origin 为 `host_model`，status 固定为 `unverified`。记录绑定原始版本和页码，host 字段是调用方归属说明，不是经过验证的模型身份；request_id 保证相同请求重试幂等。保存前检查 PDF 页确实可渲染，记录与审计共同提交。

视觉记录不覆盖 SourceVersion / Block，不写入原生 FTS，不改变来源 `needs_ocr`，也不生成 UserBelief。MCP 的 `list_visual_observations` 仅查询这些记录。当前 ClaimEvidence 仍要求原生 Block；视觉记录尚不能经自动核验转为原文证据。

## 知识、调度与输出

Claim 分为 source_assertion / model_inference / hypothesis / question，状态保持 candidate。ClaimEvidence 保存精确块内片段、不可变版本和显式关系；exact_span 只证明引用位置，不证明语义蕴含。UserBelief 只有 origin=human 且显式 confirmed 才能成为确认立场；host_model 只能产生候选。

Review 候选依据显式关系、证据和活跃项目生成，不调用语义冲突分类器。possible_conflict 要求确认观点、质疑该观点的主张以及支持该主张的来源。候选保留两侧可用证据、原因、归一化特征和加权评分。fingerprint 保证同类候选不重复插入；已有候选保持生成时的证据快照及反馈生命周期。

`tick --dry-run` 与队列查询不写业务数据。`present` 才记录曝光并消费每天三张的配额，按可配置 IANA 时区统计，默认 Asia/Shanghai；还按主题与来源限制重复曝光。反馈和曝光以事务及 request_id 防并发重复，候选默认不触发通知。

OutputRevision 将选择的主张、证据关系、来源版本、片段与 locator 保存为不可变 JSON 快照，并存下确定性 Markdown。最多 50 条主张、200 处引用、20 万字符快照。模型推断始终待核验；有支持来源也不表示已确认事实。读取校验原文引用，导出再核对 blob hash；既有修订不会随新证据被覆盖。

导出在 `outputs/<project>/<revision>/` 原子发布 `draft.md` 与 `evidence.json`，原件相对链接指向 CAS。文件已存在时逐字节比对，拒绝覆盖人工改动。`impact` 比较当前来源版本与新增证据关系，只提示修订。

## 可选提议与备份

默认 NoLLMProvider 从有限的已保存块生成问题，MockLLMProvider 仅用于测试。enrichment 使用版本固定的输入快照、租约与任务结果；最多接收五条有可匹配引用的 unverified 提议，不自动建立主张或确认立场。自定义 Python Provider 的外部执行需要显式授权；目前没有云客户端、SIWC 或后台推理集成。超时基于 async 协作取消，不隔离不合作的代码。

备份使用 SQLite online backup 取得一致快照，收集快照引用的 blobs，并从已存修订重新生成 Markdown / sidecar，同时写结构化 records.json 和 SHA256 manifest。任意工作目录文件、配置和手工改过的导出文件不被打包。恢复检查名称、大小、hash、数据库身份、迁移、外键、记录和引用，最后独占发布到尚不存在的目标目录；不会合并覆盖旧库。

## 本地 GUI 与插件

主要交互入口是 MCP Apps。`open_workspace` 通过 `_meta.ui.resourceUri` 关联自包含 `text/html;profile=mcp-app` 资源；`ui_read` / `ui_write` 仅对 app 可见，使用 action 枚举和参数白名单。`WorkspaceAdapter` 在业务层完成校验与调用，UI 和 HTTP 不复制业务逻辑。

嵌入组件先与宿主完成标准握手，之后通过 JSON-RPC postMessage 使用 `tools/call`、图片内容、`ui/message` 和 `ui/update-model-context`。它不连接 localhost；宿主桥失败会展示错误。资源 CSP 不允许外部连接与外部资源。使用者点击讨论按钮后，原文定位和选定观点随请求带回对话。

`ruminate ui` 是可选独立预览，只绑定 127.0.0.1。静态 HTML/JS/CSS 随 wheel 分发；字体、脚本、样式不依赖 CDN。想法、资料和输出三个入口直接调用应用服务，没有独立数据库或聊天历史。

写接口检查 Host、Origin、Sec-Fetch-Site 和随机会话令牌，读取响应不开放跨域访问。静态资源有固定白名单，原文按文本渲染。浏览器文件选择器提交有大小限制的文件字节，由 `Application.ingest_upload` 保存 CAS 快照再解析；页面不能提交任意本机路径。

导入的 request_id 在已有幂等表中原子预留任务；同一请求返回原任务的当前状态，显式重试继续使用已捕获的快照。原文阅读器的想法与引用由 `create_claim_with_evidence` 在同一事务提交，非法引用不会留下半条记录。

输出阅读版根据不可变 snapshot 呈现，原有 Markdown 和历史版本校验保持不变。浏览器下载提供 Markdown；含原件相对链接的完整本地输出仍可通过 CLI export 获取。

插件位于 `plugins/ruminate`，包含四套 Skills 和 MCP 配置。默认 MCP 只读集合保持兼容；插件显式启用写工具，具体安装与验证边界见 [P3_PLUGIN.md](P3_PLUGIN.md)。本地 GUI 与 ChatGPT MCP Apps iframe 是独立接入路径。

`install.sh` 与 `scripts/install_codex.py` 负责分发：独立的版本化 Python 环境、
本机 marketplace、通过 Codex CLI 注册及真实 app-server 验证。MCP 启动配置直接指向
已安装 Python；运行时不依赖源码目录、shell PATH 或在线依赖解析。
更新沿用资料目录和访问范围，失败时恢复插件配置；研究数据库由应用自己的迁移管理。

思考工作台的数据语义、交互和参考来源见 [THINKING_WORKSPACE.md](THINKING_WORKSPACE.md)。
