"""Pure selection-quality evaluation primitives for Selection MVP v2.

Signals are formed on the close of session ``t``.  Forward labels buy at the
next session open and exit at the open after ``horizon`` held sessions.  Label
construction is intentionally separate from score construction so future
prices cannot enter ranking or factor selection.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Any

import numpy as np

HORIZONS = (1, 3, 5, 10)
TOP_KS = (5, 10, 20)
BASE_VARIANT = "quality_momentum"
FACTOR_VARIANT = "quality_momentum_plus_factor"
PRIMARY_HORIZON = 5
PRIMARY_TOP_K = 10


@dataclass(frozen=True, slots=True)
class TradingCosts:
    commission_per_side: float = 0.0002
    stamp_tax_sell: float = 0.0005
    slippage_bps_per_side: float = 5.0

    @property
    def round_trip(self) -> float:
        slippage = self.slippage_bps_per_side / 10_000.0
        return 2.0 * self.commission_per_side + self.stamp_tax_sell + 2.0 * slippage

    def to_dict(self) -> dict[str, float]:
        return {
            "commission_per_side": self.commission_per_side,
            "stamp_tax_sell": self.stamp_tax_sell,
            "slippage_bps_per_side": self.slippage_bps_per_side,
            "round_trip": self.round_trip,
        }


@dataclass(frozen=True, slots=True)
class InstrumentWindow:
    symbol: str
    name: str
    list_date: date | None
    delist_date: date | None
    current_non_st: bool


@dataclass(frozen=True, slots=True)
class SessionFold:
    index: int
    train_ids: tuple[int, ...]
    test_ids: tuple[int, ...]


def parse_compact_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip().replace("-", "")
    if not text or text.lower() == "none":
        return None
    if len(text) < 8 or not text[:8].isdigit():
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def instrument_windows(rows: Iterable[Mapping[str, Any]]) -> dict[str, InstrumentWindow]:
    """Normalize current stock-basic rows without pretending they are point-in-time ST history."""
    output: dict[str, InstrumentWindow] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
        if not symbol:
            continue
        name = str(row.get("name") or "")
        upper_name = name.upper()
        current_non_st = "ST" not in upper_name and "退" not in name
        output[symbol] = InstrumentWindow(
            symbol=symbol,
            name=name,
            list_date=parse_compact_date(row.get("list_date")),
            delist_date=parse_compact_date(row.get("delist_date")),
            current_non_st=current_non_st,
        )
    return output


def dynamic_universe_mask(
    timestamp_labels: Sequence[str],
    symbols: Sequence[str],
    instruments: Mapping[str, InstrumentWindow],
    present: np.ndarray,
) -> tuple[np.ndarray, dict[str, int]]:
    """Build a daily listed/current-non-ST universe mask.

    ``current_non_st`` is a disclosed fallback because the local data package does
    not yet contain historical name-change intervals.  Listing and delisting dates
    are still applied causally per session.
    """
    shape = (len(timestamp_labels), len(symbols))
    if present.shape != shape:
        raise ValueError("present mask does not align with market axes")
    session_dates = [date.fromisoformat(str(label)[:10]) for label in timestamp_labels]
    mask = np.zeros(shape, dtype=bool)
    missing_metadata = 0
    excluded_current_st = 0
    for asset_id, symbol in enumerate(symbols):
        info = instruments.get(str(symbol))
        if info is None:
            missing_metadata += 1
            continue
        if not info.current_non_st:
            excluded_current_st += 1
            continue
        first = info.list_date or session_dates[0]
        last = info.delist_date or session_dates[-1]
        active = np.fromiter(
            (first <= session_date <= last for session_date in session_dates),
            dtype=bool,
            count=len(session_dates),
        )
        mask[:, asset_id] = active & present[:, asset_id]
    return mask, {
        "axis_symbols": len(symbols),
        "missing_stock_basic": missing_metadata,
        "excluded_current_st_or_delisting_name": excluded_current_st,
    }


def point_in_time_universe_mask(
    timestamp_labels: Sequence[str],
    symbols: Sequence[str],
    instruments: Mapping[str, InstrumentWindow],
    present: np.ndarray,
    historical_st: np.ndarray,
) -> tuple[np.ndarray, dict[str, int]]:
    """Build a daily listed, tradable and historically non-ST universe mask."""
    shape = (len(timestamp_labels), len(symbols))
    if present.shape != shape or historical_st.shape != shape:
        raise ValueError("point-in-time masks do not align with market axes")
    session_dates = [date.fromisoformat(str(label)[:10]) for label in timestamp_labels]
    mask = np.zeros(shape, dtype=bool)
    missing_metadata = 0
    for asset_id, symbol in enumerate(symbols):
        info = instruments.get(str(symbol))
        if info is None:
            missing_metadata += 1
            continue
        first = info.list_date or session_dates[0]
        last = info.delist_date or session_dates[-1]
        active = np.fromiter(
            (first <= session_date <= last for session_date in session_dates),
            dtype=bool,
            count=len(session_dates),
        )
        mask[:, asset_id] = active & present[:, asset_id] & ~historical_st[:, asset_id]
    return mask, {
        "axis_symbols": len(symbols),
        "missing_stock_basic": missing_metadata,
        "historical_st_observations_excluded": int(historical_st.sum()),
        "symbols_ever_historical_st": int(np.any(historical_st, axis=0).sum()),
    }


def _lexical_rank(symbols: Sequence[str]) -> np.ndarray:
    order = np.argsort(np.asarray(symbols, dtype=str), kind="stable")
    ranks = np.empty(len(symbols), dtype=np.int64)
    ranks[order] = np.arange(len(symbols), dtype=np.int64)
    return ranks


def cross_sectional_percentiles(
    values: np.ndarray,
    eligible: np.ndarray,
    symbols: Sequence[str],
) -> np.ndarray:
    """Ordinal daily percentile with lexical symbol tie-break and NaN outside eligibility."""
    data = np.asarray(values, dtype=np.float64)
    allowed = np.asarray(eligible, dtype=bool) & np.isfinite(data)
    if data.shape != allowed.shape or data.shape[1] != len(symbols):
        raise ValueError("cross-sectional values do not align with eligibility or symbols")
    output = np.full(data.shape, np.nan, dtype=np.float32)
    lexical = _lexical_rank(symbols)
    for time_id in range(data.shape[0]):
        ids = np.flatnonzero(allowed[time_id])
        if ids.size == 0:
            continue
        order = np.lexsort((lexical[ids], data[time_id, ids]))
        ranked = ids[order]
        if ranked.size == 1:
            output[time_id, ranked[0]] = 1.0
        else:
            output[time_id, ranked] = np.linspace(0.0, 1.0, ranked.size, dtype=np.float32)
    return output


def combine_factor_overlay(
    base_percentile: np.ndarray,
    factor_percentile: np.ndarray,
    *,
    factor_weight: float,
) -> np.ndarray:
    if not 0.0 <= factor_weight <= 1.0:
        raise ValueError("factor_weight must be in [0, 1]")
    if base_percentile.shape != factor_percentile.shape:
        raise ValueError("base and factor percentile matrices must align")
    combined = (1.0 - factor_weight) * base_percentile.astype(
        np.float64
    ) + factor_weight * factor_percentile.astype(np.float64)
    combined[~np.isfinite(base_percentile) | ~np.isfinite(factor_percentile)] = np.nan
    return combined.astype(np.float32)


def build_forward_open_labels(
    market: Any,
    horizons: Sequence[int] = HORIZONS,
) -> dict[int, dict[str, np.ndarray]]:
    """Create future returns only for evaluation; never pass these arrays to score builders."""
    open_price = np.asarray(market.open, dtype=np.float64)
    tradable = np.asarray(market.tradable, dtype=bool)
    limit_up = np.asarray(market.limit_up_locked, dtype=bool)
    limit_down = np.asarray(market.limit_down_locked, dtype=bool)
    labels: dict[int, dict[str, np.ndarray]] = {}
    for raw_horizon in horizons:
        horizon = int(raw_horizon)
        if horizon <= 0:
            raise ValueError("forward-return horizons must be positive")
        gross = np.full(open_price.shape, np.nan, dtype=np.float32)
        valid = np.zeros(open_price.shape, dtype=bool)
        stop = open_price.shape[0] - horizon - 1
        if stop > 0:
            entry = open_price[1 : stop + 1]
            exit_ = open_price[horizon + 1 : horizon + 1 + stop]
            current_valid = (
                np.isfinite(entry)
                & np.isfinite(exit_)
                & (entry > 0)
                & (exit_ > 0)
                & tradable[1 : stop + 1]
                & tradable[horizon + 1 : horizon + 1 + stop]
                & ~limit_up[1 : stop + 1]
                & ~limit_down[horizon + 1 : horizon + 1 + stop]
            )
            computed = np.full(entry.shape, np.nan, dtype=np.float64)
            np.divide(exit_, entry, out=computed, where=current_valid)
            computed[current_valid] -= 1.0
            gross[:stop] = computed.astype(np.float32)
            valid[:stop] = current_valid
        labels[horizon] = {"gross_return": gross, "valid": valid}
    return labels


def generate_session_folds(
    session_ids: Sequence[int],
    *,
    train_sessions: int,
    test_sessions: int,
    step_sessions: int,
) -> list[SessionFold]:
    if min(train_sessions, test_sessions, step_sessions) <= 0:
        raise ValueError("fold session counts must be positive")
    ids = tuple(int(value) for value in session_ids)
    folds: list[SessionFold] = []
    start = 0
    while start + train_sessions + test_sessions <= len(ids):
        split = start + train_sessions
        stop = split + test_sessions
        folds.append(
            SessionFold(
                index=len(folds),
                train_ids=ids[start:split],
                test_ids=ids[split:stop],
            )
        )
        start += step_sessions
    return folds


def _rank_ids(
    score_row: np.ndarray,
    eligible_row: np.ndarray,
    lexical: np.ndarray,
) -> np.ndarray:
    ids = np.flatnonzero(np.asarray(eligible_row, dtype=bool) & np.isfinite(score_row))
    if ids.size == 0:
        return ids
    return ids[np.lexsort((lexical[ids], -np.asarray(score_row)[ids]))]


def _rank_ic(score: np.ndarray, returns: np.ndarray, ids: np.ndarray) -> float | None:
    if ids.size < 20:
        return None
    score_values = np.asarray(score)[ids]
    return_values = np.asarray(returns)[ids]
    score_order = np.argsort(score_values, kind="stable")
    return_order = np.argsort(return_values, kind="stable")
    score_rank = np.empty(ids.size, dtype=np.float64)
    return_rank = np.empty(ids.size, dtype=np.float64)
    score_rank[score_order] = np.arange(ids.size, dtype=np.float64)
    return_rank[return_order] = np.arange(ids.size, dtype=np.float64)
    score_std = float(score_rank.std())
    return_std = float(return_rank.std())
    if score_std == 0.0 or return_std == 0.0:
        return None
    return float(np.corrcoef(score_rank, return_rank)[0, 1])


def _drawdown(returns: Sequence[float]) -> tuple[float, float]:
    equity = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        equity *= max(1e-9, 1.0 + float(value))
        peak = max(peak, equity)
        maximum_drawdown = min(maximum_drawdown, equity / peak - 1.0)
    return equity - 1.0, maximum_drawdown


def aggregate_cohort_records(
    records: Sequence[Mapping[str, Any]],
    *,
    horizon: int,
) -> dict[str, Any]:
    usable = [row for row in records if row.get("net_return") is not None]
    complete = [row for row in usable if row.get("label_coverage") == 1.0]
    net = [float(row["net_return"]) for row in usable]
    excess = [float(row["excess_return"]) for row in usable]
    spreads = [float(row["top_bottom_spread"]) for row in usable]
    rank_ics = [float(row["rank_ic"]) for row in usable if row.get("rank_ic") is not None]
    positive_sum = sum(value for value in net if value > 0)
    negative_sum = abs(sum(value for value in net if value < 0))
    stock_wins = sum(int(row.get("stock_wins", 0)) for row in usable)
    stock_count = sum(int(row.get("valid_selected", 0)) for row in usable)

    turnovers: list[float] = []
    previous: set[int] | None = None
    for row in records:
        selected = set(int(value) for value in row.get("selected_ids", ()))
        if previous is not None and selected:
            turnovers.append(1.0 - len(previous & selected) / max(len(selected), 1))
        previous = selected

    phases: list[dict[str, Any]] = []
    for phase in range(int(horizon)):
        phase_returns = [
            float(row["net_return"])
            for index, row in enumerate(records)
            if index % int(horizon) == phase
            and row.get("net_return") is not None
            and row.get("label_coverage") == 1.0
        ]
        compounded, max_drawdown = _drawdown(phase_returns)
        phases.append(
            {
                "phase": phase,
                "periods": len(phase_returns),
                "compounded_return": round(compounded, 8),
                "max_drawdown": round(max_drawdown, 8),
            }
        )

    compounded_values = [float(row["compounded_return"]) for row in phases]
    drawdown_values = [float(row["max_drawdown"]) for row in phases]
    return {
        "signal_days": len(records),
        "usable_cohorts": len(usable),
        "complete_cohorts": len(complete),
        "complete_cohort_ratio": round(len(complete) / len(records), 6) if records else 0.0,
        "mean_net_return": round(float(np.mean(net)), 8) if net else None,
        "median_net_return": round(float(np.median(net)), 8) if net else None,
        "cohort_win_rate": round(sum(value > 0 for value in net) / len(net), 6) if net else None,
        "stock_win_rate": round(stock_wins / stock_count, 6) if stock_count else None,
        "profit_factor": (round(positive_sum / negative_sum, 6) if negative_sum > 0 else None),
        "mean_excess_return": round(float(np.mean(excess)), 8) if excess else None,
        "mean_top_bottom_spread": round(float(np.mean(spreads)), 8) if spreads else None,
        "mean_rank_ic": round(float(np.mean(rank_ics)), 8) if rank_ics else None,
        "positive_rank_ic_ratio": (
            round(sum(value > 0 for value in rank_ics) / len(rank_ics), 6) if rank_ics else None
        ),
        "mean_selection_turnover": (round(float(np.mean(turnovers)), 6) if turnovers else None),
        "phase_portfolios": {
            "definition": (
                "non-overlapping open_t+1 cohorts for each phase offset; no phase is selected"
            ),
            "phases": phases,
            "median_compounded_return": (
                round(float(median(compounded_values)), 8) if compounded_values else None
            ),
            "worst_max_drawdown": min(drawdown_values) if drawdown_values else None,
        },
    }


def evaluate_score_grid(
    *,
    scores: np.ndarray,
    eligible: np.ndarray,
    symbols: Sequence[str],
    timestamp_labels: Sequence[str],
    forward_labels: Mapping[int, Mapping[str, np.ndarray]],
    time_ids: Sequence[int],
    costs: TradingCosts,
    horizons: Sequence[int] = HORIZONS,
    top_ks: Sequence[int] = TOP_KS,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[tuple[int, int], list[dict[str, Any]]]]:
    if scores.shape != eligible.shape:
        raise ValueError("scores and eligibility must align")
    lexical = _lexical_rank(symbols)
    ranked_by_time = {
        int(time_id): _rank_ids(scores[time_id], eligible[time_id], lexical) for time_id in time_ids
    }
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    record_map: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for raw_horizon in horizons:
        horizon = int(raw_horizon)
        label = forward_labels[horizon]
        gross_returns = np.asarray(label["gross_return"], dtype=np.float64)
        label_valid = np.asarray(label["valid"], dtype=bool)
        metrics[str(horizon)] = {}
        for raw_top_k in top_ks:
            top_k = int(raw_top_k)
            if top_k <= 0:
                raise ValueError("top_k must be positive")
            records: list[dict[str, Any]] = []
            for time_id in time_ids:
                ranked = ranked_by_time[int(time_id)]
                selected = ranked[:top_k]
                bottom = ranked[-top_k:] if ranked.size >= top_k else ranked
                valid_selected = selected[label_valid[time_id, selected]]
                valid_bottom = bottom[label_valid[time_id, bottom]]
                valid_universe = ranked[label_valid[time_id, ranked]]
                selected_returns = gross_returns[time_id, valid_selected]
                bottom_returns = gross_returns[time_id, valid_bottom]
                universe_returns = gross_returns[time_id, valid_universe]
                net_return = (
                    float(np.mean(selected_returns)) - costs.round_trip
                    if selected_returns.size
                    else None
                )
                reference_return = (
                    float(np.mean(universe_returns)) if universe_returns.size else None
                )
                bottom_return = float(np.mean(bottom_returns)) if bottom_returns.size else None
                ic = _rank_ic(
                    scores[time_id],
                    gross_returns[time_id],
                    valid_universe,
                )
                records.append(
                    {
                        "time_id": int(time_id),
                        "signal_date": str(timestamp_labels[time_id])[:10],
                        "selected_ids": tuple(int(value) for value in selected),
                        "selected_count": int(selected.size),
                        "valid_selected": int(valid_selected.size),
                        "label_coverage": (
                            round(valid_selected.size / selected.size, 6) if selected.size else 0.0
                        ),
                        "net_return": net_return,
                        "reference_return": reference_return,
                        "excess_return": (
                            net_return - reference_return
                            if net_return is not None and reference_return is not None
                            else None
                        ),
                        "top_bottom_spread": (
                            float(np.mean(selected_returns)) - bottom_return
                            if selected_returns.size and bottom_return is not None
                            else 0.0
                        ),
                        "rank_ic": ic,
                        "stock_wins": int(np.sum(selected_returns - costs.round_trip > 0)),
                    }
                )
            record_map[(horizon, top_k)] = records
            metrics[str(horizon)][str(top_k)] = aggregate_cohort_records(
                records,
                horizon=horizon,
            )
    return metrics, record_map


def summarize_record_grid(
    record_map: Mapping[tuple[int, int], Sequence[Mapping[str, Any]]],
    *,
    time_ids: Sequence[int],
    horizons: Sequence[int] = HORIZONS,
    top_ks: Sequence[int] = TOP_KS,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[tuple[int, int], list[dict[str, Any]]]]:
    """Aggregate a previously ranked record grid for a train or test slice."""
    wanted = {int(value) for value in time_ids}
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    sliced: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for raw_horizon in horizons:
        horizon = int(raw_horizon)
        metrics[str(horizon)] = {}
        for raw_top_k in top_ks:
            top_k = int(raw_top_k)
            records = [
                dict(row) for row in record_map[(horizon, top_k)] if int(row["time_id"]) in wanted
            ]
            sliced[(horizon, top_k)] = records
            metrics[str(horizon)][str(top_k)] = aggregate_cohort_records(
                records,
                horizon=horizon,
            )
    return metrics, sliced


def training_objective(
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    horizon: int = PRIMARY_HORIZON,
    top_k: int = PRIMARY_TOP_K,
) -> float:
    primary = metrics[str(horizon)][str(top_k)]
    excess = primary.get("mean_excess_return")
    drawdown = primary.get("phase_portfolios", {}).get("worst_max_drawdown")
    if excess is None or drawdown is None:
        return float("-inf")
    return float(excess) - 0.25 * abs(float(drawdown))


def select_variant_from_training(
    training_metrics: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
    *,
    improvement_margin: float = 0.001,
) -> dict[str, Any]:
    """Choose the optional factor using training metrics only.

    The function deliberately has no test-metric argument, making leakage harder
    to introduce accidentally.
    """
    base_objective = training_objective(training_metrics[BASE_VARIANT])
    factor_objective = training_objective(training_metrics[FACTOR_VARIANT])
    use_factor = math.isfinite(factor_objective) and factor_objective > base_objective + float(
        improvement_margin
    )
    return {
        "selected_variant": FACTOR_VARIANT if use_factor else BASE_VARIANT,
        "base_objective": base_objective,
        "factor_objective": factor_objective,
        "required_improvement_margin": float(improvement_margin),
        "selection_rule": (
            "factor overlay only when its train-only primary objective exceeds base by margin"
        ),
    }


def preferred_live_variant(fold_selections: Sequence[str]) -> dict[str, Any]:
    """Freeze the next live selector from train-only fold decisions, with base as tie-break."""
    base_count = sum(value == BASE_VARIANT for value in fold_selections)
    factor_count = sum(value == FACTOR_VARIANT for value in fold_selections)
    selected = FACTOR_VARIANT if factor_count > base_count else BASE_VARIANT
    return {
        "selected_variant": selected,
        "train_fold_votes": {BASE_VARIANT: base_count, FACTOR_VARIANT: factor_count},
        "tie_break": BASE_VARIANT,
        "test_metrics_used": False,
    }
