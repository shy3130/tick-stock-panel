from datetime import date, datetime

import polars as pl

from app.indicators.pipeline import run_pipeline_local


class FakeProvider:
    name = "fquant_local"

    def get_daily(self, symbols, start_time, end_time, asset_type):
        rows = []
        for symbol in symbols:
            rows.extend([
                {
                    "symbol": symbol,
                    "date": date(2026, 6, 30),
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": 10000.0,
                },
                {
                    "symbol": symbol,
                    "date": date(2026, 7, 1),
                    "open": 10.2,
                    "high": 10.8,
                    "low": 10.0,
                    "close": 10.6,
                    "volume": 1200.0,
                    "amount": 12000.0,
                },
            ])
        return pl.DataFrame(rows)

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        return pl.DataFrame()


def test_run_pipeline_local_writes_enriched_without_raw(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_enriched_batch_size", lambda: 1)
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "name": ["贵州茅台", "平安银行"],
        "float_shares": [1_000_000_000.0, 1_000_000_000.0],
    }).write_parquet(inst_dir / "instruments.parquet")

    progress = []
    written = run_pipeline_local(
        FakeProvider(),
        data_dir=tmp_path,
        start_time=datetime(2026, 6, 30),
        end_time=datetime(2026, 7, 1),
        on_batch_done=lambda cur, tot: progress.append((cur, tot)),
    )

    assert written == 4
    assert progress == [(1, 2), (2, 2)]
    assert not (tmp_path / "kline_daily").exists()
    out = tmp_path / "kline_daily_enriched" / "date=2026-07-01" / "part.parquet"
    assert out.exists()
    df = pl.read_parquet(out).sort("symbol")
    assert df["symbol"].to_list() == ["000001.SZ", "600519.SH"]
    assert "raw_close" in df.columns
