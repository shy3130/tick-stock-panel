"""Run a locked strategy on observed continuous-auction events."""

from collections import defaultdict, deque
from dataclasses import dataclass

from tivelo_contracts.data import Bar, BarQuery, DatasetReference, HistoryQuery, Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    BacktestResult,
    RunLock,
    SignalBatch,
    StrategyContext,
    TradeSessionIndex,
)
from tivelo_contracts.market import ReferencePrice, TradingDay
from tivelo_contracts.trade_prints import TradePrintQuery

from .account import CashEquityAccount, CashEquityPolicy
from .continuous import ContinuousLimitAdmission, ContinuousTradeReplay
from .daily import DailyBacktest, _ActionClock

VERIFIER_VERSION = "tivelo.strict.continuous.v1"


@dataclass(frozen=True)
class _Session:
    day: TradingDay
    instrument: Instrument
    reference: ReferencePrice
    index: TradeSessionIndex

    def replay(self, admission):
        query = TradePrintQuery(
            dataset=admission._pin[3],
            instrument_ids=(self.instrument.instrument_id,),
            trading_date=self.day.trading_date,
            start=self.day.open_at,
            end=self.day.close_at,
            as_of=self.day.open_at,
        )
        return ContinuousTradeReplay(
            admission,
            query,
            sequences=tuple(map(int, self.index.sequences)),
            instrument=self.instrument,
            reference=self.reference,
        )


def _require(condition, code, message):
    if not condition:
        raise TiveloError(code, message)


def _cancel(observer):
    if observer is not None and observer.cancelled():
        raise TiveloError(ErrorCode.CANCELLED, "Continuous backtest cancelled")


