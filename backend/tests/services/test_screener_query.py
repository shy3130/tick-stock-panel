from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from pydantic import ValidationError

from app.services import screener as screener_module
from app.services import screener_query as screener_query_module
from app.services.screener_query import (
    ALLOWED_OPS,
    QueryService,
    ScreenerDataUnavailableError,
    ScreenerQueryRequest,
    ScreenerSemanticError,
    _materialize,
    _enriched_columns_for,
    compile_predicate,
    execute_query,
    field_metadata,
    validate_query,
)


class _Repo:
    def __init__(self):
        self.store = SimpleNamespace(data_dir=".")

    def get_instruments(self):
        return pl.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ", "600002.SH"],
                "name": ["甲", "ST乙", "甲旧"],
                "total_shares": [100.0, 100.0, 99.0],
                "float_shares": [50.0, 50.0, 49.0],
            }
        )


class _Service:
    def __init__(self, repo):
        self.repo = repo

    def latest_date(self):
        return date(2026, 7, 16)

    def _load_enriched_for_date(self, as_of, columns=None):
        return pl.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ", "600002.SH"],
                "date": [as_of, as_of, as_of],
                "close": [10.0, 20.0, 11.0],
                "change_pct": [0.1, 0.1, 0.2],
                "ma5": [9.0, 20.0, None],
                "ma10": [8.0, 19.0, None],
                "ma20": [7.0, 18.0, None],
                "ma60": [6.0, 17.0, None],
                "vol_ratio_5d": [2.0, 1.0, 3.0],
            }
        )


def test_registry_metadata_and_deprecated_names():
    metadata = field_metadata()
    assert {"field", "label", "group", "source", "unit", "value_type", "null_policy", "availability", "ops", "sortable", "options"} == set(metadata[0])
    names = {item["field"] for item in metadata}
    assert {"pb", "main_fund_flow", "ttm"}.isdisjoint(names)
    assert next(item for item in metadata if item["field"] == "board")["options"][0]["value"] == "sh_main"
    expected = {
        "close": ("market", "元"),
        "float_market_cap": ("market_cap", "亿元"),
        "consecutive_limit_ups": ("limit_up", "次"),
        "yo_y_profit": ("financial", "%"),
        "basic_eps": ("financial", "元"),
        "pe_approx": ("financial", "倍"),
        "board": ("filter", None),
    }
    by_name = {item["field"]: item for item in metadata}
    for field, (group, unit) in expected.items():
        assert (by_name[field]["group"], by_name[field]["unit"]) == (group, unit)


def test_enriched_projection_excludes_external_snapshot_fields():
    assert _enriched_columns_for(
        {
            "close",
            "change_pct",
            "chip_profit_ratio",
            "main_net_inflow",
            "financing_balance",
            "lhb_count_30d",
        }
    ) == ["change_pct", "close"]
    assert _enriched_columns_for(
        {"above_ma20", "close", "change_pct"}
    ) == ["change_pct", "close", "ma20"]

def test_public_compiler_and_query_service_contract(monkeypatch):
    assert {">", "<", ">=", "<=", "=", "!=", "between", "in"} == ALLOWED_OPS
    expression, applied, order = compile_predicate(
        [{"field": "change_pct", "op": ">", "value": 0.05}],
        {"field": "change_pct", "direction": "desc"},
    )
    assert isinstance(expression, pl.Expr)
    assert applied == [{"field": "change_pct", "op": ">", "value": 0.05}]
    assert order.field == "change_pct"
    assert validate_query(
        ScreenerQueryRequest(
            conditions=[{"field": "change_pct", "op": ">", "value": 0}],
            order_by={"field": "above_ma5", "direction": "desc"},
        )
    )[1].field == "above_ma5"

    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    result = QueryService(_Repo()).query(
        ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": ">", "value": 0.05}])
    )
    assert result["total"] == 3


