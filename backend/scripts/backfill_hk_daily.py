"""回填指定港股历史日 K 到本地 parquet。

用法:
  cd backend && DATA_PROVIDER=fquant_local uv run python scripts/backfill_hk_daily.py --symbols 02577.HK,00700.HK
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill HK daily parquet from local TDX disk")
    parser.add_argument("--symbols", required=True, help="逗号分隔港股 symbol，例如 02577.HK,00700.HK")
    parser.add_argument("--start", default="2015-01-01", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=datetime.now().date().isoformat(), help="结束日期 YYYY-MM-DD")
    return parser.parse_args()


def _parse_symbols(value: str) -> list[str]:
    return sorted({s.strip().upper() for s in value.replace("\n", ",").split(",") if s.strip()})


def _with_date_type(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "date" not in df.columns:
        return df
    if df.schema["date"] == pl.Date:
        return df
    if df.schema["date"] == pl.Datetime:
        return df.with_columns(pl.col("date").dt.date())
    return df.with_columns(pl.col("date").cast(pl.Utf8).str.strptime(pl.Date, strict=False))


def _build_enriched(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    base = _with_date_type(df).sort(["symbol", "date"])
    source_expr = pl.col("source") if "source" in base.columns else pl.lit("fquant_local")
    return base.with_columns(
        change_pct=((pl.col("close") / pl.col("close").shift(1).over("symbol")) - 1.0) * 100.0,
        source=source_expr,
    ).select("symbol", "date", "close", "change_pct", "source")


def main() -> int:
    args = _parse_args()
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print(json.dumps({"ok": False, "error": "empty symbols"}, ensure_ascii=False))
        return 2

    from app.data_providers.fquant_provider import FQuantProvider
    from app.storage.repository import DataStore, KlineRepository

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    provider = FQuantProvider(engine_mode="disk")
    repo = KlineRepository(DataStore())

    daily = provider.get_daily(symbols, start, end, "hk")
    if daily.is_empty():
        print(json.dumps({"ok": True, "symbols": symbols, "rows": 0, "written": 0}, ensure_ascii=False))
        return 0

    daily = _with_date_type(daily)
    if "source" not in daily.columns:
        daily = daily.with_columns(pl.lit("fquant_local").alias("source"))
    raw_cols = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume", "amount", "source") if c in daily.columns]
    raw = daily.select(raw_cols)
    enriched = _build_enriched(daily)

    repo.append_hk_daily(raw)
    repo.append_hk_enriched(enriched)
    repo.refresh_index_views()

    print(json.dumps({
        "ok": True,
        "symbols": symbols,
        "rows": daily.height,
        "written": raw.height,
        "start": args.start,
        "end": args.end,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
