from types import SimpleNamespace

from app.services.strategy_export import export_strategy_formula


def strategy(meta=None, stop_loss=None):
    return SimpleNamespace(meta=meta or {"id": "s1", "name": "策略1"}, stop_loss=stop_loss)


def test_export_conditions_to_tdx_formula():
    result = export_strategy_formula(
        strategy(),
        "tdx",
        conditions=[
            {"left": "ma5", "op": ">", "right": "field:ma20"},
            {"left": "close", "op": ">", "right": "field:ma60"},
        ],
    )

    assert result.ok is True
    assert "XG:(MA(C,5)>MA(C,20) AND C>MA(C,60));" in result.formula
    assert "{Target: TDX}" in result.formula


def test_export_turnover_condition_to_ths_formula():
    result = export_strategy_formula(
        strategy(),
        "ths",
        conditions=[{"left": "turnover_rate", "op": ">", "right": 3}],
    )

    assert result.ok is True
    assert "XG:TURNOVER>3;" in result.formula
    assert result.target == "ths"


def test_export_expression_supports_or_not_and_cross():
    result = export_strategy_formula(
        strategy(),
        "tdx",
        expression={
            "all": [
                {"fn": "cross_up", "args": ["ma5", "ma20"]},
                {"not": {"left": "close", "op": "<", "right": "field:ma60"}},
            ],
        },
    )

    assert result.ok is True
    assert "CROSS(MA(C,5),MA(C,20))" in result.formula
    assert "NOT(C<MA(C,60))" in result.formula


def test_unknown_field_returns_unsupported():
    result = export_strategy_formula(
        strategy(),
        "tdx",
        conditions=[{"left": "pe_ttm", "op": ">", "right": 20}],
    )

    assert result.ok is False
    assert result.unsupported == ["unsupported field: pe_ttm"]


def test_stateful_python_strategy_without_export_metadata_is_unsupported():
    result = export_strategy_formula(strategy(stop_loss=-0.05), "tdx")

    assert result.ok is False
    assert result.unsupported == ["strategy has no META.export DSL"]


def test_meta_export_is_used_when_body_has_no_expression():
    result = export_strategy_formula(
        strategy({"id": "s2", "name": "均线", "export": {"conditions": [
            {"left": "ma10", "op": ">=", "right": "field:ma20"},
        ]}}),
        "tdx",
    )

    assert result.ok is True
    assert "MA(C,10)>=MA(C,20)" in result.formula
