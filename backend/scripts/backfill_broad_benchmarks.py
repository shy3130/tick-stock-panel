"""一次性回填宽基基准历史日 K。

用法:
  cd backend && DATA_PROVIDER=fquant_local uv run python scripts/backfill_broad_benchmarks.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))



def main() -> int:
    from app.data_providers.capability_gate import detect_capabilities
    from app.services import index_sync
    from app.storage.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore())
    capset = detect_capabilities(force=True)
    required = index_sync.REQUIRED_INDEX_HISTORY_STARTS
    n = index_sync.sync_and_persist_index_daily(
        repo,
        capset,
        symbols_override=list(required),
        start_date=datetime.combine(min(required.values()), datetime.min.time()),
        end_date=datetime.now(),
    )
    print(f"回填完成: +{n} 行")

    incomplete: list[str] = []
    for symbol, required_start in required.items():
        df = repo.get_index_daily(
            symbol,
            required_start,
            datetime.now().date(),
            columns=["date", "close"],
        )
        earliest = df["date"].min() if not df.is_empty() else None
        ok = earliest is not None and earliest <= required_start + timedelta(days=45)
        print(f"校验 {symbol}: rows={df.height}, earliest={earliest}, ok={ok}")
        if not ok:
            incomplete.append(symbol)
    if incomplete:
        print(f"回填不完整: {', '.join(incomplete)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
