"""Spike: classify TDX day/ coverage gaps for current instruments.

Usage:
    cd backend
    set -a; source ../../fquant/.env; set +a
    TDX_DATA_DIR=/Volumes/vol3/tdx uv run python scripts/spike_disk_day_coverage.py --limit 20

Gate: true_gap_active_after_2025_11 must be zero before claiming full-market
``fquant_local.daily`` coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient  # noqa: E402

TDX = Path(os.environ.get("TDX_DATA_DIR", "/Volumes/vol3/tdx"))
INSTRUMENTS = ROOT.parent / "data" / "instruments" / "instruments.parquet"
CUTOFF = "2025-11-01"


def _tdx_path(symbol: str) -> Path:
    code, _, exchange = symbol.partition(".")
    market = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper(), exchange.lower())
    return TDX / "day" / f"{market}{code[:3]}" / f"{market}{code}.csv"


def _chunks(values: list[str], size: int = 500):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _placeholders(count: int) -> str:
    return ",".join(["%s"] * count)


def load_instruments() -> list[dict[str, str]]:
    df = pl.read_parquet(INSTRUMENTS)
    if "asset_type" in df.columns:
        df = df.filter(pl.col("asset_type") == "stock")
    return df.select("symbol", "code", "name", "exchange").to_dicts()


def load_base_infos(client: FStoreDuckDBClient, codes: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(codes):
        rows = client.query(
            f"""
            SELECT code, name, asset_type, stype, ssdate, day, symbol
            FROM base_infos
            WHERE asset_type=1 AND code IN ({_placeholders(len(chunk))})
            """,
            tuple(chunk),
        )
        for row in rows:
            out[str(row["code"]).zfill(6)].append(row)
    return out


def _load_day_max_table(client: FStoreDuckDBClient, table: str, codes: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in _chunks(codes):
        rows = client.query(
            f"""
            SELECT code, max(tdate)::text AS max_date
            FROM {table}
            WHERE ktype=101 AND fq=0 AND code IN ({_placeholders(len(chunk))})
            GROUP BY code
            """,
            tuple(chunk),
        )
        for row in rows:
            if row.get("max_date"):
                out[str(row["code"]).zfill(6)] = str(row["max_date"])
    return out


def load_day_max(client: FStoreDuckDBClient, codes: list[str]) -> dict[str, str]:
    out = _load_day_max_table(client, "day_klines", codes)
    partitioned = _load_day_max_table(client, "t_1_day_klines", codes)
    for code, max_date in partitioned.items():
        if max_date > out.get(code, ""):
            out[code] = max_date
    return out


def classify(limit: int) -> dict[str, Any]:
    instruments = load_instruments()
    codes = sorted({str(r["code"]).zfill(6) for r in instruments})
    client = FStoreDuckDBClient()
    base_infos = load_base_infos(client, codes)
    day_max = load_day_max(client, codes)

    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for inst in instruments:
        symbol = str(inst["symbol"])
        code = str(inst["code"]).zfill(6)
        day_max_value = day_max.get(code)
        has_after_cutoff = bool(day_max_value and day_max_value >= CUTOFF)
        record = {
            "symbol": symbol,
            "name": inst.get("name"),
            "code": code,
            "tdx_path": str(_tdx_path(symbol)),
            "fstore_base_infos": len(base_infos.get(code, [])),
            "fstore_day_max": day_max_value,
            "fstore_has_after_cutoff": has_after_cutoff,
        }

        if _tdx_path(symbol).exists():
            key = "tdx_day_exists"
        elif not base_infos.get(code):
            key = "missing_retired_or_unlisted"
        elif has_after_cutoff:
            key = "missing_has_fstore_after_2025_11"
        elif day_max_value:
            key = "missing_has_fstore_tail"
        else:
            key = "true_gap_active_after_2025_11"

        counts[key] += 1
        if len(samples[key]) < limit:
            samples[key].append(record)

    return {
        "instruments": len(instruments),
        "tdx_data_dir": str(TDX),
        "cutoff": CUTOFF,
        "counts": dict(counts),
        "samples": dict(samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="sample rows per category")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    result = classify(args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"TDX_DATA_DIR={result['tdx_data_dir']}")
        print(f"instruments={result['instruments']} cutoff={result['cutoff']}")
        for key, count in sorted(result["counts"].items()):
            print(f"{key}: {count}")
            for row in result["samples"].get(key, []):
                print(
                    f"  {row['symbol']} {row['name']} "
                    f"base_infos={row['fstore_base_infos']} day_max={row['fstore_day_max']}"
                )

    true_gap = result["counts"].get("true_gap_active_after_2025_11", 0)
    raise SystemExit(0 if true_gap == 0 else 1)


if __name__ == "__main__":
    main()