def test_bounds_types_and_semantic_operator_errors():
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": "between", "value": [1]}])
    with pytest.raises(ScreenerSemanticError) as exc:
        validate_query(ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": "contains", "value": 1}]))
    assert exc.value.reason == "unsupported_operator"
    with pytest.raises(ScreenerSemanticError):
        validate_query(ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": ">", "value": True}]))


def test_query_deduplicates_and_applies_literal_predicates(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[{"field": "above_ma5", "op": "=", "value": True}],
            order_by={"field": "change_pct", "direction": "desc"},
        ),
    )
    assert set(result) == {"rows", "total", "applied", "as_of", "elapsed_ms"}
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"
    assert result["rows"][0]["change_pct"] == 0.1


def test_null_never_matches_not_equal(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(conditions=[{"field": "above_ma5", "op": "!=", "value": False}]),
    )
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"


def test_nan_and_infinity_never_match_numeric_predicates(monkeypatch):
    frame = pl.DataFrame(
        {
            "symbol": ["nan", "pos-inf", "neg-inf", "finite"],
            "date": [date(2026, 7, 16)] * 4,
            "close": [10.0] * 4,
            "change_pct": [float("nan"), float("inf"), float("-inf"), 0.1],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": ">", "value": 0}]),
    )
    assert [row["symbol"] for row in result["rows"]] == ["finite"]


def test_non_finite_numeric_sort_values_are_nulls_last(monkeypatch):
    frame = pl.DataFrame(
        {
            "symbol": ["nan", "pos-inf", "neg-inf", "finite"],
            "date": [date(2026, 7, 16)] * 4,
            "close": [10.0] * 4,
            "change_pct": [float("nan"), float("inf"), float("-inf"), 0.1],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[{"field": "close", "op": ">", "value": 0}],
            order_by={"field": "change_pct", "direction": "desc"},
        ),
    )
    assert result["rows"][0]["symbol"] == "finite"
    assert all(row["change_pct"] is None for row in result["rows"][1:])


def test_pre_materialized_alias_does_not_require_source_dependencies(monkeypatch):
    frame = pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "date": [date(2026, 7, 16)],
            "close": [10.0],
            "change_pct": [0.1],
            "float_market_cap": [12.0],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    class Repo(_Repo):
        def get_instruments(self):
            return pl.DataFrame({"symbol": ["600001.SH"], "name": ["甲"]})

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    result = execute_query(
        Repo(),
        ScreenerQueryRequest(conditions=[{"field": "float_market_cap", "op": ">", "value": 10}]),
    )
    assert result["total"] == 1


def test_historical_query_rejects_current_only_instrument_fields(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    with pytest.raises(ScreenerDataUnavailableError) as exc:
        execute_query(
            _Repo(),
            ScreenerQueryRequest(
                conditions=[{"field": "float_market_cap", "op": ">", "value": 10}],
                as_of=date(2026, 7, 15),
            ),
        )
    assert exc.value.fields == ["float_market_cap"]


def test_all_ma_aliases_are_strict_and_null_safe(monkeypatch):
    frame = pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
            "date": [date(2026, 7, 16)] * 4,
            "close": [10.0, 10.0, 10.0, 5.0],
            "change_pct": [0.04, 0.05, 0.06, 0.07],
            "ma5": [9.0, 10.0, None, 6.0],
            "ma10": [8.0, 10.0, None, 7.0],
            "ma20": [7.0, 10.0, None, 8.0],
            "ma60": [6.0, 10.0, None, 9.0],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    for field in ("above_ma5", "above_ma10", "above_ma20", "above_ma60"):
        result = execute_query(_Repo(), ScreenerQueryRequest(conditions=[{"field": field, "op": "=", "value": True}]))
        assert result["total"] == 1
        assert result["rows"][0]["symbol"] == "600001.SH"
    for field in ("below_ma5", "below_ma10", "below_ma20", "below_ma60"):
        result = execute_query(_Repo(), ScreenerQueryRequest(conditions=[{"field": field, "op": "=", "value": True}]))
        assert result["total"] == 1
        assert result["rows"][0]["symbol"] == "600004.SH"
    result = execute_query(_Repo(), ScreenerQueryRequest(conditions=[{"field": "ma_bullish_alignment", "op": "=", "value": True}]))
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"


def test_board_regex_and_st_exclusion(monkeypatch):
    symbols = ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH", "830001.BJ", "700001.SH"]
    frame = pl.DataFrame({"symbol": symbols, "date": [date(2026, 7, 16)] * len(symbols), "close": [10.0] * 6, "change_pct": [0.1] * 6})
    names = ["normal", "TEST", "*STX", "S*ST", "退市A", "正常"]

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    class Repo(_Repo):
        def get_instruments(self):
            return pl.DataFrame({"symbol": symbols, "name": names})

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    for symbol, board in zip(symbols[:5], ("sh_main", "sz_main", "chinext", "star", "bse"), strict=True):
        result = execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "board", "op": "=", "value": board}]))
        assert result["rows"][0]["symbol"] == symbol
    malformed = execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "board", "op": "=", "value": "sh_main"}]))
    assert malformed["total"] == 1
    st_free = execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "exclude_st", "op": "=", "value": True}]))
    assert [row["symbol"] for row in st_free["rows"]] == ["600001.SH", "700001.SH"]


