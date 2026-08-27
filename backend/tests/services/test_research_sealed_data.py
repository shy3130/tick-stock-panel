from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import polars as pl

from app.indicators.pipeline import ENRICHED_STORAGE_COLS, compute_enriched
from app.services.research_sealed_data import PublishedCanonicalDailyReader


def _publish(root: Path, generation: str, frame: pl.DataFrame) -> Path:
    generation_dir = root / "generations" / generation
    day = frame.get_column("date")[0]
    partition = generation_dir / f"date={day.isoformat()}"
    partition.mkdir(parents=True)
    frame.drop("date").write_parquet(partition / "part.parquet")
    manifest = {
        "schema_version": 2,
        "kind": "tickflow_canonical_enriched_history",
        "generation": generation,
        "path": f"generations/{generation}",
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "columns": frame.columns,
    }
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    (generation_dir / "manifest.json").write_bytes(payload)
    (root / "current.json").write_bytes(payload)
    return generation_dir


class _Repo:
    def __init__(self, root: Path):
        self._external_enriched_root = root

    def _external_partition_sources(self, generation_dir, *, start, end):
        return tuple(
            str(path / "*.parquet")
            for path in generation_dir.iterdir()
            if path.is_dir() and path.name.startswith("date=")
            and start <= date.fromisoformat(path.name[5:]) <= end
        )

    def _scan_unique_enriched(self, source, *, start, end, columns, symbols, layout_cache_key):
        frame = pl.read_parquet(list(source), hive_partitioning=True)
        return frame.filter(pl.col("symbol").is_in(symbols)).select(
            [column for column in columns if column in frame.columns]
        ).sort(["symbol", "date"])


def _frame(day: date, close: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"], "date": [day],
        "open": [close], "high": [close + 1], "low": [close - 1], "close": [close],
        "volume": [100.0], "amount": [1000.0],
        "raw_open": [close], "raw_close": [close], "raw_high": [close + 1], "raw_low": [close - 1],
        "turnover_rate": [1.0], "consecutive_limit_ups": [0], "consecutive_limit_downs": [0],
    })


def test_reader_pins_generation_and_hashes_manifest_bytes(tmp_path):
    first_dir = _publish(tmp_path, "20260827T010101-aaaaaaaa", _frame(date(2026, 8, 25), 10.0))
    repo = _Repo(tmp_path)
    reader = PublishedCanonicalDailyReader(repo)
    expected = hashlib.sha256((first_dir / "manifest.json").read_bytes()).hexdigest()

    _publish(tmp_path, "20260827T020202-bbbbbbbb", _frame(date(2026, 8, 26), 20.0))

    assert reader.generation() == "20260827T010101-aaaaaaaa"
    assert reader.manifest_sha256() == expected
    assert reader.market_days(date(2026, 8, 1), date(2026, 8, 31)) == [date(2026, 8, 25)]
    assert reader.daily_bars("600000.SH", date(2026, 8, 25), date(2026, 8, 25))["raw_close"].item() == 10.0


def test_old_generation_keeps_raw_open_missing(tmp_path):
    frame = _frame(date(2026, 8, 25)).drop("raw_open")
    _publish(tmp_path, "20260827T010101-aaaaaaaa", frame)
    reader = PublishedCanonicalDailyReader(_Repo(tmp_path))

    assert not reader.has_columns("raw_open")
    assert "raw_open" not in reader.daily_bars("600000.SH", date(2026, 8, 25), date(2026, 8, 25)).columns


def test_compute_enriched_preserves_provider_raw_open_before_adjustment():
    raw = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "date": [date(2026, 8, 25), date(2026, 8, 26)],
        "open": [10.0, 11.0], "high": [11.0, 12.0], "low": [9.0, 10.0], "close": [10.5, 11.5],
        "volume": [100.0, 120.0], "amount": [1000.0, 1200.0],
    })
    factors = pl.DataFrame({
        "symbol": ["600000.SH"],
        "trade_date": [date(2026, 8, 26)],
        "ex_factor": [2.0],
    })
    enriched = compute_enriched(raw, factors=factors)

    assert "raw_open" in ENRICHED_STORAGE_COLS
    assert enriched["raw_open"].to_list() == [10.0, 11.0]
    assert enriched["open"].to_list() != enriched["raw_open"].to_list()
