"""Chart-only definitions and bounded computations; never change enriched data."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Literal

import polars as pl
from pydantic import Field, FiniteFloat, field_validator, model_validator

from app.factors.dsl import BASE_COLUMNS
from app.indicators.chart import (
    MAX_ACTIVE,
    MAX_DEFINITIONS,
    ChartIndicator,
    ChartLibrary,
    ChartModel,
    compile_indicator,
    evaluate_frame,
    resolve_indicator,
)
from app.indicators.chart_presets import (
    BUILTIN_CHART_INDICATORS,
    default_chart_library,
    migrate_library,
)
from app.services import preferences

_CACHE: OrderedDict[str, tuple[pl.DataFrame, dict, int]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_POINTS = 300_000
_HISTORY_CACHE: OrderedDict[tuple, list[dict]] = OrderedDict()


class LibraryConflictError(ValueError):
    pass


class LibrarySave(ChartModel):
    library: ChartLibrary
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ChartBar(ChartModel):
    time: str
    close: FiniteFloat = Field(gt=0)
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    volume: FiniteFloat | None = None
    amount: FiniteFloat | None = None
    turnover_rate: FiniteFloat | None = None
    prev_close: FiniteFloat | None = None
    raw_close: FiniteFloat | None = None

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None or len(value) not in {10, 19}:
            raise ValueError("时间必须是北京时间日期或 YYYY-MM-DD HH:MM:SS")
        return (
            parsed.date().isoformat() if len(value) == 10 else parsed.strftime("%Y-%m-%d %H:%M:%S")
        )


class EvaluateRequest(ChartModel):
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    timeframe: str = Field(pattern=r"^(daily|minute)$")
    rows: list[ChartBar] = Field(max_length=12000)
    definitions: list[ChartIndicator] = Field(max_length=MAX_ACTIVE)

    @model_validator(mode="after")
    def matching_times(self):
        expected = 10 if self.timeframe == "daily" else 19
        if any(len(row.time) != expected for row in self.rows):
            raise ValueError("K线时间格式与计算周期不一致")
        if len({d.id for d in self.definitions}) != len(self.definitions):
            raise ValueError("不能重复计算相同指标")
        return self


def _revision(library: ChartLibrary) -> str:
    return hashlib.sha256(library.model_dump_json().encode()).hexdigest()


def get_library() -> dict:
    saved = preferences.load()
    library = (
        migrate_library(saved["chart_indicators"])
        if "chart_indicators" in saved
        else default_chart_library()
    )
    return {
        "library": library.model_dump(),
        "revision": _revision(library),
        "presets": [definition.model_dump() for definition in BUILTIN_CHART_INDICATORS],
    }


def validate_library(raw: object) -> ChartLibrary:
    library = migrate_library(raw)
    for definition in library.definitions:
        compile_indicator(definition)
    for selections in library.views.values():
        for selection in selections:
            definition = next(d for d in library.definitions if d.id == selection.id)
            compile_indicator(resolve_indicator(definition, selection.parameters))
    return library


def save_library(body: LibrarySave) -> dict:
    library = validate_library(body.library)
    with preferences.mutation():
        if get_library()["revision"] != body.revision:
            raise LibraryConflictError("指标配置已在其他窗口更新，请重新加载后合并；当前草稿已保留")
        preferences.save({"chart_indicators": library.model_dump()})
        return get_library()


class DefinitionsPackage(ChartModel):
    format: Literal["tick-chart-indicators"] = "tick-chart-indicators"
    version: Literal[1] = 1
    definitions: list[ChartIndicator] = Field(min_length=1, max_length=MAX_DEFINITIONS)

    @model_validator(mode="after")
    def valid_definitions(self):
        ids = [d.id for d in self.definitions]
        if len(set(ids)) != len(ids):
            raise ValueError("指标包包含重复 ID")
        for definition in self.definitions:
            compile_indicator(definition)
        return self


class DefinitionsExport(ChartModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_DEFINITIONS)
    revision: str


class DefinitionsImport(ChartModel):
    package: DefinitionsPackage
    selected: list[str] = Field(min_length=1, max_length=MAX_DEFINITIONS)
    overwrite: list[str] = Field(default_factory=list, max_length=MAX_DEFINITIONS)
    revision: str


def export_definitions(ids: list[str], revision: str) -> dict:
    current = get_library()
    if current["revision"] != revision:
        raise LibraryConflictError("指标库已变化，请刷新后重新导出")
    available = {d["id"]: d for d in current["library"]["definitions"]}
    if not ids or len(set(ids)) != len(ids) or not set(ids) <= available.keys():
        raise ValueError("请选择存在且不重复的图表指标")
    return DefinitionsPackage(definitions=[available[id] for id in ids]).model_dump()


def inspect_definitions(data: bytes) -> dict:
    # 48 definitions × 40k source characters, including four-byte UTF-8, must
    # fit in a package exported by this service.
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("指标包超过 8 MiB 上限")
    package = DefinitionsPackage.model_validate_json(data)
    current = get_library()
    existing = {d["id"]: d for d in current["library"]["definitions"]}
    return {
        "package": package.model_dump(),
        "revision": current["revision"],
        "entries": [
            {
                "id": d.id,
                "name": d.name,
                "action": "reuse"
                if existing.get(d.id) == d.model_dump()
                else "conflict"
                if d.id in existing
                else "create",
                "local": existing.get(d.id),
            }
            for d in package.definitions
        ],
    }


def import_definitions(body: DefinitionsImport) -> dict:
    incoming = {d.id: d for d in body.package.definitions}
    if len(set(body.selected)) != len(body.selected) or not set(body.selected) <= incoming.keys():
        raise ValueError("请选择指标包内存在且不重复的指标")
    if not set(body.overwrite) <= set(body.selected):
        raise ValueError("替换项必须属于已选指标")
    with preferences.mutation():
        current = get_library()
        if current["revision"] != body.revision:
            raise LibraryConflictError("指标库已变化，请重新预览后导入")
        library = ChartLibrary.model_validate(current["library"])
        existing = {d.id: d for d in library.definitions}
        for id in body.selected:
            if id in existing and existing[id] != incoming[id] and id not in body.overwrite:
                raise LibraryConflictError(f"指标 {incoming[id].name} 已存在，请确认替换")
            existing[id] = incoming[id]
        library.definitions = list(existing.values())
        # Revalidate selections against replaced parameters before making any write.
        validate_library(library.model_dump())
        return save_library(LibrarySave(library=library, revision=body.revision))


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
        _HISTORY_CACHE.clear()


def _warmup(
    repo, request: EvaluateRequest, first: str, count: int, dependencies: set[str]
) -> list[dict]:
    if repo is None or not count:
        return []
    first_day = date.fromisoformat(first[:10])
    asset = repo.resolve_asset_type(request.symbol)
    if request.timeframe == "minute" and asset == "index":
        return []  # Index minute history is not stored in stock/ETF partitions.
    days = count * 3 + 30 if request.timeframe == "daily" else (count // 200 + 2) * 3 + 7
    start = first_day - timedelta(days=min(days, (first_day - date.min).days))
    version = getattr(repo, "chart_history_version", None)
    stamp = (
        version(request.timeframe, asset, request.symbol, start, first_day)
        if callable(version)
        else None
    )
    key = (
        request.timeframe,
        asset,
        request.symbol,
        first,
        count,
        tuple(sorted(dependencies)),
        stamp,
    )
    if stamp is not None:
        with _CACHE_LOCK:
            cached = _HISTORY_CACHE.get(key)
            if cached is not None:
                _HISTORY_CACHE.move_to_end(key)
                return cached
    if request.timeframe == "daily":
        # Projection lets the repository skip its full built-in indicator pass.
        columns = sorted({"symbol", "date", "open", "high", "low", "close", *dependencies})
        history = repo.get_daily_asset(asset, request.symbol, start, first_day, columns=columns)
        rows = history.to_dicts()
        rows = [
            {**r, "time": str(r["date"])[:10]}
            for r in rows
            if str(r["date"])[:10] < first
            and all(
                isinstance(r.get(c), (int, float)) and math.isfinite(r[c]) and r[c] > 0
                for c in ("open", "high", "low", "close")
            )
        ]
    else:
        history = repo.get_minute_range([request.symbol], start, first_day, asset_type=asset)
        rows = [
            {**r, "time": str(r["datetime"]).replace("T", " ")[:19]}
            for r in history.to_dicts()
            if str(r["datetime"]).replace("T", " ")[:19] < first
        ]
        rows = [
            r
            for r in rows
            if ("09:30" <= r["time"][11:16] <= "11:30" or "13:00" <= r["time"][11:16] <= "15:00")
            and isinstance(r.get("close"), (int, float))
            and math.isfinite(r["close"])
            and r["close"] > 0
        ]
    rows = sorted({r["time"]: r for r in rows}.values(), key=lambda r: r["time"])[-count:]
    if stamp is not None and stamp == version(
        request.timeframe, asset, request.symbol, start, first_day
    ):
        with _CACHE_LOCK:
            _HISTORY_CACHE[key] = rows
            while (
                len(_HISTORY_CACHE) > 32
                or sum(len(value) for value in _HISTORY_CACHE.values()) > 100_000
            ):
                _HISTORY_CACHE.popitem(last=False)
    return rows


def evaluate(request: EvaluateRequest, repo=None) -> dict:
    started = perf_counter()
    if not request.rows or not request.definitions:
        return {
            "times": [],
            "results": [],
            "errors": [],
            "warnings": [],
            "cache_hit": False,
            "elapsed_ms": 0,
        }
    source = sorted(
        {r.time: r.model_dump() for r in request.rows}.values(), key=lambda r: r["time"]
    )
    errors = []
    valid = []
    count = 0
    dependencies: set[str] = set()
    for definition in request.definitions:
        try:
            _, warmup, required = compile_indicator(definition)
            valid.append(definition)
            dependencies.update(required)
            if not (definition.reset_daily and request.timeframe == "minute"):
                count = max(count, warmup)
        except ValueError as exc:
            errors.append({"id": definition.id, "name": definition.name, "message": str(exc)})
    history = _warmup(repo, request, source[0]["time"], count, dependencies)
    history = (
        sorted({r["time"]: r for r in history}.values(), key=lambda r: r["time"])[-count:]
        if count
        else []
    )
    records = [
        {
            "symbol": request.symbol,
            "time": r["time"],
            "date": r["time"][:10],
            **{column: r.get(column) for column in BASE_COLUMNS},
        }
        for r in [*history, *source]
    ]
    # Invalid optional trade fields in visible or historical bars are missing data.
    # Keep the price bar so formulas that only need prices can still run.
    frame = pl.DataFrame(records, schema_overrides={column: pl.Float64 for column in BASE_COLUMNS})
    frame = frame.with_columns(
        pl.when(pl.col(column).is_finite() & (pl.col(column) >= 0))
        .then(pl.col(column))
        .otherwise(None)
        .alias(column)
        for column in ("volume", "amount")
    )
    key = hashlib.sha256(
        (
            request.symbol
            + request.timeframe
            + str(len(history))
            + source[0]["time"]
            + json.dumps([d.model_dump() for d in valid], sort_keys=True)
        ).encode()
        + frame.hash_rows().to_numpy().tobytes()
    ).hexdigest()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0].equals(frame):
            _CACHE.move_to_end(key)
        else:
            cached = None
    if cached:
        response = copy.deepcopy(cached[1])
        response.update(cache_hit=True, elapsed_ms=round((perf_counter() - started) * 1000, 3))
        response["errors"] = [*errors, *response["errors"]]
        return response
    results = []
    runtime_errors = []
    for definition in valid:
        try:
            result = evaluate_frame(definition, frame, request.timeframe)
            for output in result["outputs"]:
                output["values"] = output["values"][len(history) :]
                if "colors" in output:
                    output["colors"] = output["colors"][len(history) :]
            for drawing in result.get("drawings", []):
                drawing["points"] = [
                    point
                    for point in drawing["points"]
                    if source[0]["time"] <= point["time"] <= source[-1]["time"]
                ]
            results.append(result)
        except (ValueError, pl.exceptions.PolarsError) as exc:
            runtime_errors.append(
                {"id": definition.id, "name": definition.name, "message": str(exc)}
            )
    warnings = (
        ["历史预热数据不足，较长周期的前段可能为空；递推指标从首个有效值初始化"]
        if len(history) < count
        else []
    )
    response = {
        "times": [r["time"] for r in source],
        "results": results,
        "errors": runtime_errors,
        "warnings": warnings,
    }
    points = sum(len(o["values"]) for result in results for o in result["outputs"])
    points += sum(
        len(drawing["points"]) * 4 for result in results for drawing in result.get("drawings", [])
    )
    if points <= _CACHE_POINTS:
        with _CACHE_LOCK:
            _CACHE[key] = (frame, copy.deepcopy(response), points)
            while len(_CACHE) > 32 or sum(item[2] for item in _CACHE.values()) > _CACHE_POINTS:
                _CACHE.popitem(last=False)
    return {
        **response,
        "errors": [*errors, *runtime_errors],
        "cache_hit": False,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
    }
