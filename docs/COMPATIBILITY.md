# 兼容性记录

版本 0.5.2 / schema 5，更新于 2026-11-16。下面按实际验证范围记录结果。
完整原生界面操作与协议测试分别列出；客户端版本变化后需要重新检查。

| 能力 | 当前验证结果 |
| --- | --- |
| macOS arm64 / Python 3.11 | 应用、CLI、MCP、备份恢复及构建检查通过 |
| Linux / Python 3.11、3.13 | 应用、静态检查、桥接测试、构建及独立 wheel 安装已通过 GitHub Actions |
| Windows 文件导入 | 暂不支持，安全文件访问依赖 POSIX API |
| SQLite FTS5 | 已验证中英文检索；中文使用单字/双字 n-gram 索引 |
| PDF / Markdown / TXT / 静态 HTML | 合成材料已覆盖导入、去重、版本及原文定位 |
| 公开 URL | 已完成实际抓取与引用定位；登录和动态页面不在当前范围 |
| Codex stdio MCP | 实际调用已验证检索、原文、图片、认知与输出工作流 |
| 四套 Skills | 实际 Codex 工具调用已完成收集、反刍、研究、写作流程 |
| 插件栏打开工作台 | 已通过人工验收 |
| 当前阅读内容关联到宿主对话 | 原生对话已收到选中内容；可编辑草稿以协议测试为验证范围 |
| MCP Apps 全流程交互 | 隔离 iframe + 真实 stdio MCP 验证通过，见浏览器报告 |
| 完整原生桌面自动点击 | 尚未执行 |
| Codex 定时反刍 | 手动执行与配置路径已验证；定时运行依赖使用者自己的任务设置 |
| ChatGPT Web、自定义 MCP Tunnel、SIWC | 尚未完成 Ruminate 实际验收 |
| 独立图片、DOCX、语义检索 | 尚未实现 |

Codex 集成记录包含 CLI 0.167.0-alpha.3.2 与 app-server 0.169.0-alpha.11.1。
这些是验证过的版本，不是所有未来客户端的支持承诺。插件资源使用 MCP Apps
协议和能力协商；普通 stdio 客户端仍可读取结构化工具结果。

Python 需要 3.11+；当前锁定 MCP SDK 2.3.0。独立安装与运行范围见
[测试记录](EVALUATION.md)，配置方式见[插件安装](CODEX_MCP_UI.md)。

## 复现

```bash
uv run ruminate doctor
uv run pytest
uv run python scripts/smoke_mcp.py
```

真实宿主测试的命令及环境要求见 [TESTING.md](TESTING.md)。本地资料、账户配置和
原始宿主日志不作为公开验收附件。只发布合成测试和聚合结果。
