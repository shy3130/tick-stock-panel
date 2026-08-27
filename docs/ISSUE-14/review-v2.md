# 方案 v2 Review

二审结论：**当前只允许交付显式 fail-closed 契约，禁止任何事件/OOS 输出。**

1. P1：`get_enriched_range` overlay、current universe 和日线近似不得作为数据源。generation-pinned canonical reader、PIT eligible-universe 快照（含 `as_of`/hash）与版本化交易所 calendar 是硬前置；任一缺失时整份评估返回结构化 `unavailable`，不降级、不猜口径。
2. P2：事件状态机（双 P90 放量 → 3–15 市场日箱体冻结 → 上/下突破确认）与 OOS walk-forward（purge/embargo/cluster）本次不实现。即使前置能力齐备，状态仍保持 `unavailable` 并携带未实现原因，不编造事件、基线或效果结论。
3. P3：响应固定为 factor/status/unavailable_reasons/request/capabilities/parameters/provenance/coverage/events/clusters/censored/note；字段键禁止交易语义。不接 short_pool、不进 Agent、不改 trading、不提供交易建议。

契约参数已冻结：放量事件日前严格 20 个有效市场日、raw volume 与 amount 各自 P90 且同时满足、3–15 日整理窗口、箱体宽度不超过 12%、`up_breakout`/`down_breakout`、forward horizons 1/5/10/20。当前实现只声明能力与未实现状态，不声明事件、OOS 或生产 reader 可用。
