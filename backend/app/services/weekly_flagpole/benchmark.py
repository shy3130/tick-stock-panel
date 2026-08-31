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


def attribution_layers(provenance: object) -> dict[str, dict[str, str]]:
    """Expose F5 layers only when sealed/PIT provenance is explicit."""
    if not isinstance(provenance, dict):
        return {
            "industry_momentum": {
                "status": "unavailable",
                "reason": "industry_pit_provenance_missing",
            },
            "market_index": {
                "status": "unavailable",
                "reason": "market_pit_provenance_missing",
            },
        }
    out = {}
    for name, label in (("industry_momentum", "industry"), ("market_index", "market")):
        fact = provenance.get(label)
        valid = (
            isinstance(fact, dict)
            and fact.get("sealed") is True
            and fact.get("as_of") is not None
            and fact.get("generation") is not None
        )
        out[name] = (
            {"status": "ok", "source": "sealed_pit"}
            if valid
            else {"status": "unavailable", "reason": f"{label}_pit_provenance_missing"}
        )
    return out
