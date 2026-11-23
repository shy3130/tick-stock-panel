"""Seal a standalone copy of an existing TSP strategy."""

import ast
import json
import os
import tempfile
from pathlib import Path

from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..archives import _durable_mkdir, _read, _write
from ..definitions import seal_definition
from ..research.composite import parse_composite_children
from ..research.matrix_selection import MatrixPipelineConfig

_OPERATORS = frozenset(
    {
        "MarketDataMatrix",
        "SignalMatrix",
        "make_signal_matrix",
        "matrix_feature",
        "shift",
        "valid_shift",
        "ewm_adjust_false",
        "valid_ewm_adjust_false",
        "safe_divide",
        "rolling_min",
        "rolling_max",
        "rolling_mean",
        "rolling_sum",
        "rolling_std",
        "rolling_quantile",
        "valid_rolling_min",
        "valid_rolling_max",
        "valid_rolling_mean",
        "valid_rolling_sum",
        "valid_rolling_std",
    }
)
_FACTORY = b"""from tivelo_backtest.legacy.matrix_source import bind_matrix_strategy
from .strategy import META, MATRIX_STRATEGY

def build(*, parameters_json, dependencies):
    return bind_matrix_strategy(MATRIX_STRATEGY, META, parameters_json)
"""
_POLARS_FACTORY = b"""from tivelo_backtest.legacy.panel_strategy import LegacyPolarsStrategy
from . import strategy

def build(*, parameters_json, dependencies):
    return LegacyPolarsStrategy(strategy, parameters_json)
"""
_COMPOSITE_FACTORY = b"""from tivelo_backtest.legacy.composite_strategy import LegacyCompositeStrategy
from . import strategy

def build(*, parameters_json, dependencies):
    return LegacyCompositeStrategy(strategy, parameters_json, dependencies)
"""
_MINUTE_FACTORY = b"""from tivelo_backtest.legacy.minute_strategy import LegacyMinuteStrategy
from . import strategy

def build(*, parameters_json, dependencies):
    return LegacyMinuteStrategy(strategy, parameters_json)
"""


def _portable_source(source: bytes, expected_backends: tuple[str, ...]):
    try:
        tree = ast.parse(source.decode("utf-8"))
    except SyntaxError as exc:
        raise ValueError("Legacy strategy needs valid Python syntax") from exc
    assignments = {}
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            targets = statement.targets if isinstance(statement, ast.Assign) else (statement.target,)
            assignments.update({
                target.id: statement.value for target in targets if isinstance(target, ast.Name)
            })
    if "META" not in assignments:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Expected a legacy strategy with META")
    meta = ast.literal_eval(assignments["META"])
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        raise ValueError("Legacy strategy META needs a strategy id")
    exported = assignments.keys() | {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    backend = (
        ast.literal_eval(assignments["EXECUTION_BACKEND"])
        if "EXECUTION_BACKEND" in assignments
        else meta.get("execution_backend", "python_history_legacy" if "filter_history" in exported else "polars_expr")
    )
    timeframes = meta.get("timeframes", ["1d"])
    minute = backend == "minute_filter"
    supported_timeframe = list(timeframes) == ["1m"] if minute else "1d" in timeframes
    if backend not in expected_backends or not supported_timeframe:
        label = "minute" if expected_backends == ("minute_filter",) else "daily"
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY, f"Expected a {label} {' or '.join(expected_backends)} strategy"
        )
    if backend == "matrix_native" and "MATRIX_STRATEGY" not in assignments:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Expected MATRIX_STRATEGY")
    if backend == "composite" and exported & {
        "MATRIX_STRATEGY", "filter", "filter_history", "filter_minute_history"
    }:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Expected a declarative composite")
    filter_functions = {
        "polars_expr": "filter", "python_history_legacy": "filter_history", "minute_filter": "filter_minute_history",
    }
    if backend in filter_functions:
        expected = {filter_functions[backend]}
        if exported & {"filter", "filter_history", "filter_minute_history", "MATRIX_STRATEGY"} != expected:
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Expected a single declared filter entry point")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if backend == "matrix_native" and module == "app.backtest.matrix" and not node.level:
                if any(name.name not in _OPERATORS for name in node.names):
                    raise TiveloError(
                        ErrorCode.UNSUPPORTED_CAPABILITY,
                        "Strategy imports a host-only matrix operator",
                    )
                node.module = "tivelo_backtest.research.matrix_features"
            elif node.level or module == "app" or module.startswith("app."):
                raise TiveloError(
                    ErrorCode.UNSUPPORTED_CAPABILITY, "Strategy imports a separate host source"
                )
        elif isinstance(node, ast.Import) and any(
            item.name == "app" or item.name.startswith("app.") for item in node.names
        ):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Strategy imports a separate host source"
            )
    return (ast.unparse(tree) + "\n").encode("utf-8"), meta