def test_change_pct_caps_and_derived_sort(monkeypatch):
    frame = pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH"],
            "date": [date(2026, 7, 16)] * 2,
            "close": [10.0, 20.0],
            "change_pct": [0.04, 0.06],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    class Repo(_Repo):
        def get_instruments(self):
            return pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "float_shares": [50_000_000.0, 100_000_000.0], "total_shares": [100_000_000.0, 100_000_000.0]})

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    result = execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": "between", "value": [0.04, 0.06]}], order_by={"field": "float_market_cap", "direction": "desc"}))
    assert result["total"] == 2
    assert result["rows"][0]["symbol"] == "600002.SH"
    assert result["rows"][0]["float_market_cap"] == 20.0


def test_instrument_duplicates_coalesce_each_column_without_row_multiplication(monkeypatch):
    class Repo(_Repo):
        def get_instruments(self):
            return pl.DataFrame(
                {
                    "symbol": ["600001.SH", "600001.SH"],
                    "name": [None, "正常"],
                    "total_shares": [100_000_000.0, None],
                    "float_shares": [None, 50_000_000.0],
                }
            )

    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    result = execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "float_market_cap", "op": ">", "value": 4}]))
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"


def test_technical_query_does_not_touch_financial_source(monkeypatch):
    called = False

    def fail_loader(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("financial source should not be loaded")

    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    import app.services.screener_financials as financials

    monkeypatch.setattr(financials, "load_financial_snapshot", fail_loader)
    execute_query(_Repo(), ScreenerQueryRequest(conditions=[{"field": "close", "op": ">", "value": 0}]))
    assert not called


def test_financial_point_in_time_raw_eps_and_approximate_ratios(monkeypatch, tmp_path):
    metrics = tmp_path / "financials" / "metrics"
    metrics.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH", "600003.SH", "600001.SH"],
            "report_year": [2025, 2025, 2025, 2025],
            "quarter": ["2025Q4", "2025Q4", "2025Q4", "2025Q3"],
            "notice_date": ["2026-01-01", "2026-01-01", "2026-08-01", "2025-11-01"],
            "basic_eps": [2.0, -1.0, 9.0, 1.5],
            "bps": [10.0, 0.0, 10.0, 8.0],
            "weight_avg_roe": [0.1, 0.2, 0.3, 0.08],
            "gross_margin": [0.2, 0.3, 0.4, 0.18],
            "industry": ["A", "B", "C", "A"],
            "yo_y_profit": [0.4, 0.5, 0.6, 0.2],
        }
    ).write_parquet(metrics / "part.parquet")
    frame = pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH", "600003.SH"],
            "date": [date(2026, 7, 16)] * 3,
            "close": [20.0, 20.0, 20.0],
            "change_pct": [0.1, 0.1, 0.1],
            "basic_eps": [999.0, 999.0, 999.0],
            "roe": [999.0, 999.0, 999.0],
            "pe_approx": [999.0, 999.0, 999.0],
            "pb_approx": [999.0, 999.0, 999.0],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    class Repo(_Repo):
        def __init__(self):
            self.store = SimpleNamespace(data_dir=tmp_path)

        def get_instruments(self):
            return pl.DataFrame({"symbol": frame["symbol"], "name": ["A", "B", "C"]})

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    result = execute_query(
        Repo(),
        ScreenerQueryRequest(
            conditions=[
                {"field": "pe_approx", "op": "!=", "value": 99},
                {"field": "pb_approx", "op": "!=", "value": 99},
            ]
        ),
    )
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"
    assert result["rows"][0]["pe_approx"] == 10.0
    assert result["rows"][0]["pb_approx"] == 2.0
    raw_eps = execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "basic_eps", "op": "=", "value": -1}]))
    assert raw_eps["rows"][0]["symbol"] == "600002.SH"
    assert execute_query(Repo(), ScreenerQueryRequest(conditions=[{"field": "pe_approx", "op": "=", "value": 0}]))["total"] == 0


