"""Bounded stock factor histories and explicit coverage across all requested windows."""

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from tivelo_contracts.base import utc_datetime
from tivelo_contracts.data import AdjustmentFactor, Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..providers import ProviderCapability, ProviderDescriptor
from ..repository_pages import PagedObservation
from .cn_daily import CN
from .cn_factors import DEFINITIONS, factor_values

SYMBOL_DAYS_PER_REQUEST = 2048


def collect_events(rows, symbols, start, end, context):
    """A (symbol, None, None) row confirms an explicitly empty history."""
    requested = set(symbols)
    events = {}
    empty = set()
    for symbol, day, ratio in rows:
        context.check_cancelled()
        if symbol not in requested:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Factor source returned an unrequested symbol"
            )
        dates = events.setdefault(symbol, {})
        if day is None:
            if dates:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Empty history conflicts with factor events"
                )
            empty.add(symbol)
            continue
        if symbol in empty or not start <= day < end:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Stock factor falls outside declared history"
            )
        if not isinstance(ratio, Decimal) or not ratio.is_finite() or ratio <= 0:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Stock factor must be a positive decimal")
        if day in dates and dates[day] != ratio:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Conflicting stock factors for one date")
        dates[day] = ratio
    return events


class CNFactorObservation(PagedObservation):
    definitions = DEFINITIONS

    def __init__(self, source, *, batch, now, version, baseline_prefix, declaration):
        self.source = source
        self.batch = batch
        self.now = now or (lambda: datetime.now(UTC))
        self.provider_version = version
        self.definition = self.definitions[source.representation]
        self.baseline = f"{baseline_prefix}:{source.baseline_date or 'event-pre-post'}"
        declaration = {
            **declaration,
            "definition": self.definition,
            "baseline": self.baseline,
            "bindings": [s.model_dump() for s in source.symbols],
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def describe(self):
        return ProviderDescriptor(
            provider_id=self.source.expected_provider,
            version=self.provider_version,
            source_ids=(self.source.expected_provider,),
            capabilities=(
                ProviderCapability(
                    market_id="cn.equity",
                    asset_class="equity",
                    record_type="adjustment_factor",
                    knowledge_basis="latest_reconstruction",
                    normalization_version=self.definition,
                ),
            ),
        )

    def check_ready(self, context):
        context.check_cancelled()
        self.observed_at = utc_datetime(self.now())
        if self.source.request.end > self.observed_at:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Factor capture extends into the future"
            )
        self._start_pages(self._stream(context))
        self.missing = set()

    def _read_window(self, symbols, lower, upper, context):
        raise NotImplementedError

    def _group(self, group, first, end, context):
        # A missing symbol in any window leaves its event history and cumulative base unknown.
        events = {entry.symbol: {} for entry in group}
        symbols = tuple(events)
        lower = first
        while lower < end:
            upper = lower + timedelta(days=min((end - lower).days, SYMBOL_DAYS_PER_REQUEST))
            window = self._read_window(symbols, lower, upper, context)
            context.check_cancelled()
            observed = utc_datetime(self.now())
            if observed < self.observed_at:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Clock moved backwards during stock factor capture"
                )
            self.observed_at = observed
            for symbol, days in window.items():
                if symbol in events:
                    events[symbol].update(days)
            for symbol in events.keys() - window.keys():
                del events[symbol]
            lower = upper
        return events

    def _stream(self, context):
        source = self.source
        start = source.request.start.astimezone(CN).date()
        end = source.request.end.astimezone(CN).date()
        first = source.baseline_date or start
        group_size = min(self.batch, max(1, SYMBOL_DAYS_PER_REQUEST // (end - first).days))
        for offset in range(0, len(source.symbols), group_size):
            group = source.symbols[offset : offset + group_size]
            events = self._group(group, first, end, context)
            # The last contributing response bounds what was knowable about this entire interval.
            observed = self.observed_at
            revision = self._revision(observed)
            for entry in group:
                context.check_cancelled()
                dates = events.get(entry.symbol)
                if dates is None:
                    self.missing.add(entry.instrument_id)
                    continue
                values = factor_values(
                    sorted(dates.items()),
                    representation=source.representation,
                    start=start,
                    baseline_date=source.baseline_date,
                )
                for day, value in values:
                    context.check_cancelled()
                    yield AdjustmentFactor(
                        factor_id=source.representation + ":" + day.isoformat(),
                        instrument_id=entry.instrument_id,
                        effective_at=datetime.combine(day, time.min, tzinfo=CN),
                        available_at=observed,
                        value=value,
                        definition_id=self.definition,
                        factor_kind=source.representation,
                        baseline_id=self.baseline,
                        source_id=source.expected_provider,
                        revision_id=revision,
                    )

    def _revision(self, observed):
        return "observation-" + observed.strftime("%Y%m%dT%H%M%S%fZ")

    def _coverage(self):
        issues = [
            CoverageIssue(
                instrument_id=key,
                reason="missing",
                detail="Source omitted this symbol in a requested factor interval",
            )
            for key in sorted(self.missing)
        ]
        requested = len(self.source.symbols)
        completed = requested - len(self.missing)
        if completed and not self.count:
            issues.append(
                CoverageIssue(
                    reason="no_events",
                    detail="Confirmed factor histories contained no ex-rights events",
                )
            )
        return Coverage(
            status="empty" if not self.count else ("partial" if self.missing else "complete"),
            requested_units=requested,
            completed_units=completed,
            row_count=self.count,
            issues=tuple(issues),
        )
