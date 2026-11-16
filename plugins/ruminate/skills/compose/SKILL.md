---
name: compose
description: 将 Ruminate 中的研究主张和原文证据整理为研究笔记、综述、研究计划、产品规格或文章提纲，保留可回看的引用。
---

先用 `get_topic_context` 和 `get_claim` 建立本次输出的主张与证据清单。资料观点、不同意见、假设和待回答问题各自保留身份；引用必须来自已经保存的原文块。

用 `list_output_projects` 找到要继续的项目，或用 `create_output_project` 创建用户要求的项目，填写目标和读者。`kind` 可选 `research_note`、`literature_review`、`research_proposal`、`product_spec`、`article`。

用 `create_output_draft` 传入选定的 `claim_ids`，生成带引用的本地提纲；再用 `get_output_revision` 阅读正文、证据快照和引用检查结果。每次写入使用独立 `request_id`，同一次重试复用它。需要调整选材时生成新修订，保留旧版。

本地工具负责整理和保存提纲，当前宿主可以在对话中继续组织叙述和展开论证。说明哪些文字已经保存、哪些还是对话中的扩写。交付时优先呈现论点和引用，把内部 ID 留在引用详情。用户要导出时使用 GUI 下载，或 CLI `ruminate outputs export PROJECT_ID --format md`。导出文件保存在本地。
