"""Immutable requests and reports for standalone legacy optimization."""

import hashlib
import json
import tempfile
from dataclasses import fields
from datetime import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal, Self

import polars as pl
from pydantic import Field, FiniteFloat, JsonValue, StrictBool, TypeAdapter, model_validator
from tivelo_contracts.artifacts import ArchiveContents, RunArchive
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    Key,
    ParametersJSON,
    canonical_bytes,
    fingerprint,
)
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference, MarketAdapterSpec, PriceViewSpec
from tivelo_contracts.extension_fields import ExtensionFieldRef

from ..archives import (
    MAX_ARCHIVE_BYTES,
    MAX_MANIFEST_BYTES,
    _definition_inventory,
    _durable_mkdir,
    _publish,
    _read,
    _record,
    _write,
    _write_definitions,
)
from ..definitions import DefinitionLoader
from ..market_loading import discover_market_adapter
from ..research.matrix import build_market_data_matrix
from ..research.matrix_selection import MatrixPipelineConfig
from ..research.signals import TIMEFRAME_INTRADAY, intraday_column_name, is_intraday_column
from ..research.signals import validate as validate_signal
from ..runtime import CapturedRuntime, capture_runtime, verify_runtime
from ..snapshot import PortableSnapshotReader
from .composite_strategy import LegacyCompositeStrategy
from .daily_optimization import optimize_daily_matrix, optimize_daily_panel
from .minute_optimization import optimize_legacy_minute
from .optimizer import expand_param_grid, validate_primary_objective
from .panel_strategy import LegacyPolarsStrategy
from .robust_optimizer import split_validation_windows
from .simulator import MatcherConfig
from .snapshot_daily import read_legacy_daily_panel

_MATCHER = TypeAdapter(MatcherConfig)
_MATCHER_KEYS = {field.name for field in fields(MatcherConfig)} - {
    "position_policy_compute",
    "position_policy_params",
}
_REPORT = TypeAdapter(dict[str, JsonValue])
LegacyFieldColumn = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
_SUPPLEMENTARY_BOUNDS = (
    ("market_cap", "total_shares"),
    ("float_cap", "float_shares"),
    ("turnover", "turnover_rate"),
)


class LegacySnapshotFilter(Contract):
    enabled: StrictBool = True
    price_min: FiniteFloat | None = None
    price_max: FiniteFloat | None = None
    amount_min: FiniteFloat | None = None
    amount_max: FiniteFloat | None = None
    market_cap_min: FiniteFloat | None = Field(default=None, exclude_if=lambda value: value is None)
    market_cap_max: FiniteFloat | None = Field(default=None, exclude_if=lambda value: value is None)
    float_cap_min: FiniteFloat | None = Field(default=None, exclude_if=lambda value: value is None)
    float_cap_max: FiniteFloat | None = Field(default=None, exclude_if=lambda value: value is None)
    turnover_min: FiniteFloat | None = Field(default=None, exclude_if=lambda value: value is None)
    turnover_max: FiniteFloat | None = Field(default=None, exclude_if=lambda value: value is None)
    exclude_st: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    boards: tuple[Literal["沪主板", "深主板", "创业板", "科创板", "北交所"], ...] = ()


class LegacyMatrixSelection(Contract):
    basic_filter: LegacySnapshotFilter = Field(default_factory=LegacySnapshotFilter)
    scoring: dict[Key, FiniteFloat] = Field(default_factory=dict)
    order_by: Key | None = None
    descending: StrictBool = True
    scoring_directions: dict[Key, Literal["high", "low"]] = Field(default_factory=dict)


