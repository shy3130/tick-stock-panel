from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

import numpy as np
import polars as pl


@dataclass(frozen=True)
class AlphaMeta:
    id: str
    name: str
    theme: str
    formula: str
    columns_required: tuple[str, ...]
    warmup: int
    notes: str = ""


AlphaFunc = Callable[[pl.DataFrame], pl.DataFrame]


def compute_factor(panel: pl.DataFrame, factor_name: str) -> pl.DataFrame:
    item = ALPHAS.get(factor_name)
    if item is None:
        return panel
    return item[1](panel)


def list_alphas() -> list[AlphaMeta]:
    return [meta for meta, _ in ALPHAS.values()]


def get_alpha(alpha_id: str) -> AlphaMeta:
    return ALPHAS[alpha_id][0]


def export_manifest() -> list[dict]:
    return [asdict(meta) for meta in list_alphas()]


def alpha101_001(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        close = _col(g, "close")
        returns = _returns(close)
        rolling_std = _rolling_std(returns, 20)
        x = np.where(returns < 0, rolling_std, close)
        return _rolling_argmax(np.sign(x) * np.abs(x) ** 2, 5)

    return _with_ranked_alpha(panel, "alpha101_001", calc)


def alpha101_002(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        volume = _col(g, "volume")
        open_ = _col(g, "open")
        close = _col(g, "close")
        log_volume = np.log(np.where(volume > 0, volume, np.nan))
        a = _ts_rank(_delta(log_volume, 2), 6)
        b = _ts_rank((close - open_) / open_, 6)
        return -_rolling_corr(a, b, 6)

    return _with_raw_alpha(panel, "alpha101_002", calc)


def alpha101_003(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        return -_rolling_corr(_ts_rank(_col(g, "open"), 10), _ts_rank(_col(g, "volume"), 10), 10)

    return _with_raw_alpha(panel, "alpha101_003", calc)


def alpha101_004(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        return -_ts_rank(_col(g, "low"), 9)

    return _with_raw_alpha(panel, "alpha101_004", calc)


def alpha101_006(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        return -_rolling_corr(_col(g, "open"), _col(g, "volume"), 10)

    return _with_raw_alpha(panel, "alpha101_006", calc)


def alpha101_007(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        close = _col(g, "close")
        volume = _col(g, "volume")
        adv20 = _rolling_mean(volume, 20)
        delta7 = _delta(close, 7)
        score = -_ts_rank(np.abs(delta7), 60) * np.sign(delta7)
        return np.where(adv20 < volume, score, -1.0)

    return _with_raw_alpha(panel, "alpha101_007", calc)


def alpha101_008(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        v = _rolling_sum(_col(g, "open"), 5) * _rolling_sum(_returns(_col(g, "close")), 5)
        return -_cross_section_input(v - _delay(v, 10))

    return _with_ranked_alpha(panel, "alpha101_008", calc)


def alpha101_009(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        d = _delta(_col(g, "close"), 1)
        return np.where(_rolling_min(d, 5) > 0, d, np.where(_rolling_max(d, 5) < 0, d, -d))

    return _with_raw_alpha(panel, "alpha101_009", calc)


def alpha101_010(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        d = _delta(_col(g, "close"), 1)
        return np.where(_rolling_min(d, 5) > 0, d, np.where(_rolling_max(d, 5) < 0, d, -d))

    return _with_ranked_alpha(panel, "alpha101_010", calc)


def alpha101_012(panel: pl.DataFrame) -> pl.DataFrame:
    def calc(g: pl.DataFrame) -> np.ndarray:
        return np.sign(_delta(_col(g, "volume"), 1)) * -_delta(_col(g, "close"), 1)

    return _with_raw_alpha(panel, "alpha101_012", calc)

# ---------------------------------------------------------------------------
# Alpha101 #005–#020 — ported from Vibe-Trading agent/src/factors/zoo/alpha101.
# Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991.
#
# Per-symbol adaptation (consistent with alpha101_001–012 above):
#   * Cross-sectional ``rank(x)`` from the paper is a no-op within a single
#     symbol; only an outermost ``rank(...)`` is realised as a panel-level
#     cross-sectional rank via ``_with_ranked_alpha`` (cf. #001/#008/#010).
#   * ``vwap`` is absent from the tickflow OHLCV panel, so it is approximated
#     by the typical price ``(open+high+low+close)/4`` (Vibe-Trading equity_us
#     fallback in factors/base.py::vwap).
#   * ``adv20`` = rolling mean of volume over 20 bars.
# ---------------------------------------------------------------------------


def _typical_price(g: pl.DataFrame) -> np.ndarray:
    return (_col(g, "open") + _col(g, "high") + _col(g, "low") + _col(g, "close")) / 4.0


def alpha101_005(panel: pl.DataFrame) -> pl.DataFrame:
    # rank(open - sum(vwap,10)/10) * (-1 * abs(rank(close - vwap)))
    def calc(g: pl.DataFrame) -> np.ndarray:
        open_ = _col(g, "open")
        close = _col(g, "close")
        vwap = _typical_price(g)
        term1 = open_ - _rolling_sum(vwap, 10) / 10.0
        term2 = -np.abs(close - vwap)
        return term1 * term2

    return _with_raw_alpha(panel, "alpha101_005", calc)


def alpha101_011(panel: pl.DataFrame) -> pl.DataFrame:
    # (rank(ts_max(vwap-close,3))+rank(ts_min(vwap-close,3)))*rank(delta(volume,3))
    def calc(g: pl.DataFrame) -> np.ndarray:
        close = _col(g, "close")
        volume = _col(g, "volume")
        diff = _typical_price(g) - close
        return (_rolling_max(diff, 3) + _rolling_min(diff, 3)) * _delta(volume, 3)

    return _with_raw_alpha(panel, "alpha101_011", calc)


def alpha101_013(panel: pl.DataFrame) -> pl.DataFrame:
    # -1 * rank(covariance(rank(close), rank(volume), 5))
    def calc(g: pl.DataFrame) -> np.ndarray:
        close = _col(g, "close")
        volume = _col(g, "volume")
        return -_rolling_cov(close, volume, 5)

    return _with_ranked_alpha(panel, "alpha101_013", calc)


def alpha101_014(panel: pl.DataFrame) -> pl.DataFrame:
    # (-1*rank(delta(returns,3))) * correlation(open, volume, 10)
    def calc(g: pl.DataFrame) -> np.ndarray:
        open_ = _col(g, "open")
        close = _col(g, "close")
        volume = _col(g, "volume")
        returns = _returns(close)
        return (-_delta(returns, 3)) * _rolling_corr(open_, volume, 10)

    return _with_raw_alpha(panel, "alpha101_014", calc)


def alpha101_015(panel: pl.DataFrame) -> pl.DataFrame:
    # -1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3)
    def calc(g: pl.DataFrame) -> np.ndarray:
        high = _col(g, "high")
        volume = _col(g, "volume")
        corr = _rolling_corr(high, volume, 3)
        return -_rolling_sum(corr, 3)

    return _with_raw_alpha(panel, "alpha101_015", calc)


def alpha101_016(panel: pl.DataFrame) -> pl.DataFrame:
    # -1 * rank(covariance(rank(high), rank(volume), 5))
    def calc(g: pl.DataFrame) -> np.ndarray:
        high = _col(g, "high")
        volume = _col(g, "volume")
        return -_rolling_cov(high, volume, 5)

    return _with_ranked_alpha(panel, "alpha101_016", calc)


def alpha101_017(panel: pl.DataFrame) -> pl.DataFrame:
    # ((-1*rank(ts_rank(close,10)))*rank(delta(delta(close,1),1)))*rank(ts_rank(volume/adv20,5))
    def calc(g: pl.DataFrame) -> np.ndarray:
        close = _col(g, "close")
        volume = _col(g, "volume")
        adv20 = _rolling_mean(volume, 20)
        vol_ratio = np.where(adv20 > 0, volume / adv20, np.nan)
        return (-_ts_rank(close, 10)) * _delta(_delta(close, 1), 1) * _ts_rank(vol_ratio, 5)

    return _with_raw_alpha(panel, "alpha101_017", calc)


def alpha101_018(panel: pl.DataFrame) -> pl.DataFrame:
    # -1 * rank(stddev(abs(close-open),5) + (close-open) + correlation(close,open,10))
    def calc(g: pl.DataFrame) -> np.ndarray:
        open_ = _col(g, "open")
        close = _col(g, "close")
        diff = close - open_
        return -(_rolling_std(np.abs(diff), 5) + diff + _rolling_corr(close, open_, 10))

    return _with_ranked_alpha(panel, "alpha101_018", calc)


def alpha101_019(panel: pl.DataFrame) -> pl.DataFrame:
    # (-1*sign((close-delay(close,7))+delta(close,7))) * (1+rank(1+sum(returns,250)))
    def calc(g: pl.DataFrame) -> np.ndarray:
        close = _col(g, "close")
        returns = _returns(close)
        inner_sign = (close - _delay(close, 7)) + _delta(close, 7)
        return (-np.sign(inner_sign)) * (1.0 + (1.0 + _rolling_sum(returns, 250)))

    return _with_raw_alpha(panel, "alpha101_019", calc)


def alpha101_020(panel: pl.DataFrame) -> pl.DataFrame:
    # (((-1*rank(open-delay(high,1)))*rank(open-delay(close,1)))*rank(open-delay(low,1)))
    def calc(g: pl.DataFrame) -> np.ndarray:
        open_ = _col(g, "open")
        high = _col(g, "high")
        low = _col(g, "low")
        close = _col(g, "close")
        return ((-1.0 * (open_ - _delay(high, 1))) * (open_ - _delay(close, 1))) * (open_ - _delay(low, 1))

    return _with_raw_alpha(panel, "alpha101_020", calc)



def _with_ranked_alpha(panel: pl.DataFrame, name: str, calc: Callable[[pl.DataFrame], np.ndarray]) -> pl.DataFrame:
    raw = _alpha_frame(panel, "_alpha_raw", calc)
    if raw.is_empty():
        return panel.with_columns(pl.lit(None).cast(pl.Float64).alias(name))
    ranked = raw.with_columns(
        (pl.col("_alpha_raw").rank(method="average").over("date") / pl.col("_alpha_raw").count().over("date") - 0.5)
        .alias(name)
    ).select("symbol", "date", name)
    return panel.join(ranked, on=["symbol", "date"], how="left")


def _with_raw_alpha(panel: pl.DataFrame, name: str, calc: Callable[[pl.DataFrame], np.ndarray]) -> pl.DataFrame:
    raw = _alpha_frame(panel, name, calc)
    if raw.is_empty():
        return panel.with_columns(pl.lit(None).cast(pl.Float64).alias(name))
    return panel.join(raw, on=["symbol", "date"], how="left")


def _alpha_frame(panel: pl.DataFrame, name: str, calc: Callable[[pl.DataFrame], np.ndarray]) -> pl.DataFrame:
    rows = []
    for group in panel.sort(["symbol", "date"]).partition_by("symbol", as_dict=False):
        values = calc(group)
        rows.extend(
            {"symbol": group["symbol"][0], "date": d, name: None if not np.isfinite(v) else float(v)}
            for d, v in zip(group["date"].to_list(), values, strict=False)
        )
    schema = {"symbol": pl.Utf8, "date": pl.Date, name: pl.Float64}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _col(df: pl.DataFrame, name: str) -> np.ndarray:
    return df[name].cast(pl.Float64).to_numpy()


def _returns(close: np.ndarray) -> np.ndarray:
    out = np.full(len(close), np.nan)
    out[1:] = close[1:] / close[:-1] - 1
    return out


def _delta(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    out[n:] = values[n:] - values[:-n]
    return out


def _delay(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    out[n:] = values[:-n]
    return out


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_apply(values, window, np.sum)


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_apply(values, window, np.mean)


def _rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_apply(values, window, np.min)


def _rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_apply(values, window, np.max)


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_apply(values, window, lambda x: np.std(x, ddof=1))


def _rolling_argmax(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_apply(values, window, lambda x: float(np.nanargmax(x)))


def _rolling_corr(a: np.ndarray, b: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for i in range(window - 1, len(a)):
        x = a[i - window + 1:i + 1]
        y = b[i - window + 1:i + 1]
        if np.isfinite(x).all() and np.isfinite(y).all() and np.std(x) > 0 and np.std(y) > 0:
            out[i] = float(np.corrcoef(x, y)[0, 1])
    return out

def _rolling_cov(a: np.ndarray, b: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for i in range(window - 1, len(a)):
        x = a[i - window + 1:i + 1]
        y = b[i - window + 1:i + 1]
        if np.isfinite(x).all() and np.isfinite(y).all():
            out[i] = float(np.cov(x, y)[0, 1])
    return out


def _ts_rank(values: np.ndarray, window: int) -> np.ndarray:
    def rank_last(x: np.ndarray) -> float:
        order = np.argsort(x, kind="mergesort")
        ranks = np.empty(len(x), dtype=float)
        ranks[order] = np.arange(1, len(x) + 1, dtype=float)
        return ranks[-1] / len(x)

    return _rolling_apply(values, window, rank_last)


def _rolling_apply(values: np.ndarray, window: int, func: Callable[[np.ndarray], float]) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        win = values[i - window + 1:i + 1]
        if np.isfinite(win).all():
            out[i] = float(func(win))
    return out


def _cross_section_input(values: np.ndarray) -> np.ndarray:
    return values


ALPHAS: dict[str, tuple[AlphaMeta, AlphaFunc]] = {
    "alpha101_001": (AlphaMeta("alpha101_001", "Alpha101 #001", "Alpha101", "rank(ts_argmax(signed_power(...),5))", ("close",), 25), alpha101_001),
    "alpha101_002": (AlphaMeta("alpha101_002", "Alpha101 #002", "Alpha101", "-corr(rank(delta(log(volume),2)), rank(return), 6)", ("open", "close", "volume"), 10), alpha101_002),
    "alpha101_003": (AlphaMeta("alpha101_003", "Alpha101 #003", "Alpha101", "-corr(rank(open), rank(volume), 10)", ("open", "volume"), 20), alpha101_003),
    "alpha101_004": (AlphaMeta("alpha101_004", "Alpha101 #004", "Alpha101", "-ts_rank(low, 9)", ("low",), 9), alpha101_004),
    "alpha101_006": (AlphaMeta("alpha101_006", "Alpha101 #006", "Alpha101", "-corr(open, volume, 10)", ("open", "volume"), 10), alpha101_006),
    "alpha101_007": (AlphaMeta("alpha101_007", "Alpha101 #007", "Alpha101", "adv20/volume conditional momentum", ("close", "volume"), 60), alpha101_007),
    "alpha101_008": (AlphaMeta("alpha101_008", "Alpha101 #008", "Alpha101", "-rank(sum(open,5)*sum(return,5) delta 10)", ("open", "close"), 15), alpha101_008),
    "alpha101_009": (AlphaMeta("alpha101_009", "Alpha101 #009", "Alpha101", "signed close delta trend", ("close",), 6), alpha101_009),
    "alpha101_010": (AlphaMeta("alpha101_010", "Alpha101 #010", "Alpha101", "rank(signed close delta trend)", ("close",), 6), alpha101_010),
    "alpha101_012": (AlphaMeta("alpha101_012", "Alpha101 #012", "Alpha101", "sign(delta(volume))*-delta(close)", ("close", "volume"), 2), alpha101_012),
    "alpha101_005": (AlphaMeta("alpha101_005", "Alpha101 #005", "Alpha101", "rank(open - sum(vwap,10)/10) * (-1 * abs(rank(close - vwap)))", ("open", "high", "low", "close"), 10, "Ported from Vibe-Trading alpha101/alpha_005.py; vwap~(O+H+L+C)/4"), alpha101_005),
    "alpha101_011": (AlphaMeta("alpha101_011", "Alpha101 #011", "Alpha101", "(rank(ts_max(vwap-close,3))+rank(ts_min(vwap-close,3)))*rank(delta(volume,3))", ("open", "high", "low", "close", "volume"), 5, "Ported from Vibe-Trading alpha101/alpha_011.py; vwap~(O+H+L+C)/4"), alpha101_011),
    "alpha101_013": (AlphaMeta("alpha101_013", "Alpha101 #013", "Alpha101", "-1 * rank(covariance(rank(close), rank(volume), 5))", ("close", "volume"), 5, "Ported from Vibe-Trading alpha101/alpha_013.py"), alpha101_013),
    "alpha101_014": (AlphaMeta("alpha101_014", "Alpha101 #014", "Alpha101", "(-1*rank(delta(returns,3))) * correlation(open, volume, 10)", ("open", "close", "volume"), 10, "Ported from Vibe-Trading alpha101/alpha_014.py"), alpha101_014),
    "alpha101_015": (AlphaMeta("alpha101_015", "Alpha101 #015", "Alpha101", "-1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3)", ("high", "volume"), 6, "Ported from Vibe-Trading alpha101/alpha_015.py"), alpha101_015),
    "alpha101_016": (AlphaMeta("alpha101_016", "Alpha101 #016", "Alpha101", "-1 * rank(covariance(rank(high), rank(volume), 5))", ("high", "volume"), 5, "Ported from Vibe-Trading alpha101/alpha_016.py"), alpha101_016),
    "alpha101_017": (AlphaMeta("alpha101_017", "Alpha101 #017", "Alpha101", "((-1*rank(ts_rank(close,10)))*rank(delta(delta(close,1),1)))*rank(ts_rank(volume/adv20,5))", ("close", "volume"), 25, "Ported from Vibe-Trading alpha101/alpha_017.py"), alpha101_017),
    "alpha101_018": (AlphaMeta("alpha101_018", "Alpha101 #018", "Alpha101", "-1 * rank(stddev(abs(close-open),5) + (close-open) + correlation(close,open,10))", ("open", "close"), 10, "Ported from Vibe-Trading alpha101/alpha_018.py"), alpha101_018),
    "alpha101_019": (AlphaMeta("alpha101_019", "Alpha101 #019", "Alpha101", "(-1*sign((close-delay(close,7))+delta(close,7))) * (1+rank(1+sum(returns,250)))", ("close",), 250, "Ported from Vibe-Trading alpha101/alpha_019.py; long 250-bar lookback"), alpha101_019),
    "alpha101_020": (AlphaMeta("alpha101_020", "Alpha101 #020", "Alpha101", "(((-1*rank(open-delay(high,1)))*rank(open-delay(close,1)))*rank(open-delay(low,1)))", ("open", "high", "low", "close"), 2, "Ported from Vibe-Trading alpha101/alpha_020.py"), alpha101_020),
}
