# 测试指南

安装开发依赖：`uv sync --locked`。应用测试使用临时存储、自制材料及受控网络替身，
不需要模型 API Key 或个人资料库。

## 日常检查

```bash
uv run pytest
uv run ruff check .
uv run mypy
node --test scripts/test_ui_transport.cjs
uv build
```

JavaScript 测试使用 Node.js 内置 test runner，需要 Node.js 22+。
CI 在 Linux Python 3.11 / 3.13 和 macOS Python 3.11 上执行上述检查及 wheel 安装测试。
本地检查通过与远程 CI 通过分别记录在 [EVALUATION.md](EVALUATION.md)。

## 独立安装

```bash
uv run python scripts/smoke_wheel.py dist/ruminate_local-0.5.3-py3-none-any.whl
```

该脚本在源码目录外建立临时环境，安装 wheel，检查导入、检索、PDF 页图、认知输出、
备份恢复、HTTP 与真实 stdio MCP。依赖安装可能访问包源，应用流程使用离线合成材料。
安装默认等待 300 秒；慢网络可加 `--install-timeout 600`。这不改变应用自身的请求限额。

## 插件安装器

`tests/test_installer.py` 覆盖路径含空格、默认参数、旧版 Codex 拒绝、重复安装、
本机数据隔离、已有权限保留、服务冲突及安装失败后的恢复。
`bash -n install.sh` 检查入口脚本语法。macOS 自带 Bash 3.2 也在验证范围内。

真实安装检查使用临时 Codex 配置、独立程序目录与合成资料，执行
`bash install.sh --source "$PWD"` 并指定测试用程序和资料路径。
随后通过 Codex app-server 检查新插件，导入合成材料，再次安装并核对资料仍可检索。
测试进程通过自己的 `CODEX_HOME` 指向临时配置，不修改现有客户端配置。
结果记录在 [EVALUATION.md](EVALUATION.md)；Linux CI 中的安装器单元测试不代表
对 Linux 原生插件栏的实际渲染验收。

可以用真实 Codex CLI 重跑上述流程，无需登录或模型调用：

```bash
uv run python scripts/smoke_install.py --codex /path/to/codex --output .local/install-check.json
# 标签发布后，增加 --github 验证 GitHub 下载入口
```

它还验证卸载插件后研究资料依然可用；整个测试使用临时目录，结束后自动清理。

## 浏览器工作流

需要 Node.js、Playwright 和 Chromium。开发依赖可安装到 Git 忽略的本地目录：

```bash
npm install --prefix .local/ui-test --no-save playwright
.local/ui-test/node_modules/.bin/playwright install chromium
uv run python scripts/serve_mcp_ui_harness.py --port 8767
```

在另一终端执行：

```bash
RUMINATE_PLAYWRIGHT_MODULE="$PWD/.local/ui-test/node_modules/playwright" node scripts/smoke_mcp_ui.cjs http://127.0.0.1:8767 .local/browser-results
```

宿主脚本使用隔离的临时资料库，经真实 stdio MCP 连接 iframe。测试完成后停止宿主进程。
结果覆盖上传、引用、建议、反馈、输出、窄窗口、主题和对话上下文。
这是协议验收，不是原生 Codex 桌面自动点击。

## 真实宿主检查

真实宿主检查需要自己的 Codex 安装与登录，单独按需运行：

```bash
uv run python scripts/smoke_codex.py --workflow retrieval
uv run python scripts/smoke_codex.py --workflow vision
uv run python scripts/smoke_codex.py --workflow cognition
```

每项使用自制材料，核对实际工具事件与引用。原始宿主日志保留在本地，公开结果只记录
必要的聚合信息。脚本参数以 `--help` 为准。

## 公开内容检查

```bash
git diff --cached --check
uv run python scripts/check_public_tree.py
```

检查读取 Git index 中实际将提交的内容，避免把忽略目录里的个人文件混入发布。
它检查常见凭据格式、个人绝对路径、禁止的文件类型和本地目录；人工仍需审阅新增素材。
