from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from pydantic import ValidationError

from app.services import screener as screener_module
from app.services.screener_query import (
    ALLOWED_OPS,
    QueryService,
    ScreenerDataUnavailableError,
    ScreenerQueryRequest,
    ScreenerSemanticError,
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

    def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
        def _load_enriched_for_date(self, as_of):
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