class LegacyMinuteInput(Contract):
    query: BarQuery
    minute_label: Literal["start", "end"]
    market: MarketAdapterSpec = Field(default_factory=lambda: MarketAdapterSpec(
        adapter_id="tivelo.market.cn.legacy-minute", distribution="tivelo-market-cn",
    ))
    mode: Literal["position", "full"] = "position"
    holding_days: int = Field(default=5, strict=True, gt=0)
    signal_definitions: tuple[dict[str, JsonValue], ...] = Field(
        default=(), max_length=64, exclude_if=lambda value: not value,
    )
    signal_fields: dict[Key, ExtensionFieldRef] = Field(
        default_factory=dict, max_length=64, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def coherent_signals(self) -> Self:
        names = set(self.signal_fields)
        if not all(is_intraday_column(name) for name in names):
            raise ValueError("Minute signal aliases must be csgi_ columns")
        for definition in self.signal_definitions:
            if definition.get("timeframe") != TIMEFRAME_INTRADAY:
                raise ValueError("Minute signal definitions must have the intraday timeframe")
            validate_signal(definition)
            name = intraday_column_name(definition["id"])
            if name in names:
                raise ValueError("A minute signal column can only have one input")
            names.add(name)
        return self


class LegacyRobustRequest(Contract):
    request_version: Literal["1.0"] = "1.0"
    query: BarQuery
    start: CalendarDate
    end: CalendarDate
    symbols: dict[Key, Key] | None = None
    price_view: PriceViewSpec | None = None
    strategy_parameters_json: ParametersJSON = "{}"
    field_columns: tuple[Key, ...] = ("amount",)
    extension_fields: dict[LegacyFieldColumn, ExtensionFieldRef] = Field(
        default_factory=dict, max_length=64, exclude_if=lambda value: not value
    )
    field_cutoff_time: time | None = Field(default=None, exclude_if=lambda value: value is None)
    params_meta: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=64)
    param_grid: dict[Key, JsonValue] = Field(min_length=1, max_length=64)
    base_params: dict[Key, JsonValue] = Field(default_factory=dict)
    selection: LegacyMatrixSelection | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    minute: LegacyMinuteInput | None = Field(default=None, exclude_if=lambda value: value is None)
    matcher: dict[Key, JsonValue] = Field(default_factory=dict)
    objective: Key = "sortino"
    direction: Literal["min", "max"] | None = None
    k_segments: int = Field(default=4, strict=True, ge=2, le=6)
    min_trades_total: int = Field(default=30, strict=True)
    stage2_top_n: int = Field(default=20, strict=True)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.query.timeframe != "1d" or self.end < self.start:
            raise ValueError("Legacy robust optimization needs a daily range")
        if self.field_cutoff_time is not None and self.field_cutoff_time.utcoffset() is None:
            raise ValueError("Field cutoff time needs a UTC offset")
        if self.price_view is not None and (
            self.price_view.mode == "extension" or self.query.price_basis != "raw"
        ):
            raise ValueError("Legacy robust projection needs raw bars and a fixed price view")
        if (minute := self.minute) is not None and (
            minute.query.timeframe != "1m"
            or minute.query.price_basis != "raw"
            or self.query.price_basis != "raw"
            or minute.query.as_of != self.query.as_of
            or set(minute.query.instrument_ids) != set(self.query.instrument_ids)
        ):
            raise ValueError("Minute optimization needs raw 1m and 1d queries with the same instruments and as_of")
        if not self.matcher.keys() <= _MATCHER_KEYS:
            raise ValueError("Unknown or callable-only matcher setting")
        if any(
            not isinstance(item.get("id"), str) or not isinstance(item.get("type"), str)
            for item in self.params_meta
        ):
            raise ValueError("Legacy parameter metadata requires id and type")
        return self


class LegacyRobustLock(Contract):
    semantics: Literal["legacy.daily.robust.v1", "legacy.minute.robust.v1"] = "legacy.daily.robust.v1"
    request: LegacyRobustRequest
    grid_order: tuple[Key, ...]
    scoring_order: tuple[Key, ...] = Field(default=(), exclude_if=lambda value: not value)
    snapshot_id: Digest
    strategy: DefinitionReference
    dependencies: tuple[DefinitionReference, ...] = ()
    runtime: CapturedRuntime

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.request.minute is None) != (self.semantics == "legacy.daily.robust.v1"):
            raise ValueError("Legacy optimizer semantics differ from the locked inputs")
        if (
            len(self.grid_order) != len(self.request.param_grid)
            or set(self.grid_order) != self.request.param_grid.keys()
        ):
            raise ValueError("Grid axis order differs from the locked request")
        scoring = self.request.selection.scoring if self.request.selection is not None else {}
        if len(self.scoring_order) != len(scoring) or set(self.scoring_order) != scoring.keys():
            raise ValueError("Scoring order differs from the locked request")
        return self


class LegacyRobustArchive(RunArchive):
    format: Literal["tivelo-legacy-robust-archive"] = "tivelo-legacy-robust-archive"


def _matcher(request: LegacyRobustRequest) -> MatcherConfig:
    config = _MATCHER.validate_json(json.dumps(request.matcher, allow_nan=False), strict=True)
    if (
        config.minute_fill
        or config.exit_fill == "signal_next_minute"
        or config.position_sizing == "policy"
        or config.asset_type != "stock"
    ):
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Standalone legacy optimization requires a daily stock matcher with built-in sizing",
        )
    if config.initial_capital <= 0 or config.max_positions <= 0:
        raise ValueError("Legacy optimizer capital and position count must be positive")
    return config


def _requirements(definitions, market=None):
    requirements = {"tivelo-backtest", *definitions.requirements}
    if market is not None:
        requirements.add(market.specification.distribution)
    # The optional shared indicator kernels use Numba when present. Replay must
    # pin its environment as well as the standalone Python fallback.
    try:
        version("numba")
    except PackageNotFoundError:
        pass
    else:
        requirements.add("numba")
    return tuple(sorted(requirements))


