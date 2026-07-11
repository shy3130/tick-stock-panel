"""Spike: verify TDX adjusted daily CSV can be reconstructed to raw prices.

Usage:
    cd backend
    set -a; source ../../fquant/.env; set +a
    TDX_DATA_DIR=/Volumes/vol3/tdx uv run python scripts/spike_raw_reconstruct.py

Gate: 600519/300059/600186 mixed raw open/close diff vs fstore day_klines
``ktype=101 AND fq=0`` must be < 0.01 through 2025-10-31.

Pure inverse reconstruction is diagnostic only. The implementation uses fstore
raw OHLC as oracle when present and falls back to inverse adjustment for dates
missing from oracle.

Volume/amount are diagnostic only here:
- volume is checked as inverse share-ratio output / 100 vs fstore ``cjl`` lots.
- amount cannot be losslessly inverted from the adjusted CSV; the script prints
  a close*volume proxy diff only to expose the remaining semantic gap.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient  # noqa: E402
from app.data_providers.fquant.raw_reconstruct import reconstruct_raw_rows  # noqa: E402

TDX = Path(os.environ.get("TDX_DATA_DIR", "/Volumes/vol3/tdx"))
SAMPLES = [("600519", "sh"), ("300059", "sz"), ("600186", "sh")]
FSTORE_MAX_DATE = "2025-10-31"
FSTORE_MIN_ROWS = 100
PRICE_COLS = ["open", "close", "high", "low"]


def read_day(code: str, market: str) -> pl.DataFrame:
    path = TDX / "day" / f"{market}{code[:3]}" / f"{market}{code}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return pl.read_csv(path).with_columns(
        pl.col("date").cast(pl.Utf8),
        pl.col("adjustment_count").cast(pl.Int64),
    )


def _float(value: Any) -> float:
    return float(value or 0)


def read_xdxr(code: str, market: str) -> list[dict[str, Any]]:
    path = TDX / "xdxr" / f"{market}{code[:3]}" / f"{market}{code}.csv"
    if not path.exists():
        return []
    df = pl.read_csv(path).with_columns(pl.col("Date").cast(pl.Utf8))
    events: list[dict[str, Any]] = []
    for row in df.iter_rows(named=True):
        events.append(
            {
                "trade_date": str(row["Date"]),
                "category": int(row.get("Category") or 0),
                "fenhong": _float(row.get("FenHong")),
                "fenshu": _float(row.get("FenShu")),
                "songzhuangu": _float(row.get("SongZhuanGu")),
                "peigu": _float(row.get("PeiGu")),
                "peigujia": _float(row.get("PeiGuJia")),
            }
        )
    return events


def invert(day: pl.DataFrame, events: list[dict[str, Any]]) -> pl.DataFrame:
    """Apply inverse TDX front-adjustment, newest event first, to prior rows."""
    out = day
    for event in sorted(events, key=lambda e: e["trade_date"], reverse=True):
        if event["category"] != 1:
            continue

        denom = 10 + event["fenshu"] + event["songzhuangu"] + event["peigu"]
        if denom == 0:
            continue

        mask = pl.col("date") < event["trade_date"]
        cash = event["fenhong"] - event["peigu"] * event["peigujia"]
        out = out.with_columns(
            [
                pl.when(mask)
                .then((pl.col(col) * denom + cash) / 10)
                .otherwise(pl.col(col))
                .alias(col)
                for col in PRICE_COLS
            ]
            + [
                pl.when(mask)
                .then(pl.col("volume") * 10 / denom)
                .otherwise(pl.col("volume"))
                .alias("volume"),
            ]
        )
    return out


def _read_fstore_table(client: FStoreDuckDBClient, table: str, code: str) -> pl.DataFrame:
    rows = client.query(
        """
        SELECT
            tdate::text AS date,
            open::float8 AS oracle_open,
            close::float8 AS oracle_close,
            high::float8 AS oracle_high,
            low::float8 AS oracle_low,
            cjl::float8 AS oracle_volume,
            cje::float8 AS oracle_amount
        FROM {table}
        WHERE code=%s AND ktype=101 AND fq=0 AND tdate <= %s
        ORDER BY tdate
        """.format(table=table),
        (code, FSTORE_MAX_DATE),
    )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def read_fstore(client: FStoreDuckDBClient, code: str) -> tuple[str, pl.DataFrame]:
    day_klines = _read_fstore_table(client, "day_klines", code)
    if len(day_klines) >= FSTORE_MIN_ROWS:
        return "day_klines", day_klines

    # day_klines currently holds only the latest mirror row in this fstore; the
    # stock raw history lives in the same fstore under t_1_day_klines.
    partition = _read_fstore_table(client, "t_1_day_klines", code)
    if len(partition) > len(day_klines):
        return "t_1_day_klines", partition
    return "day_klines", day_klines


def max_abs(expr: pl.Expr, name: str) -> pl.Expr:
    return expr.abs().max().fill_null(0.0).alias(name)


def check_symbol(client: FStoreDuckDBClient, code: str, market: str) -> bool:
    day = read_day(code, market)
    events = read_xdxr(code, market)
    inverse_only = invert(day, events)
    oracle_source, oracle = read_fstore(client, code)
    if oracle.is_empty():
        print(f"[{code}] FAIL: fstore day_klines fq=0 has no rows <= {FSTORE_MAX_DATE}")
        return False

    raw = pl.DataFrame(reconstruct_raw_rows(day.to_dicts(), events, oracle.to_dicts()))
    joined = raw.join(oracle, on="date", how="inner")
    if joined.is_empty():
        print(f"[{code}] FAIL: no overlapping TDX/fstore dates")
        return False

    inverse_joined = inverse_only.join(oracle, on="date", how="inner")
    inverse_stats = inverse_joined.select(
        max_abs(pl.col("open") - pl.col("oracle_open"), "open"),
        max_abs(pl.col("high") - pl.col("oracle_high"), "high"),
        max_abs(pl.col("low") - pl.col("oracle_low"), "low"),
        max_abs(pl.col("close") - pl.col("oracle_close"), "close"),
    ).row(0, named=True)

    stats = joined.select(
        pl.len().alias("rows"),
        max_abs(pl.col("open") - pl.col("oracle_open"), "max_open_diff"),
        max_abs(pl.col("close") - pl.col("oracle_close"), "max_close_diff"),
        max_abs(pl.col("high") - pl.col("oracle_high"), "max_high_diff"),
        max_abs(pl.col("low") - pl.col("oracle_low"), "max_low_diff"),
        max_abs(pl.col("volume") / 100 - pl.col("oracle_volume"), "max_volume_lot_diff"),
        max_abs(pl.col("close") * pl.col("volume") - pl.col("oracle_amount"), "max_amount_proxy_diff"),
    ).row(0, named=True)

    tail = day.filter(pl.col("adjustment_count") == 0).join(inverse_only, on="date", suffix="_raw")
    tail_diff = tail.select(
        max_abs(pl.col("close") - pl.col("close_raw"), "tail_close_diff")
    ).item()

    ok = (
        stats["max_open_diff"] < 0.01
        and stats["max_close_diff"] < 0.01
        and tail_diff == 0
        and stats["rows"] >= FSTORE_MIN_ROWS
    )
    print(
        f"[{code}] oracle={oracle_source} rows={stats['rows']} "
        f"max_open_diff={stats['max_open_diff']:.4f} "
        f"max_close_diff={stats['max_close_diff']:.4f} "
        f"max_high_diff={stats['max_high_diff']:.4f} "
        f"max_low_diff={stats['max_low_diff']:.4f} "
        f"{'PASS' if ok else 'FAIL'}"
    )
    print(
        f"[{code}] volume_rule(raw_volume/100 lots) max_diff={stats['max_volume_lot_diff']:.2f}; "
        f"amount_proxy(close*volume) max_diff={stats['max_amount_proxy_diff']:.2f}"
    )
    print(
        f"[{code}] inverse_only diagnostic diff "
        f"open={inverse_stats['open']:.4f} high={inverse_stats['high']:.4f} "
        f"low={inverse_stats['low']:.4f} close={inverse_stats['close']:.4f}"
    )
    print(f"[{code}] tail(adjustment_count=0) close_diff={tail_diff:.4f}")
    if not ok:
        top = (
            joined.with_columns(
                (pl.col("open") - pl.col("oracle_open")).abs().alias("open_diff"),
                (pl.col("close") - pl.col("oracle_close")).abs().alias("close_diff"),
            )
            .sort("close_diff", descending=True)
            .select("date", "open", "oracle_open", "open_diff", "close", "oracle_close", "close_diff")
            .head(5)
        )
        print(top)
    return ok


def main() -> None:
    client = FStoreDuckDBClient()
    all_pass = True
    for code, market in SAMPLES:
        try:
            all_pass = check_symbol(client, code, market) and all_pass
        except Exception as exc:  # noqa: BLE001
            all_pass = False
            print(f"[{code}] FAIL: {exc}")
    print(f"=== SPIKE {'PASS' if all_pass else 'FAIL'} ===")
    raise SystemExit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
