"""竞价 Parquet 仓库。不触碰 kline_daily / enriched。"""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

import polars as pl

from app.auction.contracts import AuctionFinal, AuctionSnapshot
from app.config import settings
from app.parquet import scan_parquet_compat


def _atomic_write(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    try:
        frame.write_parquet(tmp)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


class AuctionRepository:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.root = Path(data_dir or settings.data_dir) / "auction"
        # 读-改-写 (load → concat → atomic write) 非原子: 轮询线程与 /refresh 并发时会
        # 各自基于同一份旧数据合并后覆盖, 丢新点。加锁保证 append/upsert 串行。
        self._lock = threading.Lock()

    def snapshot_dir(self, trade_date: date) -> Path:
        return self.root / "snapshots" / f"trade_date={trade_date.isoformat()}"

    def final_path(self, trade_date: date) -> Path:
        return self.root / "final" / f"trade_date={trade_date.isoformat()}" / "part.parquet"

    def append_snapshots(self, items: list[AuctionSnapshot]) -> int:
        if not items:
            return 0
        with self._lock:
            by_date: dict[date, list[AuctionSnapshot]] = {}
            for item in items:
                by_date.setdefault(item.trade_date, []).append(item)
            written = 0
            for trade_date, group in by_date.items():
                folder = self.snapshot_dir(trade_date)
                folder.mkdir(parents=True, exist_ok=True)
                existing = self.load_snapshots(trade_date)
                rows = [item.to_row() for item in group]
                incoming = pl.DataFrame(rows)
                if existing.is_empty():
                    merged = incoming
                else:
                    merged = pl.concat([existing, incoming], how="diagonal_relaxed")
                merged = merged.unique(
                    subset=["symbol", "source", "source_time_ms"],
                    keep="last",
                ).sort(["symbol", "source_time_ms"])
                _atomic_write(folder / "part.parquet", merged)
                written += incoming.height
            return written

    def upsert_finals(self, items: list[AuctionFinal]) -> int:
        if not items:
            return 0
        with self._lock:
            by_date: dict[date, list[AuctionFinal]] = {}
            for item in items:
                by_date.setdefault(item.trade_date, []).append(item)
            written = 0
            for trade_date, group in by_date.items():
                path = self.final_path(trade_date)
                incoming = pl.DataFrame([item.to_row() for item in group])
                existing = self.load_finals(trade_date)
                merged = incoming if existing.is_empty() else pl.concat(
                    [existing, incoming], how="diagonal_relaxed"
                )
                merged = merged.unique(subset=["symbol", "source"], keep="last")
                _atomic_write(path, merged)
                written += incoming.height
            return written

    def load_snapshots(self, trade_date: date) -> pl.DataFrame:
        folder = self.snapshot_dir(trade_date)
        if not folder.exists():
            return pl.DataFrame()
        files = list(folder.glob("*.parquet"))
        if not files:
            return pl.DataFrame()
        return scan_parquet_compat(str(folder / "*.parquet")).collect()

    def load_finals(self, trade_date: date) -> pl.DataFrame:
        path = self.final_path(trade_date)
        if not path.exists():
            return pl.DataFrame()
        return scan_parquet_compat(str(path)).collect()

    def list_dates(self) -> list[str]:
        dates: set[str] = set()
        for kind in ("snapshots", "final"):
            base = self.root / kind
            if not base.exists():
                continue
            for path in base.glob("trade_date=*"):
                dates.add(path.name.split("=", 1)[-1])
        return sorted(dates, reverse=True)

    def coverage(self) -> dict:
        dates = self.list_dates()
        return {
            "dates": dates[:30],
            "date_count": len(dates),
            "has_snapshots": (self.root / "snapshots").exists(),
            "has_finals": (self.root / "final").exists(),
        }
