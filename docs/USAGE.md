# Ruminate 使用指南

安装与插件入口见 [README](../README.md)。以下命令在源码目录执行。

## 导入与查询

以下示例使用仓库内的测试材料，也可替换为自己的文件。

```bash
uv run ruminate ingest tests/fixtures/single_column.pdf --topic ecommerce-agents
uv run ruminate ingest tests/fixtures/notes.md --topic ecommerce-agents
uv run ruminate search '证据' --topic ecommerce-agents
uv run ruminate search 'evaluation protocol' --limit 5
uv run ruminate sources list --topic ecommerce-agents
```

CLI 明确指定的本地路径仅授权该文件，不授权整个父目录。Python API 与 MCP 的路径导入通过 `Config.allowed_roots` 配置文件或目录；GUI 使用文件选择器上传。

结果包含 `source_id`、`version_id`、`block_id` 和 locator。PDF 使用真实 1 起始页码；文本使用行与字符范围；HTML 使用保存快照中的 XPath，无法映射时明确标为提取文本定位。检索默认只查当前版本；引用旧版本时明确传入版本 ID。

把下列示例 ID 替换成导入结果中的实际 ID：

```bash
uv run ruminate sources get src_... --version-id ver_... --block-start 0 --block-limit 20
uv run ruminate jobs list
uv run ruminate jobs get job_...
uv run ruminate jobs retry job_...
```

重试已有快照的任务会复用持久化字节，不重新读取已改变的源文件或重新联网。未取得快照的失败任务仍需有效的文件或网络授权，例如：

```bash
uv run ruminate --allow-root /absolute/path/to/report.pdf jobs retry job_...
uv run ruminate jobs retry job_... --allow-network
```

任务同步执行并保存进度；进程中断后可在租约过期后重试，运行中的任务会返回 `job_busy`。扫描 PDF 返回 `needs_ocr` 并保留原件；这表示原生提取缺少文字，可继续由宿主查看页面。CLI 正常结果及成功保存的 `unverified` 记录退出 0，需处理状态退出 1，配置/参数等错误退出 2。

## 整理资料与主题

在工作台选中主题后，可从主题下拉中编辑名称和研究问题。打开资料，点击“编辑资料”可修改显示名称或所属主题。CLI 使用相同的保存逻辑：

```bash
uv run ruminate topics edit ecommerce-agents --name '电商 Agent 研究' --description '比较真实场景中的证据' --request-id topic-edit-1
uv run ruminate sources edit src_... --title '便于检索的资料名' --request-id source-name-1
uv run ruminate sources edit src_... --topic ecommerce-agents --request-id source-topics-1
uv run ruminate sources edit src_... --clear-topics --request-id source-topics-2
uv run ruminate sources edit src_... --reset-title --request-id source-name-2
```

`--topic` 可重复，表示完整的新分类；省略时保留原分类。主题可用 ID 或 slug。资料名、主题名和说明可单独修改；省略的字段不变，说明传空字符串可清空。每次新修改使用新的 request ID，重试同次修改沿用原 ID。

自定义资料名会保留到后续导入；恢复原始标题后重新跟随新版本标题。`sources get` 的 `title` 是所选版本的原始标题，`display_title` 是资料显示名称，`title_is_custom` 表示是否由用户命名。主题 ID、slug、原文版本、历史证据及提纲保持原样；重新归类资料不会移动已有想法或输出。

## 从证据到反刍与研究草稿

导入时的 `--topic` 可建立主题，`topics create` 可提前设置名称与说明。以下 `clm_...`、`blk_...`、`out_...` 等都是占位符，需替换为实际返回的 ID：

```bash
uv run ruminate topics list
uv run ruminate topics context ecommerce-agents
uv run ruminate claims add --topic ecommerce-agents --statement '文献中的一条主张' --kind source_assertion --request-id claim-1
uv run ruminate claims link-evidence clm_... blk_... --relation supports --start 0 --end 30 --request-id evidence-1
uv run ruminate claims get clm_...
```

引用范围按原文块的 Unicode 字符索引计算，开始包含、结束不包含；必须准确选择原文。`source_assertion`、`model_inference`、`hypothesis`、`question` 分开保存。`supports / challenges / mentions / unclear` 是显式登记的关系，精确片段校验不等于语义或事实核实。

`beliefs add` 默认创建候选观点。记录用户明确确认的观点时，使用 `--confirm` 并填写确认出处；模型生成的内容保留 `host_model` 来源。

