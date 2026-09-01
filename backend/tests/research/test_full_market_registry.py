from app.research.catalog import (
    FACTOR_REGISTRY,
    FULL_MARKET_MAPPINGS,
    full_market_factor_ids,
    resolve_full_market_executor,
)


def test_eleven_full_market_executors_resolve_from_single_registry():
    assert len(FACTOR_REGISTRY) == 19
    assert len(full_market_factor_ids()) == 11
    assert set(full_market_factor_ids()) == set(FULL_MARKET_MAPPINGS)
    for factor_id in full_market_factor_ids():
        adapter = resolve_full_market_executor(factor_id)
        assert adapter is not None
        assert callable(getattr(adapter, "evaluate", None))


def test_negative_exclusion_forces_v5_executor():
    assert FULL_MARKET_MAPPINGS["negative-exclusion"] == "negative-v5"
    assert resolve_full_market_executor("negative-exclusion").name == "negative-v5"


def test_non_full_market_factor_has_no_executor():
    assert resolve_full_market_executor("chip-peak-patterns") is None
