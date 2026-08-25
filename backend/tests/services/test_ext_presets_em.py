import asyncio
from datetime import date

import pytest
import polars as pl

from app.services import eastmoney_client, ext_presets_em
from app.services.ext_presets import get_preset


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


def test_fetch_research_for_symbol_sends_complete_params(monkeypatch):
    """研报拉取必须带线上实测的完整参数 (缺 industryCode 等会被 reportapi 返回 400)。"""
    calls: list[tuple[str, dict]] = []

    def fake_get_json(url, params=None):
        calls.append((url, dict(params or {})))
        return {"data": [{
            "infoCode": "AP2026080112345678",
            "title": "茅台中报点评：业绩稳健",
            "stockName": "贵州茅台",
            "publishDate": "2026-08-01 09:30:00",
            "orgSName": "中信证券",
            "researcher": "张三",
            "emRatingName": "买入",
            "predictThisYearEps": "68.5",
            "predictNextYearEps": "75.2",
            "predictThisYearPe": "22.1",
            "predictNextYearPe": "20.3",
        }]}

    monkeypatch.setattr(eastmoney_client, "get_json", fake_get_json)

    rows = ext_presets_em._fetch_research_for_symbol("600519.SH", limit=5)

    today = date.today()
    assert len(calls) == 1
    url, params = calls[0]
    # URL 锁定 reportapi 白名单域名
    assert url == ext_presets_em._REPORT_LIST
    assert url == "https://reportapi.eastmoney.com/report/list"
    # 完整参数逐项锁定: 通配过滤 + 当年窗口 + 个股六位码
    assert params == {
        "industryCode": "*",
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": f"{today.year}-01-01",
        "endTime": today.isoformat(),
        "fields": "",
        "qType": "0",
        "orgCode": "",
        "code": "600519",
        "rcode": "",
        "pageSize": "5",
        "pageNo": "1",
    }
    # 返回行经过 flatten 映射
    assert len(rows) == 1
    assert rows[0]["uid"] == "research:600519:AP2026080112345678:2026-08-01"
    assert rows[0]["stock_symbol"] == "600519.SH"
    assert rows[0]["code"] == "600519"
    assert rows[0]["name"] == "贵州茅台"
    assert rows[0]["brokerage"] == "中信证券"
    assert rows[0]["rating"] == "买入"
    assert rows[0]["eps_this_year"] == 68.5
    assert rows[0]["eps_next_year"] == 75.2
    assert rows[0]["pe_this_year"] == 22.1
    assert rows[0]["pe_next_year"] == 20.3


def test_fetch_research_for_symbol_tolerates_non_list_data(monkeypatch):
    """接口异常结构 (data 非数组) 不应崩溃, 返回空行由上层报错。"""
    monkeypatch.setattr(
        eastmoney_client, "get_json",
        lambda url, params=None: {"data": None},
    )
    assert ext_presets_em._fetch_research_for_symbol("600519.SH") == []


def test_seed_research_raises_when_all_symbols_empty(monkeypatch, tmp_path):
    """全部个股 0 行必须抛错 (失败可见, 不吞错)。"""
    monkeypatch.setattr(eastmoney_client, "get_json", lambda url, params=None: {})
    monkeypatch.setattr(
        ext_presets_em, "_watchlist_symbols", lambda data_dir, limit=50: ["600519.SH", "000001.SZ"]
    )
    config = get_preset("ext_research_em")

    with pytest.raises(ValueError, match="研报返回 0 行"):
        asyncio.run(ext_presets_em._seed_research(config, tmp_path))
