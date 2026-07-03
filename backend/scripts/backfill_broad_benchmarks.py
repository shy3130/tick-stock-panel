"""一次性回填宽基基准历史日 K。

用法:
  cd backend && DATA_PROVIDER=fquant_local uv run python scripts/backfill_broad_benchmarks.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BROAD = ["000300.SH", "000905.SH", "399006.SZ", "000688.SH"]


def main() -> int:
    from app.services import index_sync, preferences
    from app.tickflow.policy import detect_capabilities
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore())
    capset = detect_capabilities(force=True)
    n = index_sync.sync_and_persist_index_daily(
        repo,
        capset,
        symbols_override=BROAD,
        start_date=datetime(2015, 1, 1),
        end_date=datetime.now(),
    )
    print(f"回填完成: +{n} 行")

    current = {
        s.strip()
        for s in preferences.get_pipeline_index_symbols().replace("\n", ",").replace(" ", ",").split(",")
        if s.strip()
    }
    merged = ",".join(sorted(current | set(BROAD)))
    preferences.set_pipeline_index_symbols(merged)
    print(f"常驻指数已更新: {merged}")

    df = repo.get_index_daily(
        "000300.SH",
        datetime(2024, 1, 1).date(),
        datetime(2024, 12, 31).date(),
        columns=["date", "close"],
    )
    print(f"校验 000300.SH 2024 年: rows={df.height} (预期 ~242)")
    return 0 if df.height > 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
