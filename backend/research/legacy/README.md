# Legacy Research

这里保存仍需复现、但已经退出当前研究路线的历史入口。文件已按真实职责重命名，避免
`run_opt_v2.py`、`run_walkforward.py` 这类模糊名字被误认为权威协议。

- `optimization/`：早期 7 策略网格、深度网格和收益目标迭代；包含明显样本内目标。
- `regime/`：早期 MA120 breadth、leader-index 和引擎门控回放。
- `validation/`：结构牛窗口区间复验、单笔归因和集中仓位多区间回放。
- `reporting/`：旧 leader-index 报告修复工具。

规则：

1. 只允许为历史可复现性修 bug，不在这里开发新实验。
2. 不得把 legacy 输出写入 `artifacts/current/`。
3. 新研究必须回到 `research/<domain>/`，使用描述性文件名和版本后缀。
4. legacy 结果不得晋级生产或改写当前结论。

## 旧命令兼容

原模块路径暂时保留为只调用本目录 `main()` 的薄兼容入口。新代码和文档必须使用以下
描述性路径；兼容入口不得添加研究逻辑：

| 旧模块 | 新模块 |
|---|---|
| `research.optimization.run_iterate` | `research.legacy.optimization.run_structural_bull_return_target_iteration` |
| `research.optimization.run_opt_grid` | `research.legacy.optimization.run_strategy_parameter_grid` |
| `research.optimization.run_opt_v2` | `research.legacy.optimization.run_strategy_deep_grid_walkforward` |
| `research.optimization.run_optimizations` | `research.legacy.optimization.run_strategy_optimization_baseline` |
| `research.regime.run_engine_regime` | `research.legacy.regime.run_leader_index_engine_gate_replay` |
| `research.regime.run_engine_soft` | `research.legacy.regime.run_leader_index_soft_exposure_replay` |
| `research.regime.run_leader_regime` | `research.legacy.regime.run_leader_index_regime_replay` |
| `research.regime.run_regime` | `research.legacy.regime.run_market_breadth_ma120_replay` |
| `research.reporting.regen_leader_report` | `research.legacy.reporting.regenerate_leader_index_report` |
| `research.validation.run_one_trade_detail` | `research.legacy.validation.run_pullback_trade_detail` |
| `research.validation.run_range_bt` | `research.legacy.validation.run_structural_bull_range_replay` |
| `research.validation.run_verify_period` | `research.legacy.validation.run_structural_bull_trade_attribution` |
| `research.validation.run_walkforward` | `research.legacy.validation.run_concentrated_pullback_multiperiod_replay` |
