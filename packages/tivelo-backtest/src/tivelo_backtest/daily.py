"""Exact daily reference runner over immutable, point-in-time reader protocols.

This is the correctness path. Readers may optimize queries but must preserve all
as-of revision semantics. Trusted Python strategy bindings are not a sandbox;
artifact/package authentication belongs to the loader that creates the RunLock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Context, Decimal, localcontext

from pydantic import ValidationError
from tivelo_contracts.base import fingerprint
from tivelo_contracts.data import (
    Bar,
    BarQuery,
    CorporateAction,
    DatasetVersion,
    EventQuery,
    HistoryQuery,
    Instrument,
    OpeningObservation,
    ProjectedBar,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    BacktestResult,
    DefinitionReference,
    ExecutionDiagnostic,
    RunLock,
    SignalBatch,
    StrategyContext,
)
from tivelo_contracts.extension_fields import resolve_field
from tivelo_contracts.market import ReferencePrice, TradingDay
from tivelo_contracts.protocols import (
    DailyExecutionRules,
    ExecutionDataReader,
    ExtensionFieldReader,
    Observer,
    PriceViewProvider,
    Strategy,
)

from .account import CashEquityAccount, CashEquityPolicy
from .controlled_fields import extension_observations
from .order_rules import bind_order_rules

VERIFIER_VERSION = "tivelo.strict.daily.v1"
_MICROSECOND = timedelta(microseconds=1)
_QUANTITY_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class StrategyBinding:
    """A loader-resolved definition and dependency closure, not strategy self-attestation."""

    implementation: Strategy
    reference: DefinitionReference
    dependencies: tuple[DefinitionReference, ...] = ()


def _fail(code: ErrorCode, message: str):
    raise TiveloError(code, message)


def _check_cancelled(observer: Observer | None):
    if observer is not None and observer.cancelled():
        _fail(ErrorCode.CANCELLED, "Daily backtest cancelled; no successful result was produced")


class DailyBacktest:
    def __init__(self, *, rules: DailyExecutionRules, price_view: PriceViewProvider):
        self.rules, self.price_view = rules, price_view

    def preflight(
        self,
        lock: RunLock,
        reader: ExecutionDataReader,
        *,
        strategy: DefinitionReference,
        dependencies: tuple[DefinitionReference, ...] = (),
    ) -> tuple[TradingDay, ...]:
        """Validate inputs without constructing or executing a strategy instance."""
        lock = RunLock.model_validate_json(lock.model_dump_json())
        _, _, active_days, _ = self._prepare(lock, reader, strategy, dependencies)
        return active_days

    def _prepare(self, lock, reader, strategy_reference, dependencies):
        spec = lock.contents.spec
        if lock.contents.scale_proof is not None:
            from .controlled import check_controlled_spec

            check_controlled_spec(spec, lock.contents.scale_proof)
        if (
            spec.semantics_version != "strict.daily.v1"
            or spec.accounting_model != "event_ledger"
            or lock.contents.verifier_version != VERIFIER_VERSION
        ):
            _fail(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Daily runner requires its strict event-ledger semantics",
            )
        if (
            spec.execution.end_policy != "mark_to_market"
            or spec.execution.position_policy_failure != "reject"
            or spec.execution.slippage_bps >= 10000
        ):
            _fail(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Requested execution policy is not implemented by this runner",
            )
        if not isinstance(reader, ExecutionDataReader) or not isinstance(
            self.rules, DailyExecutionRules
        ):
            _fail(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Reader and rules must implement strict daily execution protocols",
            )
        if self.rules.buy_cost_monotone is not True:
            _fail(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Default sizing requires a monotone total buy cost",
            )
        if reader.manifest != lock.contents.snapshot:
            _fail(ErrorCode.SNAPSHOT_CORRUPT, "Reader does not provide the locked snapshot")
        versions = tuple(
            DatasetVersion.model_validate_json(d.model_dump_json()) for d in reader.datasets
        )
        identity = {d.contents.dataset_id: d.version_id for d in versions}
        if len(identity) != len(versions) or identity != {
            d.dataset_id: d.version_id for d in lock.contents.snapshot.contents.datasets
        }:
            _fail(ErrorCode.SNAPSHOT_CORRUPT, "Reader datasets do not match the locked manifest")
        if self.rules.reference != spec.market_profile:
            _fail(ErrorCode.DATA_CONFLICT, "Market rules/calendar differ from the locked profile")
        if strategy_reference != spec.strategy or dependencies != spec.dependencies:
            _fail(
                ErrorCode.DATA_CONFLICT, "Loaded strategy or dependencies differ from the RunLock"
            )
        if self.price_view.provider_id != spec.price_view.provider_id:
            _fail(ErrorCode.PRICE_BASIS_UNPROVEN, "Price provider differs from the locked view")
        if spec.price_view.mode == "extension":
            pin = next(p for p in lock.contents.extensions if p.kind == "price_view")
            if (
                getattr(self.price_view, "extension_reference", None) != pin
                or getattr(self.price_view, "run_lock_id", None) != lock.lock_id
            ):
                _fail(ErrorCode.DATA_CONFLICT, "Loaded price extension differs from its run lock")
        policy = CashEquityPolicy.model_validate_json(spec.account_profile.parameters_json)
        account = CashEquityAccount(
            policy.currency,
            spec.initial_capital,
            order_expiry=policy.order_expiry,
            dividend_treatment=policy.dividend_treatment,
        )
        if (
            account.reference != spec.account_profile
            or account.currency != spec.base_currency
            or policy.order_expiry != spec.execution.order_expiry
        ):
            _fail(
                ErrorCode.DATA_CONFLICT, "Account policy differs from the locked execution request"
            )
        days = tuple(
            TradingDay.model_validate_json(day.model_dump_json())
            for day in self.rules.trading_days(
                spec.warmup_start or spec.start,
                spec.end,
                as_of=datetime.combine(spec.end, time.max, UTC),
            )
        )
        if not days or not any(day.trading_date >= spec.start for day in days):
            _fail(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Requested range contains no declared trading days",
            )
        if [d.trading_date for d in days] != sorted({d.trading_date for d in days}) or any(
            not (spec.warmup_start or spec.start) <= d.trading_date <= spec.end for d in days
        ):
            _fail(
                ErrorCode.CONTRACT_INVALID,
                "Market returned duplicate, unordered or out-of-range trading days",
            )
        delay = timedelta(seconds=spec.execution.decision_delay_seconds)
        for index, day in enumerate(days):
            # Raw-bar dates define the replay axis. Actual inputs still use their event cutoffs.
            if not getattr(self.rules, "observed_bar_clock", False) and day.available_at > day.open_at:
                _fail(
                    ErrorCode.FUTURE_INFORMATION,
                    "Trading calendar was unknown at its execution event",
                )
            if index and days[index - 1].close_at + delay >= day.open_at:
                _fail(
                    ErrorCode.CONTRACT_INVALID,
                    "Delayed decision overlaps a subsequent trading open",
                )
        active_days = tuple(d for d in days if d.trading_date >= spec.start)
        proof = lock.contents.scale_proof
        operands = {item.dataset_id: item for item in proof.field_operands} if proof else {}
        first_field_day = days[0] if operands else active_days[0]
        research = []
        for reference in spec.extension_fields:
            if not isinstance(reader, ExtensionFieldReader):
                _fail(ErrorCode.UNSUPPORTED_CAPABILITY, "Reader has no extension field capability")
            dataset = resolve_field(versions, reference)
            scope = dataset.contents.scope
            operand = operands.get(reference.dataset_id)
            if operand is not None and operand.value_type != scope.extension_field.value_type:
                _fail(
                    ErrorCode.DATA_CONFLICT,
                    "Controlled field type differs from the snapshot descriptor",
                )
            subjects = () if scope.extension_field.granularity == "market" else spec.universe
            if (
                dataset.contents.knowledge_basis != "historical_versions"
                or dataset.contents.coverage.status != "complete"
                or scope.market_id != self.rules.market_id
                or scope.asset_class not in ("equity", "fund")
                or not set(subjects).issubset(scope.instrument_ids)
                or scope.start > first_field_day.close_at + delay
                or scope.end <= active_days[-1].close_at + delay
            ):
                _fail(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "Extension field lacks historical coverage for this run",
                )
            research.append((reference, dataset, subjects))
        required = {"bar", "instrument", "corporate_action", "opening_observation"}
        if spec.price_view.mode != "raw":
            required.add("adjustment_factor")
        for kind in sorted(required):
            candidates = tuple(
                d.contents
                for d in versions
                if d.contents.record_type == kind
                and (kind != "bar" or d.contents.price_basis == "raw")
            )
            if not candidates or any(
                d.knowledge_basis != "historical_versions" for d in candidates
            ):
                _fail(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "Required facts lack historical-version evidence",
                )
            for day in days if kind in ("bar", "instrument", "adjustment_factor") else active_days:
                for key in spec.universe:
                    covered = False
                    for data in candidates:
                        scope, coverage = data.scope, data.coverage
                        complete = coverage.status == "complete" or (
                            kind == "corporate_action"
                            and coverage.status == "empty"
                            and coverage.completed_units == coverage.requested_units
                            and all(issue.reason == "no_events" for issue in coverage.issues)
                        )
                        if not complete or scope is None:
                            continue
                        if (
                            scope.market_id == self.rules.market_id
                            and scope.asset_class in ("equity", "fund")
                            and key in scope.instrument_ids
                            and scope.start <= day.open_at
                            and scope.end > day.close_at
                            and (
                                kind != "bar"
                                or (
                                    "1d" in scope.timeframes
                                    and scope.bar_convention_id == day.bar_convention_id
                                )
                            )
                        ):
                            covered = True
                            break
                    if not covered:
                        _fail(
                            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                            "Snapshot does not cover a required market/date/instrument cell",
                        )
        return account, days, active_days, research

    @staticmethod
    def _instruments(reader, spec, at):
        records = tuple(
            Instrument.model_validate(r.model_dump())
            for r in reader.instruments(
                HistoryQuery(instrument_ids=spec.universe, effective_at=at, as_of=at)
            )
        )
        result = {record.instrument_id: record for record in records}
        if len(result) != len(records) or not result.keys() <= set(spec.universe):
            _fail(
                ErrorCode.DATA_CONFLICT, "Reader returned duplicate or unrelated instrument states"
            )
        if any(
            r.available_at > at
            or r.valid_from > at
            or (r.valid_to is not None and at >= r.valid_to)
            for r in records
        ):
            _fail(ErrorCode.FUTURE_INFORMATION, "Instrument query returned an unavailable state")
        return result

    @staticmethod
    def _openings(reader, spec, day, as_of):
        observations = tuple(
            OpeningObservation.model_validate(o.model_dump())
            for o in reader.openings(
                EventQuery(
                    instrument_ids=spec.universe,
                    start=day.open_at,
                    end=day.open_at + _MICROSECOND,
                    as_of=as_of,
                )
            )
        )
        result = {o.instrument_id: o for o in observations}
        if len(result) != len(observations) or result.keys() != set(spec.universe):
            _fail(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Every universe member needs an explicit opening price or nontrading state",
            )
        if any(
            o.event_at != day.open_at
            or o.trading_date != day.trading_date
            or o.available_at > as_of
            or not o.is_final
            or o.quote_currency != spec.base_currency
            for o in observations
        ):
            _fail(
                ErrorCode.FUTURE_INFORMATION,
                "Opening observation has incompatible timing, currency or state",
            )
        return result

    def _orders(self, batch, context, raw_prices, equity, account, eligible_at):
        positions = {p.instrument_id: p for p in context.portfolio.positions}
        instruments = {i.instrument_id: i for i in context.instruments}
        diagnostics = []
        with localcontext(Context(prec=96)):
            for signal in sorted(
                batch.signals,
                key=lambda s: (s.side != "sell", s.score.copy_negate(), s.instrument_id),
            ):
                if getattr(signal, "limit_quantity", None) is not None:
                    _fail(
                        ErrorCode.UNSUPPORTED_CAPABILITY,
                        "Daily sizing consumes target weights instead of explicit quantities",
                    )
                key = signal.instrument_id
                security = instruments.get(key)
                if security is None or key not in raw_prices or raw_prices[key] <= 0:
                    _fail(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Signal sizing needs an active instrument and a known raw valuation",
                    )
                price = raw_prices[key]
                sizing = bind_order_rules(
                    self.rules,
                    security,
                    signal.order_type,
                    at=context.now,
                )
                if sizing is None:
                    diagnostics.append(
                        ExecutionDiagnostic(
                            code="unsupported_order_type", instrument_id=key, at=context.now
                        )
                    )
                    continue
                held = positions[key].quantity if key in positions else Decimal(0)
                term = sizing.terms
                if signal.side == "sell":
                    target = (signal.target_weight or Decimal(0)) * equity / price
                    # Internal division can recur. Every declared lot grid fits decimal12;
                    # flooring here cannot remove any legal quantity within the budget.
                    requested = min(term.sell_maximum, max(Decimal(0), held - target)).quantize(
                        _QUANTITY_QUANTUM, rounding=ROUND_DOWN
                    )
                    quantity = sizing.quantity(
                        "sell",
                        requested,
                        available=held,
                    )
                else:
                    if signal.target_weight is None:
                        _fail(
                            ErrorCode.CONTRACT_INVALID,
                            "Strict daily buy signals must declare their target weight",
                        )
                    budget = signal.target_weight * equity - held * price
                    requested = min(term.buy_maximum, max(Decimal(0), budget / price)).quantize(
                        _QUANTITY_QUANTUM, rounding=ROUND_DOWN
                    )
                    maximum = sizing.quantity(
                        "buy",
                        requested,
                    )
                    quantity = Decimal(0)
                    if maximum >= term.buy_minimum:
                        left, right = (
                            0,
                            int(
                                (
                                    (maximum - term.buy_minimum) / term.buy_increment
                                ).to_integral_value(rounding=ROUND_DOWN)
                            ),
                        )
                        while left <= right:
                            middle = (left + right) // 2
                            candidate = term.buy_minimum + middle * term.buy_increment
                            fees = self.rules.fee_breakdown(
                                security, "buy", candidate, price, at=context.now
                            )
                            if (
                                fees.currency != account.currency
                                or fees.notional != candidate * price
                            ):
                                _fail(
                                    ErrorCode.CONTRACT_INVALID,
                                    "Sizing fee quote has inconsistent currency or notional",
                                )
                            if candidate * price + fees.total <= budget:
                                quantity, left = candidate, middle + 1
                            else:
                                right = middle - 1
                if quantity > 0:
                    account.submit(
                        signal, quantity, eligible_at=eligible_at, price_view=self.price_view
                    )
                else:
                    diagnostics.append(
                        ExecutionDiagnostic(
                            code="no_order_quantity", instrument_id=key, at=context.now
                        )
                    )
        return diagnostics

    def run(
        self,
        lock: RunLock,
        reader: ExecutionDataReader,
        strategy: StrategyBinding,
        *,
        observer: Observer | None = None,
    ) -> BacktestResult:
        lock = RunLock.model_validate_json(lock.model_dump_json())
        _check_cancelled(observer)
        account, days, active_days, research = self._prepare(
            lock, reader, strategy.reference, strategy.dependencies
        )
        spec = lock.contents.spec
        from .controlled import validate_controlled
        from .controlled_fields import FormulaFieldHistory

        controlled = validate_controlled(strategy, lock)
        field_history = None
        if controlled is not None and research:
            field_history = FormulaFieldHistory(controlled.program.field_operands)
            delay = timedelta(seconds=spec.execution.decision_delay_seconds)
            for day in days:
                if day.trading_date >= spec.start:
                    break
                _check_cancelled(observer)
                values = extension_observations(reader, research, day.close_at + delay)
                field_history.observe(day.trading_date, values)
        clock = _ActionClock(
            account,
            reader,
            spec.universe,
            active_days[0].open_at,
            active_days[-1].close_at + timedelta(seconds=spec.execution.decision_delay_seconds),
        )
        by_date = {day.trading_date: day for day in days}
        last_prices, diagnostics, equity = {}, [], []
        last_event = active_days[0].open_at
        observed_bar_clock = getattr(self.rules, "observed_bar_clock", False)
        # A market adapter can match native security calendars to a shared daily clock.
        matches_calendar = getattr(self.rules, "matches_calendar", None)
        for index, day in enumerate(active_days):
            _check_cancelled(observer)
            # Actions are queried at this phase's information cutoff, then applied
            # at their real event times before any subsequent open or decision.
            changed = clock.advance(day.open_at)
            for key in changed:
                last_prices.pop(key, None)
            securities = self._instruments(reader, spec, day.open_at)
            observations = self._openings(reader, spec, day, day.open_at)
            prices, references = {}, {}
            for key, observation in observations.items():
                security = securities.get(key)
                if observation.state == "tradable":
                    if (
                        security is None
                        or not security.tradable
                        or not self.rules.supports(security)
                        or not (
                            matches_calendar(security, day)
                            if callable(matches_calendar)
                            else security.calendar_id == day.calendar_id
                        )
                        or security.quote_currency != spec.base_currency
                        or security.settlement_currency != spec.base_currency
                    ):
                        _fail(
                            ErrorCode.UNSUPPORTED_ASSET,
                            "Tradable opening has no compatible historical security state",
                        )
                    prices[key] = observation.raw_open
                else:
                    diagnostics.append(
                        ExecutionDiagnostic(
                            code=f"opening_{observation.state}", instrument_id=key, at=day.open_at
                        )
                    )
                if observation.reference_price is not None:
                    references[key] = ReferencePrice(
                        instrument_id=key,
                        trading_date=day.trading_date,
                        value=observation.reference_price,
                        kind=observation.reference_kind,
                        available_at=observation.available_at,
                        source_id=observation.source_id,
                        revision_id=observation.revision_id,
                    )
            account.match_open(
                at=day.open_at,
                instruments=securities,
                raw_prices=prices,
                references=references,
                rules=self.rules,
                slippage_bps=spec.execution.slippage_bps,
            )
            last_event = day.open_at
            _check_cancelled(observer)
            now = day.close_at + timedelta(seconds=spec.execution.decision_delay_seconds)
            changed = clock.advance(now)
            for key in changed:
                last_prices.pop(key, None)
            securities = self._instruments(reader, spec, now)
            history = tuple(
                Bar.model_validate(b.model_dump())
                for b in reader.bars(
                    BarQuery(
                        instrument_ids=spec.universe,
                        timeframe="1d",
                        start=days[0].open_at,
                        end=now + _MICROSECOND,
                        as_of=now,
                        price_basis="raw",
                    )
                )
            )
            identities = {(b.instrument_id, b.bar_start) for b in history}
            if len(identities) != len(history):
                _fail(ErrorCode.DATA_CONFLICT, "Daily history contains duplicate fact identities")
            current = {}
            for bar in history:
                trading_day = by_date.get(bar.trading_date)
                if (
                    bar.instrument_id not in spec.universe
                    or bar.timeframe != "1d"
                    or bar.price_basis != "raw"
                    or trading_day is None
                    or bar.bar_start != trading_day.open_at
                    or bar.bar_end != trading_day.close_at
                    or bar.available_at > now
                    or bar.bar_end > now
                    or not bar.is_final
                    or bar.quote_currency != spec.base_currency
                    or bar.quantity_unit != "share"
                ):
                    _fail(
                        ErrorCode.FUTURE_INFORMATION,
                        "Daily history violates its locked market/time/price contract",
                    )
                if bar.trading_date == day.trading_date:
                    current[bar.instrument_id] = bar.close
            if any(
                key not in current
                for key, value in observations.items()
                if value.state == "tradable"
            ):
                _fail(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "An opening trade has no complete, available closing bar",
                )
            held = {p.instrument_id for p in account.snapshot(now).positions}
            if not held <= securities.keys():
                _fail(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "A held instrument lost its historical state; no delisting settlement policy exists",
                )
            if any(
                not self.rules.supports(securities[key])
                or securities[key].quote_currency != account.currency
                or securities[key].settlement_currency != account.currency
                for key in held
            ):
                _fail(
                    ErrorCode.UNSUPPORTED_ASSET,
                    "Held instrument changed to an unsupported account specification",
                )
            # Read the latest known historical revision, rather than carrying a
            # cached value past a correction or a change in corporate-action units.
            last_prices = {}
            for bar in sorted(history, key=lambda b: (b.bar_end, b.instrument_id)):
                cutoff = clock.latest_basis_at.get(bar.instrument_id)
                if cutoff is None or bar.bar_end >= cutoff:
                    last_prices[bar.instrument_id] = bar.close
            for key in held - current.keys():
                if spec.execution.missing_data != "hold_last_valuation" or key not in last_prices:
                    _fail(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Held instrument has no permitted close valuation",
                    )
                diagnostics.append(
                    ExecutionDiagnostic(code="stale_valuation", instrument_id=key, at=now)
                )
            last_prices.update(current)
            point = account.mark_to_market(now, last_prices)
            equity.append(point)
            factors = (
                reader.factors(
                    HistoryQuery(instrument_ids=spec.universe, effective_at=now, as_of=now)
                )
                if spec.price_view.mode != "raw"
                else ()
            )
            history = tuple(sorted(history, key=lambda b: (b.bar_start, b.instrument_id)))
            view_type = ProjectedBar if spec.price_view.mode == "extension" else Bar
            try:
                projected = (
                    history
                    if controlled is not None and spec.price_view.mode != "extension"
                    else tuple(
                        view_type.model_validate(b.model_dump())
                        for b in self.price_view.project(
                            history, factors, spec.price_view, decision_at=now
                        )
                    )
                )
            except (AttributeError, ValidationError) as exc:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Invalid bar returned by price provider"
                ) from exc
            if {(b.instrument_id, b.bar_start) for b in projected} != identities or len(
                projected
            ) != len(history):
                _fail(
                    ErrorCode.CONTRACT_INVALID,
                    "Price view changed the visible history's identities",
                )
            raw_by_key = {(b.instrument_id, b.bar_start): b for b in history}
            immutable = (
                "bar_end",
                "trading_date",
                "timeframe",
                "session_id",
                "quote_currency",
                "quantity_unit",
                "volume",
                "turnover",
                "trade_count",
                "is_final",
            )
            for bar in projected:
                raw = raw_by_key[bar.instrument_id, bar.bar_start]
                if any(getattr(bar, field) != getattr(raw, field) for field in immutable):
                    _fail(ErrorCode.CONTRACT_INVALID, "Price view changed non-price facts")
                if (
                    bar.available_at < raw.available_at
                    or bar.ingested_at < raw.ingested_at
                    or (
                        spec.price_view.mode == "extension"
                        and bar.source_id != self.price_view.provider_id
                    )
                    or (
                        spec.price_view.mode in ("raw", "qfq", "hfq")
                        and controlled is None
                        and bar.price_basis != spec.price_view.mode
                    )
                ):
                    _fail(
                        ErrorCode.CONTRACT_INVALID,
                        "Price view changed its declared knowledge time or basis",
                    )
                if spec.price_view.mode == "raw" and any(
                    getattr(bar, field) != getattr(raw, field)
                    for field in ("open", "high", "low", "close", "vwap")
                ):
                    _fail(ErrorCode.CONTRACT_INVALID, "Raw price provider changed market prices")
            latest_openings = self._openings(reader, spec, day, now)
            field_values = extension_observations(reader, research, now)
            if field_history is not None:
                field_history.observe(day.trading_date, field_values)
            context = StrategyContext(
                now=now,
                history=projected,
                instruments=tuple(securities[key] for key in sorted(securities)),
                portfolio=account.snapshot(now),
                parameters_json=spec.strategy.parameters_json,
                openings=tuple(latest_openings[key] for key in sorted(latest_openings)),
                extension_values=field_values,
            )
            _check_cancelled(observer)
            try:
                emitted = (
                    controlled.decide_with_factors(
                        context,
                        factors=factors,
                        price_view=spec.price_view,
                        field_history=field_history,
                    )
                    if controlled is not None
                    else strategy.implementation.decide(context)
                )
                batch = SignalBatch.model_validate(emitted.model_dump())
            except TiveloError:
                raise
            except Exception as exc:
                raise TiveloError(
                    ErrorCode.STRATEGY_FAILED, "Strategy failed to produce a valid signal batch"
                ) from exc
            if batch.decision_at != now or any(
                signal.instrument_id not in spec.universe for signal in batch.signals
            ):
                _fail(
                    ErrorCode.FUTURE_INFORMATION,
                    "Strategy returned a signal for another decision or universe",
                )
            _check_cancelled(observer)
            if batch.signals and observed_bar_clock and index + 1 == len(active_days):
                diagnostics.append(ExecutionDiagnostic(code="no_following_bar", at=now))
            elif batch.signals:
                eligible = self.rules.next_open(day.trading_date, as_of=now)
                if eligible <= now:
                    _fail(
                        ErrorCode.FUTURE_INFORMATION,
                        "Next execution opening does not follow the decision",
                    )
                diagnostics.extend(
                    self._orders(batch, context, last_prices, point.equity, account, eligible)
                )
            last_event = now
            if observer is not None:
                observer.progress(index + 1, len(active_days), "backtest")
        _check_cancelled(observer)
        if reader.manifest != lock.contents.snapshot or self.rules.reference != spec.market_profile:
            _fail(ErrorCode.DATA_CONFLICT, "An immutable run input changed during execution")
        account.finish(last_event)
        return BacktestResult(
            lock_id=lock.lock_id,
            semantics_version=spec.semantics_version,
            accounting_model=account.accounting_model,
            orders=account.orders,
            ledger=account.ledger,
            equity=tuple(equity),
            final_portfolio=account.snapshot(last_event),
            diagnostics=tuple(
                dict.fromkeys((*account.diagnostics, *(event.code for event in diagnostics)))
            ),
            diagnostic_events=tuple(diagnostics),
        )


class _ActionClock:
    def __init__(self, account, reader, universe, start, end):
        self.account, self.reader, self.universe = account, reader, universe
        self.start, self.end = start, end
        self._handled = {}
        self._steps = set()
        self.latest_basis_at = {}

    def advance(self, now):
        records = tuple(
            CorporateAction.model_validate(a.model_dump())
            for a in self.reader.actions(
                HistoryQuery(instrument_ids=self.universe, effective_at=self.end, as_of=now)
            )
        )
        current = {}
        for action in records:
            if (
                action.instrument_id not in self.universe
                or action.available_at > now
                or action.cancelled
                or action.effective_at > self.end
            ):
                _fail(ErrorCode.FUTURE_INFORMATION, "Action reader returned an unavailable event")
            if action.effective_at < self.start:
                continue  # A new cash-only account has no pre-start ownership/receivables.
            identity = action.instrument_id, action.action_id
            if identity in current:
                _fail(ErrorCode.DATA_CONFLICT, "Corporate action identity is ambiguous")
            current[identity] = action
        if any(
            key not in current or fingerprint(current[key]) != digest
            for key, digest in self._handled.items()
        ):
            _fail(
                ErrorCode.DATA_CONFLICT,
                "A recorded or applied corporate action was removed or revised",
            )
        events = []
        for key, action in current.items():
            for phase, at in (
                (0, action.record_at),
                (1, action.effective_at),
                (2, action.payment_at),
            ):
                if at is not None and at <= now and (key, phase) not in self._steps:
                    events.append((at, phase, action.instrument_id, key, action))
        changed = set()
        for at, phase, _, key, action in sorted(events, key=lambda item: item[:4]):
            if phase == 0:
                self.account.capture_entitlement(action, at=at)
            elif phase == 1:
                self.account.apply_action(action, at=at)
                changed.add(action.instrument_id)
                self.latest_basis_at[action.instrument_id] = at
            elif action.action_type == "cash_dividend":
                self.account.pay_dividend(action, at=at)
            self._steps.add((key, phase))
            self._handled[key] = fingerprint(action)
        return changed