def _verified_json(root, filename, inventory):
    payload = _read(root, filename)
    file = inventory[filename]
    if len(payload) != file.size_bytes or hashlib.sha256(payload).hexdigest() != file.sha256:
        raise ValueError("Archive JSON changed during verification")
    return payload


def read_legacy_robust(directory):
    """Verify and read an archived ranking without importing or executing its strategy."""
    root = Path(directory).expanduser().absolute()
    try:
        manifest = LegacyRobustArchive.model_validate_json(_read(root, "manifest.json"))
        inventory = {file.path: file for file in manifest.contents.files}
        if sum(file.size_bytes for file in inventory.values()) > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds its byte budget")
        for name, file in inventory.items():
            if _record(root, name, expected_size=file.size_bytes) != file:
                raise ValueError("Archive file digest differs")
        lock = LegacyRobustLock.model_validate_json(
            _verified_json(root, "run-lock.json", inventory)
        )
        if manifest.contents.lock_id != fingerprint(lock):
            raise ValueError("Archive refers to a different legacy lock")
        bundle, expected = _definition_inventory(root, lock.strategy, lock.dependencies, inventory)
        if bundle.contents.kind != "strategy" or bundle.contents.language != "trusted_python":
            raise ValueError("Legacy optimization needs a trusted strategy")
        if set(inventory) != {"run-lock.json", "report.json", *expected}:
            raise ValueError("Archive inventory differs from the locked definition graph")
        report = _REPORT.validate_json(_verified_json(root, "report.json", inventory))
        return manifest, lock, report
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Legacy optimization archive failed verification"
        ) from exc


def _check_filter_coverage(panel: pl.DataFrame, bounds: LegacySnapshotFilter) -> None:
    columns = [
        column
        for prefix, column in _SUPPLEMENTARY_BOUNDS
        if getattr(bounds, f"{prefix}_min") is not None
        or getattr(bounds, f"{prefix}_max") is not None
    ]
    if bounds.exclude_st:
        columns.append("is_st")
    if not columns:
        return
    for column in columns:
        if column not in panel.columns:
            raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, f"Filter requires {column}")
    counts = panel.group_by("date").agg(pl.col(columns).count())
    for column in columns:
        if counts[column].min() == 0:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, f"Filter lacks visible {column} on a bar date"
            )


def _prepare_panel(reader, request, bounds):
    panel = read_legacy_daily_panel(
        reader,
        request.query,
        symbols=request.symbols,
        price_view=request.price_view,
        extension_fields=request.extension_fields,
        include_st=bounds is not None and bounds.enabled and bounds.exclude_st,
        include_names=request.minute is not None or "name" in request.field_columns,
        field_cutoff_time=request.field_cutoff_time,
    )
    if panel.is_empty():
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Snapshot has no matching daily bars"
        )
    field_columns = set(request.field_columns) | request.extension_fields.keys()
    field_columns.update(name for name in ("is_st", "turnover_rate") if name in panel.columns)
    if bounds is not None and bounds.enabled:
        _check_filter_coverage(panel, bounds)
        if bounds.amount_min is not None or bounds.amount_max is not None:
            if panel["amount"].null_count() == panel.height:
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Amount filter requires turnover data"
                )
            field_columns.add("amount")
    return panel, field_columns


def _strategy_bounds(strategy):
    return LegacySnapshotFilter.model_validate({
        key: value for key, value in strategy.basic_filter.items()
        if key in LegacySnapshotFilter.model_fields
    })


def _run_locked_search(lock, definitions, matcher, reader, market):
    request = lock.request
    if market is not None:
        limit_for = market.load_legacy_minute_limits()
        settings = request.selection
        prepared = _prepare_panel(reader, request, settings.basic_filter) if settings is not None else None
        with DefinitionLoader().load_trusted_legacy_minute(definitions) as strategy:
            panel, _ = prepared if prepared is not None else _prepare_panel(reader, request, _strategy_bounds(strategy))
            return optimize_legacy_minute(
                panel, strategy, matcher, request=request, reader=reader, limit_for=limit_for,
                grid_order=lock.grid_order, scoring_order=lock.scoring_order,
            )
    selection = prepared = None
    if (settings := request.selection) is not None:
        prepared = _prepare_panel(reader, request, settings.basic_filter)
        selection = MatrixPipelineConfig(
            basic_filter=settings.basic_filter.model_dump(mode="json", exclude_defaults=True),
            scoring={name: settings.scoring[name] for name in lock.scoring_order},
            order_by=settings.order_by,
            descending=settings.descending,
            scoring_directions=dict(settings.scoring_directions),
        )
    with DefinitionLoader().load_trusted_legacy_daily(definitions) as strategy:
        if prepared is None:
            bounds = None
            if isinstance(strategy, LegacyCompositeStrategy):
                bounds = _strategy_bounds(strategy)
            prepared = _prepare_panel(reader, request, bounds)
        panel, field_columns = prepared
        options = dict(
            start=request.start,
            end=request.end,
            params_meta=list(request.params_meta),
            param_grid={key: request.param_grid[key] for key in lock.grid_order},
            base_params=request.base_params,
            selection=selection,
            objective=request.objective,
            direction=request.direction,
            k_segments=request.k_segments,
            min_trades_total=request.min_trades_total,
            stage2_top_n=request.stage2_top_n,
        )
        if isinstance(strategy, LegacyPolarsStrategy):
            return optimize_daily_panel(panel, strategy, matcher, **options)
        matrix = build_market_data_matrix(panel, field_columns=field_columns)
        return optimize_daily_matrix(matrix, strategy, matcher, **options)


