"""Research definitions must execute without a host registry or host cache."""

from dataclasses import FrozenInstanceError, replace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from tivelo_backtest.research.catalog import (
    BUILTIN_CATALOG,
    FactorCatalog,
    FactorSpec,
    factor_calculation_order,
    factor_dependencies,
    factor_warmup_bars,
)
from tivelo_backtest.research.dsl import FACTOR_COLUMN, compile_formula
from tivelo_backtest.research.factor_definitions import load_factor_definitions, to_spec
from tivelo_backtest.research.py_expr import compile_python_expression
from tivelo_backtest.research.scoring import materialize_scoring_columns


def sample_frame():
    return pl.DataFrame(
        {
            "symbol": [symbol for symbol in ("A", "B", "C") for _ in range(4)],
            "date": [1, 2, 3, 4] * 3,
            "close": [float(day * scale) for scale in (1, 2, 3) for day in range(1, 5)],
        }
    )


def graph_definitions():
    return [
        {"id": "uf_mean", "label": "mean", "formula": "ts_mean(close, 2)"},
        {
            "id": "uf_double", "label": "double", "lang": "python",
            "formula": 'ctx.col("uf_mean") * 2',
        },
        {
            "id": "cf_mix", "label": "mix", "kind": "composite",
            "members": {"uf_mean": 1, "uf_double": 2},
        },
    ]


def test_catalog_copies_input_containers_and_rejects_mutation():
    inputs = {"close"}
    factors = [FactorSpec("uf_a", "A", "test", "close", dependencies=inputs)]
    catalog = FactorCatalog(factors)
    inputs.add("open")
    factors.clear()
    assert catalog.get("uf_a").dependencies == frozenset({"close"})
    with pytest.raises(FrozenInstanceError):
        catalog.specs = ()
    with pytest.raises(TypeError):
        catalog._by_id["other"] = catalog.get("uf_a")
    assert BUILTIN_CATALOG.get("uf_a") is None


def test_catalog_rejects_duplicate_ids_versions_and_cycles():
    a = FactorSpec("uf_a", "A", "test", "uf_b", dependencies=frozenset({"uf_b"}))
    b = FactorSpec("uf_b", "B", "test", "uf_a", dependencies=frozenset({"uf_a"}))
    with pytest.raises(ValueError, match="duplicate"):
        FactorCatalog([a, a])
    with pytest.raises(ValueError, match="cycle"):
        FactorCatalog([a, b])
    first = FactorCatalog([a])
    with pytest.raises(ValueError, match="version"):
        first.with_factors([a])
    updated = first.with_factors([replace(a, version=2, dependencies=frozenset({"close"}))])
    assert factor_dependencies(["uf_a"], catalog=first) == frozenset({"uf_b"})
    assert factor_dependencies(["uf_a"], catalog=updated) == frozenset({"close"})


def test_definition_graph_works_in_reverse_order_without_host_state():
    catalog = load_factor_definitions(reversed(graph_definitions()))
    assert factor_dependencies(["cf_mix"], catalog=catalog) == frozenset({"close"})
    assert factor_calculation_order(["cf_mix"], catalog=catalog) == [
        "close", "uf_mean", "uf_double", "cf_mix",
    ]
    assert factor_warmup_bars(["cf_mix"], catalog=catalog) == 3
    frame = sample_frame()
    result = materialize_scoring_columns(frame, ["cf_mix"], catalog=catalog)
    assert result.columns == [*frame.columns, "cf_mix"]
    assert result.filter(pl.col("date") == 4)["cf_mix"].to_list() == pytest.approx([-3, 0, 3])
    assert result.filter(pl.col("date") == 1)["cf_mix"].null_count() == 3
    assert_frame_equal(frame, sample_frame())
    assert BUILTIN_CATALOG.get("cf_mix") is None


