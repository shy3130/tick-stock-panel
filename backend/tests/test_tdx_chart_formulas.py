"""TDX source compatibility, independent arithmetic references and drawing contracts.

Reference: https://help.tdx.com.cn/gspt/docs/markdown/redword/functionlist.html
These are deterministic algorithm references, not exported client results.
"""

import math

import numpy as np
import polars as pl
import pytest

from app.indicators.chart import ChartIndicator, evaluate_frame


def indicator(source, **changes):
    return ChartIndicator.model_validate(
        {
            "id": "custom_tdx",
            "name": "自定义源码",
            "syntax": "tdx",
            "source": source,
            "outputs": [],
            "pane": "sub",
            **changes,
        }
    )


def bars(count=80):
    x = np.arange(count, dtype=float)
    close = 10 + x / 20 + np.sin(x / 4)
    return pl.DataFrame(
        {
            "symbol": ["TEST"] * count,
            "date": [f"2026-{i // 28 + 1:02}-{i % 28 + 1:02}" for i in range(count)],
            "time": [f"2026-{i // 28 + 1:02}-{i % 28 + 1:02}" for i in range(count)],
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.4,
            "close": close,
            "volume": 100 + x * 10,
            "amount": (100 + x * 10) * close * 100,
        }
    )


def outputs(source, frame=None, **changes):
    return evaluate_frame(
        indicator(source, **changes), frame if frame is not None else bars(), "daily"
    )


def recursive(values, alpha):
    result = []
    previous = None
    for value in values:
        if value is None or not math.isfinite(value):
            result.append(None)
            continue
        previous = value if previous is None else alpha * value + (1 - alpha) * previous
        result.append(previous)
    return result


def test_pasted_unicode_assignments_comments_operators_and_styles():
    result = outputs(
        "{通达信源码}\n均线:=MA(CLOSE,5);\n输出:IF(C=O OR C<>O,均线,DRAWNULL),COLORRED,LINETHICK2;"
    )
    assert len(result["outputs"]) == 1
    line = result["outputs"][0]
    assert line["label"] == "输出"
    assert line["color"] == "#FF0000"
    assert line["line_width"] == 2
    np.testing.assert_allclose(line["values"][4:], bars()["close"].rolling_mean(5).drop_nulls())


def test_macd_against_independent_recurrence_and_no_future_leakage():
    source = "DIF:EMA(C,12)-EMA(C,26); DEA:EMA(DIF,9); MACD:(DIF-DEA)*2,COLORSTICK;"
    frame = bars(160)
    result = outputs(source, frame)
    fast = recursive(frame["close"].to_list(), 2 / 13)
    slow = recursive(frame["close"].to_list(), 2 / 27)
    dif = [a - b for a, b in zip(fast, slow, strict=True)]
    dea = recursive(dif, 2 / 10)
    expected = [dif, dea, [2 * (a - b) for a, b in zip(dif, dea, strict=True)]]
    for actual, reference in zip(result["outputs"], expected, strict=True):
        np.testing.assert_allclose(actual["values"], reference, atol=1e-12, rtol=1e-12)
    changed = frame.with_columns(
        pl.when(pl.int_range(pl.len()) >= 100).then(999).otherwise(pl.col("close")).alias("close")
    )
    later = outputs(source, changed)
    assert [line["values"][:100] for line in result["outputs"]] == [
        line["values"][:100] for line in later["outputs"]
    ]


def test_zero_windows_dynamic_ref_count_cross_and_bar_history():
    source = "A:SUM(V,0); B:HHV(H,0); N:=IF(C>O,1,2); R:REF(C,N); X:CROSS(C,MA(C,3)); T:COUNT(C>O,3); EVENT:BARSLAST(C<O);"
    frame = bars()
    result = outputs(source, frame)["outputs"]
    np.testing.assert_allclose(result[0]["values"], np.cumsum(frame["volume"]))
    np.testing.assert_allclose(result[1]["values"], np.maximum.accumulate(frame["high"].to_numpy()))
    assert result[2]["values"][0] is None
    np.testing.assert_allclose(result[2]["values"][1:], frame["close"][:-1])
    assert result[4]["values"][2:] == [3.0] * (frame.height - 2)


