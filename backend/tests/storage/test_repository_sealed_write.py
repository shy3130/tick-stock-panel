"""Sealed 写禁令断言：拒绝外部 fallback provenance 进入 sealed/canonical 写入口。

KlineRepository 的实时写入口（merge/flush × daily/enriched_asset）开头均调用
_assert_sealed_write_source(df)；这里直接对其做真值表覆盖，只验证这条可观察边界：
- 外部 fallback source（如 tencent_quote）必须被拒绝；
- 本地 canonical 生产的帧（无 source 列 / fquant 前缀 / null source）必须通过。

直接断言静态方法，零 DuckDB/data 依赖，纯 deterministic。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.storage.repository import KlineRepository

_assert = KlineRepository._assert_sealed_write_source


def _df(**overrides) -> pl.DataFrame:
    base = {"symbol": ["600519.SH"], "date": [date(2026, 8, 5)]}
    base.update(overrides)
    return pl.DataFrame(base)


def test_sealed_rejects_external_fallback_source():
    df = _df(source=["tencent_quote"])
    with pytest.raises(ValueError, match="sealed partition write rejected"):
        _assert(df)


def test_sealed_rejects_partial_external_rows():
    # 仅一行外部源即整体拒绝（纵深防御：宁可拒写也不污染 canonical 分区）
    df = pl.DataFrame({
        "symbol": ["600519.SH", "600000.SH"],
        "date": [date(2026, 8, 5), date(2026, 8, 5)],
        "source": ["fquant_local", "tencent_quote"],
    })
    with pytest.raises(ValueError):
        _assert(df)


def test_sealed_rejects_any_non_fquant_source():
    df = pl.DataFrame({
        "symbol": ["600519.SH", "600000.SH"],
        "date": [date(2026, 8, 5), date(2026, 8, 5)],
        "source": ["sina", "eastmoney"],
    })
    with pytest.raises(ValueError):
        _assert(df)


def test_sealed_rejects_unparseable_source_provenance():
    # List cannot cast to Utf8. A failed provenance normalization must not
    # create a fail-open path into a sealed partition.
    df = _df(source=[["tencent_quote"]])
    with pytest.raises(ValueError, match="unrecognized source provenance"):
        _assert(df)


def test_sealed_message_counts_bad_rows():
    df = pl.DataFrame({
        "symbol": ["A.SH", "B.SH", "C.SH"],
        "date": [date(2026, 8, 5)] * 3,
        "source": ["tencent_quote", "tencent_quote", None],
    })
    with pytest.raises(ValueError) as excinfo:
        _assert(df)
    assert "rows=2" in str(excinfo.value)


def test_sealed_accepts_no_source_column():
    # 现有实时链路产出的帧无 source 列 → 零误伤
    _assert(_df())  # 不抛


def test_sealed_accepts_fquant_local_prefix():
    _assert(_df(source=["fquant_local:realtime"]))
    _assert(_df(source=["fquant:fstore:daily_markets"]))


def test_sealed_accepts_null_source():
    _assert(_df(source=[None]))


def test_sealed_accepts_empty_frame():
    _assert(pl.DataFrame({"symbol": [], "date": [], "source": []}))
