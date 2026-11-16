# Ruminate 插件与 Skills

`plugins/ruminate/` 包含四个工作流和本地 MCP 配置：收集资料、反刍讨论、研究、组织输出。运行时使用已经安装的 `ruminate` 命令和当前用户的本地资料库，不需要模型 API Key。

## 本地接入

开发时先生成本地插件镜像，再安装或更新插件，让 Codex 把工作台入口关联到 Ruminate：

```bash
.venv/bin/python scripts/register_project_plugin.py --plugin-mcp --data-dir /absolute/path/to/private-data
# 检查 .local/codex-registration/config.toml 和 plan.json 后应用
.venv/bin/python scripts/register_project_plugin.py --apply
```

注册脚本使用本项目 Python、工作目录和指定资料库，只修改项目级 `.codex/config.toml` 与 `.agents/plugins/marketplace.json`；个人安装命令另从项目外登记来源，步骤见下方。当前项目启用插件，由它提供 MCP 和四套 Skills。若原先有同路径的独立 MCP，脚本备份并移除该配置，保留网络策略和文件范围；不能仅将同名服务设为 disabled，否则会覆盖插件服务。具体配置见 [Codex 内嵌工作区](CODEX_MCP_UI.md)。

个人插件栏需要项目外可发现的来源：

```bash
cd /tmp
codex plugin marketplace add /absolute/path/to/Ruminate --json
codex plugin add ruminate@ruminate-local --json
```

安装 Python 包的用户可运行 `ruminate init` 后手动配置 stdio 服务器：

```toml
[mcp_servers.ruminate]
command = "/absolute/path/to/ruminate"
args = ["mcp", "--transport", "stdio"]
env = { RUMINATE_DATA_DIR = "/absolute/path/to/data", RUMINATE_MCP_WRITES = "1" }
```

默认 `ruminate mcp` 仍只提供原有 9 个只读工具。`RUMINATE_MCP_WRITES=1` 启用工作流工具；所有新增写工具都要求幂等键，文件读取仍受 `RUMINATE_ALLOWED_ROOTS` 约束，URL 抓取仍受 `RUMINATE_NETWORK_MODE` 约束。`off` 会拒绝联网，`ask` 需要调用者明确传 `network_allowed=true`。GUI 文件选择器可直接接收用户上传，无需授予整个目录。

源码 `plugins/ruminate/` 保留 portable Agent Plugins 格式。Codex 0.167 的接入测试未发现该布局中的 MCP，因此注册脚本自动生成 `.codex-plugin/plugin.json` 和 `.mcp.json` 兼容镜像，显式关联 MCP 配置。此镜像配合项目 Python 运行，不包含运行时及全部依赖，适用于本机安装。

`--direct-mcp` 保留为独立调试路径，会关闭项目插件并单独登记服务；插件详情使用独立 MCP 入口。四个 Skill 也能单独放入宿主支持的技能目录。

## 当前工具

基础只读工具保持兼容：`health_check`、`search_sources`、`get_source`、`get_source_page_image`、`list_visual_observations`、`get_claim`、`list_review_queue`、`get_topic_context`、`list_output_projects`。

启用工作流后增加：

- 读取：`list_topics`、`list_sources`、`get_output_revision`、`get_capture_job`。
- 保存：`create_topic`、`update_topic`、`update_source`、`ingest_url`、`ingest_local_file`、`record_claim`、`link_evidence`、`present_review_queue`、`record_review_feedback`、`create_output_project`、`create_output_draft`、`record_visual_observation`。
- 内嵌 UI：`open_workspace` 渲染工作区；`ui_read`、`ui_write` 供组件通过 MCP Apps 桥调用。共 28 个工具，其中这两个组件工具不提供给模型自动选择。

导入的幂等键持久关联一个 capture job。重复请求返回该 job 当前状态，不再次读取原文件或联网；相同键不同内容会报冲突。失败或中断的 job 可通过现有 CLI 重试。其余写入沿用知识库、反馈、输出服务的事务与审计。

## 兼容性依据

2026-11-11 已实际读取 [官方插件打包规范](https://developers.openai.com/plugins/build/plugins) 和 [本地 MCP 插件规范](https://developers.openai.com/plugins/deploy/desktop-only)。根 `plugin.json`、`skills/`、`mcp.json` 以及 stdio transport 均有当前官方格式依据；公共本地 MCP 提交仍要求 approved partner 资格。

本地 stdio 协议与四套 Skills 已实际完成导入、精确引用、草稿生成和反刍反馈。
原始宿主日志留在本地；公开的结果和复现方式见 [EVALUATION.md](EVALUATION.md)
和 [TESTING.md](TESTING.md)。

插件入口可发现 28 个工具及 global/thread 入口，原生工作台已通过人工验收。
公共插件市场分发与个人本地安装是分别验证的能力，当前提供源码及本地安装方式。
