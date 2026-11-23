"""Free fixed-anchor views, with causal input checks and controlled price conversion.

This is the exact-decimal reference path. It does not implement a moving anchor.
Create one provider per task; conversion is bound to its last successful projection.
Derived prices round half-even to 12 decimal places; exchange tick rounding belongs
to the execution rules. Volume and turnover remain unadjusted quantities.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import ValidationError
from tivelo_contracts.base import utc_datetime
from tivelo_contracts.data import AdjustmentFactor, Bar
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import PriceScale, PriceValue, PriceViewSpec

_QUANTUM = Decimal("0.000000000001")
_BOUND = Decimal("100000000000000000000000000")
_OHLC = ("open", "high", "low", "close")


def open_price_view(lock, extension_runtime=None):
    specification = lock.contents.spec.price_view
    if specification.mode == "extension":
        if extension_runtime is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "The locked price view requires its installed extension runtime",
            )
        return extension_runtime.open(lock)
    return StaticPriceView(specification.mode)


def _scaled(value: Decimal, numerator: Decimal, denominator: Decimal) -> Decimal:
    # The context is local so caller Decimal settings cannot change a run.
    with localcontext(Context(prec=96, rounding=ROUND_HALF_EVEN)):
        try:
            result = (value * numerator / denominator).quantize(_QUANTUM)
        except InvalidOperation as exc:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Price exceeds decimal128 precision"
            ) from exc
    if result.copy_abs() >= _BOUND or (value != 0 and result == 0):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Price exceeds decimal128 range")
    return result


def _visible_factors(
    factors: tuple[AdjustmentFactor, ...],
    ids: set[str],
    spec: PriceViewSpec,
    decision_at: datetime,
) -> dict[str, tuple[AdjustmentFactor, ...]]:
    latest: dict[tuple[str, str], AdjustmentFactor] = {}
    for factor in factors:
        if factor.instrument_id not in ids or factor.available_at > decision_at:
            continue
        if (
            factor.definition_id != spec.factor_definition_id
            or factor.baseline_id != spec.factor_baseline_id
            or factor.factor_kind != "cumulative_forward"
        ):
            raise TiveloError(
                ErrorCode.FACTOR_INCOMPATIBLE, "Cumulative factor definition/baseline differs"
            )
        key = factor.instrument_id, factor.factor_id
        previous = latest.get(key)
        if previous is None or factor.available_at > previous.available_at:
            latest[key] = factor
        elif factor.available_at == previous.available_at and factor != previous:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Ambiguous factor revisions")
    selected: dict[str, dict[datetime, AdjustmentFactor]] = {key: {} for key in ids}
    for factor in latest.values():
        # Select revisions before filtering effective dates/cancellations. A
        # correction that moves an event must not resurrect its old revision.
        if factor.cancelled or factor.effective_at > decision_at:
            continue
        by_time = selected[factor.instrument_id]
        if factor.effective_at in by_time:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Overlapping cumulative factor events")
        by_time[factor.effective_at] = factor
    return {
        key: tuple(by_time[time] for time in sorted(by_time)) for key, by_time in selected.items()
    }


class StaticPriceView:
    """Raw/qfq/hfq reference implementation of PriceViewProvider.

    qfq and hfq use the explicitly supplied anchor, never the loaded window start
    or decision time. Future anchors require a separate core proof/execution path;
    this general-purpose provider rejects them with PRICE_BASIS_UNPROVEN.
    """

    def __init__(self, mode: Literal["raw", "qfq", "hfq"]):
        if mode not in ("raw", "qfq", "hfq"):
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unsupported free price view")
        self.mode = mode
        self.provider_id = f"tivelo.{mode}"
        self._scales: dict[str, tuple[PriceScale, Decimal, Decimal]] = {}

    def project(
        self,
        bars: tuple[Bar, ...],
        factors: tuple[AdjustmentFactor, ...],
        spec: PriceViewSpec,
        *,
        decision_at: datetime,
    ) -> tuple[Bar, ...]:
        return tuple(self._project_rows(bars, factors, spec, decision_at, prices_only=False))

    def iter_prices(
        self,
        bars: tuple[Bar, ...],
        factors: tuple[AdjustmentFactor, ...],
        spec: PriceViewSpec,
        *,
        decision_at: datetime,
    ):
        """Yield validated price columns for a numeric panel, without new bar records."""
        return self._project_rows(bars, factors, spec, decision_at, prices_only=True)

    def _project_rows(self, bars, factors, spec, decision_at, *, prices_only):
        self._scales = {}
        decision_at = utc_datetime(decision_at)
        if spec.mode != self.mode or spec.provider_id != self.provider_id:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Price view provider/spec mismatch")
        if spec.anchor_at is not None and spec.anchor_at > decision_at:
            raise TiveloError(ErrorCode.PRICE_BASIS_UNPROVEN, "Fixed anchor is after this decision")
        currencies: dict[str, str] = {}
        identities = set()
        for bar in bars:
            if bar.price_basis != "raw":
                raise TiveloError(
                    ErrorCode.RAW_PRICE_REQUIRED, "Projection requires raw input bars"
                )
            if bar.available_at > decision_at or bar.bar_end > decision_at or not bar.is_final:
                raise TiveloError(
                    ErrorCode.FUTURE_INFORMATION, "Bar is not final and visible at decision"
                )
            key = bar.instrument_id, bar.timeframe, bar.bar_start
            if key in identities:
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Select one visible revision per bar")
            identities.add(key)
            previous = currencies.setdefault(bar.instrument_id, bar.quote_currency)
            if previous != bar.quote_currency:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Instrument currency changes within the view"
                )
        if self.mode == "raw":
            self._scales = {
                key: (
                    PriceScale(
                        provider_id=self.provider_id,
                        mode=self.mode,
                        anchor_at=None,
                        decision_at=decision_at,
                        instrument_id=key,
                        currency=currency,
                        raw_multiplier="1",
                    ),
                    Decimal(1),
                    Decimal(1),
                )
                for key, currency in currencies.items()
            }
            if prices_only:
                for bar in bars:
                    yield {name: getattr(bar, name) for name in _OHLC}
            else:
                yield from bars
            return

        series = _visible_factors(factors, set(currencies), spec, decision_at)
        times = {key: [factor.effective_at for factor in values] for key, values in series.items()}

        def at(key: str, time: datetime) -> AdjustmentFactor:
            index = bisect_right(times[key], time) - 1
            if index < 0:
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "Missing cumulative factor at price/anchor time",
                )
            return series[key][index]

        assert spec.anchor_at is not None  # PriceViewSpec enforces it for qfq/hfq.
        anchors = {key: at(key, spec.anchor_at) for key in currencies}
        for bar in bars:
            factor = at(bar.instrument_id, bar.bar_start)
            anchor = anchors[bar.instrument_id]
            boundary = bisect_right(times[bar.instrument_id], bar.bar_start)
            if (
                boundary < len(times[bar.instrument_id])
                and times[bar.instrument_id][boundary] < bar.bar_end
            ):
                raise TiveloError(ErrorCode.FACTOR_INCOMPATIBLE, "Adjustment changes inside a bar")
            changes = {
                name: _scaled(getattr(bar, name), factor.value, anchor.value) for name in _OHLC
            }
            if bar.vwap is not None:
                changes["vwap"] = _scaled(bar.vwap, factor.value, anchor.value)
            if prices_only:
                yield changes
                continue
            known_at = max(bar.available_at, factor.available_at, anchor.available_at)
            provenance = json.dumps(
                {
                    "format": "tivelo-static-price-v1",
                    "bar": bar.model_dump(mode="json"),
                    "factor": factor.model_dump(mode="json"),
                    "anchor": anchor.model_dump(mode="json"),
                    "spec": spec.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            changes.update(
                price_basis=self.mode,
                available_at=known_at,
                ingested_at=max(bar.ingested_at, known_at),
                source_id=self.provider_id,
                revision_id="view:" + hashlib.sha256(provenance).hexdigest(),
            )
            yield Bar.model_validate({**bar.model_dump(), **changes})
        scales = {}
        for key, currency in currencies.items():
            numerator, denominator = anchors[key].value, at(key, decision_at).value
            scale = PriceScale(
                provider_id=self.provider_id,
                mode=self.mode,
                anchor_at=spec.anchor_at,
                decision_at=decision_at,
                instrument_id=key,
                currency=currency,
                raw_multiplier=_scaled(Decimal(1), numerator, denominator),
            )
            scales[key] = scale, numerator, denominator
        self._scales = scales

    def price(
        self, value: Decimal | str | int, *, instrument_id: str, decision_at: datetime
    ) -> PriceValue:
        decision_at = utc_datetime(decision_at)
        issued = self._scales.get(instrument_id)
        if issued is None or issued[0].decision_at != decision_at:
            raise TiveloError(ErrorCode.PRICE_BASIS_UNPROVEN, "No projected scale at this decision")
        return PriceValue(value=value, scale=issued[0])

    def to_raw(self, value: PriceValue, *, decision_at: datetime) -> Decimal:
        decision_at = utc_datetime(decision_at)
        issued = self._scales.get(value.scale.instrument_id)
        if issued is None or value.scale != issued[0] or value.scale.decision_at != decision_at:
            raise TiveloError(ErrorCode.PRICE_BASIS_UNPROVEN, "Unrecognized or stale price scale")
        # Use the full issued factor ratio, not a caller-supplied/rounded multiplier.
        try:
            return _scaled(value.value, issued[1], issued[2])
        except (InvalidOperation, ValidationError) as exc:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Price conversion exceeds precision"
            ) from exc
