from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

_NUMERIC_FIELDS = ("open", "high", "low", "close", "volume")
_MINIMUM_BARS = {"5m": 2, "15m": 2, "30m": 2, "60m": 2, "day": 2}


class InsufficientDowBars(ValueError):  # noqa: N818 - domain condition, not a generic API error
    def __init__(
        self,
        timeframe: str,
        valid_bars: int,
        required_bars: int,
    ) -> None:
        self.timeframe = timeframe
        self.valid_bars = valid_bars
        self.required_bars = required_bars
        super().__init__(
            "HISTORY_INCOMPLETE:"
            f"{timeframe}:VALID_BARS_{valid_bars}_OF_{required_bars}"
        )


def _finite_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def sanitize_engine_bars(
    timeframe: str,
    bars: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_timestamp: dict[str, dict[str, object]] = {}
    for raw in bars:
        timestamp = str(raw.get("timestamp") or "").strip()
        values = {field: _finite_number(raw.get(field)) for field in _NUMERIC_FIELDS}
        if not timestamp or any(value is None for value in values.values()):
            continue
        open_price = values["open"]
        high = values["high"]
        low = values["low"]
        close = values["close"]
        volume = values["volume"]
        assert open_price is not None
        assert high is not None
        assert low is not None
        assert close is not None
        assert volume is not None
        if min(open_price, high, low, close) <= 0 or volume < 0:
            continue
        if low > high or low > min(open_price, close):
            continue
        if high < max(open_price, close):
            continue
        by_timestamp[timestamp] = {"timestamp": timestamp, **values}
    result = [by_timestamp[key] for key in sorted(by_timestamp)]
    required = _MINIMUM_BARS.get(timeframe, 2)
    if len(result) < required:
        raise InsufficientDowBars(timeframe, len(result), required)
    return result
