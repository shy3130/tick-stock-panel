"""Validated single-symbol chart indicators, using the existing factor compiler."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.factors.dsl import BASE_COLUMNS, OPERATORS, CompiledFormula, compile_formula
from app.indicators.tdx_syntax import Program, compile_source

MAX_WARMUP = 4096
MAX_ACTIVE = 10
MAX_DEFINITIONS = 48  # Accommodate all 40 legacy definitions plus the eight presets.
ALIASES = {key.upper(): key for key in BASE_COLUMNS | OPERATORS.keys()}
ALIASES.update(
    {
        "O": "open",
        "H": "high",
        "L": "low",
        "C": "close",
        "V": "volume",
        "VOL": "volume",
        "MA": "ts_mean",
        "EMA": "ts_ema",
        "SMA": "ts_sma",
        "EMA_FIRST": "ts_ema_first",
        "SMA_FIRST": "ts_sma_first",
        "STD": "ts_std",
        "SUM": "ts_sum",
        "HHV": "ts_max",
        "LLV": "ts_min",
        "REF": "ts_delay",
        "IF": "if_else",
    }
)
ALIASES.update({key.lower(): value for key, value in list(ALIASES.items())})
# New function names must not invalidate variables accepted by the v1 editor.
RESERVED_NAMES = {key.upper() for key in ALIASES} - {
    "EMA_FIRST",
    "SMA_FIRST",
    "TS_EMA_FIRST",
    "TS_SMA_FIRST",
}
Identifier = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,23}$")


class ChartModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ChartParameter(ChartModel):
    name: str = Identifier
    value: FiniteFloat = 20
    min: FiniteFloat = 2
    max: FiniteFloat = 512
    integer: bool = True

    @model_validator(mode="after")
    def valid_value(self):
        if not self.min <= self.value <= self.max:
            raise ValueError(f"参数 {self.name} 必须在 {self.min:g}～{self.max:g} 内")
        if self.integer and self.value != int(self.value):
            raise ValueError(f"参数 {self.name} 必须是整数")
        if self.name.upper() in RESERVED_NAMES:
            raise ValueError(f"参数名 {self.name} 与行情字段或函数重名")
        return self


class ChartOutput(ChartModel):
    key: str = Identifier
    label: str = Field(min_length=1, max_length=24)
    formula: str = Field(min_length=1, max_length=2000)
    style: Literal["line", "histogram", "volume"] = "line"
    color: str = Field(default="#3b82f6", pattern=r"^#[0-9a-fA-F]{6}$")
    down_color: str = Field(default="#2D9B65", pattern=r"^#[0-9a-fA-F]{6}$")
    color_mode: Literal["sign", "fixed", "candle"] = "sign"
    hidden: bool = False


class ChartIndicator(ChartModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=40)
    pane: Literal["main", "sub"] = "sub"
    format: Literal["price", "number", "percent", "volume"] = "number"
    precision: int = Field(default=2, ge=0, le=6)
    reset_daily: bool = False
    syntax: Literal["expression", "tdx"] = "expression"
    source: str = Field(default="", max_length=40000)
    parameters: list[ChartParameter] = Field(default_factory=list, max_length=8)
    outputs: list[ChartOutput] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def unique_names(self):
        if self.syntax == "tdx":
            names = [p.name.upper() for p in self.parameters]
            if len(set(names)) != len(names):
                raise ValueError("参数名不能重复")
            return self
        if not self.outputs:
            raise ValueError("指标至少保留一条可见输出")
        names = [p.name.upper() for p in self.parameters] + [o.key.upper() for o in self.outputs]
        if len(names) != len(set(names)):
            raise ValueError("参数名和输出变量名不能重复")
        if any(o.key.upper() in RESERVED_NAMES for o in self.outputs):
            raise ValueError("输出变量不能与行情字段或函数重名")
        if all(o.hidden for o in self.outputs):
            raise ValueError("指标至少保留一条可见输出")
        if self.pane == "main" and (
            self.format != "price" or any(o.style != "line" for o in self.outputs)
        ):
            raise ValueError("主图指标使用价格单位和曲线；其他单位或柱状图请选择副图")
        return self


class VolumeCompareSettings(ChartModel):
    days: int = Field(default=1, ge=1, le=20, strict=True)
    show_labels: bool = Field(default=True, strict=True)
    show_readout: bool = Field(default=True, strict=True)


class ChartSelection(ChartModel):
    id: str
    parameters: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=8)
    visible: bool = Field(default=True, strict=True)
    volume_compare: VolumeCompareSettings | None = None


class ChartLibrary(ChartModel):
    version: Literal[1] = 1
    presets_version: Literal[1] | None = None
    definitions: list[ChartIndicator] = Field(default_factory=list, max_length=MAX_DEFINITIONS)
    views: dict[Literal["daily", "minute"], list[ChartSelection]] = Field(
        default_factory=lambda: {"daily": [], "minute": []}
    )

    @model_validator(mode="after")
    def valid_selections(self):
        ids = [d.id for d in self.definitions]
        if len(ids) != len(set(ids)):
            raise ValueError("指标 ID 不能重复")
        if set(self.views) != {"daily", "minute"}:
            raise ValueError("需要分别保存日K和分时方案")
        for selections in self.views.values():
            chosen = [s.id for s in selections]
            if (
                len(chosen) > MAX_ACTIVE
                or len(chosen) != len(set(chosen))
                or not set(chosen) <= set(ids)
            ):
                raise ValueError(
                    f"每张图指标栏最多显示{MAX_ACTIVE}个指标，且不能重复或引用已删除指标"
                )
            for selection in selections:
                resolve_indicator(
                    next(d for d in self.definitions if d.id == selection.id), selection.parameters
                )
        return self


def resolve_indicator(definition: ChartIndicator, values: dict[str, float]) -> ChartIndicator:
    if not set(values) <= {p.name for p in definition.parameters}:
        raise ValueError("方案引用了不存在的指标参数")
    data = definition.model_dump()
    for parameter in data["parameters"]:
        parameter["value"] = values.get(parameter["name"], parameter["value"])
    return ChartIndicator.model_validate(data)


@lru_cache(maxsize=128)
def _compile(serialized: str) -> tuple[tuple[CompiledFormula | Program, ...], int, frozenset[str]]:
    definition = ChartIndicator.model_validate_json(serialized)
    if definition.syntax == "tdx":
        program = compile_source(
            definition.source,
            tuple((p.name.upper(), float(p.value)) for p in definition.parameters),
        )
        return (program,), program.warmup, program.dependencies
    constants = {
        name: p.value
        for p in definition.parameters
        for name in {p.name, p.name.lower(), p.name.upper()}
    }
    aliases = {**ALIASES, **{p.name: p.name for p in definition.parameters}}
    inputs = set(BASE_COLUMNS)
    warmups: dict[str, int] = {name: 1 for name in inputs}
    roots = {name: {name} for name in inputs}
    result = []
    lookback = 1
    dependencies: set[str] = set()
    for output in definition.outputs:
        compiled = compile_formula(
            output.formula,
            input_columns=frozenset(inputs),
            parameters=constants,
            aliases=aliases,
            call_aliases=ALIASES,
            allow_constant=True,
        )
        if not compiled.ok:
            raise ValueError(
                f"{output.label}：" + "；".join(error.message for error in compiled.errors)
            )
        if compiled.cross_sectional:
            raise ValueError("图表指标只计算当前证券，不支持全市场 rank/zscore/winsorize")
        warmup = max(
            (warmups.get(name, 1) + bars for name, bars in compiled.input_windows), default=1
        )
        if warmup > MAX_WARMUP:
            raise ValueError(f"指标预热超过 {MAX_WARMUP} 根K线，请减小周期或嵌套层数")
        output_roots = set().union(*(roots.get(name, {name}) for name in compiled.dependencies))
        roots[output.key] = output_roots
        dependencies.update(output_roots)
        inputs.add(output.key)
        warmups[output.key] = warmup
        aliases.update({output.key.upper(): output.key, output.key.lower(): output.key})
        lookback = max(lookback, warmup)
        result.append(compiled)
    return tuple(result), lookback - 1, frozenset(dependencies)


def compile_indicator(definition: ChartIndicator):
    return _compile(definition.model_dump_json())


def evaluate_frame(definition: ChartIndicator, frame: pl.DataFrame, timeframe: str) -> dict:
    compiled, _, dependencies = compile_indicator(definition)
    missing = [
        name
        for name in dependencies
        if name not in frame.columns or frame[name].null_count() == frame.height
    ]
    if missing:
        raise ValueError("当前行情缺少字段：" + "、".join(sorted(missing)))
    work = frame
    if definition.reset_daily and timeframe == "minute":
        work = work.with_columns(
            pl.concat_str([pl.col("symbol"), pl.lit(":"), pl.col("date")]).alias("symbol")
        )
    if timeframe == "minute":
        # The shared compiler sorts by symbol/date. Preserve each original
        # minute, before the renderer folds the lunch-break axis position.
        work = work.with_columns(pl.col("time").alias("date"))
    if definition.syntax == "tdx":
        from app.indicators.tdx import evaluate

        groups = work.partition_by("symbol", maintain_order=True)
        group_outputs = []
        drawings = []
        for group in groups:
            result, shapes = evaluate(
                compiled[0],
                group,
                timeframe,
                [(p.name, p.value) for p in definition.parameters],
                definition.precision,
            )
            group_outputs.append(result)
            drawings.extend(shapes)
        outputs = group_outputs[0] if group_outputs else []
        for more in group_outputs[1:]:
            for output, extra in zip(outputs, more, strict=True):
                output["values"].extend(extra["values"])
        return {
            **definition.model_dump(exclude={"parameters", "outputs", "source"}),
            "outputs": outputs,
            "drawings": drawings,
        }
    outputs = []
    for output, formula in zip(definition.outputs, compiled, strict=True):
        evaluated = formula.frame_transform(work)
        if evaluated is None:
            raise ValueError(f"{output.label} 缺少依赖列")
        values = evaluated["__dsl_factor__"].cast(pl.Float64)
        values = values.set(~values.is_finite(), None)
        work = work.with_columns(values.alias(output.key))
        item = {**output.model_dump(exclude={"formula"}), "values": values.to_list()}
        for parameter in definition.parameters:
            item["label"] = item["label"].replace(
                "{" + parameter.name + "}", f"{parameter.value:g}"
            )
        if output.color_mode == "candle" and {"open", "close"} <= set(work.columns):
            item["colors"] = (
                work.select(
                    pl.when(pl.col("close") < pl.col("open"))
                    .then(pl.lit(output.down_color))
                    .otherwise(pl.lit(output.color))
                )
                .to_series()
                .to_list()
            )
        outputs.append(item)
    return {**definition.model_dump(exclude={"parameters", "outputs"}), "outputs": outputs}
