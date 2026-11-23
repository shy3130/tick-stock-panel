from datetime import date

import pytest
from test_controlled import bind, program
from test_daily import Rules, at, bar, case, factors, opening
from test_studies import request
from tivelo_contracts.base import canonical_bytes, fingerprint
from tivelo_contracts.data import DatasetScope
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import RunLock
from tivelo_contracts.extension_fields import ExtensionField, ExtensionFieldRef, ExtensionValue

from tivelo_backtest.archives import read_result
from tivelo_backtest.studies import evaluate_study
from tivelo_backtest.study_archives import read_study


def descriptor(kind="int", granularity="instrument"):
    return ExtensionField(
        table_id="desk",
        table_version="1",
        field_id=kind,
        field_version="1",
        value_type=kind,
        unit="fixture",
        granularity=granularity,
    )


def operand(field, alias, **extra):
    return dict(
        dataset_id="desk." + field.field_id,
        field_sha256=fingerprint(field),
        alias=alias,
        value_type=field.value_type,
        **extra,
    )


def value(field, value, *, known=2, effective=2, expires=None):
    key = {
        "float": "decimal_value",
        "int": "int_value",
        "bool": "bool_value",
        "string": "text_value",
    }[field.value_type]
    return ExtensionValue(
        field_sha256=fingerprint(field),
        instrument_id="A" if field.granularity == "instrument" else None,
        effective_at=at(effective),
        available_at=at(known, 5),
        ingested_at=at(known, 8),
        valid_until=at(expires) if expires else None,
        revision_id=str(known),
        source_id="desk",
        **{key: value},
    )


def field_run(
    tmp_path,
    datasets,
    operands,
    *,
    formulas=None,
    start=2,
    warmup=None,
    field_start=1,
    days=(2, 3, 6),
    mode="raw",
):
    scopes, observations, refs = {}, {}, []
    for field, rows in datasets:
        key = "desk." + field.field_id
        scopes[key] = DatasetScope(
            market_id="cn.equity",
            asset_class="equity",
            instrument_ids=("A",) if field.granularity == "instrument" else (),
            start=at(field_start, 0),
            end=at(10, 0),
            extension_field=field,
        )
        observations[key] = rows
        refs.append(ExtensionFieldRef(dataset_id=key, field_sha256=fingerprint(field)))

    class Calendar(Rules):
        dates = (*days, days[-1] + 1)

    args = {
        "entry": "ext_rank > 0",
        "exit": "ext_rank < 0",
        "score": "-ext_rank",
        **(formulas or {}),
    }
    strategy = program(tmp_path, format_version="1.1", field_operands=operands, **args)
    values = case(
        field_scopes=scopes,
        extension_values=observations,
        start=start,
        end=days[-1],
        rules=Calendar(),
        warmup=date(2025, 1, warmup) if warmup else None,
        bars=tuple(bar(day, "10") for day in days),
        openings=tuple(opening(day, "10") for day in days),
        mode=mode,
        anchor=7,
        factors=factors() if mode != "raw" else (),
    )
    runner, lock, reader, binding = bind(values, strategy)
    spec = lock.contents.spec.model_copy(update={"extension_fields": tuple(refs)})
    return runner, RunLock.create(lock.contents.model_copy(update={"spec": spec})), reader, binding


@pytest.mark.parametrize("mode", ["raw", "qfq", "hfq"])
def test_warmup_and_late_correction_observed_once_at_each_own_close(tmp_path, mode):
    field = descriptor()
    runner, lock, reader, binding = field_run(
        tmp_path,
        [(field, [value(field, 1), value(field, 9, known=3)])],
        [operand(field, "ext_rank")],
        start=3,
        warmup=2,
        mode=mode,
        formulas={"entry": "ts_delay(ext_rank, 1) == 1 and ext_rank == 9"},
    )
    observed = []
    read = reader.extension_values

    def inspect(query):
        observed.append((query.effective_at, query.as_of))
        return read(query)

    reader.extension_values = inspect
    result = runner.run(lock, reader, binding)
    assert [(row.at, row.quantity_delta) for row in result.ledger] == [(at(6), 1000)]
    assert observed == [(at(day, 7), at(day, 7)) for day in (2, 3, 6)]


