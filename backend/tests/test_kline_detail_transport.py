"""个股详情 K 线传输压缩与分钟源能力门控回归测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, kline
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

_SYMBOL = "600000.SH"
_TRADE_DATE = date(2026, 1, 15)


@pytest.fixture(autouse=True)
def _isolated_settings_and_clock(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: True)
    monkeypatch.setattr("app.services.preferences.get_minute_batch_compress", lambda: True)
    monkeypatch.setattr(kline, "cn_today", lambda: _TRADE_DATE)
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 1, 15, 10, 30))
    monkeypatch.setattr(kline, "in_continuous_session", lambda: True)


def _minute_rows(count: int = 240) -> pl.DataFrame:
    start = datetime(2026, 1, 15, 9, 30)
    return pl.DataFrame(
        {
            "symbol": [_SYMBOL] * count,
            "datetime": [start + timedelta(minutes=i) for i in range(count)],
            "open": [10.0] * count,
            "high": [10.1] * count,
            "low": [9.9] * count,
            "close": [10.05] * count,
            "volume": [1_000.0] * count,
            "amount": [10_050.0] * count,
        }
    )


class _DetailRepo:
    def __init__(self, minute: pl.DataFrame | None = None) -> None:
        self.minute = minute if minute is not None else _minute_rows()
        # /minute 读取 repo.store.data_dir 判断分钟基准标记 (无标记即旧行为)
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        self.store = SimpleNamespace(data_dir=Path(tempfile.mkdtemp()))

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": [_SYMBOL],
                "name": ["浦发银行"],
                "total_shares": [1.0],
                "float_shares": [1.0],
            }
        )

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        frame = pl.DataFrame(
            {
                "symbol": [_SYMBOL] * 30,
                "date": [date(2025, 12, 15) + timedelta(days=i) for i in range(30)],
                "open": [10.0] * 30,
                "high": [10.2] * 30,
                "low": [9.8] * 30,
                "close": [10.1] * 30,
                "volume": [1_000.0] * 30,
                "amount": [10_100.0] * 30,
                "ma5": [10.0] * 30,
                "ma10": [10.0] * 30,
                "ma20": [10.0] * 30,
            }
        )
        return frame.select(columns) if columns else frame

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        return self.minute

    def get_minute_range(self, symbols, start, end, asset_type="stock") -> pl.DataFrame:
        return self.minute


class _IndexRepo:
    def get_index_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})


def _client(repo, capset: CapabilitySet | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(kline.router)
    app.include_router(indices.router)
    app.state.repo = repo
    app.state.capabilities = capset or CapabilitySet()
    app.state.quote_service = None
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "daily_compress", "minute_compress"),
    [
        (f"/api/kline/daily?symbol={_SYMBOL}&days=120", True, False),
        (f"/api/kline/minute?symbol={_SYMBOL}&date={_TRADE_DATE}", False, True),
        (f"/api/kline/minute-range?symbol={_SYMBOL}&days=10", False, True),
    ],
)
def test_detail_kline_responses_use_configured_gzip(
    monkeypatch,
    path,
    daily_compress,
    minute_compress,
):
    monkeypatch.setattr(
        "app.services.preferences.get_daily_batch_compress",
        lambda: daily_compress,
    )
    monkeypatch.setattr(
        "app.services.preferences.get_minute_batch_compress",
        lambda: minute_compress,
    )

    response = _client(_DetailRepo()).get(
        path,
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["symbol"] == _SYMBOL
    plain = _client(_DetailRepo()).get(path, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert response.json() == plain.json()
    assert int(response.headers["content-length"]) < len(plain.content)

    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: False)
    monkeypatch.setattr("app.services.preferences.get_minute_batch_compress", lambda: False)
    disabled = _client(_DetailRepo()).get(path, headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in disabled.headers
    assert disabled.json() == plain.json()


@pytest.mark.parametrize("live", [False, True])
def test_free_tier_skips_tickflow_minute_fallback(monkeypatch, live):
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_DetailRepo(pl.DataFrame())).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE), "live": live},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_not_called()


def test_custom_minute_source_remains_available_without_tickflow_capability(monkeypatch):
    provider = MagicMock()
    provider.get_minute.return_value = _minute_rows(1)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "custom")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, dataset: True
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_DetailRepo(pl.DataFrame())).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "live"
    provider.get_minute.assert_called_once()
    get_client.assert_not_called()


def test_failed_custom_source_does_not_fall_back_to_unsupported_tickflow(monkeypatch):
    provider = MagicMock()
    provider.get_minute.side_effect = RuntimeError("custom source unavailable")
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "custom")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, dataset: True
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    response = _client(_DetailRepo(pl.DataFrame()), capset).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_not_called()


def test_gzip_payload_strips_nonfinite_floats() -> None:
    """gzip 路径不得写出 NaN/Infinity: 前端 JSON.parse 会直接炸掉。

    停牌/坏源/指标暖机窗口会出现 nan 或 inf。Python json.dumps 默认 allow_nan=True,
    压缩路径会把非法 JSON 词写进 gzip 体; 未压缩路径走 Starlette allow_nan=False, 整段 500。
    助手分时小图已经按同样口径清洗 (test_assistant_intraday_chart_finite)。
    """
    import gzip as gz
    import json
    from types import SimpleNamespace

    from fastapi.responses import Response

    req = SimpleNamespace(headers={"accept-encoding": "gzip"})
    payload = {
        "symbol": _SYMBOL,
        # 压缩路径只在 JSON 超过 1024 字节时启用
        "pad": "x" * 1200,
        "rows": [
            {"close": float("nan")},
            {"close": float("inf")},
            {"close": 10.5},
        ],
    }
    result = kline._gzip_payload(req, payload, pref_key="minute_batch_compress")
    assert isinstance(result, Response)
    text = gz.decompress(result.body).decode()
    json.dumps(json.loads(text), allow_nan=False)
    data = json.loads(text)
    assert data["rows"][0]["close"] is None
    assert data["rows"][1]["close"] is None
    assert data["rows"][2]["close"] == 10.5


def test_uncompressed_payload_strips_nonfinite_floats() -> None:
    """未压缩路径同样清洗, 避免 Starlette JSONResponse allow_nan=False 整段 500。"""
    import json
    from types import SimpleNamespace

    req = SimpleNamespace(headers={})
    payload = {"rows": [{"close": float("nan")}, {"close": float("-inf")}]}
    result = kline._gzip_payload(req, payload, pref_key="daily_batch_compress")
    assert isinstance(result, dict)
    json.dumps(result, allow_nan=False)
    assert result["rows"][0]["close"] is None
    assert result["rows"][1]["close"] is None



def test_pro_tier_keeps_tickflow_minute_fallback(monkeypatch):
    capset = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    tickflow = MagicMock()
    tickflow.klines.batch.side_effect = RuntimeError("upstream unavailable")
    get_client = MagicMock(return_value=tickflow)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_DetailRepo(pl.DataFrame()), capset).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_called_once()


def test_free_tier_skips_tickflow_index_minute_fallback(monkeypatch):
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_IndexRepo()).get(
        "/api/index/minute",
        params={"symbol": "000001.SH", "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_not_called()
