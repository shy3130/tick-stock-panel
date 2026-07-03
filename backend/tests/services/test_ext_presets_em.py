from app.services import ext_presets_em
from app.services.ext_presets import get_preset
import polars as pl


def test_em_presets_registered():
    assert {p.id for p in ext_presets_em.presets()} == {
        "ext_lockup_em",
        "ext_holder_em",
        "ext_margin_em",
        "ext_block_em",
        "ext_research_em",
        "ext_news_em",
    }
    assert get_preset("ext_lockup_em") is not None
    assert get_preset("ext_research_em") is not None


def test_flatten_lockup_uses_uid_not_symbol_key():
    rows = ext_presets_em._flatten_lockup([
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "FREE_DATE": "2026-08-01 00:00:00",
            "FREE_SHARES_TYPE": "首发原股东限售股份",
            "FREE_SHARES": "100",
        }
    ])
    assert rows[0]["uid"] == "lockup:600519:2026-08-01 00:00:00"
    assert rows[0]["stock_symbol"] == "600519.SH"
    assert "symbol" not in rows[0]


def test_flatten_holder_maps_core_fields():
    rows = ext_presets_em._flatten_holder([
        {
            "SECURITY_CODE": "000001",
            "SECURITY_NAME_ABBR": "平安银行",
            "END_DATE": "2026-03-31 00:00:00",
            "HOLDER_NUM": "188000",
            "HOLDER_NUM_RATIO": "-1.05",
        }
    ])
    assert rows[0]["stock_symbol"] == "000001.SZ"
    assert rows[0]["end_date"] == "2026-03-31"
    assert rows[0]["holder_count"] == 188000.0
    assert rows[0]["holder_count_change_pct"] == -1.05


def test_flatten_margin_accepts_scode():
    rows = ext_presets_em._flatten_margin([
        {"SCODE": "300059", "SNAME": "东方财富", "DATE": "2026-07-02", "RZRQYE": "123"}
    ])
    assert rows[0]["stock_symbol"] == "300059.SZ"
    assert rows[0]["margin_total_balance"] == 123.0


def test_flatten_block_keeps_multiple_deals_per_symbol():
    rows = ext_presets_em._flatten_block([
        {
            "SECURITY_CODE": "600519",
            "TRADE_DATE": "2026-07-01",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "DEAL_PRICE": "100",
            "DEAL_AMT": "1000",
        },
        {
            "SECURITY_CODE": "600519",
            "TRADE_DATE": "2026-07-01",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "DEAL_PRICE": "101",
            "DEAL_AMT": "2000",
        },
    ])
    assert len({r["uid"] for r in rows}) == 2


def test_flatten_research_maps_eps_fields():
    rows = ext_presets_em._flatten_research([
        {
            "infoCode": "R1",
            "title": "盈利预测上调",
            "publishDate": "2026-07-01 10:00:00",
            "orgSName": "某券商",
            "researcher": "张三",
            "emRatingName": "买入",
            "predictThisYearEps": "2.3",
            "predictNextYearPe": "15",
        }
    ], "600519.SH")
    assert rows[0]["uid"] == "research:600519:R1:2026-07-01"
    assert rows[0]["eps_this_year"] == 2.3
    assert rows[0]["pe_next_year"] == 15.0


def test_flatten_news_trims_content():
    rows = ext_presets_em._flatten_news([
        {
            "art_code": "N1",
            "title": "公司新闻",
            "date": "2026-07-01",
            "url": "https://finance.eastmoney.com/a/1.html",
            "mediaName": "东财",
            "content": "  a  " * 200,
        }
    ], "000001.SZ")
    assert rows[0]["uid"] == "news:000001:N1"
    assert rows[0]["stock_symbol"] == "000001.SZ"
    assert len(rows[0]["snippet"]) <= 280


def test_watchlist_symbols_filters_a_shares(tmp_path):
    path = tmp_path / "user_data"
    path.mkdir()
    pl.DataFrame({"symbol": ["600519.SH", "00700.HK", "000001.SZ"]}).write_parquet(path / "watchlist.parquet")
    assert ext_presets_em._watchlist_symbols(tmp_path) == ["600519.SH", "000001.SZ"]
