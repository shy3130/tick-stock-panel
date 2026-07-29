from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api import dow_monitor
from app.services.dow_monitor_bars import TimeframeBars
from app.services.dow_monitor_client import DowEngineResult, LongbridgeDowClient
from app.services.dow_monitor_data import SymbolFreshness, WebStockBatch
from app.services.dow_monitor_models import (
    DowMinuteDecision,
    DowNotification,
    DowTimeframeState,
)
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _minute_decision(
    *,
    minute: int = 26,
    confidence: int = 72,
) -> DowMinuteDecision:
    zone = ZoneInfo("Asia/Hong_Kong")
    return DowMinuteDecision(
        symbol="01347.HK",
        market="hk",
        decision_minute=datetime(2026, 7, 27, 10, minute, tzinfo=zone),
        direction="BULLISH",
        direction_label="偏涨",
        action="WATCH_BUY",
        action_label="买入观察",
        confidence=confidence,
        dominant_timeframe="15m",
        confirmation_timeframes=("30m",),
        supporting_reasons=("15/30分钟结构同向偏强",),
        contrary_risks=("60分钟仍处于震荡",),
        invalidation_conditions=("跌破136.80且大单转为净流出",),
        data_status="COMPLETE",
        status_label="数据完整",
        source_timestamp=datetime(2026, 7, 27, 10, minute - 1, tzinfo=zone),
    )


def _save_bullish_decision_states(
    store: DowMonitorStore,
    *,
    source_timestamp: datetime,
) -> None:
    operations = {
        "5m": "买入触发",
        "15m": "持有",
        "30m": "持有",
        "60m": "观察",
        "day": "持有",
    }
    trends = {
        "5m": "UP",
        "15m": "UP",
        "30m": "UP",
        "60m": "RANGE",
        "day": "UP",
    }
    for timeframe in ("5m", "15m", "30m", "60m", "day"):
        store.save_state(
            DowTimeframeState(
                symbol="01347.HK",
                market="hk",
                timeframe=timeframe,
                freshness_state="LIVE",
                source_timestamp=source_timestamp,
                snapshot={},
                chart={
                    "bars": [
                        {
                            "timestamp": source_timestamp.isoformat(),
                            "open": 137.00,
                            "high": 139.20,
                            "low": 136.80,
                            "close": 138.70,
                            "volume": 320_000,
                        }
                    ],
                    "longTerm": {
                        "trendDirection": trends[timeframe],
                        "operation": operations[timeframe],
                    },
                },
                updated_at=source_timestamp,
            )
        )


def _minute_rows(*minutes: int) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": "01347.HK",
                "datetime": datetime(2026, 7, 27, 10, minute),
                "open": 137.00 + index,
                "high": 139.20 + index,
                "low": 136.80 + index,
                "close": 138.70 + index,
                "volume": 320_000 + index * 10_000,
            }
            for index, minute in enumerate(minutes)
        ]
    )


class _UnusedGateway:
    pass


class _UnusedDowClient:
    pass


def _daily_loader(*_args):
    raise AssertionError("API handlers must not load market data")


def _service(tmp_path) -> DowMonitorService:
    return DowMonitorService(
        DowMonitorStore(tmp_path),
        _UnusedGateway(),
        _UnusedDowClient(),
        _daily_loader,
        now_fn=lambda: NOW,
    )


def _client(service: DowMonitorService) -> TestClient:
    app = FastAPI()
    app.state.dow_monitor_service = service
    app.include_router(dow_monitor.router)
    return TestClient(app)