def test_structural_bounds_and_unknown_enum_are_rejected():
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": "in", "value": []}])
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": "in", "value": list(range(51))}])
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(conditions=[{"field": "change_pct", "op": ">", "value": 1}], limit=501)
    with pytest.raises(ScreenerSemanticError) as exc:
        validate_query(ScreenerQueryRequest(conditions=[{"field": "board", "op": "=", "value": "unknown"}]))
    assert exc.value.reason == "invalid_value"


# --------------------------------------------------------------------------- #
# 60日价格位置 / 距新高 / 参考标记(AH·沪深股通·上市天数)
# --------------------------------------------------------------------------- #


def test_price_position_and_distance_materialize():
    frame = pl.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "close": [11.0, 12.0, 10.0],
            "high_60d": [12.0, 12.0, None],
            "low_60d": [10.0, 10.0, None],
        }
    )
    out = _materialize(frame, {"price_position_60d", "distance_to_60d_high"})
    rows = {r["symbol"]: r for r in out.to_dicts()}
    # A: (11-10)/(12-10)*100 = 50；距新高 (11/12-1)*100
    assert rows["A"]["price_position_60d"] == 50.0
    assert abs(rows["A"]["distance_to_60d_high"] - (-100 / 12)) < 1e-9
    # B: 区间顶点 → 位置 100，距新高 0
    assert rows["B"]["price_position_60d"] == 100.0
    assert rows["B"]["distance_to_60d_high"] == 0.0
    # C: 缺 60 日高低 → null 不伪造
    assert rows["C"]["price_position_60d"] is None
    assert rows["C"]["distance_to_60d_high"] is None


def test_price_position_zero_span_is_null():
    frame = pl.DataFrame({"close": [10.0], "high_60d": [10.0], "low_60d": [10.0]})
    out = _materialize(frame, {"price_position_60d"})
    assert out["price_position_60d"][0] is None


