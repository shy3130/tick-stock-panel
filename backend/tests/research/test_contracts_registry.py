from app.research.catalog import FACTOR_REGISTRY, FULL_MARKET_MAPPINGS, parameter_fields
from app.research.contracts import RunScopeModel


def test_catalog_has_nineteen_unique_factors_and_parameter_schemas():
    assert len(FACTOR_REGISTRY) == 19
    assert len(set(FACTOR_REGISTRY)) == 19
    for factor in FACTOR_REGISTRY.values():
        assert factor.request_model.model_json_schema()["type"] == "object"
        assert factor.data_requirements
    assert {
        field.kind for factor in FACTOR_REGISTRY.values() for field in parameter_fields(factor)
    } <= {
        "symbol_list",
        "date",
        "number",
        "integer",
        "boolean",
        "enum",
        "multi_enum",
    }
    pre_surge_fields = {
        field.name: field for field in parameter_fields(FACTOR_REGISTRY["pre-surge"])
    }
    assert pre_surge_fields["benchmark_symbol"].kind == "symbol_list"


def test_full_market_mapping_has_eleven_entries_and_negative_v5():
    assert len(FULL_MARKET_MAPPINGS) == 11
    assert FULL_MARKET_MAPPINGS["negative-exclusion"] == "negative-v5"


def test_scope_is_disjoint():
    assert RunScopeModel(type="symbols", symbols=["600519.SH"]).symbols == ["600519.SH"]
    assert RunScopeModel(type="full_market").symbols is None
