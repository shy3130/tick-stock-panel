"""示例 3 — 策略与回测 (scope: read:analysis + run:backtest)。

列出策略清单 → 用纯 Polars 引擎跑一次策略回测 (所有安装自带, 无需 vectorbt)
→ 读关键指标。GET /api/backtest/status 可查历史任务;
POST /api/backtest/run 是 vectorbt 信号回测, 需 `uv sync --extra backtest`。
"""
from datetime import date, timedelta

from common import call

# 1. 策略清单 (read:analysis)
strategies = call("GET", "/api/strategies")["strategies"]
for s in strategies[:5]:
    print(f"策略: {s['id']}  {s['name']}  [{','.join(s.get('tags', []))}]")
if not strategies:
    raise SystemExit("(无策略)")

# 2. 策略回测 (run:backtest): 第一个策略, 近 90 天, 限定单标的控制耗时
sid = strategies[0]["id"]
result = call("POST", "/api/backtest/strategy/run", {
    "strategy_id": sid,
    "symbols": ["600519.SH"],
    "start": (date.today() - timedelta(days=90)).isoformat(),
    "end": date.today().isoformat(),
})
print(f"回测完成 ({sid}):", {k: v for k, v in result.items() if k in (
    "total_return", "annual_return", "max_drawdown", "trades", "win_rate", "sharpe",
)})
