# 开发记录

面向使用者的变化见 [CHANGELOG.md](../CHANGELOG.md)。这里保留实现阶段及验收入口；
私人工作记录、原始对话和真实资料验收附件留在本地。

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| P0 | Python、SQLite、CLI、stdio MCP 骨架 | 本地初始化及真实 MCP 调用 |
| P1 | 资料导入、去重、版本、定位和检索 | 合成材料与公开来源、引用重放 |
| P1 视觉 | 本地页图渲染、宿主识别、独立视觉记录 | 自制扫描页与真实宿主调用 |
| P2 | 认知关系、反刍反馈、引用草稿与备份恢复 | 离线完整流程和独立安装 |
| P3 | 四套 Skills、MCP Apps、插件工作台 | 协议测试及原生入口人工验收 |
| P4/P5 | 统一管理页、待回看、控件、对话上下文及品牌图标 | 浏览器流程、桥接及应用回归 |
| 公开源码准备 | Apache 2.0、双语入口、贡献说明、CI 与隐私检查 | EVALUATION.md 与 Actions |

应用服务的当前结构见 [ARCHITECTURE.md](ARCHITECTURE.md)，数据及宿主限制见
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。发布前的真实测试记录集中在
[EVALUATION.md](EVALUATION.md)，避免把历史基线当成当前检查。
