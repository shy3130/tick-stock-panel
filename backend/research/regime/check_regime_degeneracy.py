"""Per-fold bull% of each regime signal — proves the 2x2 degeneracy is sample-driven, not a bug."""
from __future__ import annotations

from datetime import date, timedelta

from app.strategy.builtin.regime_conditional import _leader_bull_for_labels, _regime_bull_mask
from research.paths import DATA_DIR
from research.regime.diag_f4_regime import load_pivot, select_universe

FULL0 = date(2024, 9, 24)
FULL1 = date(2026, 6, 30)
N_FOLDS = 4
TRAIN_SKIP_TD = 80


def build_folds():
    import polars as pl
    lf = pl.scan_parquet(str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"))
    all_dates = sorted(
        set(lf.filter((pl.col("date") >= FULL0) & (pl.col("date") <= FULL1))
            .select("date").collect()["date"].to_list())
    )
    all_dates = [d for d in all_dates if FULL0 <= d <= FULL1]
    rest = all_dates[TRAIN_SKIP_TD:]
    chunk = len(rest) // N_FOLDS
    folds = []
    for k in range(N_FOLDS):
        s = k * chunk
        e = (k + 1) * chunk if k < N_FOLDS - 1 else len(rest)
        test = rest[s:e]
        train = all_dates[: TRAIN_SKIP_TD + s]
        folds.append((f"F{k+1}", train[0], train[-1], test[0], test[-1]))
    return folds


def main():
    uni = select_universe()
    folds = build_folds()
    W = 130  # diagnostic warmup
    print(f"{'fold':<5}{'test range':<26}{'ewMA20':>8}{'ewMA60':>8}{'leader60':>9}")
    for name, _tr0, _tr1, te0, te1 in folds:
        d0 = te0 - timedelta(days=W)
        dates, close = load_pivot(uni, d0, te1)
        labels = [str(d) for d in dates]
        ew20 = _regime_bull_mask(close, 20).reshape(-1)
        ew60 = _regime_bull_mask(close, 60).reshape(-1)
        ld = _leader_bull_for_labels(labels, 60).reshape(-1)
        s = next(i for i, d in enumerate(dates) if d >= te0)
        b20 = ew20[s:].mean() * 100
        b60 = ew60[s:].mean() * 100
        bld = ld[s:].mean() * 100
        print(f"{name:<5}{str(te0)+'~'+str(te1):<26}{b20:7.1f}%{b60:7.1f}%{bld:8.1f}%")


if __name__ == "__main__":
    main()
