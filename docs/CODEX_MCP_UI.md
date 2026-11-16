# Codex 内嵌工作台

Ruminate 从插件栏或对话中的 `open_workspace` 打开。页面通过 MCP Apps 读写本地资料库，CLI、插件和自动反刍使用同一套应用服务。

当前版本为 0.5.2。界面资源为 `ui://ruminate/workspace-v0.5.1.html`，MIME 类型为 `text/html;profile=mcp-app`。资源版本表示前端资源的缓存键，可与仅更新图标的插件版本不同。

## 安装到个人插件栏

在项目目录安装依赖，并生成本机插件配置：

```bash
uv sync --locked
uv run python scripts/register_project_plugin.py --plugin-mcp --data-dir /absolute/path/to/private-data
# 查看 .local/codex-registration/ 中生成的配置后应用
uv run python scripts/register_project_plugin.py --apply
```

脚本生成 `.local/codex-plugin/ruminate/` 插件镜像，配置本项目的 Python、工作目录和资料库路径。项目级注册文件为 `.codex/config.toml` 和 `.agents/plugins/marketplace.json`。这些文件保留在本机，由 Git 忽略。

随后在项目外登记插件来源并安装：

```bash
cd /tmp
codex plugin marketplace add /absolute/path/to/Ruminate --json
codex plugin add ruminate@ruminate-local --json
```

打开客户端插件列表中的 Ruminate，选择“打开插件”。默认首页为“想法”，资料、引用和输出在同一工作台阅读。使用说明见 [GUI.md](GUI.md)。

已有同名独立 MCP 配置时，注册脚本核对程序和资料库路径，备份后迁入插件，同时保留网络策略和文件范围。`--direct-mcp` 用于独立调试，会改为单独登记 MCP 服务；全局插件入口使用 `--plugin-mcp` 模式。

## 更新与刷新

更新源码和依赖后，重新生成插件镜像并执行 `codex plugin add ruminate@ruminate-local --json`。关闭旧工作台，再从插件列表打开。客户端保留旧 MCP 进程时，退出并重新启动客户端以加载新版。

公开 app-server 方法 `config/mcpServer/reload` 可用于独立协议测试；刷新行为取决于客户端的会话管理。[官方协议](https://learn.chatgpt.com/docs/app-server)

## 插件包与界面能力

源码 `plugins/ruminate/` 使用 Agent Plugins 布局。注册脚本生成带 `.codex-plugin/plugin.json`、`.mcp.json` 的 Codex 兼容镜像，并复制四套 Skills 和品牌图标。图标同时覆盖插件列表、对话入口以及 MCP 的 `serverInfo.icons`，支持深浅色主题。

`open_workspace` 声明 `global` 与 `thread` 入口，分别用于插件工作台和对话侧面板。页面经 `ui/initialize` 获取客户端能力，再通过 `tools/call` 读取或保存内容。`ui_read`、`ui_write` 为组件专用工具。

当前阅读内容通过 `ui/update-model-context` 关联到工作台对应的聊天。支持 `openai/message` 扩展时，“继续聊这条”填入可编辑草稿，由用户发送；其他客户端根据可用能力提供讨论入口。具体协议见 [OpenAI 扩展说明](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md)。

## 验证方式

读取已安装插件的工具、入口与界面资源：

```bash
uv run python scripts/probe_codex_mcp_ui.py --installed-plugin --output .local/plugin-protocol.json
```

探针从临时目录启动独立 Codex app-server，检查插件归属、global/thread 入口、资源 MIME、CSP 和完整 HTML。输出保存在本地，实际支持范围见 [COMPATIBILITY.md](COMPATIBILITY.md)。

界面交互使用隔离 iframe 连接真实 stdio MCP，覆盖资料导入、引用定位、反刍反馈、输出、下拉菜单、窄窗口和对话上下文。[交互记录](evidence/browser-workflow.json)

原生桌面的工作台和插件入口已通过人工验收；当前阅读上下文也已进入原生对话。草稿填入和完整点击流程目前由协议测试覆盖。按能力区分的结果见 [兼容性记录](COMPATIBILITY.md)。