```bash
uv run ruminate beliefs add --topic ecommerce-agents --text '待用户核对的观点' --provenance '本次讨论中的候选记录' --request-id belief-1
uv run ruminate beliefs link bel_... clm_... --relation challenges --justification '需要比较的条件或证据' --request-id belief-link-1
uv run ruminate tick --topic ecommerce-agents --dry-run
uv run ruminate tick --topic ecommerce-agents
uv run ruminate review list --topic ecommerce-agents --limit 3
uv run ruminate review present --topic ecommerce-agents --limit 3 --request-id show-1
uv run ruminate review feedback rev_... postpone --postpone-days 7 --request-id postpone-1
```

可能冲突必须具备真实已确认观点、明确的质疑关系与原文证据；否则不会生成该类候选。`tick` 只生成队列，不发送通知；`--dry-run` 不写业务记录。`review list` 仅预览，`review present` 记录曝光，每天最多三张，按 `RUMINATE_REVIEW_TIMEZONE` 统计（默认 Asia/Shanghai）。同主题/来源限频，延后或忽略的候选不会立即重新出现。

```bash
uv run ruminate outputs create --topic ecommerce-agents --title '电商 Agent 研究提纲' --type research_proposal --audience '研究团队' --request-id project-1
uv run ruminate outputs draft out_... --claim clm_... --request-id draft-1
uv run ruminate outputs revision out_...
uv run ruminate outputs export out_... --format md
uv run ruminate outputs impact out_...
```

`--claim` 可重复传入，省略时选择主题中当前候选主张，最多 50 条。五种模板为 `research_note`、`literature_review`、`research_proposal`、`product_spec`、`article`。每版保存主张与证据快照，区分 supported、contested、hypothesis、needs_verification；模型推断不会因有引用而变为已确认事实。

导出在本地 `outputs/<project_id>/<revision_id>/` 生成 `draft.md` 与 `evidence.json`，带原件相对链接。引用会再次校验真实块、版本、原文片段和 blob hash；已有导出被人工修改后拒绝覆盖。`impact` 提示引用旧版本或新质疑证据，保留原修订。

认知写入请求需要稳定 `--request-id`：同一操作的相同请求重试返回原结果，复用该 ID 修改输入会报冲突。GUI、CLI 与 MCP 共用 Application 服务。

## 可选提议生成

```bash
uv run ruminate enrich src_... --version-id ver_... --request-id enrich-1
uv run ruminate jobs get job_...
uv run ruminate jobs retry job_...
```

CLI 默认使用 `NoLLMProvider`，根据已有片段产生待复核问题。`MockLLMProvider` 用于可重复测试；Python 接口允许显式传入自定义 Provider，但尚未集成云端模型服务或订阅 API。提议只保存为 unverified 任务结果，不自动创建主张、证据关系或用户立场。超时/失败可重试，已解析资料保持不变；异步截止时间依赖 Provider 配合取消，不是隔离任意模型代码的进程沙箱。

## 让 Codex 读取扫描页和图表

Ruminate 将已保存 PDF 的指定页渲染为图片，通过 `get_source_page_image` MCP 工具交给当前 Codex 对话识别文字或理解图表。渲染在本地完成，识别复用宿主模型；无需另接 OCR API，也不会自动处理整份文件。图片带有来源版本、真实页码和内容 hash。

页图传递路径已用自制无文字层 PDF 验证；测试范围见 [EVALUATION.md](EVALUATION.md)。

CLI 也可导出指定页供宿主查看；输出路径必须是尚不存在的 PNG 文件：

```bash
uv run ruminate sources page-image src_... --version-id ver_... --page 1 --output /absolute/path/to/page-1.png
```

将宿主转写保存成 UTF-8 文本后，可记录为 `visual_transcription`；图表解释或推理用 `visual_interpretation`：

```bash
uv run ruminate observations add src_... --version-id ver_... --page 1 --kind visual_transcription --text-file /absolute/path/to/transcription.txt --host codex --request-id scan-page-1-v1
uv run ruminate observations list src_... --version-id ver_... --page 1
```

视觉记录使用 `host_model / unverified` 标记并保留页级定位，单独存储。原生文字、全文索引和解析状态保持原样。同一请求 ID 可安全重试；保存修改后的内容时使用新的请求 ID。

阅读页图时使用当前 Codex 会话，图片进入该会话的上下文。目前支持已入库的 PDF；独立图片导入和批量视觉处理列在[功能范围](KNOWN_LIMITATIONS.md)中。

## URL 授权

默认 `RUMINATE_NETWORK_MODE=ask`，URL 抓取需要本次显式授权：

