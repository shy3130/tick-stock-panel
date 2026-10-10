"""涨停梯队挂时序扩展列时, 一只票不能按历史分区数被 JOIN 放大。

ext_{config_id} DuckDB 视图对 timeseries 模式覆盖 timeseries/**/*.parquet 全部
分区 (见 app/api/ext_data._refresh_views), 一只票在 N 天快照里就有 N 行。
自选列表 (app/api/watchlist) 对同一场景先走 _read_ext_dataframe 取最新分区
再按 symbol 去重, 梯队这边直接查视图, 于是同一只涨停股在梯队里出现 N 次、
档位 count 被放大 N 倍。
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import screener as screener_api
from app.services.screener import ScreenerService

_AS_OF = date(2026, 9, 10)


class _FakeQuery:
    def __init__(self, df: pl.DataFrame) -> None:
        self._df = df

    def arrow(self):
        return self._df.to_arrow()


class _FakeDB:
    """模拟 ext_{id} 视图: timeseries 模式下返回全部分区的行。"""

    def __init__(self, df: pl.DataFrame) -> None:
        self._df = df

    def query(self, sql: str) -> _FakeQuery:
        return _FakeQuery(self._df)


class _NoDepth:
    def get_sealed_map(self, _as_of, is_down: bool = False) -> dict:
        return {}

    def is_sealed_ready(self, _as_of) -> bool:
        return False

    def get_sealed_age(self, _as_of):
        return None


def _write_timeseries_ext(data_dir, partitions: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """写 timeseries 扩展表; 返回视图口径 (全部分区拼接) 的 DataFrame。"""
    cfg_dir = data_dir / "ext_data" / "concept_ts"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath("config.json").write_text(
        json.dumps({
            "id": "concept_ts",
            "label": "概念时序",
            "mode": "timeseries",
            "fields": [{"name": "concept", "dtype": "string", "label": "概念"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    for day, frame in partitions.items():
        part = cfg_dir / "timeseries" / f"date={day}"
        part.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(part / "part.parquet")
    return pl.concat([partitions[d] for d in sorted(partitions)])


def _request(data_dir, view_df: pl.DataFrame):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=data_dir, db=_FakeDB(view_df)),
    )
    state = SimpleNamespace(repo=repo, depth_service=_NoDepth())
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
def _stub_enriched(monkeypatch):
    """单只涨停股的 enriched 快照 + 空的昨日连板表。"""
    snapshot = pl.DataFrame({
        "symbol": ["600000.SH"],
        "name": ["示例"],
        "close": [11.0],
        "change_pct": [0.1],
        "signal_limit_up": [True],
        "signal_limit_down": [False],
        "signal_broken_limit_up": [False],
        "consecutive_limit_ups": [2],
    })
    monkeypatch.setattr(
        ScreenerService, "_load_enriched_for_date", lambda self, d: snapshot
    )
    monkeypatch.setattr(
        ScreenerService, "load_prior_consecutive", lambda self, d, c: pl.DataFrame()
    )
    return snapshot


def test_timeseries_ext_column_does_not_duplicate_ladder_rows(tmp_path, _stub_enriched):
    view_df = _write_timeseries_ext(tmp_path, {
        "2026-09-08": pl.DataFrame({"symbol": ["600000.SH"], "concept": ["旧概念"]}),
        "2026-09-09": pl.DataFrame({"symbol": ["600000.SH"], "concept": ["次新概念"]}),
        "2026-09-10": pl.DataFrame({"symbol": ["600000.SH"], "concept": ["最新概念"]}),
    })

    payload = screener_api.limit_ladder(
        _request(tmp_path, view_df),
        as_of=_AS_OF,
        direction="up",
        ext_columns="concept_ts.concept",
    )

    tiers = payload["tiers"]
    assert len(tiers) == 1
    stocks = tiers[0]["stocks"]
    assert [s["symbol"] for s in stocks] == ["600000.SH"], "同一只票被历史分区放大了"
    assert tiers[0]["count"] == 1
    # 取最新分区的值, 不是任意一期的历史值
    assert stocks[0]["concept_ts__concept"] == "最新概念"


def test_snapshot_ext_column_still_joins(tmp_path, _stub_enriched):
    """snapshot 模式扩展表照常挂列 (回归保护)。"""
    cfg_dir = tmp_path / "ext_data" / "concept_snap"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath("config.json").write_text(
        json.dumps({
            "id": "concept_snap",
            "label": "概念快照",
            "mode": "snapshot",
            "fields": [{"name": "concept", "dtype": "string", "label": "概念"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    snap = pl.DataFrame({"symbol": ["600000.SH"], "concept": ["人工智能"]})
    snap.write_parquet(cfg_dir / "part.parquet")

    payload = screener_api.limit_ladder(
        _request(tmp_path, snap),
        as_of=_AS_OF,
        direction="up",
        ext_columns="concept_snap.concept",
    )

    stocks = payload["tiers"][0]["stocks"]
    assert len(stocks) == 1
    assert stocks[0]["concept_snap__concept"] == "人工智能"
