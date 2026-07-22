"""粗略市场代理基准: 区间内每只股票前复权 close 的首尾收益, 取等权均值/中位数。
用于佐证"大盘/个股整体在区间内是否上涨" (Tushare daily 未含指数, 故无上证基准)。
"""
from __future__ import annotations
import sys
from pathlib import Path
from datetime import date
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
START = date(2026, 3, 24)
END = date(2026, 6, 24)

df = (
    pl.scan_parquet(str(ROOT / "data" / "kline_daily_enriched" / "**" / "*.parquet"))
    .filter((pl.col("date") >= START) & (pl.col("date") <= END))
    .select("symbol", "date", "close")
    .collect()
)

first = df.group_by("symbol").agg(pl.col("close").first().alias("c0"))
last = df.group_by("symbol").agg(pl.col("close").last().alias("c1"))
m = first.join(last, on="symbol").filter(pl.col("c0") > 0)
m = m.with_columns(((pl.col("c1") / pl.col("c0") - 1)).alias("ret"))
print(f"区间 {START}~{END} 全市场代理基准 (前复权, 等权持有到期):")
print(f"  样本股票数: {m.height}")
print(f"  均值收益:   {m['ret'].mean():.4f}")
print(f"  中位数收益: {m['ret'].median():.4f}")
print(f"  上涨占比:   {(m['ret'] > 0).mean():.4f}")
