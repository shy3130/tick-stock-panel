"""kline_sync 不能把目录异常 fail-soft 成空数据。

tdx_duckdb_client 把 StaleCatalogError 抛出来之后,如果 kline_sync 这一层的
`except Exception` 再吞一次,整个修复就白做了:批量同步会 `continue`、单股拉取会返回
空 DataFrame,job 照常 success + 0 行,没人知道读的是一个还没发布的快照。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.data_providers.fquant.catalog_resolver import RouteNotFoundError, StaleCatalogError
from app.services import kline_sync


class _StaleProvider:
    def get_minute(self, *_args: object, **_kwargs: object) -> pl.DataFrame:
        raise StaleCatalogError("catalog pins 20260713T090000, current is 20260714T090000")


class _NoRouteProvider:
    def get_minute(self, *_args: object, **_kwargs: object) -> pl.DataFrame:
        raise RouteNotFoundError("no route for 2019-05-01")


def test_sync_minute_batch_propagates_stale_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kline_sync, "_get_data_provider", lambda: _StaleProvider())
    with pytest.raises(StaleCatalogError):
        kline_sync.sync_minute_batch(
            ["sh600000"],
            start_time=datetime(2026, 7, 13, 9, 25),
            end_time=datetime(2026, 7, 13, 15, 5),
        )


def test_fetch_minute_single_propagates_stale_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kline_sync, "_get_data_provider", lambda: _StaleProvider())
    with pytest.raises(StaleCatalogError):
        kline_sync.fetch_minute_single("sh600000", date(2026, 7, 13))


def test_route_not_found_still_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """目录没覆盖这个日期是真的没数据,保持原来的 fail-soft 行为 (仅用于 fetch_single)。"""
    monkeypatch.setattr(kline_sync, "_get_data_provider", lambda: _NoRouteProvider())
    got = kline_sync.fetch_minute_single("sh600000", date(2019, 5, 1))
    assert got.is_empty()


def test_sync_minute_batch_propagates_route_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """sync_minute_batch 不吞 RouteNotFoundError。"""
    monkeypatch.setattr(kline_sync, "_get_data_provider", lambda: _NoRouteProvider())
    with pytest.raises(RouteNotFoundError):
        kline_sync.sync_minute_batch(
            ["sh600000"],
            start_time=datetime(2026, 7, 13, 9, 25),
            end_time=datetime(2026, 7, 14, 15, 5),
            asset_type="stock",
        )
