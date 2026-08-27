# 最终设计

## 定稿结论

`n_shape_golden_phoenix_v1` 作为独立、只读、默认关闭的日线事件研究因子实施。它不改变现有短线池，也不进入 Agent 工具。评估 API 首先验证 generation-pinned sealed reader 与 PIT 制度/ST 数据能力；任一不存在则返回 `unavailable`，从而把当前数据能力缺口显式化。

## 实现边界

- 读取边界：唯一允许的 repository generation-pinned canonical reader；禁止 `get_enriched_range` 合并 overlay 作为替代。
- 计算边界：事件状态机在内存中按 symbol/date 运行，raw 字段统一价格尺度；窗口按固定市场交易日集合校验。
- 输出边界：结构化证据、删失原因、coverage、provenance、forward 描述统计；reachability 明确 `daily_price_only`。
- 评估边界：同一日线价格定义的首板基准；重叠样本不使用独立 bootstrap；未达样本/OOS 增量门槛标记 `rejected`。
- 产品边界：前端只展示 API 原值；Agent 只解释证据；不产生订单、方向、目标价或止损语义。

## 交付与验证

子代理在 `issue-8-n-shape-golden-phoenix` worktree 实现代码与夹具测试。主会话读取全部 diff，运行完整相关测试模块，再派 coding reviewer；修复 review 问题后重新运行完整模块。只有验证通过才提交、推送并创建 PR，Issue 评论和本目录 `verification.md` 记录真实输出。

## 生产化状态更新（2026-08-27）

generation-pinned canonical reader、manifest 字节哈希和事件评估层已实现。剩余唯一硬门禁是历史 PIT ST/制度证据：板块制度日期可编码，但本地 ST 变更数据不构成完整时点序列，禁止据当前名称或派生涨停信号回填。因此当前生产调用仍诚实返回 unavailable；注入完整 PIT provider 的夹具路径已可运行。
