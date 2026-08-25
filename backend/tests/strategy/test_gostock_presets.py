from app.services.screener_query import compile_predicate
from app.strategy.gostock_presets import GOSTOCK_PRESETS, list_gostock_presets


def test_gostock_presets_are_unique_and_executable():
    presets = list_gostock_presets()
    assert {preset["id"] for preset in presets} == {
        "strong_momentum",
        "bullish_macd",
        "midcap_breakout",
        "quality_growth",
        "consecutive_boards",
    }
    assert len({preset["id"] for preset in presets}) == len(presets)
    assert any(preset["executable_level"] == "full" for preset in presets)
    assert any(preset["executable_level"] == "needs_fundamental" for preset in presets)

    for preset in presets:
        assert set(preset) == {
            "id",
            "name",
            "description",
            "predicate",
            "executable_level",
        }
        assert preset["executable_level"] in {"full", "needs_fundamental"}
        predicate = preset["predicate"]
        compile_predicate(predicate["conditions"], predicate["order_by"])

    assert list(GOSTOCK_PRESETS) == presets
