# 安装到 Codex 插件栏

先安装支持插件的 Codex 桌面客户端，然后在终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/hanikasarfa94-max/Ruminate/main/install.sh | bash
```

完成后重新打开 Codex，进入 **插件 → Ruminate → 打开插件**。
也可以在新对话中说「打开 Ruminate 工作台」。首页是“想法”，可以直接添加资料。

支持 macOS 和 Linux。本地文件处理暂不支持 Windows；Linux 的内核安装和自动化测试
由 CI 验证，插件栏需要宿主本身支持桌面插件。网页端不能启动本机的 stdio 服务。

## 安装器会完成什么

1. 查找 Codex 命令；macOS 也会查找桌面应用自带的 CLI，不要求手动设置 PATH。
2. 从 GitHub 下载固定版本源码，准备 uv、Python 3.11 和锁定的应用依赖。
3. 初始化资料库，生成本机插件配置，通过 Codex 官方命令添加来源并安装插件。
4. 通过真实 Codex app-server 检查插件归属、工作台入口及 MCP Apps 页面资源。

不需要 sudo、GitHub 登录、预装 Python 或模型 API Key。下载依赖需要联网；
安装后 MCP 直接运行本机 Python，打开工作台不会再次下载或解析依赖。
安装器不修改 shell 启动文件，不创建后台定时任务。

安装结果保存在安装目录的 `installation.json`，包括版本、资料路径和协议检查结果。
程序与资料分开存储：

| 平台 | 程序与插件来源 | 研究资料 |
| --- | --- | --- |
| macOS | `~/Library/Application Support/RuminatePlugin/` | `~/Library/Application Support/Ruminate/` |
| Linux | `$XDG_DATA_HOME/ruminate-plugin/` | `$XDG_DATA_HOME/ruminate/` |

Linux 未设置 `XDG_DATA_HOME` 时使用 `~/.local/share`。
Codex 另外管理自己的插件缓存。安装器生成的绝对路径只保存在本机。

## 更新与卸载

**更新：再次执行同一条安装命令。** 新版运行环境安装并检查后再替换插件配置，沿用资料库、
已有网络和文件访问设置。旧运行环境暂时保留，失败时恢复上一份插件配置。
资料库迁移不会随插件回退；升级前可按[使用指南](USAGE.md)备份。
更新后关闭旧工作台并重新启动客户端。

**卸载：在 Codex 的插件管理中移除 Ruminate。** 如已将 CLI 加入 PATH，也可执行：

```bash
codex plugin remove ruminate@ruminate
codex plugin marketplace remove ruminate
```

卸载插件保留研究资料。确认不再使用后可手动删除上表中的程序目录。

## 先查看脚本或指定路径

```bash
curl -fsSL https://raw.githubusercontent.com/hanikasarfa94-max/Ruminate/main/install.sh -o install-ruminate.sh
# 阅读 install-ruminate.sh 后执行；省略参数即使用默认路径
bash install-ruminate.sh --data-dir "$HOME/Documents/Ruminate Research"
```

可选参数：

| 参数 | 用途 |
| --- | --- |
| `--data-dir DIR` | 首次安装指定资料库；后续安装自动沿用 |
| `--install-dir DIR` | 指定程序目录，放在资料库及源码项目之外 |
| `--codex FILE` | 使用指定的 Codex CLI，例如不在标准位置的桌面应用 |
| `--uv FILE` | 使用指定的 uv |
| `--source DIR` | 从已经下载的源码目录安装，仍自动准备运行环境 |

已有仓库的用户只需执行 `bash install.sh --source "$PWD"`。
环境变量 `RUMINATE_REF` 可选择 GitHub 标签或提交；默认版本固定在安装脚本中。
新安装不会自动导入示例、读取个人文件或设置反刍计划；[定时反刍](AUTO_REFLECTION.md)
可以在工作台可用后按自己的习惯配置。

## 常见问题

**找不到 Codex / 当前版本不支持插件**：更新桌面客户端。安装器支持 PATH 上的 `codex`，
也识别 macOS 标准应用位置；自定义位置用 `--codex` 指定。

**已有开发版 Ruminate 或独立 MCP**：继续使用原来的[开发版更新流程](CODEX_MCP_UI.md)，
或先在 Codex 中停用旧插件／移除独立 MCP 配置，再以 `--data-dir` 指向原资料库安装。
安装器不会覆盖同名服务或自动切换到空资料库。开发项目自己的 `.codex/config.toml`
仍可能覆盖全局插件；请在项目之外的新对话验证。

**安装中断**：恢复网络后重试。只有 Codex 已启用插件且能够读取工作台资源时，安装器才会
报告成功。若协议检查失败，先更新或重新启动 Codex，再重试；错误信息会标出失败步骤。

**插件已安装，页面没有刷新**：关闭旧工作台，退出并重新启动客户端；再从插件栏打开。
协议检查确认的是入口和资源传输，具体渲染仍取决于客户端版本。

## 分发方式

这是从 GitHub 下载、由 Codex 登记的个人本地插件来源，名称为 `ruminate`，插件 ID 为
`ruminate@ruminate`。目前没有发布到 OpenAI 公共插件目录或 PyPI。
Codex 的 GitHub marketplace 命令本身不负责安装本地 Python 运行环境，所以首次安装使用
上面的统一命令。[官方插件打包与 marketplace 说明](https://developers.openai.com/plugins/build/plugins)

uv 通过固定版本的[官方安装器](https://docs.astral.sh/uv/getting-started/installation/)
获取；仅在找不到已有 uv 时安装到程序目录。[uv 安装路径选项](https://docs.astral.sh/uv/reference/installer/)
