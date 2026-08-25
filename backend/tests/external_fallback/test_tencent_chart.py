"""腾讯 chart_live 分时兜底的口径与门控契约测试。

所有 HTTP 都由假响应提供：不得访问真实网络，也不得触及 repository / 持久化链路。
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.services.external_fallback import adapter as fb_adapter
from app.services.external_fallback.adapter import ExternalFallbackAdapter, FallbackReason
from app.services.external_fallback.circuit import CircuitBreaker
from app.services.external_fallback.sources.tencent_chart import (
    ChartFetchResult,
    TencentChartSource,
    _parse_minute_payload,
)


TRADE_DATE = date(2026, 8, 24)


def _payload(records: list[str], *, payload_date: str = "20260824") -> str:
    return json.dumps(
        {
            "code": 0,
            "data": {
                "sh600519": {
                    "data": {
                        "date": payload_date,
                        "data": records,
                    }
                }
            },
        }
    )


def _response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=text.encode(),
        request=httpx.Request("GET", "https://web.ifzq.gtimg.cn/appstock/app/minute/query"),
    )


def test_parser_converts_hands_and_yuan_cumulative_values_to_incremental_rows():
    rows, daily = _parse_minute_payload(
        _payload(
            [
                "0930 1271.01 298 37876098.00",
                "0931 1272.00 300 38130498.00",
            ]
        ),
        exch_code="sh600519",
        trade_date=TRADE_DATE,
    )

    assert [row.time for row in rows] == ["09:30", "09:31"]
    assert rows[0].volume == 29_800  # 298 手 → 股
    assert rows[0].amount == 37_876_098.0  # 腾讯字段已经是元，不能再 ×10000
    assert rows[1].volume == 200
    assert rows[1].amount == 254_400.0
    assert rows[0].source == "tencent_chart"
    assert rows[0].provisional is True
    assert daily.volume == 30_000
    assert daily.amount == 38_130_498.0
    assert daily.is_live is True


def test_parser_drops_lunch_and_postclose_rows_without_discarding_valid_market_data():
    rows, daily = _parse_minute_payload(
        _payload(
            [
                "1130 10.00 10 10000",
                "1200 10.10 10 10000",
                "1300 10.20 12 12040",
                "1506 10.30 12 12040",
            ]
        ),
        exch_code="sh600519",
        trade_date=TRADE_DATE,
    )

    assert [row.time for row in rows] == ["11:30", "13:00"]
    assert daily.close == 10.20
    assert daily.volume == 1_200


def test_parser_rejects_mismatched_trade_date():
    with pytest.raises(ValueError, match="date mismatch"):
        _parse_minute_payload(
            _payload(["0930 10.00 1 1000"], payload_date="20260821"),
            exch_code="sh600519",
            trade_date=TRADE_DATE,
        )


def test_source_opens_circuit_after_three_consecutive_calibration_rejections():
    circuit = CircuitBreaker(failure_threshold=5, cooldown_seconds=600)
    source = TencentChartSource(
        circuit=circuit,
        clock=lambda: 100.0,
        sleeper=lambda _seconds: None,
        rng=lambda: 0.0,
        http_getter=lambda _url, **_kwargs: _response(
            _payload(["0930 10.00 1 1000"], payload_date="20260821")
        ),
    )
    try:
        for _ in range(3):
            result = source.get_minute_chart("600519.SH", TRADE_DATE)
            assert result.transport_succeeded is False
        assert circuit.source_available("tencent_chart") is False
    finally:
        source._client.close()


class _FakeChartSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date]] = []

    def get_minute_chart(self, symbol: str, trade_date: date) -> ChartFetchResult:
        self.calls.append((symbol, trade_date))
        return ChartFetchResult(
            minutes=[
                {
                    "datetime": "2026-08-24 09:30:00",
                    "time": "09:30",
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                    "source": "tencent_chart",
                    "provisional": True,
                }
            ],
            daily={
                "date": "2026-08-24",
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 100.0,
                "amount": 1000.0,
                "source": "tencent_chart",
                "provisional": True,
                "is_live": True,
            },
            transport_succeeded=True,
        )


def test_adapter_only_fetches_current_a_share_when_local_target_day_is_empty(monkeypatch):
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_enabled", lambda: True
    )
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_scopes", lambda: ["chart_live"]
    )
    monkeypatch.setattr(fb_adapter, "_cn_today", lambda: TRADE_DATE)

    source = _FakeChartSource()
    adapter = ExternalFallbackAdapter(chart_source=source)

    result = adapter.resolve_chart_live(
        "600519.SH", TRADE_DATE, local_rows_empty=True
    )
    assert result.used_fallback is True
    assert result.source == "tencent_chart"
    assert result.reason is FallbackReason.LOCAL_CHART_MISSING
    assert source.calls == [("600519.SH", TRADE_DATE)]

    assert adapter.resolve_chart_live(
        "600519.SH", TRADE_DATE, local_rows_empty=False
    ).used_fallback is False
    assert adapter.resolve_chart_live(
        "600519.SH", date(2026, 8, 21), local_rows_empty=True
    ).used_fallback is False
    assert adapter.resolve_chart_live(
        "00700.HK", TRADE_DATE, local_rows_empty=True
    ).used_fallback is False
    assert source.calls == [("600519.SH", TRADE_DATE)]


def test_adapter_chart_live_is_default_off_and_never_calls_source(monkeypatch):
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_enabled", lambda: False
    )
    monkeypatch.setattr(
        "app.services.preferences.get_external_fallback_scopes", lambda: ["chart_live"]
    )
    monkeypatch.setattr(fb_adapter, "_cn_today", lambda: TRADE_DATE)

    source = _FakeChartSource()
    adapter = ExternalFallbackAdapter(chart_source=source)

    result = adapter.resolve_chart_live(
        "600519.SH", TRADE_DATE, local_rows_empty=True
    )

    assert result.used_fallback is False
    assert source.calls == []
