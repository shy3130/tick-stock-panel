from pathlib import Path

from app.strategy.engine import StrategyEngine

EXPECTED = {
    "dow_head_shoulders_bottom": ("头肩底结构", "buy"),
    "dow_head_shoulders_top": ("头肩顶结构", "risk"),
    "dow_symmetric_triangle": ("普通三角型", "buy"),
    "dow_right_triangle": ("直角三角型", "buy"),
    "dow_morning_star": ("启明星", "buy"),
    "dow_evening_star": ("黄昏之星", "risk"),
    "dow_three_red_soldiers": ("三个红小兵", "buy"),
    "dow_bottom_pregnancy": ("底部孕线", "buy"),
    "dow_top_pregnancy": ("顶部孕线", "risk"),
    "dow_2b_reversal": ("2B结构", "buy"),
    "dow_head_shoulders_right_shoulder": ("底部头肩右肩", "early_buy"),
    "dow_second_test": ("二次测试", "buy"),
    "dow_strong_emergence": ("强势出现", "buy"),
}


def test_dow_pattern_cards_have_exact_registry_contract() -> None:
    strategy_dir = Path(__file__).parents[1] / "app" / "strategy" / "builtin"
    engine = StrategyEngine([strategy_dir])
    strategies = {item["id"]: item for item in engine.list_strategies()}

    assert len(strategies) == 33
    assert {
        strategy_id: (strategies[strategy_id]["name"], strategies[strategy_id]["strategy_role"])
        for strategy_id in EXPECTED
    } == EXPECTED
    for strategy_id in EXPECTED:
        item = strategies[strategy_id]
        assert item["source"] == "builtin"
        assert item["execution_backend"] == "matrix_native"
        assert item["asset_types"] == ["stock"]
        assert item["timeframes"] == ["1d"]


def test_excluded_note_methods_are_not_registered() -> None:
    strategy_dir = Path(__file__).parents[1] / "app" / "strategy" / "builtin"
    ids = {item["id"] for item in StrategyEngine([strategy_dir]).list_strategies()}

    assert "dow_trendline_buy_sell" not in ids
    assert "dow_strong_weak_combination" not in ids
    assert "dow_thirteen_buy_points" not in ids