def test_p0_derived_indicators_materialize_with_null_guards():
    frame = pl.DataFrame(
        {
            "symbol": ["A", "B", "C", "D"],
            "close": [100.0, 100.0, 0.0, 100.0],
            "atr_14": [2.0, 1.0, 1.0, 1.0],
            "ma20": [95.0, 0.0, 100.0, 100.0],
            "boll_upper": [105.0, 100.0, 101.0, 99.0],
            "boll_lower": [85.0, 100.0, 99.0, 101.0],
        }
    )
    out = _materialize(
        frame,
        {"atr_pct_14", "distance_to_ma20", "boll_band_width_20", "boll_position_20"},
    )
    rows = {row["symbol"]: row for row in out.to_dicts()}

    assert rows["A"]["atr_pct_14"] == 2.0
    assert abs(rows["A"]["distance_to_ma20"] - (100 / 95 - 1) * 100) < 1e-9
    assert abs(rows["A"]["boll_band_width_20"] - (20 / 95 * 100)) < 1e-9
    assert rows["A"]["boll_position_20"] == 75.0

    # 零 MA / 零带宽 / 零收盘价 / 损坏带宽均不能产出伪数值。
    assert rows["B"]["distance_to_ma20"] is None
    assert rows["B"]["boll_band_width_20"] is None
    assert rows["B"]["boll_position_20"] is None
    assert rows["C"]["atr_pct_14"] is None
    assert rows["D"]["boll_band_width_20"] is None
    assert rows["D"]["boll_position_20"] is None


def test_p0_signal_aliases_filter_and_project(monkeypatch):
    frame = pl.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ", "600002.SH"],
            "date": [date(2026, 7, 16)] * 3,
            "close": [10.0, 20.0, 11.0],
            "change_pct": [0.1, 0.1, 0.2],
            "signal_n_day_high": [True, True, False],
            "signal_volume_surge": [True, False, True],
            "signal_broken_limit_up": [False, False, True],
            "signal_ma20_breakdown": [False, False, True],
        }
    )

    class Service(_Service):
        def _load_enriched_for_date(self, as_of, columns=None):
            return frame

    monkeypatch.setattr(screener_module, "ScreenerService", Service)
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[
                {"field": "n_day_high", "op": "=", "value": True},
                {"field": "volume_surge", "op": "=", "value": True},
                {"field": "broken_limit_up", "op": "=", "value": False},
                {"field": "ma20_breakdown", "op": "=", "value": False},
            ],
            order_by={"field": "n_day_high", "direction": "desc"},
        ),
    )

    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"
    assert result["rows"][0]["n_day_high"] is True
    assert result["rows"][0]["volume_surge"] is True
    assert result["rows"][0]["broken_limit_up"] is False
    assert result["rows"][0]["ma20_breakdown"] is False


def _fake_flags_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ"],
            "is_ah": [True, False],
            "ah_premium": [50.5, None],
            "hk_connect": [True, False],
            "listing_date": [date(2020, 1, 1), date(1991, 4, 3)],
        }
    )


def test_reference_join_filters_ah_and_computes_listing_days(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(screener_query_module, "_get_reference_flags", _fake_flags_df)
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[
                {"field": "is_ah", "op": "=", "value": True},
                {"field": "hk_connect", "op": "=", "value": True},
                {"field": "listing_days", "op": "<", "value": 10000},
            ],
            order_by={"field": "ah_premium", "direction": "desc"},
        ),
    )
    # 600001.SH 是唯一 AH 股；600002.SH join 缺失(is_ah null → 不匹配)
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["symbol"] == "600001.SH"
    assert row["ah_premium"] == 50.5
    assert row["is_ah"] is True
    assert row["hk_connect"] is True
    assert row["listing_days"] == (date(2026, 7, 16) - date(2020, 1, 1)).days


