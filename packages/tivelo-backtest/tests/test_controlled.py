import dataclasses
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_daily import ScheduledStrategy, at, bar, case, factors, opening, security
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    CashBalance,
    ExecutionModel,
    Portfolio,
    Position,
    PriceViewSpec,
    RunLock,
    StrategyContext,
)

from tivelo_backtest.controlled import ControlledProgram, load_controlled
from tivelo_backtest.daily import StrategyBinding
from tivelo_backtest.definitions import DefinitionLoader, seal_definition
from tivelo_backtest.price_views import StaticPriceView


def program(tmp_path, **changes):
    values = dict(entry="close >= open", exit="close < open", score="close / open")
    values.update(changes)
    root = tmp_path / str(len(list(tmp_path.iterdir())))
    root.mkdir()
    (root / "program.json").write_text(json.dumps(values), encoding="utf-8")
    manifest = seal_definition(
        root,
        definition_id="controlled.example",
        version="1",
        kind="strategy",
        language="controlled_daily_v1",
        entrypoint="program.json",
        files=("program.json",),
    )
    (root / "definition.json").write_bytes(canonical_bytes(manifest))
    return load_controlled(DefinitionLoader().inspect(root))


def bind(values, strategy):
    runner, lock, reader, _ = values
    contents = lock.contents.model_copy(
        update={
            "spec": lock.contents.spec.model_copy(update={"strategy": strategy.reference}),
            "scale_proof": strategy.implementation.proof,
        }
    )
    return runner, RunLock.create(contents), reader, strategy


def context(bars, positions=()):
    current = {}
    for value in sorted(bars, key=lambda item: item.bar_end):
        current[value.instrument_id] = value
    return StrategyContext(
        now=max(value.bar_end for value in bars),
        history=tuple(bars),
        instruments=tuple(security().model_copy(update={"instrument_id": key}) for key in current),
        portfolio=Portfolio(
            cash=(CashBalance(currency="CNY", total="10000", available="10000"),),
            positions=positions,
        ),
        parameters_json="{}",
        openings=tuple(
            opening(value.trading_date.day, value.open, instrument_id=key)
            for key, value in current.items()
        ),
    )


def decide(binding, ctx, *, mode="raw", anchor=7, fact=None):
    spec = PriceViewSpec(
        provider_id=f"tivelo.{mode}",
        mode=mode,
        **(
            {}
            if mode == "raw"
            else dict(
                anchor_at=at(anchor, 0),
                factor_definition_id="cumulative",
                factor_baseline_id="listing",
            )
        ),
    )
    return binding.implementation.decide_with_factors(
        ctx, factors=factors() if fact is None else fact, price_view=spec
    )


@pytest.mark.parametrize(
    "field,expression",
    [
        ("entry", "close < 10"),
        ("exit", "ts_mean(close, 3) > 5"),
        ("score", "close"),
        ("score", "rank(close)"),
        ("score", "zscore(close)"),
        ("score", "winsorize(close, 3)"),
        ("score", "log(close)"),
        ("score", "clamp(close, 0, 10)"),
        ("score", "raw_close / close"),
        ("score", "turnover_rate"),
        ("score", "rsi_14"),
        ("entry", "close / open"),
        ("score", "close > open"),
        ("entry", "close and open"),
        ("score", "(close > open) + volume"),
    ],
)
def test_whole_program_rejects_unproved_inputs_types_and_units(tmp_path, field, expression):
    with pytest.raises(TiveloError) as error:
        program(tmp_path, **{field: expression})
    assert error.value.code in (ErrorCode.PRICE_BASIS_UNPROVEN, ErrorCode.CONTRACT_INVALID)


@pytest.mark.parametrize(
    "score",
    [
        "ts_mean(close, 3) / close",
        "ts_std(close, 3) / close",
        "ts_cov(open, close, 3) / (open * close)",
        "ts_corr(open, close, 3)",
        "rank(close / open)",
        "zscore(close / open)",
        "winsorize(close / open)",
        "sqrt(close) / sqrt(open)",
        "power(close, 2) / power(open, 2)",
        "if_else(close > open, close / open, open / close)",
        "ts_rank(close, 3)",
        "ts_zscore(close, 3)",
        "sign(close - open)",
        "min(close, open) / max(close, open)",
        "log(close / open)",
        "clamp(close / open, 0, 2)",
        "amount / volume",
    ],
)
def test_conservative_scale_algebra_accepts_bounded_invariant_forms(tmp_path, score):
    loaded = program(tmp_path, score=score)
    assert loaded.implementation.proof.strategy == loaded.reference


