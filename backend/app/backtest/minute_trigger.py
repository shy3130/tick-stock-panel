"""分钟级卖出信号回放的支持范围与参考价计算。"""
from __future__ import annotations

import numpy as np

MINUTE_EXIT_TRIGGER_SIGNALS = frozenset({
    "signal_ma5_breakdown",
    "signal_ma10_breakdown",
    "signal_ma20_breakdown",
    "signal_ma_dead_5_20",
})


def unsupported_minute_exit_signals(signals: list[str] | tuple[str, ...]) -> list[str]:
    return sorted(set(signals) - MINUTE_EXIT_TRIGGER_SIGNALS)


def build_minute_exit_reference(
    close: np.ndarray,
    fields: dict[str, np.ndarray],
    exit_signal_code: np.ndarray,
    exit_signal_ids: tuple[str, ...],
) -> np.ndarray:
    """为可回放的卖出信号计算当日已知的价格触发线。"""
    result = np.full(close.shape, np.nan, dtype=np.float32)

    def _apply(code: int, value: np.ndarray) -> None:
        mask = (exit_signal_code == code) & np.isfinite(value) & (value > 0)
        result[mask] = value[mask].astype(np.float32)

    with np.errstate(divide="ignore", invalid="ignore"):
        for code, signal_id in enumerate(exit_signal_ids):
            if signal_id == "signal_ma5_breakdown" and "ma5" in fields:
                _apply(code, (5.0 * fields["ma5"] - close) / 4.0)
            elif signal_id == "signal_ma10_breakdown" and "ma10" in fields:
                _apply(code, (10.0 * fields["ma10"] - close) / 9.0)
            elif signal_id == "signal_ma20_breakdown" and "ma20" in fields:
                _apply(code, (20.0 * fields["ma20"] - close) / 19.0)
            elif (
                signal_id == "signal_ma_dead_5_20"
                and "ma5" in fields
                and "ma20" in fields
            ):
                sum4 = 5.0 * fields["ma5"] - close
                sum19 = 20.0 * fields["ma20"] - close
                _apply(code, (sum19 - 4.0 * sum4) / 3.0)

    result.setflags(write=False)
    return result