def test_reference_join_missing_provider_data_raises(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(screener_query_module, "_get_reference_flags", lambda: pl.DataFrame())
    with pytest.raises(ScreenerDataUnavailableError) as exc:
        execute_query(
            _Repo(),
            ScreenerQueryRequest(conditions=[{"field": "hk_connect", "op": "=", "value": True}]),
        )
    assert exc.value.fields == ["hk_connect"]


def test_reference_fields_rejected_for_historical_as_of(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(screener_query_module, "_get_reference_flags", _fake_flags_df)
    with pytest.raises(ScreenerDataUnavailableError) as exc:
        execute_query(
            _Repo(),
            ScreenerQueryRequest(
                conditions=[{"field": "ah_premium", "op": "<", "value": 100}],
                as_of=date(2026, 7, 15),
            ),
        )
    assert exc.value.fields == ["ah_premium"]


def test_new_fields_registry_metadata():
    by_name = {item["field"]: item for item in field_metadata()}
    reference = ["is_ah", "ah_premium", "hk_connect", "listing_days"]
    for field in reference:
        spec = by_name[field]
        assert spec["group"] == "reference"
        assert spec["availability"] == "available"
    assert by_name["is_ah"]["ops"] == ["=", "!="]
    assert by_name["ah_premium"]["unit"] == "%"
    assert by_name["price_position_60d"]["group"] == "technical"
    assert by_name["amplitude"]["group"] == "market"


def test_p0_fields_registry_metadata():
    by_name = {item["field"]: item for item in field_metadata()}
    numeric = {
        "atr_pct_14": ("technical", "%"),
        "distance_to_ma20": ("technical", "%"),
        "boll_band_width_20": ("technical", "%"),
        "boll_position_20": ("technical", "%, 100=上轨"),
    }
    for field, (group, unit) in numeric.items():
        spec = by_name[field]
        assert (spec["group"], spec["unit"], spec["value_type"]) == (group, unit, "numeric")

    boolean = {
        "volume_surge": "market",
        "ma20_breakdown": "technical",
        "n_day_high": "technical",
        "broken_limit_up": "limit_up",
    }
    for field, group in boolean.items():
        spec = by_name[field]
        assert (spec["group"], spec["ops"], spec["value_type"]) == (group, ["=", "!="], "boolean")


def _fake_lhb_records_df() -> pl.DataFrame:
    # as_of=2026-07-16; 窗口起点: 30d=06-16, 90d=04-17, 180d=01-17
    return pl.DataFrame(
        {
            "symbol": [
                "600001.SH", "600001.SH",  # 30d 窗口内 2 次
                "000001.SZ", "000001.SZ",  # 90d 内 1 次(06-01), 180d 内另 1 次(04-10)
            ],
            "trade_date": [
                date(2026, 7, 15), date(2026, 7, 1),
                date(2026, 6, 1), date(2026, 4, 10),
            ],
        }
    )


def test_lhb_join_counts_and_days_since_last(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module, "_get_lhb_records", lambda s, e: _fake_lhb_records_df()
    )
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[
                {"field": "lhb_count_30d", "op": ">=", "value": 2},
                {"field": "lhb_count_90d", "op": ">=", "value": 2},
            ],
            order_by={"field": "lhb_days_since_last", "direction": "asc"},
        ),
    )
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["symbol"] == "600001.SH"
    assert row["lhb_count_30d"] == 2
    assert row["lhb_count_90d"] == 2
    assert row["lhb_days_since_last"] == 1


def test_lhb_zero_count_matches_unlisted_and_null_days_excluded(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module, "_get_lhb_records", lambda s, e: _fake_lhb_records_df()
    )
    # 000001.SZ: 30 天 0 次且距最近上榜 45 天 → 命中;
    # 600002.SH: 0 次但距最近上榜 null(180 天回看无记录) → 数值条件不匹配;
    # 600001.SH: 30 天 2 次 → 不命中。
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[
                {"field": "lhb_count_30d", "op": "=", "value": 0},
                {"field": "lhb_days_since_last", "op": "<=", "value": 45},
            ]
        ),
    )
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "000001.SZ"
    assert result["rows"][0]["lhb_count_30d"] == 0
    assert result["rows"][0]["lhb_days_since_last"] == 45


def test_lhb_historical_as_of_supported(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    calls: list[tuple[date, date]] = []

    def fake_loader(start: date, end: date) -> pl.DataFrame:
        calls.append((start, end))
        return _fake_lhb_records_df()

    monkeypatch.setattr(screener_query_module, "_get_lhb_records", fake_loader)
    # 龙虎榜按 as_of 回看窗口聚合, 历史 as_of 不应像 reference 字段一样 503
    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[{"field": "lhb_days_since_last", "op": "<=", "value": 30}],
            as_of=date(2026, 7, 15),
        ),
    )
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600001.SH"
    assert result["rows"][0]["lhb_days_since_last"] == 0
    # loader 收到以 as_of 为终点的 180 天回看窗口
    assert calls == [(date(2026, 1, 16), date(2026, 7, 15))]