def test_future_anchor_runs_raw_account_and_python_cannot_reuse_proof(tmp_path):
    binding = program(tmp_path)
    values = case(
        bars=(bar(2, "10", open="9", low="9"), bar(3, "9", open="10", high="10"), bar(6, "11")),
        mode="qfq",
        anchor=7,
        factors=factors(),
    )
    runner, lock, reader, binding = bind(values, binding)
    result = runner.run(lock, reader, binding)
    filled = [order for order in result.orders if order.status == "filled"]
    assert [(order.side, order.raw_fill_price, order.filled_quantity) for order in filled] == [
        ("buy", Decimal("10"), Decimal("1000")),
        ("sell", Decimal("11"), Decimal("1000")),
    ]
    assert result.final_portfolio.cash[0].total == 11000
    with pytest.raises(TiveloError, match="core executor"):
        runner.run(lock, reader, StrategyBinding(ScheduledStrategy(), binding.reference))
    without = RunLock.create(lock.contents.model_copy(update={"scale_proof": None}))
    assert "scale_proof" not in without.contents.model_dump(mode="json")
    with pytest.raises(TiveloError, match="core executor"):
        runner.run(without, reader, binding)
    with pytest.raises(TiveloError) as error:
        StaticPriceView("qfq").project((), (), lock.contents.spec.price_view, decision_at=at(2, 7))
    assert error.value.code == ErrorCode.PRICE_BASIS_UNPROVEN


def test_controlled_slots_require_next_session_expiry(tmp_path):
    binding = program(tmp_path)
    runner, lock, reader, binding = bind(
        case(execution=ExecutionModel(timeframe="1d", order_expiry="good_till_cancelled")), binding
    )
    with pytest.raises(TiveloError, match="next-session order expiry"):
        runner.run(lock, reader, binding)


def test_known_anchor_must_exist_even_when_its_value_cancels(tmp_path):
    binding = program(tmp_path)
    base = factors()[0].model_copy(update={"effective_at": at(2, 0), "available_at": at(2, 0)})
    with pytest.raises(TiveloError, match="anchor precedes"):
        decide(binding, context((bar(2, "10"),)), mode="hfq", anchor=1, fact=(base,))


def test_proof_recompiles_archived_bytes_and_rejects_program_or_proof_tampering(tmp_path):
    binding = program(tmp_path)
    runner, lock, reader, binding = bind(case(), binding)
    original = runner.run(lock, reader, binding)
    # A modified cached compiler closure cannot change an admitted execution.
    binding.implementation.formulas[0].frame_transform = lambda frame: (_ for _ in ()).throw(
        AssertionError()
    )
    assert runner.run(lock, reader, binding) == original
    proof = lock.contents.scale_proof.model_copy(update={"program_sha256": "f" * 64})
    changed = RunLock.create(lock.contents.model_copy(update={"scale_proof": proof}))
    with pytest.raises(TiveloError, match="differs from its proof"):
        runner.run(changed, reader, binding)
    node = binding.implementation.definitions.nodes[0]
    bad = dataclasses.replace(node, files=(("program.json", b"{}"),))
    graph = dataclasses.replace(binding.implementation.definitions, nodes=(bad,))
    invalid = dataclasses.replace(
        binding, implementation=dataclasses.replace(binding.implementation, definitions=graph)
    )
    with pytest.raises(TiveloError) as error:
        runner.run(lock, reader, invalid)
    assert error.value.code == ErrorCode.DEFINITION_CORRUPT


def test_per_instrument_scaling_preserves_ranking_ties_and_fixed_weights(tmp_path):
    binding = program(
        tmp_path, score="rank(close / open)", max_positions=2, weight_per_position="0.4"
    )
    bars = tuple(
        bar(2, str(price), open=str(base), low=str(base), instrument_id=key)
        for key, price, base in (("C", 30, 10), ("B", 20, 10), ("A", 20, 10))
    )
    batch = decide(binding, context(bars))
    assert [signal.instrument_id for signal in batch.signals] == ["C", "A"]
    assert [signal.target_weight for signal in batch.signals] == [Decimal("0.4")] * 2
    changed = tuple(
        value.model_copy(
            update={
                field: getattr(value, field) * multiplier
                for field in ("open", "high", "low", "close")
            }
        )
        for value, multiplier in zip(bars, (Decimal(4), Decimal("0.5"), Decimal(8)), strict=True)
    )
    assert decide(binding, context(changed)) == batch


def test_boolean_if_else_preserves_predicate_type(tmp_path):
    binding = program(
        tmp_path,
        entry="if_else(close > open, close >= open, close == open)",
        exit="if_else(close > open, close < open, close != open)",
    )
    batch = decide(binding, context((bar(2, "10"),)))
    assert len(batch.signals) == 1
    assert batch.signals[0].side == "buy"


def test_exit_keeps_slot_until_actual_fill_and_exit_wins_over_entry(tmp_path):
    binding = program(tmp_path, entry="close >= 0", exit="close < open")
    bars = (bar(2, "9", open="10", high="10"), bar(2, "11", instrument_id="B"))
    positions = (
        Position(
            instrument_id="A", quantity="100", sellable_quantity="0", cost="1000", currency="CNY"
        ),
    )
    batch = decide(binding, context(bars, positions))
    assert [
        (signal.instrument_id, signal.side, signal.target_weight) for signal in batch.signals
    ] == [("A", "sell", Decimal(0))]
    assert decide(binding, context((bars[0],))).signals == ()


