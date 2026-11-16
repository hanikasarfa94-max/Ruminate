<p align="center">
  <img src="plugins/ruminate/assets/ruminate-light.png" width="80" alt="Ruminate">
</p>

# Ruminate

**把读过的资料，变成值得继续思考的问题和有出处的输出。**

[English](README.en.md) · [使用指南](docs/USAGE.md) · [贡献指南](CONTRIBUTING.md) · [路线图](docs/ROADMAP.md)

[![CI](https://github.com/hanikasarfa94-max/Ruminate/actions/workflows/ci.yml/badge.svg)](https://github.com/hanikasarfa94-max/Ruminate/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Ruminate 是一个本地优先的研究工作台，通过 MCP 和 Skills 接入 Codex / ChatGPT。
资料、引用、想法和输出保存在你自己的电脑上，阅读与推理由现有 AI 宿主完成。
内核无需模型 API Key、云数据库或其他知识管理软件。

当前版本 **0.5.3**，处于早期开发阶段，可从 GitHub 一条命令安装到个人插件栏。
PyPI 包及公共插件市场发布尚未提供。Codex 插件工作台已完成实际验收，
不同客户端的支持范围见[兼容性记录](docs/COMPATIBILITY.md)。

## 能做什么

- **收集资料**：导入 PDF、Markdown、TXT、静态 HTML 和公开 URL，保留原件与历史版本。
- **定位证据**：全文检索中英文内容，引用回到具体版本、页面或原文片段。
- **回看想法**：记录问题与证据关系，处理有启发、延后或略过的建议。
- **推进输出**：从已有资料生成带引用的研究提纲，保留修订并导出 Markdown。
- **在对话中继续**：工作台中的阅读内容可关联到宿主对话，复用现有聊天与视觉能力。

统一管理页默认打开“想法”，有 **想法 / 资料 / 输出** 三个入口。
自动反刍建议进入 **想法 → 待回看**；定时任务需在自己的 Codex 客户端中配置，
见[自动反刍](docs/AUTO_REFLECTION.md)。

## 安装到 Codex 插件栏

安装好 Codex 桌面客户端后，在终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/hanikasarfa94-max/Ruminate/main/install.sh | bash
```

安装器自动准备 Python 和依赖、注册插件，并检查工作台能否通过 Codex 加载。
完成后重新打开 Codex，进入 **插件 → Ruminate → 打开插件**。
不需要预装 Python、GitHub 登录或 API Key；macOS 可直接使用桌面应用自带的 CLI。

再次运行同一命令即可更新，研究资料会保留。
自定义路径、卸载及已有开发版的迁移见[安装指南](docs/INSTALL.md)。
支持 macOS / Linux；插件栏和内嵌页面需要宿主支持。

## 从源码开发或使用 CLI

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)
和支持 FTS5 的 SQLite。macOS arm64 已实测；Linux Python 3.11 / 3.13 已通过 CI。
当前文件导入依赖 POSIX，Windows 尚未支持。

```bash
git clone https://github.com/hanikasarfa94-max/Ruminate.git
cd Ruminate
uv sync --locked
uv run ruminate init
uv run ruminate doctor
```

用仓库自制材料体验导入与搜索：

```bash
uv run ruminate ingest tests/fixtures/notes.md --topic demo
uv run ruminate ingest tests/fixtures/single_column.pdf --topic demo
uv run ruminate search '证据' --topic demo
```

也可运行 `uv run python scripts/smoke_p2.py`，在临时资料库里体验从多源证据、
反刍反馈到有引用输出的完整流程；结束后自动清理测试数据。

开发中需要将插件指向当前源码时，见[开发版插件注册](docs/CODEX_MCP_UI.md)。
正式安装的工作台通过 MCP Apps 内嵌，不需要另启网页服务器。

如果只想预览页面，运行 `uv run ruminate ui`，打开终端显示的本地地址即可。
CLI 和 MCP 使用同一套应用逻辑；不支持内嵌界面的客户端仍可使用工具。

## 数据与隐私

默认数据目录在源码仓库外：macOS 为 `~/Library/Application Support/Ruminate/`，
Linux 为 `$XDG_DATA_HOME/ruminate/` 或 `~/.local/share/ruminate/`。
可用 `RUMINATE_DATA_DIR` 或 `--data-dir` 指定其他位置。

导入、检索、引用和模板输出可在本地完成。URL 导入需要显式网络授权；
发送给 Codex / ChatGPT 的片段和页图会进入当前会话上下文。
备份与恢复命令见[使用指南](docs/USAGE.md)，访问范围见[安全说明](docs/SECURITY.md)。

## 文档与开发

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/USAGE.md) / [界面操作](docs/GUI.md) | 导入、检索、反刍、输出和备份 |
| [插件安装](docs/INSTALL.md) / [开发接入](docs/CODEX_MCP_UI.md) / [Skills](docs/P3_PLUGIN.md) | Codex 接入与工作流 |
| [架构](docs/ARCHITECTURE.md) / [技术决策](docs/DECISIONS.md) | 模块、存储和证据模型 |
| [测试](docs/TESTING.md) / [验收记录](docs/EVALUATION.md) | 可复现命令与实际结果 |
| [功能范围](docs/KNOWN_LIMITATIONS.md) / [路线图](docs/ROADMAP.md) | 已有能力与后续方向 |
| [贡献指南](CONTRIBUTING.md) / [支持](SUPPORT.md) | 开发和反馈 |

```bash
uv run pytest
uv run ruff check .
uv run mypy
node --test scripts/test_ui_transport.cjs
uv build
```

JavaScript 协议测试需要 Node.js 22+；使用工作台无需安装 Node.js。
CI 运行应用测试、静态检查、协议测试及构建后的独立安装检查。

## 许可证

[Apache License 2.0](LICENSE)。版权及依赖说明见 [NOTICE](NOTICE) 和
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。导入的文献和用户研究内容保留各自的权利。

Ruminate 是独立项目，与 OpenAI 没有隶属关系。ChatGPT、Codex 等名称用于说明兼容方式。
