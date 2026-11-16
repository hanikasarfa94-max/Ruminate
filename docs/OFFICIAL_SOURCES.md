# 技术参考

这些文档解释协议、工具和依赖；Ruminate 的实际验证结果见
[COMPATIBILITY.md](COMPATIBILITY.md)。参考文档更新不自动代表当前客户端已支持。

## 宿主与协议

- [OpenAI 插件架构](https://developers.openai.com/plugins/concepts/plugins)
- [插件打包](https://developers.openai.com/plugins/build/plugins)
- [MCP 服务](https://developers.openai.com/plugins/build/mcp-server)
- [MCP Apps UI](https://developers.openai.com/plugins/build/chatgpt-ui)
- [桌面本地插件发布](https://developers.openai.com/plugins/deploy/desktop-only)
- [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex 定时任务](https://learn.chatgpt.com/docs/automations)
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)
- [OpenAI MCP 扩展](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md)

## 存储与解析

- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [pypdf](https://pypdf.readthedocs.io/)
- [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/readme.html)
- [Pillow](https://pillow.readthedocs.io/)
- [Trafilatura](https://trafilatura.readthedocs.io/)

版本由 `uv.lock` 固定；依赖许可说明见
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。项目许可证为
[Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)。

## 仓库组织参考

[Pi](https://github.com/earendil-works/pi) 的贡献指南、安全政策、开发说明和 CI
为仓库结构提供参考。Ruminate 未复制 Pi 的实现，也不依赖 Pi 运行。
