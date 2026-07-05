from pathlib import Path

from app.data_providers.fquant.engine_data_disk import EngineDataDiskClient, _csv_path, _symbol_from_code


DAY = """date,open,close,high,low,volume,amount,up,down,datetime,adjustment_count
2026-07-01,1180.1,1193.01,1196.8,1166.33,4247300,5033838080,0,0,2026-07-01 15:00:00,0
"""
WIDE = """date,open,close,high,low,volume,amount,last_close,change_rate,datetime
2026-07-02,1180.1,1193.01,1196.8,1166.33,4247300,5033838080,1185.49,0.63,2026-07-02 15:00:00
"""
XDXR = """Date,Category,Name,FenHong,PeiGuJia,SongZhuanGu,PeiGu,FenShu
2002-07-25,1,除权除息,6,0,1,0,0
"""
MINUTES = """Price,Vol
1184.88,748
1183.32,209
"""
TRANS = """time,price,vol,num,amount,buyorsell
09:25,1180.1,18600,142,21949860,2
09:30,1180.1,1400,18,1652140,1
"""
FUND = """Date,Code,Main,MainRatio,SuperLarge,SuperLargeRatio,Large,LargeRatio,Medium,MediumRatio,Small,SmallRatio
2026-07-01,sh600519,300,3,100,1,200,2,-50,-0.5,-250,-2.5
"""
FUND_RANGE = """Date,Code,Main,MainRatio,SuperLarge,SuperLargeRatio,Large,LargeRatio,Medium,MediumRatio,Small,SmallRatio
2026-06-29,sh600519,100,1,50,0.5,50,0.5,-20,-0.2,-30,-0.3
2026-06-30,sh600519,-50,-0.5,10,0.1,-30,-0.3,-10,-0.1,-20,-0.2
2026-07-01,sh600519,300,3,100,1,200,2,-50,-0.5,-250,-2.5
2026-07-02,sh600519,80,0.8,40,0.4,20,0.2,10,0.1,10,0.1
"""


def make_disk(root: Path):
    (root / "day" / "sh600").mkdir(parents=True)
    (root / "wide" / "sh600").mkdir(parents=True)
    (root / "xdxr" / "sh600").mkdir(parents=True)
    (root / "fund" / "sh600").mkdir(parents=True)
    (root / "minutes" / "2026" / "20260701").mkdir(parents=True)
    (root / "trans" / "2026" / "20260701").mkdir(parents=True)
    (root / "day" / "sh600" / "sh600519.csv").write_text(DAY)
    (root / "wide" / "sh600" / "sh600519.csv").write_text(WIDE)
    (root / "xdxr" / "sh600" / "sh600519.csv").write_text(XDXR)
    (root / "fund" / "sh600" / "sh600519.csv").write_text(FUND)
    (root / "minutes" / "2026" / "20260701" / "sh600519.csv").write_text(MINUTES)
    (root / "trans" / "2026" / "20260701" / "sh600519.csv").write_text(TRANS)


def make_disk_fund_range(root: Path):
    (root / "fund" / "sh600").mkdir(parents=True)
    (root / "fund" / "sh600" / "sh600519.csv").write_text(FUND_RANGE)


def test_csv_path():
    root = Path("/tmp/tdx")
    assert _csv_path(root, "day", "600519.SH") == root / "day" / "sh600" / "sh600519.csv"
    assert _csv_path(root, "day", "300059.SZ") == root / "day" / "sz300" / "sz300059.csv"
    assert _csv_path(root, "day", "00700.HK") == root / "day" / "hk00" / "hk00700.csv"


def test_get_wide_prefers_wide(tmp_path, monkeypatch):
    make_disk(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_wide("600519", limit=10)

    assert rows[0]["last_close"] == 1185.49
    assert rows[0]["change_rate"] == 0.63


def test_get_wide_falls_back_to_day(tmp_path, monkeypatch):
    make_disk(tmp_path)
    (tmp_path / "wide" / "sh600" / "sh600519.csv").unlink()
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_wide("600519", limit=10)

    assert rows[0]["close"] == 1193.01
    assert "last_close" not in rows[0]


def test_get_wide_reads_hk_path(tmp_path, monkeypatch):
    (tmp_path / "wide" / "hk00").mkdir(parents=True)
    (tmp_path / "wide" / "hk00" / "hk00700.csv").write_text(WIDE)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_wide("00700", limit=10, asset_type="hk")

    assert rows[0]["close"] == 1193.01


def test_symbol_from_code_does_not_guess_hk_by_length():
    assert _symbol_from_code("00700") == "00700.SZ"
    assert _symbol_from_code("00700", asset_type="hk") == "00700.HK"
    assert _symbol_from_code("510330") == "510330.SH"


def test_get_xdxr(tmp_path, monkeypatch):
    make_disk(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_xdxr("600519")

    assert rows[0]["category"] == 1
    assert rows[0]["fenhong"] == 6.0


def test_freshness_uses_wide_before_day(tmp_path, monkeypatch):
    make_disk(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    assert EngineDataDiskClient().freshness("600519").isoformat() == "2026-07-02"


def test_get_minutes_reads_date_partition(tmp_path, monkeypatch):
    make_disk(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_minutes("600519", "20260701", limit=1)

    assert rows == [{"price": 1184.88, "volume": 74800.0}]


def test_get_trans_reads_date_partition(tmp_path, monkeypatch):
    make_disk(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_trans("600519", "20260701", limit=1)

    assert rows == [{
        "time": "09:25",
        "price": 1180.1,
        "volume": 18600,
        "amount": 21949860,
        "order_count": 142,
        "direction": 2,
    }]


def test_get_fund_daily_reads_net_amounts(tmp_path, monkeypatch):
    make_disk(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    row = EngineDataDiskClient().get_fund_daily("600519", "2026-07-01")

    assert row["main_net"] == 300.0
    assert row["total_net"] == 0.0
    assert row["super_large_net"] == 100.0
    assert row["large_net"] == 200.0
    assert row["main_ratio"] == 3.0


def test_get_fund_range_filters_to_window(tmp_path, monkeypatch):
    make_disk_fund_range(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    df = EngineDataDiskClient().get_fund_range("600519", "2026-06-30", "2026-07-01")

    assert df.columns == ["date", "main_net_inflow"]
    rows = df.to_dicts()
    assert [str(r["date"]) for r in rows] == ["2026-06-30", "2026-07-01"]
    assert rows[0]["main_net_inflow"] == -50.0
    assert rows[1]["main_net_inflow"] == 300.0


def test_get_fund_range_missing_symbol_returns_empty(tmp_path, monkeypatch):
    make_disk_fund_range(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    df = EngineDataDiskClient().get_fund_range("000001.SZ", "2026-06-30", "2026-07-01")

    assert df.is_empty()


def test_get_fund_range_full_window_beyond_available_dates(tmp_path, monkeypatch):
    make_disk_fund_range(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    df = EngineDataDiskClient().get_fund_range("600519", "2026-01-01", "2026-12-31")

    assert df.height == 4
    assert [str(d) for d in df["date"].to_list()] == [
        "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02",
    ]
