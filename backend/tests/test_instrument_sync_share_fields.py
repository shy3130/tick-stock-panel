"""标的维表同步: provider 不提供股本时不覆盖已有值。

回归背景: 部分行情源的 get_instruments 只返回代码/名称/市场/类型, 没有
股本与交易规则字段。旧实现整表覆盖后 float_shares/total_shares 变 null,
enriched 的 turnover_rate 随之全空, 依赖换手率与流通市值的策略 (起爆/5连阳)
直接筛出 0 只 —— 同一个策略换数据源结果就不同。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services import instrument_sync as isync


def _write_existing(data_dir: Path) -> None:
    out = data_dir / "instruments"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "code": ["600000", "000001"],
        "exchange": ["SH", "SZ"],
        "region": ["CN", "CN"],
        "type": ["stock", "stock"],
        "listing_date": ["1999-11-10", "1991-04-03"],
        "total_shares": [2.9e10, 1.9e10],
        "float_shares": [2.9e10, 1.9e10],
        "tick_size": [0.01, 0.01],
        "limit_up": [11.0, 12.0],
        "limit_down": [9.0, 10.0],
    }).write_parquet(out / "instruments.parquet")


def _provider_rows(*symbols: str) -> list[dict]:
    """模拟只提供标识字段的行情源 (无股本/交易规则)。"""
    return [
        {
            "symbol": s, "name": s, "code": s.split(".")[0],
            "exchange": s.split(".")[1], "region": "CN", "type": "stock",
            "listing_date": None, "total_shares": None, "float_shares": None,
            "tick_size": None, "limit_up": None, "limit_down": None,
        }
        for s in symbols
    ]


def test_share_fields_restored_from_existing_table(tmp_path, monkeypatch) -> None:
    _write_existing(tmp_path)
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH", "000001.SZ"),
    )

    assert isync.sync_instruments(tmp_path) == 2

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["float_shares"] == pytest.approx(2.9e10)
    assert by_symbol["600000.SH"]["total_shares"] == pytest.approx(2.9e10)
    assert by_symbol["600000.SH"]["tick_size"] == pytest.approx(0.01)
    assert by_symbol["000001.SZ"]["float_shares"] == pytest.approx(1.9e10)


def test_new_symbol_keeps_null_shares_instead_of_faking(tmp_path, monkeypatch) -> None:
    """新上市标的没有历史值 → 保持 null, 不伪造股本。"""
    _write_existing(tmp_path)
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH", "301999.SZ"),
    )

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    new = df.filter(pl.col("symbol") == "301999.SZ")
    assert new.height == 1
    assert new["float_shares"][0] is None
    assert new["total_shares"][0] is None


def test_provider_fields_win_when_present(tmp_path, monkeypatch) -> None:
    """provider 自带股本时以 provider 为准 (不复用旧值)。"""
    _write_existing(tmp_path)
    rows = _provider_rows("600000.SH")
    rows[0]["float_shares"] = 1.0e9
    rows[0]["total_shares"] = 2.0e9
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: rows)

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df["float_shares"][0] == pytest.approx(1.0e9)
    assert df["total_shares"][0] == pytest.approx(2.0e9)


def test_no_existing_table_is_tolerated(tmp_path, monkeypatch) -> None:
    """首次同步且无历史维表: 不报错, 字段保持 null。"""
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH"),
    )

    assert isync.sync_instruments(tmp_path) == 1

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df["float_shares"][0] is None


def test_partial_provider_fields_filled_per_row(tmp_path, monkeypatch) -> None:
    """provider 只给部分标的股本时, 也要逐行补全 (不能整列跳过)。"""
    _write_existing(tmp_path)
    rows = _provider_rows("600000.SH", "000001.SZ")
    rows[0]["float_shares"] = 5.0e8  # 第一只有值, 第二只仍为空
    rows[0]["total_shares"] = 6.0e8
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: rows)

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["float_shares"] == pytest.approx(5.0e8)
    assert by_symbol["000001.SZ"]["float_shares"] == pytest.approx(1.9e10)
