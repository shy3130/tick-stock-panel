# Issue #50 编码复核

实现由纯检测/聚合模块 `negative_exclusion.py`、pinned production 编排 `negative_exclusion_production.py` 与研究 API 组成。

独立 review 指出三处 fail-closed 缺口，均已修正：

1. V2 契约收敛到 sealed markets 唯一规范的 PIT `is_st` 风险警示/ST 字段，不再把同一值伪装成第二个 `risk_warning`；缺事实删失。
2. combined OR 在“无 active 且任一 component censored”时整体删失；已有 active 时可证明应排除。
3. 单类与 combined 的 portfolio 指标只消费 evaluable rows，删失行不再被当作 inactive 持仓。

另已核对 V4 MA20 warmup、V5 前窗不含当日、非重叠 horizon cohort、T+1 open、总收益/年化/Sharpe/MaxDD、对称 missed/avoided 统计与 `promoted=false`。V1/V3 只有 capability，不接受启用或伪信号。
