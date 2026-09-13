"""指数分钟 API 契约: 非当日 fail-fast (不触数据源) + 当日 TTL 缓存。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import polars as pl

from app.api import indices


def _request() -> SimpleNamespace:
    repo = MagicMock()
    repo.get_index_instruments.return_value = pl.DataFrame({
        "symbol": ["000001.SH"],
        "name": ["上证指数"],
    })
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=MagicMock())),
    )


def _bars() -> pl.DataFrame:
    return pl.DataFrame({"datetime": ["2026-09-12 09:35:00"], "close": [3000.0]})


def test_past_date_returns_empty_without_provider_call():
    """回放/历史日期: 实时源不提供历史分时, 直接返回空, 不做数据源网络等待。"""
    indices._index_minute_cache.clear()
    past = date.today() - timedelta(days=1)
    with patch.object(indices.kline_sync, "fetch_minute_single") as fetch:
        result = indices.get_index_minute(_request(), symbol="000001.SH", trade_date=past)
    fetch.assert_not_called()
    assert result["rows"] == []
    assert result["source"] == "not_today"
    assert result["date"] == past.isoformat()
    assert result["name"] == "上证指数"
    indices._index_minute_cache.clear()


def test_today_result_cached_within_ttl():
    """当日请求走数据源, 10s 内同代码同日的重复轮询命中缓存只打一次数据源。"""
    indices._index_minute_cache.clear()
    with patch.object(indices.kline_sync, "fetch_minute_single", return_value=_bars()) as fetch:
        first = indices.get_index_minute(_request(), symbol="000001.SH", trade_date=None)
        second = indices.get_index_minute(_request(), symbol="000001.SH", trade_date=None)
    fetch.assert_called_once()
    assert first["source"] == "live"
    assert len(first["rows"]) == 1
    assert second["rows"] == first["rows"]
    assert second["source"] == "live"
    indices._index_minute_cache.clear()