def test_drawing_statements_emit_time_bound_geometry_and_nodraw_keeps_readout():
    source = "数值:C,NODRAW; STICKLINE(C>O,H,L,2,0),COLORRED; DRAWTEXT(C>O,L,'买入'),COLORGREEN; DRAWICON(C>O,L,1);"
    result = outputs(source)
    assert result["outputs"][0]["nodraw"] is True
    assert {drawing["kind"] for drawing in result["drawings"]} == {"stick", "text", "icon"}
    assert result["drawings"][0]["points"][0]["time"] == bars()["time"][0]
    assert result["drawings"][1]["points"][0]["text"] == "买入"


@pytest.mark.parametrize(
    "source, message",
    [
        ("A:MA(C,5);\nB:UNKNOWN(C);", "第2行"),
        ("A:REF(C,-1);", "未来"),
        ("A:BACKSET(C>O,3);", "未来"),
        ("A:FINANCE(1);", "未接入"),
        ("A:__import__('os');", "不支持"),
    ],
)
def test_unsupported_syntax_and_future_functions_fail_with_location(source, message):
    with pytest.raises(ValueError, match=message):
        outputs(source)


def naive_window(values, n, fn):
    return [fn(values[max(0, i - n + 1) : i + 1]) for i in range(len(values))]


def assert_numbers(actual, expected, atol=1e-10):
    np.testing.assert_allclose(
        np.asarray(actual, dtype=float),
        np.asarray(expected, dtype=float),
        atol=atol,
        rtol=1e-10,
        equal_nan=True,
    )


def test_boll_kdj_rsi_against_independent_scalar_references():
    frame = bars(160)
    close, high, low = [frame[name].to_numpy() for name in ["close", "high", "low"]]
    boll = outputs("MID:MA(C,20); UPPER:MID+2*STD(C,20); LOWER:MID-2*STD(C,20);", frame)["outputs"]
    mean = np.array(naive_window(close, 20, np.mean))
    std = np.array(naive_window(close, 20, lambda a: np.std(a, ddof=1) if len(a) > 1 else np.nan))
    for line, expected in zip(boll, [mean, mean + 2 * std, mean - 2 * std], strict=True):
        assert_numbers(line["values"], expected)
    kdj = outputs(
        "RSV:=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100; K:SMA(RSV,3,1); D:SMA(K,3,1); J:3*K-2*D;", frame
    )["outputs"]
    minima, maxima = np.array(naive_window(low, 9, min)), np.array(naive_window(high, 9, max))
    rsv = (close - minima) / (maxima - minima) * 100
    k = recursive(rsv, 1 / 3)
    d = recursive(k, 1 / 3)
    for line, expected in zip(
        kdj, [k, d, [3 * a - 2 * b for a, b in zip(k, d, strict=True)]], strict=True
    ):
        assert_numbers(line["values"], expected)
    rsi = outputs("LC:=REF(C,1); RSI:SMA(MAX(C-LC,0),6,1)/SMA(ABS(C-LC),6,1)*100;", frame)[
        "outputs"
    ][0]["values"]
    changes = np.r_[np.nan, np.diff(close)]
    gains, magnitude = recursive(np.maximum(changes, 0), 1 / 6), recursive(np.abs(changes), 1 / 6)
    reference = [
        a / b * 100 if a is not None and b else None for a, b in zip(gains, magnitude, strict=True)
    ]
    assert_numbers(rsi, reference)


@pytest.mark.parametrize(
    "function",
    [
        "MA",
        "SUM",
        "STD",
        "STDP",
        "VAR",
        "VARP",
        "AVEDEV",
        "DEVSQ",
        "WMA",
        "HHV",
        "LLV",
        "HHVBARS",
        "LLVBARS",
    ],
)
def test_dynamic_window_and_nulls_against_naive_reference(function):
    values = [10.0, None, 10.0, 9.0, 11.0, 11.0, 8.0, 13.0]
    frame = bars(8).with_columns(pl.Series("volume", values))
    actual = outputs(f"N:=IF(C>11,4,3); X:{function}(V,N);", frame)["outputs"][0]["values"]
    periods = np.where(frame["close"].to_numpy() > 11, 4, 3)
    expected = []
    for i, n in enumerate(periods):
        start = max(0, i - n + 1)
        pairs = [(j, x) for j, x in enumerate(values[start : i + 1], start) if x is not None]
        a = np.array([x for _, x in pairs])
        if function in {"STD", "VAR"} and len(a) < 2:
            expected.append(None)
            continue
        operations = {
            "MA": np.mean(a),
            "SUM": sum(a),
            "STD": np.std(a, ddof=1) if len(a) > 1 else np.nan,
            "STDP": np.std(a),
            "VAR": np.var(a, ddof=1) if len(a) > 1 else np.nan,
            "VARP": np.var(a),
            "AVEDEV": np.mean(np.abs(a - np.mean(a))),
            "DEVSQ": sum((a - np.mean(a)) ** 2),
            "WMA": sum(x * (j - start + 1) for j, x in pairs)
            / sum(j - start + 1 for j, _ in pairs),
            "HHV": max(a),
            "LLV": min(a),
            "HHVBARS": i - max(j for j, x in pairs if x == max(a)),
            "LLVBARS": i - max(j for j, x in pairs if x == min(a)),
        }
        expected.append(operations[function])
    assert_numbers(actual, expected)