def test_adjusted_history_uses_visible_fixed_baseline_and_never_future_factor(tmp_path):
    binding = program(
        tmp_path,
        entry="close >= ts_delay(close, 1)",
        exit="close < 0",
        score="close / ts_delay(close, 1)",
    )
    history = (bar(2, "10"), bar(3, "5"))
    base, split = factors()
    split = split.model_copy(update={"effective_at": at(3, 0), "available_at": at(3, 0)})
    current = context(history)
    batch = decide(binding, current, mode="qfq", fact=(base, split))
    assert len(batch.signals) == 1
    assert batch.signals[0].score == 1
    assert decide(binding, current, mode="hfq", anchor=1, fact=(base, split)) == batch
    assert decide(binding, current, mode="qfq", anchor=8, fact=(base, split)) == batch
    future = split.model_copy(
        update={"value": Decimal(999), "available_at": at(6), "revision_id": "later"}
    )
    assert decide(binding, current, mode="qfq", fact=(base, split, future)) == batch
    assert decide(binding, current).signals == ()
    # Removing warmup data cannot invent an adjusted signal.
    assert decide(binding, context((history[1],)), mode="qfq", fact=(base, split)).signals == ()


def test_known_revision_can_change_history_but_unknown_revision_cannot(tmp_path):
    binding = program(
        tmp_path, entry="close >= ts_delay(close, 1)", exit="close < 0", score="close / open"
    )
    base, split = factors()
    split = split.model_copy(update={"effective_at": at(3, 0), "available_at": at(3, 0)})
    revised = split.model_copy(
        update={"value": Decimal(3), "available_at": at(3, 6), "revision_id": "revision"}
    )
    ctx = context((bar(2, "10"), bar(3, "5")))
    assert decide(binding, ctx, mode="qfq", fact=(base, split)).signals
    assert not decide(binding, ctx, mode="qfq", fact=(base, split, revised)).signals


def test_null_computation_missing_factors_and_intrabar_events_fail_closed(tmp_path):
    binding = program(tmp_path, score="close / (close - open)")
    with pytest.raises(TiveloError, match="null, nonfinite"):
        decide(binding, context((bar(2, "10"),)))
    binding = program(tmp_path)
    with pytest.raises(TiveloError) as error:
        decide(binding, context((bar(2, "10"),)), mode="qfq", fact=())
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    base, event = factors()
    event = event.model_copy(update={"effective_at": at(2, 4), "available_at": at(2, 4)})
    with pytest.raises(TiveloError, match="inside a bar"):
        decide(binding, context((bar(2, "10"),)), mode="qfq", fact=(base, event))
    huge = program(tmp_path, score="power(close / open, 4)")
    with pytest.raises(TiveloError, match="score exceeds"):
        decide(
            huge,
            context(
                (bar(2, "10000000000000000000000000", open="0.000000000001", low="0.000000000001"),)
            ),
        )


def test_anchor_cancelled_numeric_semantics_do_not_claim_rounded_chart_equivalence(tmp_path):
    binding = program(tmp_path, entry="close > open")
    base, anchor = factors()
    base = base.model_copy(update={"value": Decimal(1)})
    anchor = anchor.model_copy(
        update={
            "effective_at": at(7, 0),
            "available_at": at(7, 0),
            "value": Decimal("1000000000000"),
        }
    )
    value = bar(2, "1.000000000001", open="1", low="1")
    assert decide(binding, context((value,)), mode="qfq", fact=(base, anchor)).signals
    rounded = StaticPriceView("qfq").project(
        (value,),
        (base, anchor),
        PriceViewSpec(
            provider_id="tivelo.qfq",
            mode="qfq",
            anchor_at=at(7, 0),
            factor_definition_id="cumulative",
            factor_baseline_id="listing",
        ),
        decision_at=at(7, 7),
    )
    assert rounded[0].close == rounded[0].open


@pytest.mark.parametrize(
    "changes",
    [
        {"max_positions": 2},
        {"weight_per_position": "0"},
        {"max_positions": True},
        {"scale_invariant": True},
        {"limit_price": "close"},
        {"min_history_bars": 0},
    ],
)
def test_program_rejects_invalid_weights_self_attestation_and_price_outputs(changes):
    with pytest.raises(ValidationError):
        ControlledProgram(
            entry="close > open", exit="close < open", score="close / open", **changes
        )


def test_controlled_definition_never_uses_python_loader_or_unlocked_parameters(tmp_path):
    binding = program(tmp_path)
    with pytest.raises(TiveloError, match="Python loader"):
        with DefinitionLoader().load_trusted_python(binding.implementation.definitions):
            pytest.fail("controlled JSON ran as Python")
    node = binding.implementation.definitions.nodes[0]
    ref = node.reference.model_copy(update={"parameters_json": '{"unlocked":1}'})
    graph = dataclasses.replace(
        binding.implementation.definitions,
        root=ref,
        nodes=(dataclasses.replace(node, reference=ref),),
    )
    with pytest.raises(TiveloError, match="all parameters"):
        load_controlled(graph)
