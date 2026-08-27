# 最终设计

## 定稿结论

`n_shape_golden_phoenix_v1` 作为独立、只读、默认关闭的日线事件研究因子实施。它不改变现有短线池，也不进入 Agent 工具。评估 API 必须同时固定 canonical OHLCV generation 与 fstore markets PIT 事实 generation；任一来源、manifest 身份、历史名称、日期有效制度或 exact `ztj` 缺失时返回 `unavailable` 或按日期删失。

## 实现边界

- 读取边界：canonical generation 只提供 sealed raw OHLCV 与市场日历；fstore markets generation 只提供历史 universe、同日历史名称和 exact `ztj`。禁止 `get_enriched_range` 合并 overlay，也禁止由当前名称或收盘价反推历史制度。
- 身份边界：一次调用固定两份 generation 与 manifest 字节 SHA-256，复合 reader 暴露两份 source provenance；构造后不跟随任一 `current`。
- 制度边界：历史名称确定当日 ST 标记；symbol/date 确定 `main_10`、`star_20`、`chinext_20` 或 `beijing_30`，ST 优先为 `st_5`；制度、名称、`ztj` 任一缺失即删失。
- 计算边界：事件状态机在内存中按 symbol/date 运行，raw 字段统一价格尺度；窗口按固定市场交易日集合校验。首板 `raw_open == raw_high` 仅作一字板形态排除，不宣称成交可达。
- 输出边界：结构化证据、删失原因、coverage、双源 provenance、forward 描述统计；reachability 明确 `daily_price_only`。
- 评估边界：同一日线价格定义的首板基准；重叠样本不使用独立 bootstrap；未达样本/OOS 增量门槛标记 `rejected`。
- 产品边界：前端只展示 API 原值；Agent 只解释证据；不产生订单、方向、目标价或止损语义。

## 交付与验证

实现位于 `issue-8-pit-daily-facts` worktree。主会话读取全部 diff、用真实 published generation 冒烟运行评估路径并执行完整相关测试，再派独立 coding reviewer；修复 review 问题后重新验证。只有验证通过才提交、推送并创建 PR，Issue 评论和本目录 `verification.md` 记录真实输出。

## 生产化状态更新（2026-08-27）

原先的 PIT 硬门禁已经由 published `daily_markets` 历史事实补齐：该表按 `trade_date` 保存历史 `name` 与 source exact `ztj`，不是当前名称回填，也不使用 `updated_at` 冒充历史可见时间。生产 composite reader 将 canonical `20260827T054651-63f500a4` 与运行时固定的 markets generation 绑定；缺名称、制度或 `ztj` 的 symbol-day 保持删失。此更新只解除数据能力 `unavailable`，不改变研究 verdict 门槛，也不表示因子已经获得样本外准入。
