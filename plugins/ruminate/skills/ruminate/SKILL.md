---
name: ruminate
description: 从 Ruminate 的待反刍问题开始一次有证据的讨论，结合用户反馈决定继续研究、稍后再看或放下。
---

先用 `list_review_queue` 预览；准备主动推送卡片时调用 `present_review_queue`，它负责当天的展示数量和重复间隔。不要绕过它批量推送队列。

围绕一张卡片说清三件事：什么值得重新想、为什么现在值得看、哪些原文让这个问题成立。需要细节时用 `get_claim` 和 `get_source` 阅读引用。只有记录中确有用户确认观点，才用“你之前认为”；其余直接作为资料观点或一个问题来讨论。

根据用户的实际回复调用 `record_review_feedback`：有用 `useful`、需要更多证据 `needs_more_evidence`、继续讨论 `discuss`、稍后再想 `postpone`、放下 `dismiss`，或用接口中的其他对应动作。不要根据沉默猜测反馈。每次写入生成 `request_id`，同一次重试复用它。

讨论形成新问题或假设时，可以用 `record_claim` 保存；明确保留其类型和来源。队列为空就说明目前没有待展示卡片，可以围绕用户选择的主题用 `get_topic_context` 继续。需要从新资料生成卡片时，可使用 CLI `ruminate tick --topic TOPIC`。

用户主动打开管理页时，在“想法 → 待回看”查找建议；该列表展示全部到期事项，不消耗主动推送次数。自动反刍生成后先保存问题与精确引用，再运行 tick，界面会读取这些记录。AI 提出的内容保持 host_model 来源；只依据用户实际点击或明确回复保存反馈。