class ContinuousBacktest:
    def __init__(self, *, rules, price_view):
        self.market, self.price_view = rules, price_view
        self.rules = rules.execution_rules

    def preflight(self, lock, reader, *, strategy, dependencies=()):
        self._prepare(
            RunLock.model_validate_json(lock.model_dump_json()), reader, strategy, dependencies
        )

    def _prepare(self, lock, reader, strategy, dependencies):
        contents = lock.contents
        spec = contents.spec
        model = spec.execution
        plan = model.continuous
        _require(
            spec.semantics_version == "strict.continuous.v1"
            and contents.verifier_version == VERIFIER_VERSION
            and contents.scale_proof is None
            and plan is not None
            and spec.price_view.mode in ("raw", "qfq", "hfq")
            and model.end_policy == "mark_to_market"
            and model.missing_data == "reject"
            and model.position_policy_failure == "reject",
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Continuous replay requires fixed price views and witnessed limit-order execution",
        )
        _require(
            contents.snapshot == reader.manifest
            and self.market.reference == spec.market_profile
            and self.price_view.provider_id == spec.price_view.provider_id
            and (strategy, dependencies) == (spec.strategy, spec.dependencies),
            ErrorCode.DATA_CONFLICT,
            "Continuous run bindings differ from the lock",
        )
        versions = {version.contents.dataset_id: version for version in reader.datasets}
        _require(
            len(versions) == len(reader.datasets)
            and {k: v.version_id for k, v in versions.items()}
            == {v.dataset_id: v.version_id for v in reader.manifest.contents.datasets},
            ErrorCode.SNAPSHOT_CORRUPT,
            "Reader does not contain the locked dataset versions",
        )
        deadline = max(index.confirmed_through_at for index in plan.sessions)
        complete_days = self.rules.trading_days(
            spec.warmup_start or spec.start, spec.end, as_of=deadline
        )
        days = [day for day in complete_days if day.trading_date >= spec.start]
        indexes = {(s.trading_date, s.instrument_id): s for s in plan.sessions}
        _require(
            days
            and set(indexes) == {(day.trading_date, key) for day in days for key in spec.universe}
            and all(
                indexes[day.trading_date, key].confirmed_through_at >= day.close_at
                for day in days
                for key in spec.universe
            ),
            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
            "Each trading day and instrument needs a confirmed full-session index",
        )
        _require(
            all(days[0].open_at <= t <= deadline for t in plan.observe_at)
            and all(any(d.open_at < t < d.close_at for d in days) for t in plan.decide_at),
            ErrorCode.CONTRACT_INVALID,
            "Continuous clock is outside the locked trading days",
        )
        for at in plan.decide_at:
            self.market.continuous_session_id(at)
            _require(
                spec.price_view.anchor_at is None or spec.price_view.anchor_at <= at,
                ErrorCode.PRICE_BASIS_UNPROVEN,
                "Price anchor follows a strategy decision",
            )

        def dataset(dataset_id, kind):
            value = versions.get(dataset_id)
            _require(
                value is not None
                and value.contents.record_type == kind
                and value.contents.knowledge_basis == "historical_versions"
                and value.contents.coverage.status == "complete",
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                f"Continuous run lacks historical coverage for {kind}",
            )
            return DatasetReference(dataset_id=dataset_id, version_id=value.version_id)

        books = dataset(plan.book_dataset_id, "order_book_observation")
        trades = dataset(plan.trade_dataset_id, "trade_print")
        policy = CashEquityPolicy.model_validate_json(spec.account_profile.parameters_json)
        account = CashEquityAccount(
            policy.currency,
            spec.initial_capital,
            order_expiry=policy.order_expiry,
            dividend_treatment=policy.dividend_treatment,
        )
        _require(
            (account.reference, account.currency, account.order_expiry)
            == (spec.account_profile, spec.base_currency, model.order_expiry),
            ErrorCode.DATA_CONFLICT,
            "Continuous account differs from its lock",
        )
        admission = ContinuousLimitAdmission(
            account, self.market, reader=reader, dataset=books, trade_dataset=trades
        )
        self._history_coverage(lock, versions.values(), complete_days, days, deadline)
        sessions = defaultdict(deque)
        for day in days:
            instruments = DailyBacktest._instruments(reader, spec, day.open_at)
            openings = DailyBacktest._openings(reader, spec, day, day.open_at)
            for key in sorted(spec.universe):
                instrument, opening = instruments.get(key), openings[key]
                _require(
                    instrument is not None
                    and instrument.tradable
                    and instrument.calendar_id == day.calendar_id
                    and (instrument.valid_to is None or instrument.valid_to >= day.close_at)
                    and self.rules.supports(instrument)
                    and opening.state == "tradable"
                    and opening.reference_price is not None,
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "Continuous session needs an active instrument and an exchange reference",
                )
                reference = ReferencePrice(
                    instrument_id=key,
                    trading_date=day.trading_date,
                    value=opening.reference_price,
                    kind=opening.reference_kind,
                    available_at=opening.available_at,
                    source_id=opening.source_id,
                    revision_id=opening.revision_id,
                )
                index = indexes[day.trading_date, key]
                session = _Session(day, instrument, reference, index)
                session.replay(admission)  # Validate the pinned scope before loading strategy code.
                sessions[key].append(session)
        return account, admission, sessions, complete_days[0].open_at, days, deadline

    def _history_coverage(self, lock, versions, complete_days, active_days, deadline):
        spec = lock.contents.spec
        required = ["bar", "instrument", "opening_observation", "corporate_action"]
        if spec.price_view.mode != "raw":
            required.append("adjustment_factor")
        for kind in required:
            candidates = [
                v.contents
                for v in versions
                if v.contents.record_type == kind
                and (kind != "bar" or v.contents.price_basis == "raw")
            ]
            _require(
                candidates and all(c.knowledge_basis == "historical_versions" for c in candidates),
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                f"Continuous run needs a historical {kind} dataset",
            )
            for day in (
                active_days
                if kind in ("opening_observation", "corporate_action")
                else complete_days
            ):
                for key in spec.universe:
                    end = deadline if kind == "corporate_action" else day.close_at
                    _require(
                        any(
                            self._covers(c, kind, key, day.open_at, end, spec.execution.timeframe)
                            for c in candidates
                        ),
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        f"Continuous run has an uncovered {kind} input",
                    )

    def _covers(self, dataset, kind, key, start, end, timeframe):
        scope, coverage = dataset.scope, dataset.coverage
        return (
            scope is not None
            and scope.market_id == self.rules.market_id
            and scope.asset_class == "equity"
            and key in scope.instrument_ids
            and scope.start <= start
            and (scope.end > end if kind == "corporate_action" else scope.end >= end)
            and (kind != "bar" or timeframe in scope.timeframes)
            and (
                coverage.status == "complete"
                or (
                    kind == "corporate_action"
                    and coverage.status == "empty"
                    and coverage.completed_units == coverage.requested_units
                    and all(issue.reason == "no_events" for issue in coverage.issues)
                )
            )
        )

    def run(self, lock, reader, strategy, *, observer=None):
        lock = RunLock.model_validate_json(lock.model_dump_json())
        _cancel(observer)
        account, admission, sessions, start, days, end = self._prepare(
            lock, reader, strategy.reference, strategy.dependencies
        )
        spec = lock.contents.spec
        plan = spec.execution.continuous
        deciding = set(plan.decide_at)
        closing_marks = {day.close_at: day.close_at for day in days}
        closing_marks[end] = days[-1].close_at
        marks = deciding | closing_marks.keys()
        timeline = sorted(
            marks | set(plan.observe_at) | {s.confirmed_through_at for s in plan.sessions}
        )
        actions = _ActionClock(account, reader, spec.universe, days[0].open_at, end)
        equity, replays, orders = [], {}, defaultdict(list)
        day_by_time = {
            now: next(day.trading_date for day in days if day.open_at < now < day.close_at)
            for now in deciding
        }
        sessions_by_day = {
            (session.day.trading_date, key): session
            for key, values in sessions.items()
            for session in values
        }
        for step, now in enumerate(timeline, 1):
            _cancel(observer)
            actions.advance(now)
            for key, pending in sessions.items():
                while pending and pending[0].day.open_at <= now:
                    item = pending[0]
                    replay = replays.get(key)
                    if replay is None:
                        replay = replays[key] = item.replay(admission)
                    finished = now >= item.index.confirmed_through_at
                    replay.advance(at=now, require_complete=finished)
                    if not finished:
                        break
                    for order in orders.pop((item.day.trading_date, key), ()):
                        account.confirm_expiry(order, at=now)
                    pending.popleft()
                    del replays[key]
            if now in marks:
                query = BarQuery(
                    instrument_ids=spec.universe,
                    timeframe=spec.execution.timeframe,
                    start=start,
                    end=min(now, days[-1].close_at),
                    as_of=now,
                    price_basis="raw",
                )
                history = tuple(Bar.model_validate(row.model_dump()) for row in reader.bars(query))
                _require(
                    len({(bar.instrument_id, bar.bar_start) for bar in history}) == len(history)
                    and all(
                        bar.instrument_id in spec.universe
                        and bar.quote_currency == spec.base_currency
                        and bar.timeframe == query.timeframe
                        and bar.price_basis == "raw"
                        and bar.is_final
                        and query.start <= bar.bar_start < query.end
                        and bar.bar_end <= query.end
                        and bar.available_at <= now
                        for bar in history
                    ),
                    ErrorCode.DATA_CONFLICT,
                    "Continuous history must contain unique and visible raw bars",
                )
                prices = {
                    bar.instrument_id: bar.close
                    for bar in sorted(history, key=lambda row: (row.bar_end, row.instrument_id))
                    if (now not in closing_marks or bar.bar_end == closing_marks[now])
                    and (
                        bar.instrument_id not in actions.latest_basis_at
                        or bar.bar_start >= actions.latest_basis_at[bar.instrument_id]
                    )
                }
                equity.append(account.mark_to_market(now, prices))
                if now in deciding:
                    day = day_by_time[now]
                    expected = {key: sessions_by_day[day, key].instrument for key in spec.universe}
                    batch = self._decide(spec, reader, strategy, account, history, now, expected)
                    for signal in sorted(
                        batch.signals,
                        key=lambda s: (s.side != "sell", s.score.copy_negate(), s.instrument_id),
                    ):
                        key = day, signal.instrument_id
                        session = sessions_by_day[key]
                        accepted = admission.submit(
                            signal,
                            signal.limit_quantity,
                            at=now,
                            instrument=session.instrument,
                            reference=session.reference,
                            price_view=self.price_view,
                        ).event
                        if accepted.status == "accepted":
                            orders[key].append(accepted.order_id)
            if observer is not None:
                observer.progress(step, len(timeline), "continuous")
        _cancel(observer)
        account.finish(end)
        return BacktestResult(
            lock_id=lock.lock_id,
            semantics_version=spec.semantics_version,
            accounting_model=account.accounting_model,
            orders=account.orders,
            ledger=account.ledger,
            equity=tuple(equity),
            final_portfolio=account.snapshot(end),
            diagnostics=account.diagnostics,
        )

    def _decide(self, spec, reader, strategy, account, history, now, expected):
        instruments = DailyBacktest._instruments(reader, spec, now)
        _require(
            instruments == expected,
            ErrorCode.DATA_CONFLICT,
            "Continuous instrument terms changed within the trading day",
        )
        _require(
            instruments.keys() == set(spec.universe)
            and {bar.instrument_id for bar in history} == instruments.keys(),
            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
            "Continuous decision lacks known history or instrument states",
        )
        factors = (
            ()
            if spec.price_view.mode == "raw"
            else reader.factors(
                HistoryQuery(instrument_ids=spec.universe, effective_at=now, as_of=now)
            )
        )
        projected = self.price_view.project(history, factors, spec.price_view, decision_at=now)
        keys = sorted(instruments)
        context = StrategyContext(
            now=now,
            history=projected,
            instruments=tuple(instruments[key] for key in keys),
            portfolio=account.snapshot(now),
            parameters_json=spec.strategy.parameters_json,
            price_scales=tuple(
                self.price_view.price(1, instrument_id=key, decision_at=now).scale for key in keys
            ),
        )
        try:
            batch = SignalBatch.model_validate(strategy.implementation.decide(context).model_dump())
        except TiveloError:
            raise
        except Exception as exc:
            raise TiveloError(
                ErrorCode.STRATEGY_FAILED, "Invalid continuous strategy signals"
            ) from exc
        _require(
            batch.decision_at == now
            and all(
                signal.instrument_id in instruments and signal.limit_quantity is not None
                for signal in batch.signals
            ),
            ErrorCode.CONTRACT_INVALID,
            "Continuous signals need a current instrument and explicit limit order quantity",
        )
        return batch
