# Issue #49 验证

- 六项研究与共享适配层定向回归：`140 passed`。
- N 字专项覆盖 causal zigzag、固定百分比/ATR 阈值、A/B/C、黄金分割组合、未确认尾段、结构破坏、T+1、5/10/20、市场日历 60/20/20、split-end outcome censor、placebo、边界/zigzag 敏感性与 API JSON contract。
- 真实 pinned smoke：`canonical:20260829T002957-4b1bfcad|markets:20260829T000704`，标的 `000001.SZ/000002.SZ/000004.SZ`，返回 `status=ok, events=0, promoted=false`；零事件只证明生产读链可运行，不作为因子结论。
- Ruff changed-scope `--select F,E9`：All checks passed。
- 完整后端：`3690 passed, 3 skipped, 8 warnings`（138.55s）。
