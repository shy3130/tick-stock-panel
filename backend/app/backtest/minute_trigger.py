"""分钟级成交回放的参考价计算 — 买卖两侧同守「当日已知」纪律。

参考线在当日开盘即必须已知: 含当根收盘的均线会让盘中模拟成交价依赖
15:00 才确定的收盘价 (前视), 污染 minute_fill 声称的分钟级精确成交。
"""
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


def build_minute_entry_reference(close: np.ndarray, window: int = 5) -> np.ndarray:
    """分钟穿越成交的参考线: 前 window-1 根收盘均值 (当日开盘即已知)。

    rolling_mean 第 t 行含当根收盘, 代数剔除当根即得前 window-1 根均值:
    (window·MA_window[t] - close[t]) / (window-1)。与卖出侧
    build_minute_exit_reference 同一纪律 — 买卖参考线都不得使用当日收盘。
    MA 不足窗口 (NaN) 的行结果为 NaN, 调用方按无参考线退化到 VWAP。
    """
    from app.backtest.matrix import rolling_mean  # 延迟导入: matrix 模块级依赖本模块

    ma = rolling_mean(close, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (float(window) * ma - close) / float(window - 1)
