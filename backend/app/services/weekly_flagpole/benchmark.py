"""Sealed-universe equal-weight benchmark; non-sealed layers fail closed."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from statistics import mean


class EqualWeightBenchmark:
    def __init__(self, closes: Mapping[str, Mapping[date, float]], calendar: list[date]):
        self.closes = {str(s): dict(v) for s, v in closes.items()}
        self.calendar = calendar
        self.index = {d: i for i, d in enumerate(calendar)}
        self.cache = {}

    def forward_return(self, anchor: date, horizon: int) -> float | None:
        key = (anchor, horizon)
        if key in self.cache:
            return self.cache[key]
        idx = self.index.get(anchor)
        value = None
        if idx is not None and idx + horizon < len(self.calendar):
            end = self.calendar[idx + horizon]
            values = []
            for series in self.closes.values():
                if anchor in series and end in series and series[anchor] > 0:
                    values.append(series[end] / series[anchor] - 1)
            if values:
                value = mean(values)
        self.cache[key] = value
        return value


def layer_status() -> dict[str, dict[str, str]]:
    return {
        "equal_weight_universe": {"status": "ok", "source": "sealed_universe"},
        "industry_momentum": {"status": "unavailable", "reason": "industry_layer_not_sealed"},
        "market_index": {"status": "unavailable", "reason": "index_layer_not_sealed"},
    }
