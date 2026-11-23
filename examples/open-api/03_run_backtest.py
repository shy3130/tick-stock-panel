"""示例 3 — 策略与回测 (scope: read:analysis + run:backtest)。

列出策略清单，用内置 Tivelo 引擎运行一次策略回测并读取关键指标。
POST /api/backtest/run 提供同一引擎支持的信号回测。
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
