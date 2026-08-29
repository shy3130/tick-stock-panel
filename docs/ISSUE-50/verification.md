# Issue #50 验证

- 六项研究与共享适配层定向回归：`136 passed`。
- 负面排除专项覆盖 V2/V4/V5、V1/V3 unavailable、PIT available-date、MA20 warmup、前窗不含当日、combined OR 删失传播、非重叠 horizon cohort、T+1、portfolio 指标、missed rebound/avoided decline 与 API JSON contract。
- 新增回归确认删失行不进入单类或 combined 组合收益，且无 active 的部分删失不能被误作 inactive。
- Ruff changed-scope `--select F,E9`：All checks passed。
- 完整后端：`3686 passed, 3 skipped, 8 warnings`（132.55s）。
- 仅研究结论；`promoted=false`，未接默认池、short_pool、Agent 或交易链。
