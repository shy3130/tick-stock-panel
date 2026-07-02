import polars as pl

from app.api.data import _safe_aggregate_daily


class FakeStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class FakeRepo:
    def __init__(self, data_dir):
        self.store = FakeStore(data_dir)

    def execute_one(self, sql):
        if "count(DISTINCT symbol)" in sql:
            return (2,)
        return None


def test_daily_status_uses_enriched_when_local_raw_mirror_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    for ds in ("2026-06-30", "2026-07-01"):
        out = tmp_path / "kline_daily_enriched" / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600519.SH"], "date": [ds]}).write_parquet(out)

    stats = _safe_aggregate_daily(FakeRepo(tmp_path))

    assert stats["earliest_date"] == "2026-06-30"
    assert stats["latest_date"] == "2026-07-01"
    assert stats["trading_days"] == 2
    assert stats["raw_mirror_disabled"] is True
