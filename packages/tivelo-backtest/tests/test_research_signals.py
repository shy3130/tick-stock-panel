"""Independent signal plans preserve grouping and reject incomplete evaluation."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from polars.testing import assert_frame_equal
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.research.factor_definitions import load_factor_definitions
from tivelo_backtest.research.intraday import build_feature_frame
from tivelo_backtest.research.signal_plan import compile_daily_signals
from tivelo_backtest.research.signals import (
    SignalFields,
    apply_intraday_edges,
    build_expressions,
    build_expressions_prev,
    build_intraday_expressions,
    inject,
    validate,
)


def definition(**changes):
    result = {
        "id": "rising", "name": "Rising", "kind": "entry",
        "conditions": [{"left": "close", "op": ">", "right": "field:close", "rightDays": 1}],
    }
    result.update(changes)
    return result


def frame():
    return pl.DataFrame({
        "symbol": ["A"] * 4 + ["B"] * 4,
        "date": [1, 2, 3, 4] * 2,
        "close": [10.0, 12.0, 8.0, 10.0, 1.0, 0.5, 1.5, 1.5],
    })


def test_daily_plan_groups_offsets_and_returns_boolean_null_policy():
    source = definition()
    plan = compile_daily_signals([source])
    source["conditions"][0]["op"] = "<"
    result = plan.evaluate(frame())
    assert result["csg_rising"].to_list() == [False, True, False, True, False, False, True, False]
    assert result["csg_rising"].dtype == pl.Boolean
    assert plan.warmup_bars == 2
    assert plan.dependencies["csg_rising"] == frozenset({"close", "symbol"})
    with pytest.raises(TypeError):
        plan.dependencies["changed"] = frozenset()
    assert_frame_equal(frame(), result.drop("csg_rising"))


def test_plan_materializes_factor_graph_and_counts_factor_plus_signal_warmup():
    catalog = load_factor_definitions([
        {"id": "uf_mean", "label": "mean", "formula": "ts_mean(close, 2)"},
        {"id": "uf_ratio", "label": "ratio", "formula": "close / uf_mean"},
    ])
    plan = compile_daily_signals([definition(conditions=[
        {"left": "uf_ratio", "leftDays": 1, "op": ">", "right": 1},
    ])], catalog=catalog)
    assert plan.warmup_bars == 4
    result = plan.evaluate(frame())
    assert result["csg_rising"].to_list() == [False, False, True, False, False, False, False, True]
    assert "uf_ratio" in result.columns
    assert "uf_mean" not in result.columns
    assert not any(name.startswith("__tsfx_") for name in result.columns)


def test_plan_precompiles_python_factors_and_reuses_the_same_numeric_path(monkeypatch):
    from tivelo_backtest.research import scoring, signal_plan

    catalog = load_factor_definitions([{
        "id": "uf_previous", "label": "previous", "lang": "python",
        "formula": 'ctx.col("close").shift(1).over("symbol")',
    }])
    plan = compile_daily_signals([definition(conditions=[
        {"left": "close", "op": ">", "right": "field:uf_previous"},
    ])], catalog=catalog)

    def unexpected_compile(*args, **kwargs):
        raise AssertionError("factor compilation must finish before evaluation")

    monkeypatch.setattr(scoring, "compile_python_expression", unexpected_compile)
    monkeypatch.setattr(signal_plan, "compile_python_expression", unexpected_compile)
    assert plan.warmup_bars == 2
    assert plan.evaluate(frame())["csg_rising"].to_list() == [False, True, False, True, False, False, True, False]


def test_nonfinite_computed_factor_is_not_a_successful_signal():
    catalog = load_factor_definitions([{
        "id": "uf_invalid", "label": "invalid", "lang": "python", "formula": 'ctx.col("close") / 0',
    }])
    plan = compile_daily_signals([definition(conditions=[
        {"left": "uf_invalid", "op": ">", "right": 1},
    ])], catalog=catalog)
    with pytest.raises(TiveloError, match="finite"):
        plan.evaluate(frame())


def test_decimal_numeric_input_is_checked_without_changing_comparison_math():
    plan = compile_daily_signals([definition()])
    decimal_frame = frame().with_columns(pl.col("close").cast(pl.Decimal(20, 4)))
    assert plan.evaluate(decimal_frame)["csg_rising"].to_list() == plan.evaluate(frame())["csg_rising"].to_list()


def test_string_fields_are_explicit_literal_and_isolated():
    strings = {"ext_tags"}
    sig = definition(conditions=[{"left": "ext_tags", "op": "contains", "right": ".*"}])
    plan = compile_daily_signals([sig], string_fields=strings)
    strings.clear()
    data = frame().with_columns(pl.Series("ext_tags", ["AI", "AI.*", None, "x"] * 2))
    assert plan.evaluate(data)["csg_rising"].to_list() == [False, True, False, False] * 2
    with pytest.raises(TiveloError):
        compile_daily_signals([sig])
    fields = SignalFields(allowed={"close"}, string_fields={"ext_tags"})
    validate(sig, fields=fields)
    assert fields.allowed == frozenset({"close", "ext_tags"})


@pytest.mark.parametrize("changes", [
    {"conditions": [{"left": "missing", "op": ">", "right": 1}]},
    {"conditions": [{"left": "close", "op": ">", "right": float("nan")}]},
    {"conditions": [{"left": "close", "op": ">", "right": "inf"}]},
    {"conditions": [{"left": "close", "op": ">", "right": 1, "leftDays": 1.5}]},
    {"conditions": [{"left": "close", "op": ">", "right": 1, "leftDays": True}]},
    {"column": "close"},
    {"column": "symbol"},
    {"id": "invalid\n"},
    {"enabled": "false"},
    {"timeframe": "intraday"},
])
def test_strict_plan_rejects_invalid_or_incompatible_definitions(changes):
    with pytest.raises(TiveloError) as error:
        compile_daily_signals([definition(**changes)])
    assert error.value.code in (ErrorCode.CONTRACT_INVALID, ErrorCode.UNSUPPORTED_CAPABILITY)


def test_plan_rejects_duplicate_ids_and_output_columns():
    with pytest.raises(TiveloError):
        compile_daily_signals([definition(), definition()])
    with pytest.raises(TiveloError):
        compile_daily_signals([definition(column="same"), definition(id="second", column="same")])
    with pytest.raises(TiveloError):
        compile_daily_signals([definition(id=f"signal_{index}") for index in range(257)])


def test_plan_rejects_numeric_string_comparisons_and_grouping_field_overrides():
    with pytest.raises(TiveloError):
        compile_daily_signals([definition(conditions=[
            {"left": "close", "op": ">", "right": "field:ext_tags"},
        ])], string_fields={"ext_tags"})
    with pytest.raises(TiveloError):
        compile_daily_signals([definition()], string_fields={"symbol"})


@pytest.mark.parametrize("data", [
    frame().drop("close"),
    frame().with_columns(pl.lit(float("inf")).alias("close")),
    frame().with_columns(pl.lit("not numeric").alias("close")),
    frame().reverse(),
    pl.concat([frame(), frame().head(1)]).sort(["symbol", "date"]),
    frame().with_columns(pl.lit(None).cast(pl.String).alias("symbol")),
    frame().with_columns(pl.lit(False).alias("csg_rising")),
])
def test_plan_never_publishes_partial_or_ambiguous_signal_columns(data):
    with pytest.raises(TiveloError):
        compile_daily_signals([definition()]).evaluate(data)


def test_disabled_signals_and_known_empty_frames():
    unchanged = compile_daily_signals([definition(enabled=False)]).evaluate(frame())
    assert_frame_equal(unchanged, frame())
    empty = compile_daily_signals([definition()]).evaluate(frame().head(0))
    assert empty.height == 0
    assert empty.schema["csg_rising"] == pl.Boolean


def test_legacy_snapshot_previous_values_match_historical_tail():
    sig = definition()
    historical = inject(frame(), build_expressions([sig]))
    current = pl.DataFrame({
        "symbol": ["A", "B"], "close": [10.0, 1.5], "_prev_close": [8.0, 1.5],
    })
    now = inject(current, build_expressions_prev([sig], {"close"}))
    assert now["csg_rising"].to_list() == historical.filter(pl.col("date") == 4)["csg_rising"].to_list()


def minute_frame():
    times = [datetime(2025, 1, 2, 14, 30, tzinfo=UTC) + timedelta(minutes=i) for i in range(3)]
    times += [datetime(2025, 1, 2, 18, 0, tzinfo=UTC) + timedelta(minutes=i) for i in range(2)]
    return pl.DataFrame({
        "symbol": ["A"] * 5, "datetime": times, "open": [10.0] * 5,
        "high": [10.0] * 5, "low": [10.0] * 5, "close": [10.0] * 5,
        "volume": [1.0, 2.0, 4.0, 8.0, 16.0], "amount": [1000.0, 2000, 4000, 8000, 16000],
    })


def test_intraday_timezone_session_and_cutoff_are_explicit():
    cutoff = datetime(2025, 1, 2, 18, 1, tzinfo=UTC)
    result = build_feature_frame(
        minute_frame(), timezone=ZoneInfo("America/New_York"),
        session_expr=pl.when(pl.col("datetime").dt.hour() >= 13).then(1).otherwise(0),
        cutoff=cutoff,
    )
    assert result["datetime"][0] == datetime(2025, 1, 2, 9, 30)
    assert result.height == 4
    assert result["vwap"].to_list() == [10.0] * 4
    assert result["vol_ratio_1m_today"].to_list() == [None, 2.0, 2.0, None]
    continuous = build_feature_frame(
        minute_frame(), timezone=ZoneInfo("America/New_York"), session_expr=pl.lit(0),
    )
    assert continuous["vol_ratio_1m_today"][3] == 2.0


def test_intraday_edges_reset_by_symbol_and_day():
    sig = definition(timeframe="intraday", conditions=[{"left": "price", "op": ">", "right": 10}])
    data = pl.DataFrame({
        "symbol": ["A"] * 6 + ["B"] * 3,
        "date": [1] * 3 + [2] * 3 + [1] * 3,
        "price": [11.0, 9.0, 11.0] * 3,
    })
    result = apply_intraday_edges(data, build_intraday_expressions([sig]))
    assert result["csgi_rising"].to_list() == [False, False, True] * 3
