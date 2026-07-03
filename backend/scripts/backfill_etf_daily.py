"""一次性回填 ETF 历史日 K。

用法:
  cd backend && DATA_PROVIDER=fquant_local uv run python scripts/backfill_etf_daily.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATA_PROVIDER", "fquant_local")


def main() -> int:
    from app.services import index_sync
    from app.tickflow.policy import detect_capabilities
    from app.storage.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore())
    capset = detect_capabilities(force=True)

    n_inst = index_sync.sync_etf_instruments(repo)
    instruments = repo.get_etf_instruments()
    symbols = sorted(instruments["symbol"].to_list()) if not instruments.is_empty() else []
    print(f"ETF 标的: {len(symbols)} 只 (refreshed={n_inst})")
    if not symbols:
        return 1

    def progress(current: int, total: int) -> None:
        print(f"回填进度: {current}/{total}")

    n_rows = index_sync.sync_and_persist_etf_daily(
        repo,
        capset,
        start_date=datetime(2015, 1, 1),
        end_date=datetime.now(),
        symbols_override=symbols,
        on_chunk_done=progress,
    )
    print(f"ETF 日K回填完成: +{n_rows} 行")

    check = repo.get_etf_daily(
        "513050.SH",
        datetime(2024, 1, 1).date(),
        datetime(2024, 12, 31).date(),
        columns=["date", "close"],
    )
    print(f"校验 513050.SH 2024 年: rows={check.height} (预期 >200)")
    return 0 if n_rows > 0 and check.height > 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