def _engine_payload_with_head_shoulders() -> dict:
    bar_time = "2026-07-23T09:55:00+08:00"
    left_shoulder = {
        "role": "LEFT_SHOULDER",
        "barIndex": 0,
        "barTime": "2026-07-23T09:30:00+08:00",
        "confirmedIndex": 1,
        "confirmedTime": "2026-07-23T09:35:00+08:00",
        "price": 9.6,
    }
    neckline_1 = {
        "role": "NECKLINE_1",
        "barIndex": 1,
        "barTime": "2026-07-23T09:35:00+08:00",
        "confirmedIndex": 2,
        "confirmedTime": "2026-07-23T09:40:00+08:00",
        "price": 10.8,
    }
    head = {
        "role": "HEAD",
        "barIndex": 2,
        "barTime": "2026-07-23T09:40:00+08:00",
        "confirmedIndex": 3,
        "confirmedTime": "2026-07-23T09:45:00+08:00",
        "price": 9.1,
    }
    neckline_2 = {
        "role": "NECKLINE_2",
        "barIndex": 3,
        "barTime": "2026-07-23T09:45:00+08:00",
        "confirmedIndex": 4,
        "confirmedTime": "2026-07-23T09:50:00+08:00",
        "price": 10.0,
    }
    right_shoulder = {
        "role": "RIGHT_SHOULDER",
        "barIndex": 4,
        "barTime": "2026-07-23T09:50:00+08:00",
        "confirmedIndex": 5,
        "confirmedTime": bar_time,
        "price": 9.35,
    }
    breakout = {
        "role": "BREAKOUT",
        "barIndex": 5,
        "barTime": bar_time,
        "confirmedIndex": 5,
        "confirmedTime": bar_time,
        "price": 10.3,
    }
    signal = {
        "family": "HEAD_SHOULDERS",
        "patternId": "hs-bottom-confirmed",
        "side": "BUY",
        "stage": "CONFIRMED",
        "barIndex": 5,
        "barTime": bar_time,
        "price": 10.3,
    }
    head_shoulders = {
        "patterns": [
            {
                "id": "hs-bottom-confirmed",
                "type": "BOTTOM",
                "stage": "CONFIRMED",
                "side": "BUY",
                "signal": signal,
                "points": {
                    "leftShoulder": left_shoulder,
                    "neckline1": neckline_1,
                    "head": head,
                    "neckline2": neckline_2,
                    "rightShoulder": right_shoulder,
                    "breakout": breakout,
                },
                "neckline": {
                    "anchors": [neckline_1, neckline_2],
                    "anchorIndexes": [1, 3],
                    "anchorTimes": [neckline_1["barTime"], neckline_2["barTime"]],
                    "anchorPrices": [10.8, 10.0],
                    "value": 9.2,
                    "triggerIndex": 5,
                    "triggerTime": bar_time,
                    "triggerValue": 9.2,
                },
                "volume": {
                    "ratio": 1.64,
                    "requiredRatio": 1.2,
                    "baseline": 109.75,
                    "triggerIndex": 5,
                    "triggerTime": bar_time,
                },
                "invalidation": {"price": 9.05},
                "geometryScore": 82.0,
                "volumeScore": 71.0,
                "contextScore": 63.0,
                "qualityScore": 216.0,
                "evidence": ["BREAK_WATCH", "CONFIRMED"],
                "lifecycle": {
                    "createdIndex": 4,
                    "lastUpdatedIndex": 5,
                    "evidence": ["BREAK_WATCH", "CONFIRMED"],
                },
            }
        ],
        "signals": [signal],
    }
    return {
        "symbol": "NBIS.US",
        "timeframe": "5m",
        "snapshot": {
            "symbol": "NBIS.US",
            "timeframe": "5m",
            "bar_time": bar_time,
            "bar_completion": "FINAL",
            "provisional": False,
            "phase": "观察",
            "phase_code": "HOLD",
            "candle_pattern": None,
            "line_id": None,
            "line_role": None,
            "line_side": None,
            "line_anchor_times": [],
            "line_value": None,
            "price_to_line_pct": None,
            "sequence_count": 0,
            "volume_ratio_20": None,
            "volume_confirmation": "NOT_CONFIRMED",
            "action": "观察",
            "action_code": "HOLD",
            "reason_codes": [],
        },
        "bars": [
            {
                "index": 0,
                "timestamp": bar_time,
                "open": 9.6,
                "high": 10.4,
                "low": 9.55,
                "close": 10.3,
                "volume": 180.0,
            }
        ],
        "lines": [],
        "signals": [],
        "longTerm": {
            "symbol": "NBIS.US",
            "timeframe": "5m",
            "bar_time": bar_time,
            "bar_completion": "FINAL",
            "provisional": False,
            "trend_direction": "UNKNOWN",
            "trend_name": "",
            "pattern_name": "",
            "operation": "观察",
            "signal_stage": "NONE",
            "breakout_type": "NONE",
            "line_id": None,
            "line_side": None,
            "line_status": None,
            "first_anchor_time": None,
            "first_anchor_price": None,
            "second_anchor_time": None,
            "second_anchor_price": None,
            "line_value": None,
            "key_level_type": None,
            "key_level_time": None,
            "key_level_price": None,
            "first_break_time": None,
            "recent_low_scale": None,
            "recent_low_label": None,
            "recent_low_time": None,
            "recent_low_price": None,
            "recent_low_confirmed_time": None,
            "evidence_codes": [],
            "failure_reason": None,
        },
        "headShoulders": head_shoulders,
        "evaluatedAt": "2026-07-23T01:55:00Z",
    }


def test_longbridge_client_accepts_head_shoulders_engine_payload() -> None:
    payload = _engine_payload_with_head_shoulders()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=payload)
    )

    with LongbridgeDowClient(
        "http://dow-engine.test",
        transport=transport,
    ) as engine_client:
        result = engine_client.evaluate(
            "NBIS.US",
            "5m",
            payload["bars"],
            "FINAL",
            NOW,
        )

    assert result.model_dump(mode="json", by_alias=True)["headShoulders"] == (
        payload["headShoulders"]
    )


