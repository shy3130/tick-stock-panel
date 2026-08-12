# Optimization

训练区间内的参数、仓位和退出研究。这里的结果默认都是历史 replay，不是 fresh OOS。

当前入口：

- `run_core_strategy_walkforward_v1.py`：核心策略参数。
- `run_core_portfolio_walkforward_v1.py`：持仓数量与分配。
- `run_core_exit_walkforward_v1.py`：退出与风控。
- `run_bullish_breadth_walkforward_v1.py`：市场宽度保护。
- `run_structural_bull_challenge_v1.py`：已污染目标窗口的挑战审计。

早期 `opt/grid/iterate/v2` 脚本已移入 `research/legacy/optimization/`，禁止作为当前入口。