def test_lhb_missing_provider_data_raises(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module, "_get_lhb_records", lambda s, e: pl.DataFrame()
    )
    with pytest.raises(ScreenerDataUnavailableError) as exc:
        execute_query(
            _Repo(),
            ScreenerQueryRequest(
                conditions=[{"field": "lhb_count_90d", "op": ">=", "value": 1}]
            ),
        )
    assert exc.value.fields == ["lhb_count_90d"]


def test_lhb_fields_registry_metadata():
    by_name = {item["field"]: item for item in field_metadata()}
    expected = {
        "lhb_days_since_last": "天",
        "lhb_count_30d": "次",
        "lhb_count_90d": "次",
        "lhb_count_180d": "次",
    }
    for field, unit in expected.items():
        spec = by_name[field]
        assert (spec["group"], spec["unit"], spec["value_type"], spec["availability"]) == (
            "lhb", unit, "numeric", "available",
        )


def test_special_snapshot_fields_registry_metadata():
    by_name = {item["field"]: item for item in field_metadata()}
    expected = {
        "chip_profit_ratio": ("chip", "%"),
        "chip_avg_cost_deviation": ("chip", "%"),
        "chip_concentration_90": ("chip", "%"),
        "chip_peak_count": ("chip", "个"),
        "main_net_inflow": ("moneyflow", "元"),
        "main_net_inflow_ratio": ("moneyflow", "%"),
        "super_large_net_inflow": ("moneyflow", "元"),
        "lhb_institution_count_20d": ("lhb", "次"),
        "lhb_institution_net_buy_20d": ("lhb", "元"),
        "financing_balance": ("margin", "万元"),
        "financing_net_buy": ("margin", "万元"),
        "financing_net_buy_5d": ("margin", "万元"),
    }
    for field, (group, unit) in expected.items():
        spec = by_name[field]
        assert (spec["group"], spec["unit"], spec["availability"]) == (
            group, unit, "available",
        )


def test_chip_and_moneyflow_snapshot_fields_filter_and_project(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module,
        "_get_chip_snapshot",
        lambda _as_of: pl.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ", "600002.SH"],
                "chip_profit_ratio": [70.0, 20.0, 85.0],
                "chip_avg_cost": [8.0, 25.0, 0.0],
                "chip_concentration_90": [12.0, 50.0, 8.0],
                "chip_peak_count": [3, 5, 1],
            }
        ),
    )
    monkeypatch.setattr(
        screener_query_module,
        "_get_moneyflow_snapshot",
        lambda _as_of: pl.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ", "600002.SH"],
                "moneyflow_total_amount": [1_000.0, 1_000.0, 1_000.0],
                "main_net_inflow": [120.0, -20.0, 200.0],
                "super_large_net_inflow": [60.0, 0.0, 80.0],
                "valid_count": [1, 1, 1],
                "invalid_count": [0, 0, 0],
            }
        ),
    )

    result = execute_query(
        _Repo(),
        ScreenerQueryRequest(
            conditions=[
                {"field": "chip_profit_ratio", "op": ">=", "value": 50},
                {"field": "chip_avg_cost_deviation", "op": ">=", "value": 20},
                {"field": "main_net_inflow_ratio", "op": ">=", "value": 10},
                {"field": "super_large_net_inflow", "op": ">=", "value": 50},
            ],
            order_by={"field": "chip_concentration_90", "direction": "asc"},
        ),
    )

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["symbol"] == "600001.SH"
    assert row["chip_profit_ratio"] == 70.0
    assert row["chip_avg_cost_deviation"] == 25.0
    assert row["main_net_inflow_ratio"] == 12.0
    assert row["super_large_net_inflow"] == 60.0
    assert row["chip_concentration_90"] == 12.0