def test_catalog_update_recomputes_transitive_warmup_without_changing_old_snapshot():
    old = load_factor_definitions(graph_definitions())
    new = old.with_factors([
        to_spec(
            {"id": "uf_mean", "label": "mean", "formula": "ts_mean(close, 10)", "version": 2},
            catalog=old,
        ),
    ])
    assert factor_warmup_bars(["cf_mix"], catalog=old) == 3
    assert factor_warmup_bars(["cf_mix"], catalog=new) == 11
    assert compile_formula("cf_mix", catalog=new).warmup_bars == 11
    assert compile_python_expression('ctx.col("cf_mix")', catalog=new).warmup_bars == 11


@pytest.mark.parametrize("definitions", [
    [{"id": "uf_bad", "label": "bad", "formula": "uf_missing"}],
    [{"id": "uf_a", "label": "A", "formula": "uf_b"},
     {"id": "uf_b", "label": "B", "formula": "uf_a"}],
    [graph_definitions()[0], graph_definitions()[0]],
])
def test_invalid_import_cannot_modify_existing_catalog(definitions):
    original = load_factor_definitions(graph_definitions())
    before = original.specs
    with pytest.raises(ValueError):
        load_factor_definitions(definitions, catalog=original)
    assert original.specs == before


@pytest.mark.parametrize(("formula", "code"), [
    ("ts_delay(close, -1)", "E005"),
    ("ts_mean(rank(close), 2)", "E009"),
    ("uf_missing + close", "E001"),
    ("1 + 2", "E016"),
])
def test_dsl_preserves_rejection_semantics(formula, code):
    compiled = compile_formula(formula)
    assert not compiled.ok
    assert code in [error.code for error in compiled.errors]


def test_staged_windows_never_cross_symbols_or_leak_temporary_columns():
    catalog = load_factor_definitions([
        {"id": "uf_rank", "label": "rank", "formula": "rank(ts_mean(close, 2))"},
    ])
    result = materialize_scoring_columns(sample_frame(), ["uf_rank"], catalog=catalog)
    assert result.columns == ["symbol", "date", "close", "uf_rank"]
    assert result.filter(pl.col("date") == 1)["uf_rank"].null_count() == 3
    assert result.filter(pl.col("date") == 4)["uf_rank"].to_list() == pytest.approx([1 / 3, 2 / 3, 1])


def test_legacy_quote_and_lot_units_remain_explicit():
    frame = pl.DataFrame({
        "symbol": ["A"], "date": [1], "close": [5.0], "raw_close": [12.0],
        "amount": [2000.0], "volume": [2.0], "turnover_rate": [0.2],
    })
    result = materialize_scoring_columns(frame, ["vwap_bias", "log_float_mv"])
    assert result["vwap_bias"][0] == pytest.approx(0.2)
    import math

    assert result["log_float_mv"][0] == pytest.approx(math.log(120))


def test_optional_cache_is_injected_and_cold_warm_results_match():
    class Cache:
        values = {}
        writes = 0

        def cache_key(self, factor_id, formula, version, frame, dependencies):
            assert dependencies == {"close", "symbol", "date"}
            return factor_id

        def get_cached(self, key, rows):
            return self.values.get(key)

        def put_cached(self, key, column):
            self.writes += 1
            self.values[key] = column

    cache = Cache()
    catalog = load_factor_definitions(graph_definitions()[:1])
    cold = materialize_scoring_columns(sample_frame(), ["uf_mean"], catalog=catalog, cache=cache)
    warm = materialize_scoring_columns(sample_frame(), ["uf_mean"], catalog=catalog, cache=cache)
    uncached = materialize_scoring_columns(sample_frame(), ["uf_mean"], catalog=catalog)
    assert cache.writes == 1
    assert_frame_equal(cold, warm)
    assert_frame_equal(warm, uncached)
    assert FACTOR_COLUMN not in cold.columns


def test_python_expression_retains_grammar_and_missing_input_behavior():
    assert not compile_python_expression('pl.read_csv("unused")').ok
    compiled = compile_python_expression('ctx.col("close").shift(1).over("symbol")')
    assert compiled.ok
    assert compiled.frame_transform(pl.DataFrame({"other": [1]})) is None
    result = compiled.frame_transform(sample_frame())
    assert result.filter(pl.col("date") == 1)[FACTOR_COLUMN].null_count() == 3
