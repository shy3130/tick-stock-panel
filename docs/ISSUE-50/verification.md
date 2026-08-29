# Issue #50 验证

- 六项研究与共享适配层定向回归：`140 passed`。
- 负面排除专项覆盖 V2/V4/V5、V1/V3 unavailable、PIT available-date、MA20 warmup、前窗不含当日、combined OR 删失传播、非重叠 horizon cohort、T+1、portfolio 指标、missed rebound/avoided decline 与 API JSON contract。
- 回归确认删失行不进入单类或 combined 组合收益、无 active 的部分删失不能被误作 inactive，且全部 OOS 行删失时返回保留 provenance/coverage 的 unavailable envelope 而非 500。
- 真实 pinned smoke：`canonical:20260829T002957-4b1bfcad|markets:20260829T000704|universe:20260829T020332Z-6e648967c37e6739`，标的 `000001.SZ/000002.SZ/000004.SZ`（2024-01-01..2026-06-30，OOS 2025-01-01，V2/V4/V5，horizon=10，20bps），返回 `status=ok, promoted=false, observations=72, rebalance_days=36`，删失仅 `canonical_symbol_empty=1`；三类 verdict 均 `unavailable_insufficient_samples`（active_days<30），只证明生产读链真实可用，不作为因子结论。执行环境注记：worktree `.venv` 为空壳且 `uv run/sync` 会因 hatchling 拒绝 `../README.md` 构建失败，本次用主检出 venv + `PYTHONPATH` 指向 worktree backend（代码即 PR head `8ba9d95`）执行；pyproject readme 路径问题另行修复。
- Ruff changed-scope `--select F,E9`：All checks passed。
- 完整后端：`3690 passed, 3 skipped, 8 warnings`（138.55s）。
- 仅研究结论；`promoted=false`，未接默认池、short_pool、Agent 或交易链。
