"""Shared CLI/library preparation and execution over portable, locked inputs."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Self

from pydantic import Field, model_validator
from tivelo_contracts.base import CalendarDate, Contract, Key, ParametersJSON, PositiveDecimal
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    BacktestSpec,
    ExecutionModel,
    MarketAdapterSpec,
    PriceViewSpec,
    RunLock,
    RunLockContents,
)
from tivelo_contracts.extension_fields import ExtensionFieldRef
from tivelo_contracts.protocols import PriceViewRuntime

from .account import CashEquityAccount, CashEquityPolicy
from .archives import publish_result, publish_run, read_result_inputs, read_run
from .controlled import load_controlled
from .daily import VERIFIER_VERSION, DailyBacktest
from .definitions import DefinitionLoader
from .market_loading import discover_market_adapter
from .price_views import open_price_view
from .runtime import CapturedRuntime, capture_runtime, verify_runtime
from .snapshot import PortableSnapshotReader


class RunRequest(Contract):
    """User inputs; snapshot, code, profile and environment hashes are resolved by prepare."""

    request_version: Literal["1.0"] = "1.0"
    universe: tuple[Key, ...] = Field(min_length=1)
    start: CalendarDate
    end: CalendarDate
    warmup_start: CalendarDate | None = None
    strategy_parameters_json: ParametersJSON = "{}"
    market: MarketAdapterSpec
    account: CashEquityPolicy
    initial_capital: PositiveDecimal
    price_view: PriceViewSpec
    execution: ExecutionModel = Field(default_factory=lambda: ExecutionModel(timeframe="1d"))
    extension_fields: tuple[ExtensionFieldRef, ...] = Field(default=(), max_length=64, exclude_if=lambda v: not v)

    @property
    def semantics_version(self) -> str:
        return ("strict.continuous.v1" if getattr(self.execution, "continuous", None) is not None
                else "strict.daily.v1")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end < self.start or (
            self.warmup_start is not None and self.warmup_start > self.start
        ):
            raise ValueError("invalid run or warmup date range")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe must contain unique instrument identities")
        if self.account.order_expiry != self.execution.order_expiry:
            raise ValueError("account and execution order expiry differ")
        return self


def run_requirements(definitions, adapter, extension_requirements=()):
    return tuple(sorted({"tivelo-backtest", adapter.distribution, *definitions.requirements, *extension_requirements}))


def _engine(semantics):
    if semantics == "strict.continuous.v1":
        from .continuous_run import VERIFIER_VERSION as verifier
        from .continuous_run import ContinuousBacktest

        return verifier, ContinuousBacktest
    return VERIFIER_VERSION, DailyBacktest


def prepare_run(
    request: RunRequest,
    *,
    snapshot,
    definition,
    destination,
    dependency_root=None,
    observer=None,
    extension_runtime: PriceViewRuntime | None = None,
):
    """Preflight and archive inputs without constructing or executing the strategy."""
    request = RunRequest.model_validate_json(request.model_dump_json())
    if observer is not None and observer.cancelled():
        raise TiveloError(ErrorCode.CANCELLED, "Run preparation cancelled")
    continuous = request.semantics_version == "strict.continuous.v1"
    if continuous and request.price_view.mode == "extension":
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Continuous orders need a fixed price view")
    pins = ()
    if request.price_view.mode == "extension":
        if extension_runtime is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "No installed runtime for the requested price view",
            )
        pins = (extension_runtime.resolve(request.price_view.provider_id),)
    loader = DefinitionLoader(dependency_root)
    definitions = loader.inspect(definition, parameters_json=request.strategy_parameters_json)
    if definitions.nodes[-1].bundle.contents.kind != "strategy":
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "The run root must be a strategy definition")
    if continuous and definitions.nodes[-1].bundle.contents.language != "trusted_python":
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY, "Continuous replay requires an explicit limit-order strategy"
        )
    controlled = (
        load_controlled(definitions)
        if definitions.nodes[-1].bundle.contents.language == "controlled_daily_v1"
        else None
    )
    adapter = discover_market_adapter(request.market)
    extension_requirements = tuple(sorted(set(getattr(extension_runtime, "software_requirements", ())))) if pins else ()
    requirements = run_requirements(definitions, request.market, extension_requirements)
    runtime = capture_runtime(requirements)
    verifier, engine = _engine(request.semantics_version)
    policy = request.account
    account = CashEquityAccount(
        policy.currency,
        request.initial_capital,
        order_expiry=policy.order_expiry,
        dividend_treatment=policy.dividend_treatment,
    )
    with PortableSnapshotReader(snapshot) as reader:
        reader.verify()
        rules = adapter.load(continuous=continuous, reader=reader, run=request)
        spec = BacktestSpec(
            semantics_version=request.semantics_version,
            snapshot_id=reader.manifest.snapshot_id,
            universe=request.universe,
            start=request.start,
            end=request.end,
            warmup_start=request.warmup_start,
            strategy=definitions.root,
            dependencies=definitions.dependencies,
            market_profile=rules.reference,
            account_profile=account.reference,
            price_view=request.price_view,
            execution=request.execution,
            extension_fields=request.extension_fields,
            accounting_model="event_ledger",
            knowledge_requirement="historical_versions",
            initial_capital=request.initial_capital,
            base_currency=policy.currency,
        )
        lock = RunLock.create(
            RunLockContents(
                spec=spec,
                snapshot=reader.manifest,
                packages=runtime.packages,
                extensions=pins,
                extension_requirements=extension_requirements,
                runtime=runtime.reference,
                verifier_version=verifier,
                market_adapter=request.market,
                scale_proof=controlled.implementation.proof if controlled is not None else None,
            )
        )
        view = open_price_view(lock, extension_runtime)
        runner = engine(rules=rules, price_view=view)
        active_days = runner.preflight(
            lock,
            reader,
            strategy=definitions.root,
            dependencies=definitions.dependencies,
        )
        if not continuous and (controlled is None or spec.price_view.mode == "extension"):
            view.project(
                (),
                (),
                spec.price_view,
                decision_at=(
                    active_days[0].close_at
                    + timedelta(seconds=spec.execution.decision_delay_seconds)
                ),
            )
    verify_runtime(runtime, requirements)
    return publish_run(lock, definitions, destination, observer=observer)


def execute_run(
    *, run, snapshot, destination, observer=None, extension_runtime: PriceViewRuntime | None = None
):
    """Execute archived trusted Python in the current, content-verified environment.

    Use a fresh process per run (the CLI does). This function is not an environment
    installer, worker supervisor, arbitrary-code sandbox or implicit license grant.
    """
    lock, definitions = read_run(run)
    return _execute_locked(
        lock,
        definitions,
        snapshot=snapshot,
        destination=destination,
        observer=observer,
        extension_runtime=extension_runtime,
    )


def restore_run(*, archive, destination, observer=None):
    """Recover a prepared run for execution by its original runtime."""
    _, lock, definitions = read_result_inputs(archive)
    return publish_run(lock, definitions, destination, observer=observer)


def replay_result(
    *, archive, snapshot, destination, observer=None, extension_runtime: PriceViewRuntime | None = None
):
    """Recompute a single result and require an exact archive match before publishing."""
    expected, lock, definitions = read_result_inputs(archive)
    return _execute_locked(
        lock,
        definitions,
        snapshot=snapshot,
        destination=destination,
        observer=observer,
        extension_runtime=extension_runtime,
        expected_archive=expected,
    )


def _execute_locked(
    lock, definitions, *, snapshot, destination, observer, extension_runtime, expected_archive=None
):
    specification = lock.contents.market_adapter
    if specification is None:
        raise TiveloError(
            ErrorCode.RULES_UNAVAILABLE, "RunLock has no installed market adapter binding"
        )
    requirements = run_requirements(definitions, specification, lock.contents.extension_requirements)
    runtime = CapturedRuntime(lock.contents.packages, lock.contents.runtime)
    verify_runtime(runtime, requirements)
    adapter = discover_market_adapter(specification)
    semantics = lock.contents.spec.semantics_version
    continuous = semantics == "strict.continuous.v1"
    _, engine = _engine(semantics)
    with PortableSnapshotReader(snapshot, snapshot_id=lock.contents.spec.snapshot_id) as reader:
        reader.verify()
        rules = adapter.load(continuous=continuous, reader=reader, run=lock.contents.spec)
        runner = engine(rules=rules, price_view=open_price_view(lock, extension_runtime))
        runner.preflight(
            lock, reader, strategy=definitions.root, dependencies=definitions.dependencies
        )
        if definitions.nodes[-1].bundle.contents.language == "controlled_daily_v1":
            binding = load_controlled(definitions)
            result = runner.run(lock, reader, binding, observer=observer)
        else:
            with DefinitionLoader().load_trusted_python(definitions) as binding:
                result = runner.run(lock, reader, binding, observer=observer)
        reader.verify()
    verify_runtime(runtime, requirements)
    return publish_result(
        lock, definitions, result, destination, observer=observer, expected_archive=expected_archive
    )
