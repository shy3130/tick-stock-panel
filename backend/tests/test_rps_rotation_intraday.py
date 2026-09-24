"""盘中 enriched generation 变化后涨幅轮动仍须有数据。

交易时段实时 enriched 每个 tick 落盘都会换 generation, 而历史缓存几分钟才
重建一次; get_enriched_range 按代校验不一致即返回 None, 轮动矩阵因此在盘中
几乎一直为空("暂无数据")。轮动只读涨跌幅, 应退回内存历史缓存, 并用当日
实时缓存覆盖今天的行。
"""
from __future__ import annotations

import types
from datetime import date, timedelta

import polars as pl
import pytest

from app.services import rps_rotation

_TODAY = date(2026, 9, 24)
_HISTORY_DAYS = 40
_MEMBERS = ("银行", "证券")


@pytest.fixture(autouse=True)
def _clear_caches():
    rps_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    yield
    rps_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()


@pytest.fixture(autouse=True)
def _map(monkeypatch):
    map_df = pl.DataFrame(
        {"_sym_up": ["S1.SH", "S2.SH"], "concept": list(_MEMBERS)},
        schema={"_sym_up": pl.Utf8, "concept": pl.Utf8},
    )
    monkeypatch.setattr(
        rps_rotation, "_load_concept_map_df", lambda _repo, kind: (map_df, len(_MEMBERS))
    )


def _history() -> pl.DataFrame:
    """历史缓存: 今天的行是上次重建时的旧快照 (S1 跌, S2 涨)。"""
    rows = []
    for offset in range(_HISTORY_DAYS):
        day = _TODAY - timedelta(days=offset)
        rows.append({"symbol": "S1.SH", "date": day, "change_pct": -0.01})
        rows.append({"symbol": "S2.SH", "date": day, "change_pct": 0.01})
    return pl.DataFrame(rows)


def _live_today() -> pl.DataFrame:
    """当日实时缓存: 盘中已反转 (S1 涨, S2 跌)。"""
    return pl.DataFrame(
        {
            "symbol": ["S1.SH", "S2.SH"],
            "date": [_TODAY, _TODAY],
            "change_pct": [0.05, -0.02],
            "close": [10.5, 9.8],
        }
    )


def _generation_mismatch_repo(history: pl.DataFrame, live: pl.DataFrame, live_date):
    return types.SimpleNamespace(
        _enriched_history_cache=history,
        _enriched_cache_date=live_date,
        # generation 已被实时落盘换掉: 按代校验的读取恒返回 None
        get_enriched_range=lambda start, end, columns=None: None,
        get_enriched_latest=lambda: (live, live_date),
        store=types.SimpleNamespace(data_dir=None),
    )


def test_generation_mismatch_still_returns_matrix_with_live_today():
    repo = _generation_mismatch_repo(_history(), _live_today(), _TODAY)

    result = rps_rotation.build_rps_rotation(repo, days=12)

    assert len(result["dates"]) == 12
    assert result["dates"][0] == _TODAY.isoformat()
    assert result["concept_count"] == len(_MEMBERS)
    # 今天一列取实时缓存 (银行领涨), 而不是历史缓存里的旧快照
    today = dict(result["columns"][_TODAY.isoformat()])
    assert today["银行"] == pytest.approx(0.05)
    assert today["证券"] == pytest.approx(-0.02)
    assert result["columns"][_TODAY.isoformat()][0][0] == "银行"
    # 历史日期仍来自历史缓存
    yesterday = dict(result["columns"][(_TODAY - timedelta(days=1)).isoformat()])
    assert yesterday["证券"] == pytest.approx(0.01)


def test_live_date_newer_than_history_extends_matrix_to_today():
    """历史缓存还停在昨天、实时缓存已进入今天时, 矩阵右端应是今天。"""
    history = _history().filter(pl.col("date") < _TODAY)
    repo = _generation_mismatch_repo(history, _live_today(), _TODAY)

    result = rps_rotation.build_rps_rotation(repo, days=12)

    assert result["dates"][0] == _TODAY.isoformat()
    assert len(result["dates"]) == 12


def test_no_live_cache_falls_back_to_history_only():
    repo = _generation_mismatch_repo(_history(), pl.DataFrame(), None)

    result = rps_rotation.build_rps_rotation(repo, days=12)

    assert len(result["dates"]) == 12
    today = dict(result["columns"][_TODAY.isoformat()])
    assert today["证券"] == pytest.approx(0.01)


def test_empty_history_cache_returns_empty():
    repo = _generation_mismatch_repo(None, _live_today(), _TODAY)

    result = rps_rotation.build_rps_rotation(repo, days=12)

    assert result == {"dates": [], "columns": {}, "concept_count": 0}