class _BoundMatrixStrategy:
    def __init__(self, strategy, defaults, meta):
        self.strategy = strategy
        self.defaults = defaults
        self.selection = MatrixPipelineConfig(
            basic_filter={}, scoring=dict(meta.get("scoring") or {}),
            order_by=meta.get("order_by"), descending=bool(meta.get("descending", True)),
        )

    def required_warmup_bars(self, params):
        return self.strategy.required_warmup_bars({**self.defaults, **params})

    def required_fields_for_params(self, params):
        resolve = getattr(self.strategy, "required_fields_for_params", None)
        return resolve({**self.defaults, **params}) if callable(resolve) else ()

    def compute_signals(self, market, params):
        from ..research.matrix_features import _activate_valid_bar_index

        with _activate_valid_bar_index(market.valid_bars):
            return self.strategy.compute_signals(market, {**self.defaults, **params})


def bind_matrix_strategy(strategy, meta, parameters_json):
    defaults = {item["id"]: item["default"] for item in meta.get("params", ()) if "default" in item}
    return _BoundMatrixStrategy(strategy, {**defaults, **json.loads(parameters_json)}, meta)


def seal_legacy_matrix(source: bytes, destination, *, version: str, requirements=()):
    """Adapt imports and seal explicit source bytes without executing the strategy."""
    strategy, meta = _portable_source(source, ("matrix_native",))
    return _seal_source(strategy, _FACTORY, meta["id"], destination, version, requirements)


def seal_legacy_polars(source: bytes, destination, *, version: str, requirements=()):
    """Seal a legacy Polars expression or history filter without executing it."""
    strategy, meta = _portable_source(source, ("polars_expr", "python_history_legacy"))
    return _seal_source(strategy, _POLARS_FACTORY, meta["id"], destination, version, requirements)


def seal_legacy_composite(source: bytes, destination, *, version: str, dependencies, requirements=()):
    """Seal an existing composite with explicit child definition references."""
    strategy, meta = _portable_source(source, ("composite",))
    children = parse_composite_children(meta.get("children")).children
    if {child.strategy_id for child in children} != {ref.definition_id for ref in dependencies}:
        raise TiveloError(ErrorCode.DEFINITION_CONFLICT, "Composite children differ from the supplied references")
    return _seal_source(
        strategy, _COMPOSITE_FACTORY, meta["id"], destination, version, requirements,
        dependencies=dependencies,
    )


def seal_legacy_minute(source: bytes, destination, *, version: str, requirements=()):
    """Seal an existing single-file minute filter."""
    strategy, meta = _portable_source(source, ("minute_filter",))
    return _seal_source(strategy, _MINUTE_FACTORY, meta["id"], destination, version, requirements)


def _seal_source(strategy, factory, definition_id, destination, version, requirements, *, dependencies=()):
    requested = Path(destination).expanduser().absolute()
    target = requested.parent.resolve() / requested.name
    _durable_mkdir(target.parent)
    with tempfile.TemporaryDirectory(prefix=".tivelo-seal-strategy-", dir=target.parent) as temp:
        stage = Path(temp)
        _write(stage, "strategy.py", strategy)
        _write(stage, "factory.py", factory)
        bundle = seal_definition(
            stage,
            definition_id=definition_id,
            version=version,
            kind="strategy",
            entrypoint="factory:build",
            files=("strategy.py", "factory.py"),
            dependencies=dependencies,
            requirements=requirements,
        )
        files = {
            "strategy.py": strategy,
            "factory.py": factory,
            "definition.json": canonical_bytes(bundle),
        }
        _write(stage, "definition.json", files["definition.json"])
        try:
            target.mkdir()
        except FileExistsError:
            try:
                if all(_read(target, name) == payload for name, payload in files.items()):
                    return bundle
            except (OSError, TiveloError):
                pass
            raise TiveloError(
                ErrorCode.DEFINITION_CONFLICT, "Legacy definition destination is already in use"
            ) from None
        try:
            for name in files:
                os.link(stage / name, target / name)
        except BaseException:
            if not (target / "definition.json").exists():
                for name in files:
                    (target / name).unlink(missing_ok=True)
                target.rmdir()
            raise
    return bundle