def test_head_shoulders_engine_payload_keeps_strict_nested_validation() -> None:
    payload = _engine_payload_with_head_shoulders()
    payload["headShoulders"]["patterns"][0]["unknownField"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        DowEngineResult.model_validate(payload)


def test_head_shoulders_survives_engine_result_into_detail_chart(
    tmp_path,
    monkeypatch,
) -> None:
    payload = _engine_payload_with_head_shoulders()
    service = _service(tmp_path)
    item = service.store.upsert_symbol("NBIS.US", "us", True)
    monkeypatch.setattr(
        "app.services.dow_monitor_service.enrich_dow_chart_bars",
        lambda _symbol, bars: bars,
    )

    service._save_result(
        item,
        "5m",
        TimeframeBars(
            completed=payload["bars"],
            forming={},
            completion="FINAL",
            source_timestamp=NOW,
        ),
        DowEngineResult.model_validate(payload),
        NOW,
        {},
    )

    response = _client(service).get("/api/dow-monitor/NBIS.US?timeframe=5m")

    assert response.status_code == 200
    assert response.json()["chart"]["headShoulders"] == payload["headShoulders"]


def test_symbols_are_normalized_and_duplicate_add_is_idempotent(tmp_path) -> None:
    service = _service(tmp_path)
    client = _client(service)

    first = client.post("/api/dow-monitor/symbols", json={"symbol": " 01347.hk "})
    second = client.post("/api/dow-monitor/symbols", json={"symbol": "01347.HK"})

    assert first.status_code == 200
    assert first.json()["symbol"] == "01347.HK"
    assert second.status_code == 200
    assert len(client.get("/api/dow-monitor/symbols").json()["symbols"]) == 1


def test_zero_padded_hk_alias_cannot_create_a_duplicate_monitor(tmp_path) -> None:
    service = _service(tmp_path)
    client = _client(service)

    first = client.post("/api/dow-monitor/symbols", json={"symbol": "02714.HK"})
    second = client.post("/api/dow-monitor/symbols", json={"symbol": "2714.HK"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["symbol"] == "02714.HK"
    assert [item["symbol"] for item in client.get("/api/dow-monitor/symbols").json()["symbols"]] == [
        "02714.HK"
    ]


def test_symbols_reject_unsupported_suffix(tmp_path) -> None:
    client = _client(_service(tmp_path))

    response = client.post("/api/dow-monitor/symbols", json={"symbol": "BTC.CRYPTO"})

    assert response.status_code == 400


def test_patch_switch_persists_normalized_symbol(tmp_path) -> None:
    service = _service(tmp_path)
    client = _client(service)
    client.post("/api/dow-monitor/symbols", json={"symbol": "01347.HK"})

    response = client.patch("/api/dow-monitor/symbols/ 01347.hk ", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert service.store.list_symbols()[0].enabled is False


def test_market_filter_changes_response_only_not_enabled_state(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.upsert_symbol("INTC.US", "us", True)
    client = _client(service)

    response = client.get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    assert {item["market"] for item in response.json()["symbols"]} == {"hk"}
    assert next(item for item in service.store.list_symbols() if item.symbol == "INTC.US").enabled


def test_overview_api_exposes_authoritative_quote_header_fields(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service._latest_quotes_by_symbol["01347.HK"] = {
        "symbol": "01347.HK",
        "name": "华丰科技",
        "last_price": 13.47,
        "change_pct": 0.0125,
        "timestamp": int(NOW.timestamp() * 1_000),
    }

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    expected = {
        "name": "华丰科技",
        "last_price": 13.47,
        "change_pct": 0.0125,
        "quote_timestamp": int(NOW.timestamp() * 1_000),
    }
    item = response.json()["symbols"][0]
    assert {key: item[key] for key in expected} == expected


def test_overview_api_exposes_trading_day_intraday_capital(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)

    def fake_fetch(symbols, *, now, max_quote_age_minutes):
        assert symbols == ["1347.HK"]
        assert max_quote_age_minutes >= 60
        return {
            "1347.HK": {
                "capital_minute": "2026-07-23 15:30:00",
                "total_net": 186.5,
                "large_net": 92.25,
                "total_in": 560.0,
                "total_out": 373.5,
                "large_net_ratio": 0.19,
                "flow_15m": -8.5,
                "flow_30m": 22.0,
                "flow_today": 186.5,
                "last_flow_time": "2026-07-23 15:29:00",
                "flow_points": 88,
            }
        }

    monkeypatch.setattr(
        "app.services.dow_monitor_service._fetch_realtime_signal_rows",
        fake_fetch,
    )
    monkeypatch.setattr(
        service,
        "_intraday_capital_windows_by_symbol",
        lambda symbols: {
            "1347.HK": [
                {
                    "label": "近30分钟",
                    "minutes": 30,
                    "start_time": "2026-07-23 15:00:00",
                    "end_time": "2026-07-23 15:30:00",
                    "start_price": 13.1,
                    "end_price": 13.47,
                    "price_change_pct": 2.824,
                    "start_total_net": 100.0,
                    "end_total_net": 186.5,
                    "total_net_delta": 86.5,
                    "start_large_net": 48.0,
                    "end_large_net": 92.25,
                    "large_net_delta": 44.25,
                }
            ]
        },
    )

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    assert response.json()["symbols"][0]["intraday_capital"] == {
        "capital_minute": "2026-07-23 15:30:00",
        "total_net": 186.5,
        "large_net": 92.25,
        "total_in": 560.0,
        "total_out": 373.5,
        "large_net_ratio": 0.19,
        "flow_15m": -8.5,
        "flow_30m": 22.0,
        "flow_today": 186.5,
        "last_flow_time": "2026-07-23 15:29:00",
        "flow_points": 88,
        "source": "trading_day",
        "quality": "DELAYED",
        "windows": [
            {
                "label": "近30分钟",
                "minutes": 30,
                "start_time": "2026-07-23 15:00:00",
                "end_time": "2026-07-23 15:30:00",
                "start_price": 13.1,
                "end_price": 13.47,
                "price_change_pct": 2.824,
                "start_total_net": 100.0,
                "end_total_net": 186.5,
                "total_net_delta": 86.5,
                "start_large_net": 48.0,
                "end_large_net": 92.25,
                "large_net_delta": 44.25,
            }
        ],
    }


def test_overview_matches_leading_zero_display_alias_to_canonical_capital(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)

    monkeypatch.setattr(
        service,
        "_intraday_capital_by_symbol",
        lambda symbols: {
            "1347.HK": {
                "capital_minute": "2026-07-23 15:30:00",
                "total_net": 0,
                "large_net": 0,
                "flow_15m": 0,
                "flow_30m": 0,
                "flow_points": 17,
                "source": "trading_day",
            }
        },
    )

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    item = response.json()["symbols"][0]
    assert item["symbol"] == "01347.HK"
    assert item["intraday_capital"]["total_net"] == 0
    assert item["intraday_capital"]["large_net"] == 0


def test_overview_matches_leading_zero_display_alias_to_canonical_quote(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service._retain_latest_quotes(
        [
            {
                "symbol": "1347.HK",
                "name": "HUA HONG SEMI",
                "last_price": 148.6,
                "change_pct": -0.8,
                "timestamp": int(NOW.timestamp() * 1_000),
            }
        ]
    )
    monkeypatch.setattr(service, "_intraday_capital_by_symbol", lambda symbols: {})

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    item = response.json()["symbols"][0]
    assert item["symbol"] == "01347.HK"
    assert item["name"] == "HUA HONG SEMI"
    assert item["last_price"] == 148.6


def test_overview_api_exposes_next_day_realtime_context(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service._latest_quotes_by_symbol["01347.HK"] = {
        "symbol": "01347.HK",
        "last_price": 14.25,
        "timestamp": int(NOW.timestamp() * 1_000),
    }
    service._next_day_direction_by_symbol["01347.HK"] = {
        "symbol": "01347.HK",
        "as_of": "2026-07-23",
        "score": 86.0,
        "probability": 0.86,
        "direction_label": "强势偏多",
        "key_levels": {"support": 12.8, "resistance": 14.2, "stop": 12.42},
        "evidence": ["趋势站上MA20且MA20不弱于MA60"],
    }

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    next_day = response.json()["symbols"][0]["next_day_direction"]
    assert next_day["realtime_signal"] == "BUY_TRIGGER"
    assert next_day["realtime_label"] == "买点触发"


def test_next_day_direction_context_reuses_daily_strategy_factors(tmp_path) -> None:
    service = _service(tmp_path)
    rows = []
    for index in range(70):
        close = 10.0 + index * 0.08 + (-0.35 if index % 6 == 0 else 0.0)
        volume = 1_000_000 + index * 5_000
        if index >= 66:
            volume *= 1.8
        rows.append(
            {
                "symbol": "01347.HK",
                "trade_date": (datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=index)).date().isoformat(),
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.2,
                "close": close,
                "volume": volume,
            }
        )

    context = service._compute_next_day_direction("01347.HK", pl.DataFrame(rows))

    assert context is not None
    assert context["score"] >= 70
    assert context["key_levels"]["support"] is not None
    assert "ma20" in context["metrics"]


def test_next_day_direction_context_does_not_emit_intraday_monitor_notification(tmp_path) -> None:
    service = _service(tmp_path)
    item = service.store.upsert_symbol("01347.HK", "hk", True)
    service._latest_quotes_by_symbol["01347.HK"] = {"symbol": "01347.HK", "last_price": 14.25}
    service._next_day_direction_by_symbol["01347.HK"] = {
        "symbol": "01347.HK",
        "as_of": "2026-07-23",
        "score": 86.0,
        "probability": 0.86,
        "direction_label": "强势偏多",
        "key_levels": {"support": 12.8, "resistance": 14.2, "stop": 12.42},
        "evidence": ["趋势站上MA20且MA20不弱于MA60"],
    }
    notification_index = {}

    service._maybe_append_next_day_notification(item, NOW, notification_index)
    service._maybe_append_next_day_notification(item, NOW + timedelta(seconds=15), notification_index)

    notifications = service.store.list_notifications()
    assert notifications == []


def test_detail_validates_timeframe_and_preserves_long_term_sidecar(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.save_state(
        DowTimeframeState(
            symbol="01347.HK",
            market="hk",
            timeframe="5m",
            freshness_state="LIVE",
            source_timestamp=NOW,
            snapshot={},
            chart={"longTerm": {"trendDirection": "UP", "operation": "持有"}},
            updated_at=NOW,
        )
    )
    client = _client(service)

    valid = client.get("/api/dow-monitor/01347.hk?timeframe=5m")
    invalid = client.get("/api/dow-monitor/01347.HK?timeframe=1m")

    assert valid.status_code == 200
    assert valid.json()["chart"]["longTerm"] == {
        "trendDirection": "UP",
        "operation": "持有",
    }
    assert invalid.status_code == 422


def test_notifications_read_and_status_expose_persisted_timestamps(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.upsert_symbol("INTC.US", "us", False)
    service.store.append_notification(
        DowNotification(
            notification_id="notification-1",
            event_key="event-1",
            symbol="01347.HK",
            market="hk",
            timeframe="5m",
            side="BUY",
            action_name="buy",
            shape_name="shape",
            triggered_at=NOW,
            trigger_price=12.3,
            snapshot_payload={"engine": {"longTerm": {"trendDirection": "UP"}}},
        )
    )
    client = _client(service)

    notifications = client.get("/api/dow-monitor/notifications?market=hk&unreadOnly=true")
    read = client.patch("/api/dow-monitor/notifications/notification-1/read")
    status = client.get("/api/dow-monitor/status")

    assert notifications.status_code == 200
    assert notifications.json()["notifications"][0]["triggered_at"] == "2026-07-23T08:00:00Z"
    assert read.status_code == 200
    assert read.json()["read_at"] is not None
    assert (
        client.get("/api/dow-monitor/notifications?unreadOnly=true").json()["notifications"] == []
    )
    assert status.status_code == 200
    assert set(status.json()) >= {
        "last_started_at",
        "last_completed_at",
        "last_success_at",
    }
    assert status.json()["enabled_markets"] == ["hk"]
    assert status.json()["open_enabled_markets"] == []


def test_health_status_is_available_only_to_loopback_patrol_without_browser_session(
    monkeypatch,
) -> None:
    from app import main
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def request(peer: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/dow-monitor/status",
                "raw_path": b"/api/dow-monitor/status",
                "query_string": b"",
                "headers": headers or [],
                "client": (peer, 50_000),
                "server": ("tickflow", 3018),
            }
        )

    ipv4 = asyncio.run(main.auth_middleware(request("127.0.0.1"), call_next))
    ipv6 = asyncio.run(main.auth_middleware(request("::1"), call_next))
    remote = asyncio.run(main.auth_middleware(request("192.168.10.99"), call_next))
    spoofed = asyncio.run(
        main.auth_middleware(
            request("192.168.10.99", [(b"x-forwarded-for", b"127.0.0.1")]),
            call_next,
        )
    )

    assert ipv4.status_code == 200
    assert ipv6.status_code == 200
    assert remote.status_code == 401
    assert spoofed.status_code == 401


def test_monitor_symbol_feed_is_get_only_and_loopback_only_without_browser_session(
    monkeypatch,
) -> None:
    from app import main
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def request(peer: str, method: str) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": "/api/dow-monitor/symbols",
                "raw_path": b"/api/dow-monitor/symbols",
                "query_string": b"",
                "headers": [],
                "client": (peer, 50_000),
                "server": ("tickflow", 3018),
            }
        )

    loopback_get = asyncio.run(main.auth_middleware(request("127.0.0.1", "GET"), call_next))
    loopback_post = asyncio.run(main.auth_middleware(request("127.0.0.1", "POST"), call_next))
    remote_get = asyncio.run(main.auth_middleware(request("192.168.10.99", "GET"), call_next))

    assert loopback_get.status_code == 200
    assert loopback_post.status_code == 401
    assert remote_get.status_code == 401


def test_notification_read_returns_exact_oldest_notification_beyond_list_limit(tmp_path) -> None:
    service = _service(tmp_path)
    first = DowNotification(
        notification_id="first-notification",
        event_key="first-event",
        symbol="01347.HK",
        market="hk",
        timeframe="5m",
        side="BUY",
        action_name="buy",
        shape_name="shape",
        triggered_at=NOW - timedelta(days=1),
        trigger_price=12.3,
        snapshot_payload={},
    )
    assert service.store.append_notification(first)
    for index in range(1, 1_001):
        assert service.store.append_notification(
            first.model_copy(
                update={
                    "notification_id": f"notification-{index}",
                    "event_key": f"event-{index}",
                    "triggered_at": NOW + timedelta(seconds=index),
                }
            )
        )

    response = _client(service).patch("/api/dow-monitor/notifications/first-notification/read")

    assert response.status_code == 200
    assert response.json()["notification_id"] == "first-notification"
    assert response.json()["read_at"] is not None


def test_uninitialized_service_returns_503() -> None:
    app = FastAPI()
    app.include_router(dow_monitor.router)

    response = TestClient(app).get("/api/dow-monitor/status")

    assert response.status_code == 503


def test_lifecycle_starts_single_monitor_with_registered_clickhouse_provider(
    monkeypatch, tmp_path
) -> None:
    from app import main

    events: list[object] = []

    class FakeStore:
        def __init__(self, data_dir) -> None:
            events.append(("store", data_dir))

    class FakeGateway:
        def __init__(self, provider) -> None:
            events.append(("gateway", provider))

    class FakeClient:
        def __init__(self, endpoint) -> None:
            events.append(("client", endpoint))

    class FakeService:
        def __init__(
            self,
            store,
            gateway,
            client,
            daily_loader,
            *,
            minute_result_materializer=None,
        ) -> None:
            self.store = store
            self.gateway = gateway
            self.client = client
            self.daily_loader = daily_loader
            self.minute_result_materializer = minute_result_materializer
            self.started = 0

        async def start(self) -> None:
            self.started += 1
            events.append("start")

    provider = SimpleNamespace(get_daily=lambda *_args: "daily")
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(main, "DowMonitorStore", FakeStore, raising=False)
    monkeypatch.setattr(main, "WebStockMonitorGateway", FakeGateway, raising=False)
    monkeypatch.setattr(main, "LongbridgeDowClient", FakeClient, raising=False)
    monkeypatch.setattr(main, "DowMonitorService", FakeService, raising=False)

    asyncio.run(main._start_dow_monitor(app, tmp_path, provider, "http://engine"))

    assert events == [
        ("store", tmp_path),
        ("gateway", provider),
        ("client", "http://engine"),
        "start",
    ]
    assert app.state.dow_monitor_service.started == 1
    assert app.state.dow_monitor_service.daily_loader("01347.HK", NOW) == "daily"
    assert app.state.dow_monitor_service.minute_result_materializer is not None


def test_lifecycle_stops_monitor_before_closing_its_client() -> None:
    from app import main

    events: list[str] = []

    class FakeService:
        async def stop(self) -> None:
            events.append("stop")

    class FakeClient:
        def close(self) -> None:
            events.append("close")

    app = SimpleNamespace(
        state=SimpleNamespace(dow_monitor_service=FakeService(), dow_monitor_client=FakeClient())
    )

    asyncio.run(main._stop_dow_monitor(app))

    assert events == ["stop", "close"]


def test_real_lifespan_loads_provider_before_monitor_and_stops_before_shared_close(
    monkeypatch, tmp_path
) -> None:
    from app import main
    from app.data_providers import custom as custom_sources
    from app.jobs import daily_pipeline
    from app.services import (
        auth,
        ext_presets,
        ext_pull,
        financial_sync,
        preferences,
        wecom_bot_service,
    )
    from app.services import depth_service as depth_service_module
    from app.services import screener as screener_module
    from app.strategy import engine as strategy_engine_module
    from app.strategy import monitor as strategy_monitor_module
    from app.strategy import monitor_rules

    events: list[str] = []
    provider = object()

    class FakeDataStore:
        data_dir = tmp_path

    class FakeRepository:
        enriched_ready = False

        def __init__(self, _store) -> None:
            pass

        def get_matrix_data_generation(self, _asset_type) -> None:
            events.append("matrix")

        def refresh_cache(self, *, background) -> None:
            assert background is True

    class FakeCapabilities:
        def all(self) -> list[object]:
            return []

    class FakeQuoteService:
        def set_repo(self, _repo) -> None:
            pass

        def boot_check(self) -> None:
            pass

        def set_app_state(self, _state) -> None:
            pass

        def stop(self) -> None:
            events.append("shared-close")

    class FakeStrategyMonitor:
        pass

    class FakeDepthService:
        def set_repo(self, _repo) -> None:
            pass

        def set_app_state(self, _state) -> None:
            pass

        def boot_check(self) -> None:
            pass

        def start_polling(self) -> None:
            pass

        def stop_polling(self) -> None:
            pass

    class FakeScheduler:
        def shutdown(self, *, wait) -> None:
            assert wait is False

    class FakePullScheduler:
        def start(self, _data_dir) -> None:
            pass

        def refresh(self, _data_dir) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeFinancialScheduler:
        def start(self, _data_dir, _capset) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeScreener:
        _load_enriched_history = staticmethod(lambda *_args: None)

        def __init__(self, _repo, asset_type="stock") -> None:
            assert asset_type in {"stock", "etf"}

    class FakeStrategyEngine:
        def __init__(self, *, strategy_dirs) -> None:
            assert strategy_dirs

        def list_strategies(self) -> list[dict]:
            return []

    class FakeMonitorEngine:
        rule_count = 0

        def set_strategy_engine(self, _engine) -> None:
            pass

        def set_data_dir(self, _data_dir) -> None:
            pass

        def set_history_loader(self, _loader) -> None:
            pass

        def set_history_loader_etf(self, _loader) -> None:
            pass

        def set_rules(self, _rules) -> None:
            pass

    class FakeMonitorService:
        async def stop(self) -> None:
            events.append("monitor-stop")

    class FakeMonitorClient:
        def close(self) -> None:
            events.append("monitor-client-close")

    async def fake_start(app, _data_dir, resolved_provider, _endpoint) -> None:
        assert resolved_provider is provider
        assert events == ["providers-loaded", "clickhouse-resolved"]
        app.state.dow_monitor_service = FakeMonitorService()
        app.state.dow_monitor_client = FakeMonitorClient()
        events.append("monitor-start")

    async def fake_presets(_data_dir) -> None:
        pass

    monkeypatch.setattr(main, "DataStore", FakeDataStore)
    monkeypatch.setattr(main, "KlineRepository", FakeRepository)
    monkeypatch.setattr(main, "QuoteService", FakeQuoteService)
    monkeypatch.setattr(main, "detect_capabilities", lambda: FakeCapabilities())
    monkeypatch.setattr(main, "_start_dow_monitor", fake_start)
    monkeypatch.setattr(main.settings, "backtest_matrix_disk_cache_enabled", False)
    monkeypatch.setattr(main.settings, "backtest_matrix_cache_prewarm", False)
    monkeypatch.setattr(auth, "bootstrap_from_env", lambda: None)
    monkeypatch.setattr(custom_sources, "load_all", lambda: events.append("providers-loaded"))
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(
        custom_sources,
        "get_provider",
        lambda name: (
            events.append("clickhouse-resolved") or provider if name == "clickhouse" else None
        ),
    )
    monkeypatch.setattr(daily_pipeline, "set_app_state", lambda _state: None)
    monkeypatch.setattr(daily_pipeline, "start_scheduler", lambda *_args: FakeScheduler())
    monkeypatch.setattr(strategy_monitor_module, "StrategyMonitorService", FakeStrategyMonitor)
    monkeypatch.setattr(strategy_monitor_module, "MonitorRuleEngine", FakeMonitorEngine)
    monkeypatch.setattr(depth_service_module, "DepthService", FakeDepthService)
    monkeypatch.setattr(ext_presets, "ensure_builtin_presets", fake_presets)
    monkeypatch.setattr(ext_pull, "pull_scheduler", FakePullScheduler())
    monkeypatch.setattr(financial_sync, "financial_scheduler", FakeFinancialScheduler())
    monkeypatch.setattr(screener_module, "ScreenerService", FakeScreener)
    monkeypatch.setattr(strategy_engine_module, "StrategyEngine", FakeStrategyEngine)
    monkeypatch.setattr(preferences, "get_strategy_monitor_enabled", lambda: False)
    monkeypatch.setattr(monitor_rules, "load_all", lambda _data_dir: [])
    monkeypatch.setattr(
        wecom_bot_service,
        "WecomBotService",
        lambda: SimpleNamespace(
            set_app_state=lambda _state: None,
            boot_check=lambda: None,
            stop=lambda: None,
        ),
    )

    async def exercise() -> None:
        app = FastAPI()
        async with main.lifespan(app):
            assert events == ["providers-loaded", "clickhouse-resolved", "monitor-start"]

    asyncio.run(exercise())

    assert events.index("monitor-stop") < events.index("monitor-client-close")
    assert events.index("monitor-client-close") < events.index("shared-close")


def test_minute_decision_is_immutable_for_same_symbol_and_minute(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    first = _minute_decision(confidence=72)
    changed = first.model_copy(update={"confidence": 91})

    assert store.save_minute_decision(first) == first
    assert store.save_minute_decision(changed) == first
    assert DowMonitorStore(tmp_path).get_minute_decision("01347.HK") == first


def test_minute_decision_advances_once_for_a_newer_minute(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    current = _minute_decision(minute=26, confidence=72)
    newer = _minute_decision(minute=27, confidence=81)

    store.save_minute_decision(current)

    assert store.save_minute_decision(newer) == newer
    assert store.save_minute_decision(current) == newer
    assert store.get_minute_decision("01347.HK") == newer


def test_remove_symbol_also_removes_its_minute_decision(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("01347.HK", "hk", True)
    store.save_minute_decision(_minute_decision())

    assert store.remove_symbol("01347.HK") is True
    assert store.get_minute_decision("01347.HK") is None


def test_corrupt_minute_decision_file_is_ignored(tmp_path) -> None:
    decision_path = tmp_path / "user_data" / "dow_monitor_minute_decisions.json"
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text("{not-json", encoding="utf-8")

    assert DowMonitorStore(tmp_path).get_minute_decision("01347.HK") is None


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"decision_minute": datetime(2026, 7, 27, 10, 26)}, "timezone-aware"),
        ({"confidence": 101}, "less than or equal to 100"),
        ({"confidence": -1}, "greater than or equal to 0"),
    ],
)
def test_minute_decision_rejects_invalid_time_and_confidence(update, message) -> None:
    payload = _minute_decision().model_dump()
    payload.update(update)

    with pytest.raises(ValidationError, match=message):
        DowMinuteDecision.model_validate(payload)


def test_service_creates_only_one_decision_for_each_complete_minute(tmp_path) -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    now = datetime(2026, 7, 27, 10, 26, 10, tzinfo=zone)
    service = _service(tmp_path)
    item = service.store.upsert_symbol("01347.HK", "hk", True)
    _save_bullish_decision_states(
        service.store,
        source_timestamp=datetime(2026, 7, 27, 10, 25, tzinfo=zone),
    )
    positive_capital = {
        "total_net": 8_000_000,
        "large_net": 2_000_000,
        "flow_15m": 600_000,
        "flow_30m": 1_200_000,
    }

    service._refresh_minute_decision(
        item,
        _minute_rows(25),
        positive_capital,
        now,
    )
    first = service.store.get_minute_decision("01347.HK")
    service._refresh_minute_decision(
        item,
        _minute_rows(25),
        {
            "total_net": -8_000_000,
            "large_net": -2_000_000,
            "flow_15m": -600_000,
            "flow_30m": -1_200_000,
        },
        now + timedelta(seconds=15),
    )

    assert first is not None
    assert first.decision_minute == datetime(2026, 7, 27, 10, 26, tzinfo=zone)
    assert first.direction_label == "偏涨"
    assert service.store.get_minute_decision("01347.HK") == first


def test_service_advances_decision_when_next_complete_minute_arrives(tmp_path) -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    service = _service(tmp_path)
    item = service.store.upsert_symbol("01347.HK", "hk", True)
    _save_bullish_decision_states(
        service.store,
        source_timestamp=datetime(2026, 7, 27, 10, 26, tzinfo=zone),
    )
    capital = {
        "total_net": 8_000_000,
        "large_net": 2_000_000,
        "flow_15m": 600_000,
        "flow_30m": 1_200_000,
    }
    service._refresh_minute_decision(
        item,
        _minute_rows(25),
        capital,
        datetime(2026, 7, 27, 10, 26, 10, tzinfo=zone),
    )

    service._refresh_minute_decision(
        item,
        _minute_rows(25, 26),
        capital,
        datetime(2026, 7, 27, 10, 27, 10, tzinfo=zone),
    )

    advanced = service.store.get_minute_decision("01347.HK")
    assert advanced is not None
    assert advanced.decision_minute == datetime(2026, 7, 27, 10, 27, tzinfo=zone)
    assert advanced.source_timestamp == datetime(2026, 7, 27, 10, 26, tzinfo=zone)


def test_presented_minute_decision_waits_then_degrades_after_90_seconds(tmp_path) -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    service = _service(tmp_path)
    item = service.store.upsert_symbol("01347.HK", "hk", True)
    decision = _minute_decision()

    waiting = service._present_minute_decision(
        item,
        decision,
        datetime(2026, 7, 27, 10, 27, 10, tzinfo=zone),
    )
    delayed = service._present_minute_decision(
        item,
        decision,
        datetime(2026, 7, 27, 10, 27, 31, tzinfo=zone),
    )

    assert waiting is not None
    assert waiting["data_status"] == "WAITING_NEW_MINUTE"
    assert waiting["action_label"] == "买入观察"
    assert delayed is not None
    assert delayed["data_status"] == "DELAYED"
    assert delayed["status_label"] == "数据延迟"
    assert delayed["action"] == "OBSERVE"
    assert delayed["action_label"] == "继续观察"


def test_presented_minute_decision_is_preserved_as_observe_when_market_closed(
    tmp_path,
) -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    service = _service(tmp_path)
    item = service.store.upsert_symbol("01347.HK", "hk", True)

    presented = service._present_minute_decision(
        item,
        _minute_decision(),
        datetime(2026, 7, 27, 17, 0, tzinfo=zone),
    )

    assert presented is not None
    assert presented["direction_label"] == "偏涨"
    assert presented["confidence"] == 72
    assert presented["data_status"] == "MARKET_CLOSED"
    assert presented["status_label"] == "已收盘"
    assert presented["action_label"] == "继续观察"


def test_overview_api_exposes_persisted_minute_decision(tmp_path) -> None:
    service = _service(tmp_path)
    service.store.upsert_symbol("01347.HK", "hk", True)
    service.store.save_minute_decision(_minute_decision())

    response = _client(service).get("/api/dow-monitor/overview?market=hk")

    assert response.status_code == 200
    decision = response.json()["symbols"][0]["minute_decision"]
    assert decision["direction_label"] == "偏涨"
    assert decision["action_label"] == "继续观察"
    assert decision["data_status"] == "MARKET_CLOSED"


def test_run_once_persists_minute_decision_after_successful_symbol_cycle(
    tmp_path,
    monkeypatch,
) -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    now = datetime(2026, 7, 27, 10, 26, 10, tzinfo=zone)
    rows = _minute_rows(25)

    class Gateway:
        def fetch_since(self, _starts, _end):
            return WebStockBatch(
                quotes=[],
                minute_rows=rows,
                source_timestamp=datetime(2026, 7, 27, 2, 25, tzinfo=UTC),
                freshness_by_symbol={
                    "01347.HK": SymbolFreshness(state="LIVE", reason=None)
                },
                gap_details={"01347.HK": []},
            )

    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    _save_bullish_decision_states(
        store,
        source_timestamp=datetime(2026, 7, 27, 10, 25, tzinfo=zone),
    )
    service = DowMonitorService(
        store,
        Gateway(),
        _UnusedDowClient(),
        _daily_loader,
        now_fn=lambda: now,
    )
    monkeypatch.setattr(
        service,
        "_evaluate_symbol",
        lambda *_args, **_kwargs: (None, True),
    )
    monkeypatch.setattr(
        service,
        "_intraday_capital_by_symbol",
        lambda _symbols: {
            item.symbol: {
                "total_net": 8_000_000,
                "large_net": 2_000_000,
                "flow_15m": 600_000,
                "flow_30m": 1_200_000,
            }
        },
    )

    asyncio.run(service.run_once())

    decision = store.get_minute_decision(item.symbol)
    assert decision is not None
    assert decision.decision_minute == datetime(2026, 7, 27, 10, 26, tzinfo=zone)
    assert decision.action_label == "买入观察"


def test_store_preserves_the_production_symbol_feed_contract(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("01347.HK", "hk", True)

    authoritative, symbols = store.load_symbol_feed()

    assert authoritative is True
    assert [item.symbol for item in symbols] == ["01347.HK"]


def test_store_preserves_the_production_state_listing_contract(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    state = DowTimeframeState(
        symbol="01347.HK",
        market="hk",
        timeframe="15m",
        freshness_state="LIVE",
        source_timestamp=NOW,
        snapshot={},
        chart={},
        updated_at=NOW,
    )
    store.save_state(state)

    assert store.list_states() == [state]


def test_run_once_persists_each_symbol_decision_before_evaluating_the_next(
    tmp_path,
    monkeypatch,
) -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    now = datetime(2026, 7, 27, 10, 26, 10, tzinfo=zone)
    rows = _minute_rows(25)

    class Gateway:
        def fetch_since(self, _starts, _end):
            return WebStockBatch(
                quotes=[],
                minute_rows=rows,
                source_timestamp=datetime(2026, 7, 27, 2, 25, tzinfo=UTC),
                freshness_by_symbol={
                    first.symbol: SymbolFreshness(state="LIVE", reason=None),
                    second.symbol: SymbolFreshness(state="LIVE", reason=None),
                },
                gap_details={first.symbol: [], second.symbol: []},
            )

    store = DowMonitorStore(tmp_path)
    first = store.upsert_symbol("01347.HK", "hk", True)
    second = store.upsert_symbol("00981.HK", "hk", True)
    _save_bullish_decision_states(store, source_timestamp=now)
    for timeframe in ("5m", "15m", "30m", "60m", "day"):
        first_state = store.get_state(first.symbol, timeframe)
        assert first_state is not None
        store.save_state(first_state.model_copy(update={"symbol": second.symbol}))
    service = DowMonitorService(
        store,
        Gateway(),
        _UnusedDowClient(),
        _daily_loader,
        now_fn=lambda: now,
    )
    observed_before_second: list[bool] = []

    def evaluate(item, *_args, **_kwargs):
        if item.symbol == second.symbol:
            observed_before_second.append(
                store.get_minute_decision(first.symbol) is not None
            )
        return None, True

    monkeypatch.setattr(service, "_evaluate_symbol", evaluate)
    monkeypatch.setattr(
        service,
        "_intraday_capital_by_symbol",
        lambda _symbols: {
            first.symbol: {
                "total_net": 8_000_000,
                "large_net": 2_000_000,
                "flow_15m": 600_000,
                "flow_30m": 1_200_000,
            }
        },
    )

    asyncio.run(service.run_once())

    assert observed_before_second == [True]
