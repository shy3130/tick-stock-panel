# Issue #50 最终设计

服务入口：`app.services.negative_exclusion_production.evaluate_negative_exclusion_production`；能力 API 为 `GET /api/research/negative-exclusion`，评估 API 为 `POST /api/research/factors/negative-exclusion/evaluate`。

## 固定定义

- V1：`unavailable_definition_unverified`。
- V2：同日 canonical PIT `is_st` active；这是本地 sealed markets 对风险警示/ST 的唯一规范字段，不再推断第二个 `risk_warning` 标志；缺事实删失。
- V3：`unavailable_no_pit_announcement_source`。
- V4：MA5<MA10<MA20、三线一阶斜率<0、close<MA20 连续五日。
- V5：距 60 日收盘高回撤≥30%、close 跌破前 20 日最低 raw low、当日 volume≥前 20 日均量×2；三条件同时成立。

所有检测器 prefix-closed。生产层只评估 PIT presence 可证明的 OOS symbol-day，并按 horizon 取互不重叠的再平衡 cohort，避免把重叠 forward label 复利成伪 NAV；信号日后下一市场日开盘进入 forward 观察，缺未来 bar 删失。V1/V3 不可在 `enabled_classes` 中启用。
每类与 `all_available` 分别报告 coverage、删失、错过反弹、规避下跌、等权组合总收益/年化收益/Sharpe/MaxDD 及相对未过滤池增量。全部排除的日期按现金 0 收益。至少 30 个 active 样本且整体、前半、后半净收益均为正才 `accepted`；否则 `unavailable_insufficient_samples` 或 `rejected`。`promoted=false`。
