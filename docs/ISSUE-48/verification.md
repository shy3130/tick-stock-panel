# Issue #48 验证

- 六项研究与共享适配层定向回归：`136 passed`。
- 逃顶专项覆盖 S1/S8/S9 边界、available date/session、已有持仓 gate、S9 同日开盘、S1/S8 下一日开盘、horizon 终点及 capability API。
- 测试确认 S2-S7/S10 在分钟 capability 缺失时 fail-closed，且日线 evaluator 不输出无冻结基线的胜负 verdict。
- Ruff changed-scope `--select F,E9`：All checks passed。
- 完整后端：`3686 passed, 3 skipped, 8 warnings`（132.55s）。
- 输出只有风险计数/统计，不含交易方向、订单或自动执行动作。
