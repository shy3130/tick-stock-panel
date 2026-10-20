from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from app.api import screener as screener_api


class _MonitorEngine:
    def __init__(self, results=None):
        self.results = results or {}

    def latest_strategy_results(self):
        return self.results


def _request(tmp_path, monitor_results=None, live=None, live_date=None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    if live is not None:
        repo.get_enriched_latest = lambda: (live, live_date)
    state = SimpleNamespace(repo=repo, monitor_engine=_MonitorEngine(monitor_results))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_cached_summary_omits_rows_and_counts_realtime_expirations(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-07-20",
        "results": {
            "strategy_a": {
                "as_of": "2026-07-20",
                "total": 2,
                "rows": [{"symbol": "000001.SZ"}, {"symbol": "000002.SZ"}],
            },
        },
        "today_ever_rows": {
            "strategy_a": {
                "000001.SZ": {"symbol": "000001.SZ"},
                "600000.SH": {"symbol": "600000.SH"},
            },
        },
        "updated_at": 1,
    }
    realtime = {
        "strategy_a": {
            "as_of": "2026-07-20",
            "total": 2,
            "rows": [{"symbol": "000002.SZ"}, {"symbol": "300001.SZ"}],
        },
    }
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)

    payload = screener_api.get_cached_summary(_request(tmp_path, realtime))

    assert payload["results"] == {
        "strategy_a": {"total": 2, "as_of": "2026-07-20", "computed_at": None}
    }
    assert payload["today_ever_counts"] == {"strategy_a": 4}
    assert "rows" not in payload["results"]["strategy_a"]


def test_cached_result_returns_only_requested_rows_with_ext_and_strategy_membership(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-07-20",
        "results": {
            "strategy_a": {
                "as_of": "2026-07-20",
                "total": 1,
                "rows": [{"symbol": "000001.SZ"}],
            },
            "strategy_b": {
                "as_of": "2026-07-20",
                "total": 2,
                "rows": [{"symbol": "000001.SZ"}, {"symbol": "600000.SH"}],
            },
        },
        "today_ever_rows": {
            "strategy_a": {
                "000001.SZ": {"symbol": "000001.SZ"},
                "000002.SZ": {"symbol": "000002.SZ"},
            },
        },
        "updated_at": 1,
    }
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)
    monkeypatch.setattr(
        screener_api,
        "_load_ext_value_maps",
        lambda *_args, **_kwargs: {"concept.concept": {"000001.SZ": "银行", "000002.SZ": "科技"}},
    )

    payload = screener_api.get_cached_result(
        "strategy_a",
        _request(tmp_path),
        ext_columns="concept.concept",
    )

    assert payload["result"]["strategy"] == "strategy_a"
    assert payload["result"]["rows"] == [{"symbol": "000001.SZ", "concept.concept": "银行"}]
    assert payload["today_ever_rows"]["000002.SZ"]["concept.concept"] == "科技"
    assert payload["strategy_ids_by_symbol"] == {"000001.SZ": ["strategy_a", "strategy_b"]}


# ---------------------------------------------------------------------------
# 列表价格与实时行情对齐: 缓存只记录策略计算时刻的价格
# ---------------------------------------------------------------------------

def _live_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["002543.SZ", "600000.SH"],
        "date": ["2026-10-20", "2026-10-20"],
        "open": [7.8, 10.0],
        "high": [8.44, 10.5],
        "low": [7.77, 9.9],
        "close": [8.39, 10.2],
        "prev_close": [8.03, 10.0],
        "volume": [449688.0, 1000.0],
        "amount": [3.63e8, 1.0e7],
        "change_pct": [0.04483, 0.02],
        "change_amount": [0.36, 0.2],
        "turnover_rate": [6.8, 1.0],
        "raw_close": [8.39, 10.2],
        "raw_high": [8.44, 10.5],
        "raw_low": [7.77, 9.9],
        "quote_ts": [1792479600000, 1792479600000],
    })