@pytest.mark.parametrize("skip", [False, True])
def test_missing_before_publication_expiry_and_withdrawal(tmp_path, skip):
    field = descriptor()
    runner, lock, reader, binding = field_run(
        tmp_path,
        [(field, [value(field, 2, known=3, expires=6), value(field, None, known=7, effective=7)])],
        [operand(field, "ext_rank")],
        days=(2, 3, 6, 7),
        formulas={"missing_fields": "skip_signal" if skip else "error"},
    )
    if skip:
        result = runner.run(lock, reader, binding)
        assert [(row.at, row.kind) for row in result.ledger] == [(at(6), "buy")]
        assert len(result.final_portfolio.positions) == 1
    else:
        with pytest.raises(TiveloError) as err:
            runner.run(lock, reader, binding)
        assert err.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_multiple_typed_market_inputs_and_pinned_categorical_matches(tmp_path):
    phase, enabled, scale = (
        descriptor("string", "market"),
        descriptor("bool", "market"),
        descriptor("float"),
    )
    operands = [
        operand(phase, "ext_up", equals_text="上涨"),
        operand(phase, "ext_down", equals_text="下跌"),
        operand(enabled, "ext_enabled"),
        operand(scale, "ext_score"),
    ]
    runner, lock, reader, binding = field_run(
        tmp_path,
        [
            (phase, [value(phase, "上涨"), value(phase, "下跌", known=3)]),
            (enabled, [value(enabled, True)]),
            (scale, [value(scale, "0.125")]),
        ],
        operands,
        formulas={
            "entry": "ext_enabled and ext_up",
            "exit": "ext_down",
            "score": "rank(ext_score)",
        },
    )
    assert [(row.kind, row.at.day) for row in runner.run(lock, reader, binding).ledger] == [
        ("buy", 3),
        ("sell", 6),
    ]


def test_wrong_manifest_type_unrepresentable_integer_and_short_coverage_fail(tmp_path):
    field = descriptor()
    args = (tmp_path, [(field, [value(field, 2**53 + 1)])])
    bound = operand(field, "ext_rank")
    runner, lock, reader, binding = field_run(*args, [{**bound, "value_type": "float"}])
    with pytest.raises(TiveloError) as wrong_type:
        runner.preflight(lock, reader, strategy=binding.reference)
    assert wrong_type.value.code == ErrorCode.DATA_CONFLICT
    runner, lock, reader, binding = field_run(*args, [bound])
    with pytest.raises(TiveloError) as inexact:
        runner.run(lock, reader, binding)
    assert inexact.value.code == ErrorCode.CONTRACT_INVALID
    runner, lock, reader, binding = field_run(
        tmp_path,
        [(field, [value(field, 1, effective=3, known=3)])],
        [bound],
        start=3,
        warmup=2,
        field_start=3,
    )
    with pytest.raises(TiveloError) as coverage:
        runner.preflight(lock, reader, strategy=binding.reference)
    assert coverage.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    shifted = lock.contents.spec.model_copy(update={"extension_fields": ()})
    with pytest.raises(TiveloError) as refs:
        runner.preflight(
            RunLock.create(lock.contents.model_copy(update={"spec": shifted})),
            reader,
            strategy=binding.reference,
        )
    assert refs.value.code == ErrorCode.DATA_CONFLICT


@pytest.mark.parametrize(
    "changes",
    [
        {"format_version": "1.0"},
        {"score": "ext_undeclared"},
        {"score": "close + ext_rank"},
        {"entry": "ext_rank and ext_rank"},
        {"entry": "close > ext_rank"},
        {"field_operands": []},
        {"score": "close / open"},
    ],
)
def test_sealed_inputs_cannot_bypass_formula_proofs(tmp_path, changes):
    fields = [operand(descriptor(), "ext_rank")]
    with pytest.raises(TiveloError):
        program(
            tmp_path,
            **{"format_version": "1.1", "field_operands": fields, "score": "ext_rank", **changes},
        )


@pytest.mark.parametrize("walk", [False, True])
def test_studies_recompile_the_same_bindings_and_replay_their_archives(tmp_path, walk):
    field = descriptor()
    runner, lock, reader, binding = field_run(
        tmp_path,
        [(field, [value(field, 3), value(field, 1, known=5)])],
        [operand(field, "ext_rank")],
        days=(2, 3, 4, 5, 6, 7),
    )
    kwargs = dict(rules=runner.rules, reader=reader)
    archives = []
    for name in ("study", "repeat"):
        path = tmp_path / name
        evaluate_study(
            lock, binding.implementation.definitions, request(walk=walk), destination=path, **kwargs
        )
        archives.append(read_study(path))
    assert archives[0] == archives[1]
    assert archives[0][1].base_lock_id == lock.lock_id
    for window in archives[0][2].windows:
        children = (*window.training, window.test) if window.test else window.training
        for child in children:
            trial, _, _ = read_result(tmp_path / "study" / child.path)
            assert (
                trial.contents.scale_proof.field_operands
                == lock.contents.scale_proof.field_operands
            )
            assert trial.contents.spec.extension_fields == lock.contents.spec.extension_fields


def test_price_only_program_and_proof_keep_their_legacy_wire_format(tmp_path):
    binding = program(tmp_path).implementation
    assert b'"field_operands"' not in canonical_bytes(binding.proof)
    assert b'"missing_fields"' not in canonical_bytes(binding.program)
    assert b'"field_operands"' not in canonical_bytes(binding.program)
    assert binding.proof.verifier_version == "tivelo.scale.controlled.v1"
