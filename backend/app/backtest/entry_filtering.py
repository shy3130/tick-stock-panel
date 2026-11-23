"""Evaluate market and stock extension filters on candidate signal dates."""

from __future__ import annotations

import math
import operator
from collections import defaultdict
from dataclasses import replace
from datetime import date

import numpy as np
import polars as pl

from app.backtest.entry_inputs import _DATE, _KNOWN, EntryFilters, EntryInputData
from app.factors.ext_factors import ext_column_name

_COMPARISONS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


def _by_table(conditions):
    tables = defaultdict(list)
    for condition in conditions:
        tables[condition.table_id].append(condition)
    return tables.items()


def _conditions(config, frame, rules):
    fields = {field.name: field for field in config.fields}
    expressions = []
    aliases = []
    available = []
    for rule in rules:
        field = fields.get(rule.field)
        column = ext_column_name(config.id, rule.field)
        if field is None or column not in frame.columns:
            raise ValueError(f"扩展表「{config.label}」的字段「{rule.field}」没有可用历史数据")
        value = pl.col(column)
        aliases.append(column)
        available.append(value.is_finite() if field.dtype in ("float", "int") else value.is_not_null())
        if field.dtype == "string":
            if rule.op == "contains":
                expressions.append(value.str.contains(rule.value, literal=True))
            elif rule.op in ("==", "!="):
                expressions.append(_COMPARISONS[rule.op](value, rule.value))
            else:
                raise ValueError(f"文本字段「{rule.field}」请选择等于、不等于或包含")
        elif field.dtype in ("float", "int") and rule.op in _COMPARISONS:
            try:
                threshold = float(rule.value)
            except ValueError as exc:
                raise ValueError(f"扩展字段「{rule.field}」需要有效数值") from exc
            if not math.isfinite(threshold):
                raise ValueError(f"扩展字段「{rule.field}」需要有限数值")
            expressions.append(value.is_finite() & _COMPARISONS[rule.op](value, threshold))
        else:
            raise ValueError(f"扩展字段「{rule.field}」不支持所选比较方式")
    return (
        sorted(set(aliases)), pl.all_horizontal(expressions).fill_null(False),
        pl.all_horizontal(available).fill_null(False),
    )


def _evaluate(inputs, keys, rules, *, market):
    result = pl.Series("entry", np.ones(keys.height, dtype=bool))
    join_keys = [_DATE] if market else [_DATE, "symbol"]
    for table_id, conditions in _by_table(rules):
        config, frame = inputs.table(table_id, market=market)
        if frame.is_empty():
            raise ValueError(f"扩展表「{config.label}」在回测区间内没有历史数据")
        aliases, expression, covered = _conditions(config, frame, conditions)
        if _KNOWN in frame.columns:
            source = frame.select(*join_keys, *aliases, _KNOWN)
            joined = (
                keys.with_row_index("_entry_row")
                .join(source, on=join_keys, how="inner")
                .filter(pl.col(_KNOWN) <= pl.col("decision_at"))
                .sort("_entry_row", _KNOWN, maintain_order=True)
                .unique(subset=["_entry_row"], keep="last", maintain_order=True)
                .select("_entry_row", expression.alias("_pass"), covered.alias("_covered"))
            )
            evaluated = keys.with_row_index("_entry_row").join(
                joined, on="_entry_row", how="left", maintain_order="left",
            )
        else:
            source = frame.select(*join_keys, *aliases).unique(subset=join_keys, keep="last")
            evaluated = keys.join(source, on=join_keys, how="left", maintain_order="left").with_columns(
                expression.alias("_pass"),
                covered.alias("_covered"),
            )
        if market:
            missing = evaluated.filter(~pl.col("_covered").fill_null(False))
            if missing.height:
                day, predecessor = missing.select("date", _DATE).row(0)
                if predecessor is None:
                    raise ValueError(f"市场交易日历缺少 {day} 的上一交易日")
                raise ValueError(
                    f"扩展表「{config.label}」缺少 {predecessor} 在 {day} 入场前可用的市场数据"
                )
        result = result & evaluated["_pass"].fill_null(False)
    return result


def market_pass(inputs: EntryInputData, dates: pl.Series, filters: EntryFilters) -> pl.Series:
    keys = pl.DataFrame({"date": dates}).join(
        inputs.calendar(), on="date", how="left", maintain_order="left",
    ).rename({"previous_date": _DATE})
    return _evaluate(inputs, keys, filters.market, market=True)


def stock_pass(inputs: EntryInputData, keys: pl.DataFrame, filters: EntryFilters) -> pl.Series:
    # Evaluate on the strategy's daily decision, irrespective of a next-open fill.
    keys = keys.select(
        "symbol",
        pl.col("date").cast(pl.Date).alias(_DATE),
        (
            pl.col("date").cast(pl.Date).cast(pl.Datetime("us")) + pl.duration(hours=15)
        ).dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone("UTC").alias("decision_at"),
    )
    return _evaluate(inputs, keys, filters.stocks, market=False)


def apply_matrix_entry_filter(inputs, market, signals, filters, start: date, end: date):
    dates = pl.Series("date", [date.fromisoformat(label[:10]) for label in market.timestamp_labels])
    formal = (dates >= start) & (dates <= end)
    entry = signals.entry.copy()
    if filters.market:
        allowed = market_pass(inputs, dates.filter(formal), filters)
        entry[np.flatnonzero(formal.to_numpy())[~allowed.to_numpy()]] = 0
    if filters.stocks:
        times, assets = np.nonzero(entry)
        if len(times):
            keys = pl.DataFrame({
                "date": dates.gather(times),
                "symbol": np.asarray(market.symbols, dtype=str)[assets],
            })
            allowed = stock_pass(inputs, keys, filters).to_numpy()
            entry[times[~allowed], assets[~allowed]] = 0
    entry.flags.writeable = False
    return replace(signals, entry=entry)


def panel_entry_filter(
    inputs: EntryInputData, panel: pl.DataFrame, formal: pl.Series, gate: pl.Series, filters: EntryFilters,
) -> pl.Series:
    # Evaluate each market date once; stock rules read only existing candidate rows.
    panel = panel.select(pl.col("date").cast(pl.Date), "symbol")
    if filters.market:
        dates = panel.filter(formal)["date"].unique().sort()
        accepted = dates.filter(market_pass(inputs, dates, filters))
        gate = gate & panel["date"].is_in(accepted.implode())
    if filters.stocks and gate.any():
        filtered = stock_pass(inputs, panel.filter(gate), filters)
        result = gate.to_numpy().copy()
        result[result] = filtered.to_numpy()
        gate = pl.Series("entry", result)
    return gate
