"""示例 2 — 写入扩展数据 (scope: write:ext + read:ext)。

把自有数据程序化喂进扩展表, 与内置数据同台分析:
  列出扩展表 → ingest 写入一行 → /rows 读回验证。
表结构 (字段/模式) 需先在面板「数据 → 扩展数据」配好 — 结构配置属管理面,
Token 只能写行数据, 防止外部改写表结构污染策略数据面。
"""
from common import call

# 1. 列出已配置的扩展表 (items: [{id, label, mode, fields:[{name,dtype,...}]}])
items = call("GET", "/api/ext-data")["items"]
if not items:
    raise SystemExit("还没有扩展表 — 先在面板 数据 → 扩展数据 创建一张 (如 人气排行, 字段: symbol,rank)")

cfg = items[0]
cid = cfg["id"]
fields = [f["name"] for f in cfg["fields"]]
print(f"目标表: {cfg.get('label', cid)} ({cid}) | 模式: {cfg.get('mode')} | 字段: {fields}")

# 2. 写入一行 (date 不传按北京当天落盘; 时序表自动进当日分区)
#    示例按字段名给合理演示值 — 实际接入时换成你的数据
from datetime import date

today = date.today().isoformat()
row = {"symbol": "600519.SH"}
for f in fields:
    if f in row:
        continue
    if f == "date":
        row[f] = today
    elif f == "name":
        row[f] = "演示标的"
    elif f == "code":
        row[f] = "600519"
    elif f.startswith("rank") or f == "heat":
        row[f] = 1
    else:
        row[f] = 0
resp = call("POST", f"/api/ext-data/{cid}/ingest", {"rows": [row]})
print("写入:", resp)

# 3. 读回验证 (等值过滤 + 分页)
back = call("GET", f"/api/ext-data/{cid}/rows?filter=symbol:600519.SH&limit=3")
print(f"读回 {len(back['rows'])} 行 (共 {back.get('total', '?')}):", back["rows"])
