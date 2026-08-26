import httpx
import pytest

from app.services.ext_presets import (
    _a_symbol,
    _fetch_json,
    _flatten_concept_rows,
    _flatten_dragon_tiger_rows,
    _flatten_industry_rows,
    get_preset,
)


def test_dragon_tiger_preset_registered():
    preset = get_preset("ext_lhb_em")

    assert preset is not None
    assert preset.label == "龙虎榜"


def test_flatten_dragon_tiger_rows_maps_symbol_and_fields():
    rows = _flatten_dragon_tiger_rows([{
        "SECURITY_CODE": "600519",
        "TRADE_DATE": "2026-07-01 00:00:00",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "CLOSE_PRICE": 100.0,
        "CHANGE_RATE": 10.0,
        "BILLBOARD_NET_AMT": 1,
        "BILLBOARD_BUY_AMT": 2,
        "BILLBOARD_SELL_AMT": 3,
        "ACCUM_AMOUNT": 4,
        "EXPLANATION": "日涨幅偏离值达7%",
    }])

    assert rows == [{
        "symbol": "600519.SH",
        "code": "600519",
        "trade_date": "2026-07-01",
        "name": "贵州茅台",
        "close": 100.0,
        "change_pct": 10.0,
        "net_buy": 1,
        "buy_amount": 2,
        "sell_amount": 3,
        "turnover": 4,
        "reason": "日涨幅偏离值达7%",
    }]


def test_a_symbol_maps_bj_before_sh_b_share_prefix():
    assert _a_symbol("920001") == "920001.BJ"
    assert _a_symbol("900901") == "900901.SH"


def test_flatten_concept_rows():
    rows = _flatten_concept_rows([{"symbol": "000001.SZ", "name": "平安银行", "concepts": ["银行", "金融"]}])

    assert rows == [{
        "股票代码": "000001.SZ",
        "股票简称": "平安银行",
        "所属概念": "银行;金融",
        "symbol": "000001.SZ",
        "code": "000001",
    }]


def test_flatten_industry_rows():
    rows = _flatten_industry_rows([{"symbol": "600519.SH", "name": "贵州茅台", "industries": ["食品饮料", "白酒"]}])

    assert rows == [{
        "股票代码": "600519.SH",
        "股票简称": "贵州茅台",
        "所属同花顺行业": "食品饮料-白酒",
        "symbol": "600519.SH",
        "code": "600519",
    }]


@pytest.mark.asyncio
async def test_fetch_json_does_not_inherit_environment_proxy(monkeypatch):
    client_kwargs = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"symbol": "000001.SZ"}]

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    rows = await _fetch_json("https://example.test/data.json")

    assert rows == [{"symbol": "000001.SZ"}]
    assert client_kwargs == {"timeout": 30, "trust_env": False}