def _execute_locked(lock, definitions, matcher, reader, destination, market=None, expected_archive=None):
    if (lock.strategy, lock.dependencies) != (definitions.root, definitions.dependencies):
        raise TiveloError(ErrorCode.DEFINITION_CONFLICT, "Legacy strategy differs from its lock")
    # Capture request bytes before the trusted strategy is called.
    lock_bytes = canonical_bytes(lock)
    result = _run_locked_search(lock, definitions, matcher, reader, market)
    result.pop("elapsed_ms")
    report_bytes = json.dumps(
        json.loads(_REPORT.dump_json(result, warnings="error")),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(report_bytes) > MAX_MANIFEST_BYTES:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Legacy optimization report exceeds its budget"
        )
    requested = Path(destination).expanduser().absolute()
    target = requested.parent.resolve() / requested.name
    _durable_mkdir(target.parent)
    with tempfile.TemporaryDirectory(prefix=".tivelo-legacy-robust-", dir=target.parent) as tmp:
        stage = Path(tmp)
        _write(stage, "run-lock.json", lock_bytes)
        _write(stage, "report.json", report_bytes)
        paths = ["run-lock.json", "report.json", *_write_definitions(stage, definitions)]
        manifest = LegacyRobustArchive.create(
            ArchiveContents(
                lock_id=hashlib.sha256(lock_bytes).hexdigest(),
                files=tuple(_record(stage, name) for name in sorted(paths)),
            )
        )
        if expected_archive is not None and manifest != expected_archive:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Legacy optimization replay changed the report"
            )
        return _publish(
            stage, target, manifest, None, inspect_existing=lambda path: read_legacy_robust(path)[0]
        )


def run_legacy_robust(request, *, definition, snapshot, destination, dependency_root=None):
    # Grid ties and score accumulation depend on the order before JSON canonicalization.
    grid_order = tuple(request.param_grid)
    scoring_order = tuple(request.selection.scoring) if request.selection is not None else ()
    request = LegacyRobustRequest.model_validate_json(canonical_bytes(request))
    matcher = _matcher(request)
    validate_primary_objective(request.objective)
    split_validation_windows(request.start, request.end, request.k_segments)
    expand_param_grid(list(request.params_meta), request.param_grid)
    definitions = DefinitionLoader(dependency_root).inspect(
        definition, parameters_json=request.strategy_parameters_json
    )
    market = discover_market_adapter(request.minute.market) if request.minute is not None else None
    runtime = capture_runtime(_requirements(definitions, market))
    with PortableSnapshotReader(snapshot) as reader:
        lock = LegacyRobustLock(
            semantics="legacy.minute.robust.v1" if market is not None else "legacy.daily.robust.v1",
            request=request,
            grid_order=grid_order,
            scoring_order=scoring_order,
            snapshot_id=reader.manifest.snapshot_id,
            strategy=definitions.root,
            dependencies=definitions.dependencies,
            runtime=runtime,
        )
        return _execute_locked(lock, definitions, matcher, reader, destination, market=market)


def replay_legacy_robust(*, archive, snapshot, destination):
    manifest, lock, _ = read_legacy_robust(archive)
    definitions_root = Path(archive).expanduser().absolute() / "definitions"
    definitions = DefinitionLoader(definitions_root).inspect(
        definitions_root / lock.strategy.sha256, expected=lock.strategy
    )
    minute = lock.request.minute
    market = discover_market_adapter(minute.market) if minute is not None else None
    verify_runtime(lock.runtime, _requirements(definitions, market))
    with PortableSnapshotReader(snapshot, snapshot_id=lock.snapshot_id) as reader:
        return _execute_locked(
            lock,
            definitions,
            _matcher(lock.request),
            reader,
            destination,
            market=market,
            expected_archive=manifest,
        )
