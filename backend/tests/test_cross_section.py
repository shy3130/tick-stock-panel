"""Deterministic tests for the cross-section analysis suite.

All tests use fixed in-memory DataFrames — no disk I/O, no network, no
randomness.  They verify the acceptance criteria:

* Correlation matrix symmetry, min-sample guard, flat-series guard, beta consistency.
* Relative-strength alignment and exact window returns.
* Peer-comparison industry filter.
* Reverse-screen condition relaxation and executability.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import cross_section as xs
from app.services.cross_section import (
    _compute_corr_matrix_from_returns,
    _compute_returns,
    _process_benchmark_pair,
    build_reverse_screen_conditions,
    compute_correlation_matrix,
    compute_peer_comparison,
    compute_relative_strength,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_close_df(symbols: list[str], dates: list[date], prices: dict[str, list[float]]) -> pl.DataFrame:
    rows = []
    for sym in symbols:
        for i, d in enumerate(dates):
            rows.append({"symbol": sym, "date": d, "close": prices[sym][i]})
    return pl.DataFrame(rows)


def _make_returns_long(
    dates: list[date], series: dict[str, list[float]]
) -> pl.DataFrame:
    rows = []
    for sym, rets in series.items():
        for d, r in zip(dates, rets):
            rows.append({"date": d, "symbol": sym, "ret": r})
    return pl.DataFrame(rows)


# ── 1. Correlation matrix symmetry ────────────────────────────────────────


def test_correlation_matrix_symmetric():
    """Matrix[i][j].correlation == Matrix[j][i].correlation within 1e-9."""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
    # Deterministic non-flat series
    a = [0.01 * ((i * 7) % 11 - 5) for i in range(30)]
    b = [0.01 * ((i * 3) % 13 - 6) for i in range(30)]
    c = [0.01 * ((i * 5) % 7 - 3) for i in range(30)]

    returns = _make_returns_long(dates, {"A": a, "B": b, "C": c})
    aligned = returns.pivot("symbol", index="date", values="ret").drop_nulls()

    result = _compute_corr_matrix_from_returns(aligned, ["A", "B", "C"], window=25, min_samples=10)
    instruments = result["matrix"]["instruments"]
    corr = result["matrix"]["correlation"]
    n = len(instruments)

    for i in range(n):
        for j in range(n):
            if corr[i][j] is not None and corr[j][i] is not None:
                assert abs(corr[i][j] - corr[j][i]) < 1e-9, (
                    f"asymmetry at [{i}][{j}] vs [{j}][i]: {corr[i][j]} vs {corr[j][i]}"
                )

    # Diagonal should be 1.0 for non-flat series
    for i in range(n):
        assert corr[i][i] is not None
        assert abs(corr[i][i] - 1.0) < 1e-9


# ── 2. Minimum-sample guard ───────────────────────────────────────────────


def test_correlation_min_sample_guard():
    """Fewer common dates than min_samples → all cells null."""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
    a = [0.01, 0.02, -0.01, 0.03, -0.02]
    b = [0.02, -0.01, 0.03, -0.02, 0.01]

    returns = _make_returns_long(dates, {"A": a, "B": b})
    aligned = returns.pivot("symbol", index="date", values="ret").drop_nulls()
    assert aligned.height == 5

    result = _compute_corr_matrix_from_returns(aligned, ["A", "B"], window=5, min_samples=20)
    corr = result["matrix"]["correlation"]
    # Only 5 samples < 20 min_samples → null
    assert corr[0][1] is None
    assert corr[1][0] is None


# ── 3. Flat-series (zero-variance) guard ──────────────────────────────────


def test_correlation_flat_series_protection():
    """Constant returns (zero variance) → correlation null, not NaN/1.0."""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
    flat = [0.01] * 30  # constant → var = 0
    noisy = [0.01 * ((i * 7) % 11 - 5) for i in range(30)]

    returns = _make_returns_long(dates, {"FLAT": flat, "NOISY": noisy})
    aligned = returns.pivot("symbol", index="date", values="ret").drop_nulls()

    result = _compute_corr_matrix_from_returns(aligned, ["FLAT", "NOISY"], window=25, min_samples=10)
    corr = result["matrix"]["correlation"]

    # FLAT-NOISY and NOISY-FLAT should be null
    idx = result["matrix"]["instruments"]
    fi = idx.index("FLAT")
    ni = idx.index("NOISY")
    assert corr[fi][ni] is None
    assert corr[ni][fi] is None
    # FLAT-FLAT should also be null (self-correlation of constant is undefined)
    assert corr[fi][fi] is None


# ── 4. Beta consistency: beta == cov / var, corr == cov / (σx·σy) ─────────


def test_correlation_beta_consistency():
    """beta = cov(selected, peer) / var(selected); corr = cov / sqrt(varX * varY)."""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(40)]
    a = [0.01 * ((i * 7) % 11 - 5) for i in range(40)]
    b = [0.01 * ((i * 3) % 13 - 6) for i in range(40)]

    returns = pl.DataFrame({
        "date": dates * 2,
        "symbol": ["SEL"] * 40 + ["PEER"] * 40,
        "ret": a + b,
    })
    aligned = returns.pivot("symbol", index="date", values="ret").drop_nulls()

    result = _compute_corr_matrix_from_returns(aligned, ["SEL", "PEER"], window=35, min_samples=10)

    pair = result["pairRows"][0]
    assert pair["peer"] == "PEER"
    assert pair["correlation"] is not None
    assert pair["covariance"] is not None
    assert pair["beta"] is not None

    # Retrieve variance of SEL from the matrix diagonal covariance
    # beta = cov / var_selected
    cov = pair["covariance"]
    beta = pair["beta"]
    corr = pair["correlation"]

    # Recompute var over the same windowed sample production uses
    # (``_compute_corr_matrix_from_returns`` windows to ``tail(window)``);
    # beta and corr must share one aligned sample + sample-variance (ddof=1) basis.
    current = aligned.tail(35)
    var_sel = current["SEL"].var()
    var_peer = current["PEER"].var()

    assert abs(beta - cov / var_sel) < 1e-9, f"beta {beta} != cov/var {cov / var_sel}"
    expected_corr = cov / math.sqrt(var_sel * var_peer)
    assert abs(corr - expected_corr) < 1e-9, f"corr {corr} != expected {expected_corr}"


# ── 5. Relative-strength benchmark alignment ──────────────────────────────


def test_relative_strength_alignment():
    """Stock has 100 days, benchmark 80 → aligned length 80, first NAV == 100."""
    dates_100 = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    dates_80 = dates_100[20:]  # last 80 overlap

    stock_df = pl.DataFrame({
        "date": dates_100,
        "close": [100.0 + i for i in range(100)],
    })
    bench_df = pl.DataFrame({
        "date": dates_80,
        "close": [1000.0 + i * 2 for i in range(80)],
    })

    result = _process_benchmark_pair(stock_df, bench_df, "TEST", "Test", days=80)
    assert result is not None
    assert result["alignedDays"] == 80  # inner join → 80 common dates

    # First point: NAV normalised to 100
    p0 = result["points"][0]
    assert abs(p0["stockNav"] - 100.0) < 1e-9
    assert abs(p0["benchmarkNav"] - 100.0) < 1e-9


# ── 6. Relative-strength exact window returns ─────────────────────────────


def test_relative_strength_window_returns():
    """Linear-growth close → exact mathematical window return."""
    n = 130
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    stock_close = [100.0 * (1.01 ** i) for i in range(n)]
    bench_close = [100.0 * (1.005 ** i) for i in range(n)]

    stock_df = pl.DataFrame({"date": dates, "close": stock_close})
    bench_df = pl.DataFrame({"date": dates, "close": bench_close})

    result = _process_benchmark_pair(stock_df, bench_df, "TEST", "Test", days=120)
    assert result is not None
    assert result["alignedDays"] == 120

    # 60-day window: exact compounded growth
    wr60 = result["windowReturns"][60]
    assert wr60["returnPct"] is not None
    assert wr60["relativeReturnPct"] is not None

    expected_bench = (1.005 ** 60 - 1) * 100
    expected_stock = (1.01 ** 60 - 1) * 100
    assert abs(wr60["returnPct"] - expected_bench) < 0.01
    assert abs(wr60["relativeReturnPct"] - (expected_stock - expected_bench)) < 0.01
    assert abs(result["stockReturns"][60] - expected_stock) < 0.01


# ── 7. Peer-comparison industry filter ────────────────────────────────────


class _MockRepo:
    """Minimal in-memory repo for cross-section tests."""

    def __init__(
        self,
        *,
        latest_date: date | None = None,
        enriched_latest: pl.DataFrame | None = None,
        enriched_range: pl.DataFrame | None = None,
        daily: dict[str, pl.DataFrame] | None = None,
        index_daily: dict[str, pl.DataFrame] | None = None,
        instruments: pl.DataFrame | None = None,
    ) -> None:
        self._latest = latest_date
        self._enriched_latest = enriched_latest if enriched_latest is not None else pl.DataFrame()
        self._enriched_range = enriched_range
        self._daily = daily if daily is not None else {}
        self._index_daily = index_daily if index_daily is not None else {}
        self._instruments = instruments if instruments is not None else pl.DataFrame()
        self.store = SimpleNamespace(data_dir=Path("."))

    def enriched_latest_date(self) -> date | None:
        return self._latest

    def get_enriched_latest(self) -> tuple[pl.DataFrame, date | None]:
        return self._enriched_latest, self._latest

    def get_enriched_range(self, start, end, symbols=None, columns=None):
        if self._enriched_range is None:
            return pl.DataFrame()
        df = self._enriched_range
        if symbols:
            df = df.filter(pl.col("symbol").is_in(symbols))
        if columns:
            existing = [c for c in columns if c in df.columns]
            if existing:
                df = df.select(existing)
        return df

    def get_daily(self, symbol, start, end, columns=None):
        df = self._daily.get(symbol, pl.DataFrame()).clone()
        if columns:
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing) if existing else df
        return df

    def get_index_daily(self, symbol, start, end, columns=None):
        df = self._index_daily.get(symbol, pl.DataFrame()).clone()
        if columns:
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing) if existing else df
        return df

    def get_instruments(self) -> pl.DataFrame:
        return self._instruments


def test_peer_comparison_industry_filter(monkeypatch):
    """Peer universe contains only same-industry members."""
    latest = date(2026, 8, 9)
    enriched = pl.DataFrame({
        "symbol": ["000001.SZ", "600036.SH", "601398.SH", "300750.SZ"],
        "date": [latest] * 4,
        "close": [12.0, 40.0, 5.0, 200.0],
        "change_pct": [1.5, 2.0, -0.5, 3.0],
        "amount": [5e9, 8e9, 3e9, 6e9],
        "turnover_rate": [1.2, 0.8, 0.5, 2.0],
    })

    fin = pl.DataFrame({
        "symbol": ["000001.SZ", "600036.SH", "601398.SH", "300750.SZ"],
        "industry": ["银行", "银行", "银行", "电池"],
        "weight_avg_roe": [12.0, 16.0, 11.0, 8.0],
        "basic_eps": [1.2, 3.5, 0.6, 5.0],
        "gross_margin": [40.0, 45.0, 38.0, 20.0],
        "bps": [10.0, 30.0, 6.0, 40.0],
        "eps_ttm": [1.2, 3.5, 0.6, 5.0],
        "report_year": [2025] * 4,
        "quarter_num": [2] * 4,
    })

    repo = _MockRepo(latest_date=latest, enriched_latest=enriched)
    monkeypatch.setattr(xs, "load_financial_snapshot", lambda data_dir, as_of: fin)

    result = compute_peer_comparison(repo, "000001.SZ", mode="industry", limit=12, sort_key="amount")

    assert result["universe"] == "银行"
    symbols = [r["symbol"] for r in result["allRows"]]
    # 300750.SZ is in "电池" — must be excluded
    assert "300750.SZ" not in symbols
    assert set(symbols) == {"000001.SZ", "600036.SH", "601398.SH"}

    # Current stock is pinned and marked
    assert result["rows"][0]["symbol"] == "000001.SZ"
    assert result["rows"][0]["isCurrent"] is True

    # Summary ranks
    assert result["summary"]["total"] == 3
    assert result["summary"]["currentRank"] is not None


# ── 8. Reverse-screen condition relaxation ────────────────────────────────


def test_reverse_screen_condition_relaxation():
    """Momentum=15 → change_pct condition ratio in [0.35, 0.45]."""
    features = {
        "change_pct": 15.0,
        "amount": 1e9,
        "turnover_rate": 3.0,
        "roe": 12.0,
        "pe_approx": 20.0,
        "industry": "半导体",
    }
    conditions, reasons = build_reverse_screen_conditions(features)

    # Industry exact match
    ind = [c for c in conditions if c["field"] == "industry"]
    assert len(ind) == 1
    assert ind[0]["op"] == "="
    assert ind[0]["value"] == "半导体"

    # Change_pct relaxed lower bound
    pct = [c for c in conditions if c["field"] == "change_pct"]
    assert len(pct) == 1
    assert pct[0]["op"] == ">="
    ratio = pct[0]["value"] / 15.0
    assert 0.35 <= ratio <= 0.45, f"ratio {ratio} not in [0.35, 0.45]"

    # PE bounded with relaxed lower and upper
    pe = [c for c in conditions if c["field"] == "pe_approx"]
    assert len(pe) == 1
    assert pe[0]["op"] == "between"
    lo, hi = pe[0]["value"]
    assert 0.35 <= lo / 20.0 <= 0.45
    assert 1.8 <= hi / 20.0 <= 2.5

    # exclude_st always present
    st = [c for c in conditions if c["field"] == "exclude_st"]
    assert len(st) == 1
    assert st[0]["value"] is True

    # Reasons match conditions
    assert len(reasons) == len(conditions)


def test_reverse_screen_conditions_executable():
    """Conditions produce a valid, executable ScreenerQueryRequest."""
    from app.services.screener_query import ScreenerQueryRequest, validate_query

    features = {
        "change_pct": 5.0,
        "amount": 1e9,
        "turnover_rate": 3.0,
        "roe": 15.0,
        "pe_approx": 8.0,
        "industry": "银行",
    }
    conditions, _ = build_reverse_screen_conditions(features)

    req = ScreenerQueryRequest(conditions=conditions, limit=80)
    applied, order = validate_query(req)
    assert len(applied) == len(conditions)
    assert order.field == "change_pct"  # default order field


def test_reverse_screen_skips_null_features():
    """Null / non-positive features produce no condition for that metric."""
    conditions, reasons = build_reverse_screen_conditions({})
    # Only exclude_st remains
    assert len(conditions) == 1
    assert conditions[0]["field"] == "exclude_st"
    assert len(reasons) == 1


# ── 9. Correlation integration with mock repo ─────────────────────────────


def test_correlation_matrix_integration(monkeypatch):
    """Full compute_correlation_matrix with mock repo — symmetric and bounded."""
    latest = date(2026, 8, 9)
    symbols = ["000001.SZ", "600036.SH", "601398.SH"]
    n_days = 130
    dates = [latest - timedelta(days=n_days - 1 - i) for i in range(n_days)]

    prices = {}
    for k, sym in enumerate(symbols):
        seed = (k + 1) * 37
        prices[sym] = [10.0 + k * 5 + ((i * seed) % 23 - 11) * 0.05 for i in range(n_days)]

    enriched = pl.DataFrame({
        "symbol": symbols,
        "date": [latest] * len(symbols),
        "amount": [5e9, 8e9, 3e9],
        "close": [prices[s][-1] for s in symbols],
    })

    close_history = _make_close_df(symbols, dates, prices)

    fin = pl.DataFrame({
        "symbol": symbols,
        "industry": ["银行"] * len(symbols),
        "weight_avg_roe": [12.0] * len(symbols),
        "basic_eps": [1.0] * len(symbols),
        "gross_margin": [40.0] * len(symbols),
        "bps": [10.0] * len(symbols),
        "eps_ttm": [1.0] * len(symbols),
        "report_year": [2025] * len(symbols),
        "quarter_num": [2] * len(symbols),
    })

    repo = _MockRepo(
        latest_date=latest,
        enriched_latest=enriched,
        enriched_range=close_history,
    )
    monkeypatch.setattr(xs, "load_financial_snapshot", lambda data_dir, as_of: fin)

    result = compute_correlation_matrix(
        repo, "000001.SZ", window=120, min_samples=20, max_peers=6,
    )

    assert result["selected"] == "000001.SZ"
    assert set(result["peers"]) <= {"600036.SH", "601398.SH"}
    assert result["industry"] == "银行"
    assert result["alignedDays"] > 0

    # Symmetry
    instruments = result["matrix"]["instruments"]
    corr = result["matrix"]["correlation"]
    n = len(instruments)
    for i in range(n):
        for j in range(n):
            if corr[i][j] is not None and corr[j][i] is not None:
                assert abs(corr[i][j] - corr[j][i]) < 1e-9


# ── 10. Parameter validation ──────────────────────────────────────────────


def test_correlation_rejects_invalid_window():
    repo = _MockRepo(latest_date=date(2026, 8, 9))
    with pytest.raises(ValueError, match="window"):
        compute_correlation_matrix(repo, "000001.SZ", window=99)


def test_correlation_rejects_invalid_min_samples():
    repo = _MockRepo(latest_date=date(2026, 8, 9))
    with pytest.raises(ValueError, match="min_samples"):
        compute_correlation_matrix(repo, "000001.SZ", window=60, min_samples=100)


def test_relative_strength_rejects_invalid_benchmark():
    repo = _MockRepo(latest_date=date(2026, 8, 9))
    with pytest.raises(ValueError, match="benchmark"):
        compute_relative_strength(repo, "000001.SZ", benchmark="INVALID.XX")


# ── 11. _compute_returns correctness ──────────────────────────────────────


def test_compute_returns_drops_first_null():
    """pct_change first row per symbol is null → dropped."""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
    df = pl.DataFrame({
        "symbol": ["A"] * 5,
        "date": dates,
        "close": [10.0, 11.0, 12.0, 13.0, 14.0],
    })
    returns = _compute_returns(df)
    assert returns.height == 4  # first day dropped
    # First return = (11-10)/10 = 0.1
    assert abs(returns.row(0, named=True)["ret"] - 0.1) < 1e-9