```bash
uv run ruminate ingest 'https://docs.python.org/3.11/library/http.client.html' --allow-network --topic official-documentation
```

该授权覆盖本次抓取及通过安全校验的公开重定向。`off` 始终拒绝外部抓取；`allowlist` 仅允许精确主机名，每次重定向也重新检查：

```bash
RUMINATE_NETWORK_MODE=allowlist RUMINATE_ALLOWED_HOSTS=docs.python.org uv run ruminate ingest 'https://docs.python.org/3.11/library/http.client.html'
```

只支持默认端口的 HTTP/HTTPS；拒绝私网地址、URL 凭据、HTTPS 降级和压缩响应，不使用代理环境变量、登录 cookie 或浏览器执行。动态页、登录/付费页、无可用正文页面返回明确状态，不绕过访问限制。

可选公开 URL 演示：

```bash
uv run python scripts/smoke_capture.py --live-url https://docs.python.org/3.11/library/http.client.html
```

## 数据与版本

默认数据目录在仓库外：macOS 为 `~/Library/Application Support/Ruminate/`，Linux 为 `$XDG_DATA_HOME/ruminate/`（未设置时为 `~/.local/share/ruminate/`）。Windows 配置路径为 `%LOCALAPPDATA%\Ruminate\`，但当前安全导入实现要求 POSIX。

可用 `RUMINATE_DATA_DIR` 或子命令之前的 `--data-dir` 覆盖。已有数据运行 `init` 即应用至 schema 6 的新增迁移，原迁移历史保留：

```bash
uv run ruminate --data-dir /absolute/path/to/private-data init
uv run ruminate --data-dir /absolute/path/to/private-data doctor
```

原始字节保存于 `blobs/<sha256前两位>/<sha256>`；重复文件复用版本，内容更新保留旧版与旧块。网页 canonical 仅记录为元数据提示，不凭页面声明自动合并来源。原始文本不因分词而改写；中文索引采用单字 / 双字 n-gram，英文使用词项；查询为 AND 匹配与 BM25 排名，不含向量/语义搜索或作者/日期高级筛选。

`doctor` 在未初始化时不创建目录。读取已初始化数据库不修改业务记录或原始文件，但 SQLite WAL 可能创建 `-wal` / `-shm` 协调文件。数据库需放在本机、用户控制的位置。备份恢复的当前验收状态见[测试报告](EVALUATION.md)。

备份路径必须尚不存在；恢复目标必须是新目录，父目录需已存在：

```bash
uv run ruminate --data-dir /absolute/path/to/private-data export-backup /absolute/path/to/backup.zip
uv run ruminate --data-dir /absolute/path/to/new-data import-backup /absolute/path/to/backup.zip
```

备份包含一致的 SQLite 快照、引用原件、结构化 JSON 和从已存修订生成的 Markdown/sidecar。它不复制配置、任意工作文件或已被手工修改的导出；这些改动需另行保存。恢复先校验内容，再发布新目录，不覆盖已有库。当前支持 schema 5 / 6 的备份；旧库在新目录内升级，原 ZIP 保持不变。单包展开内容上限 512 MiB，单数据库 256 MiB，最多 10000 个条目。

## MCP

官方 MCP Python SDK 2.3.0 默认暴露九个只读工具：`health_check`、`search_sources`、`get_source`、`get_source_page_image`、`list_visual_observations`、`get_claim`、`list_review_queue`、`get_topic_context`、`list_output_projects`。

设置 `RUMINATE_MCP_WRITES=1` 可启用导入、保存主张与引用、反刍反馈和生成提纲等工作流工具。插件目录包含 capture、ruminate、research、compose 四套 Skills，接入方式见 [插件说明](P3_PLUGIN.md)。

```bash
uv run ruminate mcp --transport stdio
```

stdout 保留给 JSON-RPC。以下配置中的路径需替换为本机安装路径：

```toml
[mcp_servers.ruminate]
command = "/absolute/path/to/Ruminate/.venv/bin/python"
args = ["-m", "ruminate", "mcp", "--transport", "stdio"]
startup_timeout_sec = 15
```

可加 `env = { RUMINATE_DATA_DIR = "/absolute/path/to/private-data" }` 指定数据。配置方式见[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。保存配置后，在客户端加载 Ruminate；插件安装也可使用本文开头的注册脚本。


返回给 Codex / ChatGPT 的片段和页图会进入当前会话上下文。Ruminate 保存原文与引用位置，研究结论由阅读和讨论形成。数据与执行边界见[安全说明](SECURITY.md)。