def test_stable_variance_regression_and_string_drawing():
    frame = bars(80).with_columns((pl.col("close") + 1e7).alias("close"))
    actual = outputs("S:STDP(C,12); A:SLOPE(C,12); B:FORCAST(C,12);", frame)["outputs"]
    assert_numbers(
        actual[0]["values"], naive_window(frame["close"].to_numpy(), 12, np.std), atol=1e-7
    )
    linear = bars().with_columns((pl.int_range(pl.len()) * 3 + 2).cast(pl.Float64).alias("close"))
    regression = outputs("A:SLOPE(C,12); B:FORCAST(C,12);", linear)["outputs"]
    assert_numbers(regression[0]["values"][1:], [3] * 79)
    assert_numbers(regression[1]["values"][1:], linear["close"].to_list()[1:])
    drawing = outputs("说明:=STRCAT('价格：',CON2STR(C,2)); DRAWTEXT(ISLASTBAR,L,说明);")[
        "drawings"
    ][0]
    assert len(drawing["points"]) == 1
    assert drawing["points"][0]["text"] == f"价格：{bars()['close'][-1]:.2f}"


def test_event_and_logical_functions_match_explicit_sequences():
    frame = bars(8).with_columns(pl.Series("volume", [0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 1.0]))
    source = "A:BARSLAST(V); B:BARSSINCE(V); D:BARSLASTCOUNT(V); E:VALUEWHEN(V,C); F:FILTER(V,2); G:EVERY(V,2); H:EXIST(V,2);"
    result = outputs(source, frame)["outputs"]
    expected = [
        [1, 0, 0, 1, 2, 0, 1, 0],
        [None, 0, 1, 2, 3, 4, 5, 6],
        [0, 1, 2, 0, 0, 1, 0, 1],
        [
            None,
            frame["close"][1],
            frame["close"][2],
            frame["close"][2],
            frame["close"][2],
            frame["close"][5],
            frame["close"][5],
            frame["close"][7],
        ],
        [0, 1, 0, 0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0, 0, 0, 0],
        [0, 1, 1, 1, 0, 1, 1, 1],
    ]
    for line, reference in zip(result, expected, strict=True):
        assert_numbers(line["values"], reference)


@pytest.mark.parametrize(
    "source",
    [
        "A:'text';",
        "A:SMA(C,3,4);",
        "DRAWICON(C>0,L,9);",
        "STICKLINE(C>0,H,L,2,2);",
        "A:MA(C,99999);",
        "A:MA(C,0.5);",
    ],
)
def test_invalid_source_rejected_before_market_data_or_save(source):
    from app.indicators.chart import compile_indicator

    with pytest.raises(ValueError, match="第1行"):
        compile_indicator(indicator(source))


def test_nested_history_and_minute_reset_keep_original_minutes():
    from app.indicators.chart import compile_indicator

    assert compile_indicator(indicator("X:MA(MA(C,10),20);"))[1] >= 28
    frame = bars(6).with_columns(
        pl.Series("date", ["2026-01-01"] * 3 + ["2026-01-02"] * 3),
        pl.Series(
            "time",
            [
                "2026-01-01 11:29:00",
                "2026-01-01 11:30:00",
                "2026-01-01 13:00:00",
                "2026-01-02 09:30:00",
                "2026-01-02 09:31:00",
                "2026-01-02 09:32:00",
            ],
        ),
    )
    result = evaluate_frame(indicator("X:REF(C,1);", reset_daily=True), frame, "minute")["outputs"][
        0
    ]["values"]
    assert result[0] is None and result[3] is None
    assert result[2] == frame["close"][1]


