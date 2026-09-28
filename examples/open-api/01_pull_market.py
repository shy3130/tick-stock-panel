"""示例 1 — 读行情 (scope: read:market)。

标的搜索 → 拉日K → 市场总览。契约与参数详见 /api/openapi.json?tier=a。
"""
from common import call

# 1. 标的搜索 (支持代码/名称/拼音)
hits = call("GET", "/api/kline/instruments/search?q=600519&limit=3")
for h in hits["results"]:
    print(f"搜索命中: {h['symbol']}  {h['name']}")

# 2. 贵州茅台 最近 10 根日K (前复权口径)
daily = call("GET", "/api/kline/daily?symbol=600519.SH&limit=10")
rows = daily["rows"]
if rows:
    last = rows[-1]
    print(f"日K 最近 {len(rows)} 根, 最新 {last['date']} 收盘 {last['close']:.2f}")
else:
    print("日K 无数据 — 先在面板「数据」页跑一次同步")

# 3. 市场总览 (快照日期 / 行情源状态)
overview = call("GET", "/api/overview/market")
print("市场总览 as_of:", overview.get("as_of"), "| 行情轮询:", overview["quote_status"]["enabled"])
