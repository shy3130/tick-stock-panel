"""示例 3 — 策略与回测 (scope: read:analysis + run:backtest)。

列出策略清单 → 对单标的跑一次信号回测 → 读关键指标。
可用信号名以面板「信号库」/ GET /api/strategies 为准;
策略全周期回测走 POST /api/backtest/strategy/run (body 详见契约)。
"""
from common import call

# 1. 策略清单 (read:analysis)
strategies = call("GET", "/api/strategies")["strategies"]
for s in strategies[:5]:
    print(f"策略: {s['id']}  {s['name']}  [{','.join(s.get('tags', []))}]")
if not strategies:
    print("(无策略)")

# 2. 信号回测 (run:backtest): 贵州茅台, 5/20 均线金叉入场
result = call("POST", "/api/backtest/run", {
    "symbols": ["600519.SH"],
    "entries": ["signal_ma_golden_5_20"],   # 内置信号; 更多见 信号库 页面
    "exits": [],
})
for key in ("total_return", "win_rate", "trades", "max_drawdown"):
    if key in result:
        print(f"回测 {key}: {result[key]}")