def test_all_drawing_contracts_volume_colors_and_source_styles():
    result = outputs(
        "量:VOL,VOLSTICK; 隐:C,NODRAW; STICKLINE(C>O,H,L,0,-1),COLOR0000FF; DRAWNUMBER(ISLASTBAR,H,C); DRAWBAND(H,RGB(255,0,0),L,RGB(0,255,0)); DRAWKLINE(H,O,L,C); DRAWTEXT_FIX(ISLASTBAR,0,0,0,'说明'); DRAWNUMBER_FIX(ISLASTBAR,1,0,1,C);"
    )
    assert result["outputs"][0]["colors"] == ["#C74040"] * 80
    assert result["drawings"][0]["color"] == "#FF0000"
    assert result["drawings"][0]["width"] == 0
    assert [shape["kind"] for shape in result["drawings"]] == [
        "stick",
        "text",
        "band",
        "candle",
        "fixed_text",
        "fixed_text",
    ]
    assert result["drawings"][-2]["points"][0]["text"] == "说明"
    assert result["drawings"][-2]["x"] == 0 and result["drawings"][-1]["align"] == 1
    assert len(result["drawings"][1]["points"]) == 1
    assert outputs("A:C,LINESTICK; B:C,LINETHICK9,DOTLINE;")["outputs"][0]["style"] == "line_stick"
    assert outputs("A:C; B:C,NODRAW;")["outputs"][1]["nodraw"] is True


def test_text_growth_duplicate_output_and_nonfinite_constants_are_bounded():
    with pytest.raises(ValueError, match="文字"):
        outputs("A:='" + "a" * 200 + "'; B:=STRCAT(A,A); DRAWTEXT(ISLASTBAR,C,B);")
    for source in ["A:C; A:O;", "A:1e309;"]:
        with pytest.raises(ValueError, match="第1行"):
            outputs(source)


def test_service_trims_warmup_drawings_and_recomputes_repaired_history():
    from app.services.chart_indicators import EvaluateRequest, clear_cache, evaluate

    frame = bars(80)

    class History:
        data = frame.head(60)

        def resolve_asset_type(self, symbol):
            return "stock"

        def get_daily_asset(self, *args, columns):
            return self.data.select(columns)

    repo = History()
    rows = [
        {key: value for key, value in row.items() if key not in {"symbol", "date"}}
        for row in frame.tail(20).to_dicts()
    ]
    request = EvaluateRequest(
        symbol="TEST",
        timeframe="daily",
        rows=rows,
        definitions=[indicator("X:MA(C,5); STICKLINE(C>0,H,L,2,0);")],
    )
    clear_cache()
    result = evaluate(request, repo)
    assert not result["errors"]
    assert len(result["results"][0]["drawings"][0]["points"]) == 20
    assert [p["time"] for p in result["results"][0]["drawings"][0]["points"]] == result["times"]
    reference = naive_window(frame["close"].to_numpy(), 5, np.mean)[60:]
    assert_numbers(result["results"][0]["outputs"][0]["values"], reference)
    assert evaluate(request, repo)["cache_hit"] is True
    repo.data = repo.data.with_columns(
        pl.when(pl.int_range(pl.len()) == 59).then(99).otherwise(pl.col("close")).alias("close")
    )
    updated = evaluate(request, repo)
    assert updated["cache_hit"] is False
    assert (
        updated["results"][0]["outputs"][0]["values"][0]
        != result["results"][0]["outputs"][0]["values"][0]
    )


def test_draw_above_circle_style_custom_candles_and_band_null_breaks():
    result = outputs("A:C,CIRCLEDOT; DRAWICON(C>O,L,1),DRAWABOVE; DRAWKLINE(H,O,L,C),COLORBLUE;")
    assert result["outputs"][0]["line_style"] == "circles"
    assert result["drawings"][0]["above"] is True
    assert result["drawings"][1]["points"][0]["color"] == "#0000FF"
    frame = bars(4).with_columns(pl.Series("volume", [1.0, None, 2.0, 3.0]))
    points = outputs("DRAWBAND(V,RGB(255,0,0),C,RGB(0,255,0));", frame)["drawings"][0]["points"]
    assert [point["break_before"] for point in points] == [True, True, False]
    with pytest.raises(ValueError, match="文字栏"):
        outputs("DRAWTEXT(ISLASTBAR,C,'文字'),NODRAW;")