def _cached_row() -> dict:
    return {
        "symbol": "002543.SZ",
        "name": "万和电气",
        "date": "2026-10-20",
        "close": 8.23,
        "prev_close": 8.03,
        "change_pct": 0.0249,
        "change_amount": 0.20,
        "amount": 1.84e8,
        "score": 50.0,
        "ma20": 7.5,
        "vol_ratio_5d": 1.32,
    }


def _cached_payload(rows=None, as_of="2026-10-20") -> dict:
    return {
        "as_of": as_of,
        "results": {
            "everbloom_qibao": {
                "as_of": as_of,
                "total": 1,
                "rows": rows if rows is not None else [_cached_row()],
            },
        },
        "today_ever_rows": {
            "everbloom_qibao": {"002543.SZ": dict(_cached_row())},
        },
        "updated_at": 1,
    }


def test_cached_rows_take_live_quote_fields_but_keep_indicator_values(monkeypatch, tmp_path):
    """列表的 close/change_pct 用实时值; 指标列仍是策略计算时点值。

    实测现场: 13:27 扫描时涨幅 2.49%, 收盘 4.48%, 详情弹窗读实时行情,
    同一只票在两处显示不同涨幅。
    """
    monkeypatch.setattr(
        screener_api.strategy_cache, "read_cache", lambda *_a: _cached_payload()
    )

    payload = screener_api.get_cached(
        _request(tmp_path, live=_live_frame(), live_date="2026-10-20"),
        ext_columns=None,
    )

    row = payload["results"]["everbloom_qibao"]["rows"][0]
    assert row["close"] == 8.39
    assert row["change_pct"] == 0.04483
    assert row["change_amount"] == 0.36
    assert row["amount"] == 3.63e8
    # 指标/评分不覆盖: 它们属于「计算时点」的策略结果
    assert row["score"] == 50.0
    assert row["ma20"] == 7.5
    assert row["vol_ratio_5d"] == 1.32

    ever = payload["today_ever_rows"]["everbloom_qibao"]["002543.SZ"]
    assert ever["close"] == 8.39
    assert ever["score"] == 50.0


def test_cached_rows_keep_historical_prices_for_other_dates(monkeypatch, tmp_path):
    """历史日期的结果不能被今天的实时价覆盖 (回看昨日/上周必须显示当时价格)。"""
    monkeypatch.setattr(
        screener_api.strategy_cache,
        "read_cache",
        lambda *_a: _cached_payload(as_of="2026-10-17"),
    )

    payload = screener_api.get_cached(
        _request(tmp_path, live=_live_frame(), live_date="2026-10-20"),
        ext_columns=None,
    )

    row = payload["results"]["everbloom_qibao"]["rows"][0]
    assert row["close"] == 8.23
    assert row["change_pct"] == 0.0249


def test_cached_rows_unchanged_when_live_cache_is_empty(monkeypatch, tmp_path):
    """实时缓存为空 (启动预热中) 时保持原样, 不能伪造价格。"""
    monkeypatch.setattr(
        screener_api.strategy_cache, "read_cache", lambda *_a: _cached_payload()
    )

    payload = screener_api.get_cached(
        _request(tmp_path, live=pl.DataFrame(), live_date=None),
        ext_columns=None,
    )

    assert payload["results"]["everbloom_qibao"]["rows"][0]["close"] == 8.23


def test_cached_result_overlays_live_quotes(monkeypatch, tmp_path):
    """/cached-result (详情接口) 与列表使用同一套实时覆盖。"""
    monkeypatch.setattr(
        screener_api.strategy_cache, "read_cache", lambda *_a: _cached_payload()
    )
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *_a, **_k: {})

    payload = screener_api.get_cached_result(
        "everbloom_qibao",
        _request(tmp_path, live=_live_frame(), live_date="2026-10-20"),
        ext_columns=None,
    )

    row = payload["result"]["rows"][0]
    assert row["close"] == 8.39
    assert row["change_pct"] == 0.04483
    assert row["score"] == 50.0
    assert payload["today_ever_rows"]["002543.SZ"]["close"] == 8.39
