from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import polars as pl
import pytest

from app.backtest.factor import FactorBacktestService, FactorConfig
from app.backtest.factor_zoo import ALPHAS, compute_factor, export_manifest

ALPHA_IDS = list(ALPHAS)


def test_export_manifest_contains_registered_alphas():
    manifest = export_manifest()

    assert len(manifest) >= 10
    assert {x["id"] for x in manifest} >= {"alpha101_001", "alpha101_012"}
    assert all(x["columns_required"] for x in manifest)


def test_compute_factor_unknown_factor_keeps_legacy_behavior():
    panel = _panel()

    assert compute_factor(panel, "missing").equals(panel)


def test_random_control_ic_returns_distribution():
    panel = _panel()
    engine = SimpleNamespace(load_panel=lambda symbols, start, end, columns: panel)
    svc = FactorBacktestService(engine)
    cfg = FactorConfig("alpha101_012", None, date(2026, 1, 10), date(2026, 3, 15), rebalance="daily")

    out = svc.random_control_ic(cfg, n_runs=3)

    assert out["random_control_ic_mean"] is not None
    assert out["random_control_ic_std"] is not None


@pytest.mark.parametrize("alpha_id", ALPHA_IDS)
def test_alpha_matches_pandas_reference(alpha_id):
    panel = _panel()
    actual = compute_factor(panel, alpha_id).select("symbol", "date", alpha_id).sort(["date", "symbol"])
    expected = _pandas_reference(panel, alpha_id).sort(["date", "symbol"])
    joined = actual.join(expected, on=["symbol", "date"]).drop_nulls()

    assert joined.height > 0
    assert np.allclose(joined[alpha_id], joined["expected"], atol=1e-9, equal_nan=True)


def _panel() -> pl.DataFrame:
    start = date(2026, 1, 1)
    rows = []
    for s_idx, sym in enumerate(["A", "B", "C"]):
        for i in range(90):
            trend = 10 + s_idx * 3 + i * (0.08 + s_idx * 0.02)
            wave = ((i % 7) - 3) * 0.03
            close = trend + wave
            volume_wave = ((i * (s_idx + 2)) % 11 - 5) * 13
            open_wave = ((i * 3 + s_idx) % 9 - 4) * 0.006
            rows.append({
                "symbol": sym,
                "date": start + timedelta(days=i),
                "open": close * (1 + open_wave),
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1000 + s_idx * 200 + i * (8 + s_idx) + volume_wave,
                "amount": None if i == 37 and s_idx == 1 else close * (1000 + s_idx * 200 + i * (8 + s_idx) + volume_wave),
                "vwap": None if i == 41 and s_idx == 2 else close,
            })
    return pl.DataFrame(rows)


def _pandas_reference(panel: pl.DataFrame, alpha_id: str) -> pl.DataFrame:
    pdf = panel.to_pandas().sort_values(["symbol", "date"])
    parts = []
    for _, g in pdf.groupby("symbol", sort=False):
        parts.append(pd.DataFrame({
            "symbol": g["symbol"].to_numpy(),
            "date": g["date"].to_numpy(),
            "_raw": _raw(g.reset_index(drop=True), alpha_id),
        }))
    raw = pd.concat(parts, ignore_index=True)
    if alpha_id in {"alpha101_001", "alpha101_008", "alpha101_010"}:
        raw["expected"] = raw.groupby("date")["_raw"].rank(method="average", pct=True) - 0.5
    else:
        raw["expected"] = raw["_raw"]
    return pl.from_pandas(raw[["symbol", "date", "expected"]]).with_columns(pl.col("date").cast(pl.Date))


def _raw(g: pd.DataFrame, alpha_id: str) -> np.ndarray:
    close = g["close"].astype(float)
    open_ = g["open"].astype(float)
    low = g["low"].astype(float)
    volume = g["volume"].astype(float)
    returns = close.pct_change()
    if alpha_id == "alpha101_001":
        x = returns.rolling(20, min_periods=20).std().where(returns < 0, close)
        return (np.sign(x) * np.abs(x) ** 2).rolling(5, min_periods=5).apply(lambda a: float(np.nanargmax(a)), raw=True).to_numpy()
    if alpha_id == "alpha101_002":
        a = _ts_rank(np.log(volume).diff(2), 6)
        b = _ts_rank((close - open_) / open_, 6)
        return -a.rolling(6, min_periods=6).corr(b).to_numpy()
    if alpha_id == "alpha101_003":
        return -_ts_rank(open_, 10).rolling(10, min_periods=10).corr(_ts_rank(volume, 10)).to_numpy()
    if alpha_id == "alpha101_004":
        return -_ts_rank(low, 9).to_numpy()
    if alpha_id == "alpha101_006":
        return -open_.rolling(10, min_periods=10).corr(volume).to_numpy()
    if alpha_id == "alpha101_007":
        delta7 = close.diff(7)
        score = -_ts_rank(delta7.abs(), 60) * np.sign(delta7)
        return score.where(volume.rolling(20, min_periods=20).mean() < volume, -1.0).to_numpy()
    if alpha_id == "alpha101_008":
        v = open_.rolling(5, min_periods=5).sum() * returns.rolling(5, min_periods=5).sum()
        return (v - v.shift(10)).mul(-1).to_numpy()
    if alpha_id in {"alpha101_009", "alpha101_010"}:
        d = close.diff()
        return d.where(d.rolling(5, min_periods=5).min() > 0, d.where(d.rolling(5, min_periods=5).max() < 0, -d)).to_numpy()
    if alpha_id == "alpha101_012":
        return (np.sign(volume.diff()) * -close.diff()).to_numpy()
    raise AssertionError(alpha_id)


def _ts_rank(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window, min_periods=window).apply(
        lambda a: pd.Series(a).rank(method="first").iloc[-1] / len(a),
        raw=False,
    )