def test_moneyflow_snapshot_rejects_unvalidated_rows(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module,
        "_get_moneyflow_snapshot",
        lambda _as_of: pl.DataFrame(
            {
                "symbol": ["600001.SH"],
                "moneyflow_total_amount": [1_000.0],
                "main_net_inflow": [120.0],
                "super_large_net_inflow": [60.0],
                "valid_count": [0],
                "invalid_count": [1],
            }
        ),
    )

    with pytest.raises(ScreenerDataUnavailableError) as exc:
        execute_query(
            _Repo(),
            ScreenerQueryRequest(
                conditions=[{"field": "main_net_inflow", "op": ">", "value": 0}]
            ),
        )
    assert exc.value.fields == ["main_net_inflow"]


def test_lhb_institution_and_margin_stats_support_historical_as_of(monkeypatch):
    class Repo(_Repo):
        def get_enriched_range(self, _start, _end, columns):
            assert columns == ["date"]
            return pl.DataFrame(
                {
                    "date": [
                        date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13),
                        date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16),
                    ]
                }
            )

    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module,
        "_get_lhb_institution_records",
        lambda _start, _end: pl.DataFrame(
            {
                "symbol": ["600001.SH", "600001.SH", "000001.SZ"],
                "trade_date": [date(2026, 7, 15), date(2026, 7, 8), date(2026, 7, 15)],
                "net_buy_amount": [200.0, -50.0, 500.0],
            }
        ),
    )
    monkeypatch.setattr(
        screener_query_module,
        "_get_margin_records",
        lambda _start, _end: pl.DataFrame(
            {
                "symbol": ["600001.SH"] * 5 + ["000001.SZ"],
                "trade_date": [
                    date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13),
                    date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 15),
                ],
                "financing_balance": [90.0, 92.0, 95.0, 97.0, 100.0, 80.0],
                "financing_net_buy": [1.0, 2.0, 3.0, 4.0, 5.0, 20.0],
            }
        ),
    )

    result = execute_query(
        Repo(),
        ScreenerQueryRequest(
            as_of=date(2026, 7, 16),
            conditions=[
                {"field": "lhb_institution_count_20d", "op": ">=", "value": 2},
                {"field": "lhb_institution_net_buy_20d", "op": ">=", "value": 100},
                {"field": "financing_balance", "op": ">=", "value": 90},
                {"field": "financing_net_buy_5d", "op": "=", "value": 15},
            ],
            order_by={"field": "financing_net_buy", "direction": "desc"},
        ),
    )

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["symbol"] == "600001.SH"
    assert row["lhb_institution_count_20d"] == 2
    assert row["lhb_institution_net_buy_20d"] == 150.0
    assert row["financing_balance"] == 100.0
    assert row["financing_net_buy"] == 5.0
    assert row["financing_net_buy_5d"] == 15.0


def test_margin_stats_rejects_more_than_one_trading_day_staleness(monkeypatch):
    class Repo(_Repo):
        def get_enriched_range(self, _start, _end, columns):
            assert columns == ["date"]
            return pl.DataFrame(
                {
                    "date": [
                        date(2026, 7, 13), date(2026, 7, 14),
                        date(2026, 7, 15), date(2026, 7, 16),
                    ]
                }
            )

    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    monkeypatch.setattr(
        screener_query_module,
        "_get_margin_records",
        lambda _start, _end: pl.DataFrame(
            {
                "symbol": ["600001.SH"],
                "trade_date": [date(2026, 7, 13)],
                "financing_balance": [90.0],
                "financing_net_buy": [1.0],
            }
        ),
    )

    with pytest.raises(ScreenerDataUnavailableError) as exc:
        execute_query(
            Repo(),
            ScreenerQueryRequest(
                conditions=[{"field": "financing_balance", "op": ">", "value": 0}]
            ),
        )
    assert exc.value.fields == ["financing_balance"]
