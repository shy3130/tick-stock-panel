import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_controlled import bind, program
from test_daily import Rules, bar, case, opening
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.archives import read_result
from tivelo_backtest.studies import evaluate_study, prepare_study, select_winner, trial_lock
from tivelo_backtest.study_archives import read_study
from tivelo_backtest.study_models import (
    SearchAxis,
    SearchSpace,
    StudyLimits,
    StudyObjective,
    StudyRequest,
    WalkForwardWindows,
)


def request(*, walk=False, **changes):
    values = dict(
        mode="walk_forward" if walk else "grid",
        search=SearchSpace(
            target="controlled_program",
            axes=(SearchAxis(name="weight_per_position", values=("0.5", "1")),),
        ),
        walk_forward=WalkForwardWindows(train_sessions=2, test_sessions=2, step_sessions=2)
        if walk
        else None,
    )
    values.update(changes)
    return StudyRequest(**values)


def setup(tmp_path, *, walk=False, test_close="10"):
    strategy = program(tmp_path)
    if not walk:
        values = case()
    else:

        class Calendar(Rules):
            dates = (2, 3, 4, 5, 6, 7, 8, 9)

        bars = tuple(
            bar(
                day,
                close,
                open=open_,
                low=min(Decimal(open_), Decimal(close)),
                high=max(Decimal(open_), Decimal(close)),
            )
            for day, open_, close in (
                (2, "10", "10"),
                (3, "10", "12"),
                (4, "20", "20"),
                (5, "20", test_close),
                (6, "10", "10"),
                (7, "10", "14"),
            )
        )
        values = case(
            bars=bars,
            openings=tuple(opening(value.trading_date.day, value.open) for value in bars),
            rules=Calendar(),
            end=7,
        )
    runner, lock, reader, strategy = bind(values, strategy)
    return runner.rules, lock, reader, strategy.implementation.definitions


def execute(tmp_path, *, walk=False, test_close="10", req=None, observer=None):
    rules, lock, reader, definitions = setup(tmp_path, walk=walk, test_close=test_close)
    configuration = req or request(walk=walk)
    output = tmp_path / "study"
    manifest = evaluate_study(
        lock,
        definitions,
        configuration,
        rules=rules,
        reader=reader,
        destination=output,
        observer=observer,
    )
    return manifest, *read_study(output)[1:], (rules, lock, reader, definitions, output)


def test_grid_selects_and_archives_exact_reference_trials(tmp_path):
    manifest, plan, report, inputs = execute(tmp_path)
    rules, base, reader, definitions, output = inputs
    best = report.windows[0].winner_candidate_id
    assert (
        next(
            candidate.overrides_json
            for candidate in plan.candidates
            if candidate.candidate_id == best
        )
        == '{"weight_per_position":"1"}'
    )
    assert report.oos_compounded_return is None and report.aggregate_method == "none"
    prepared, graphs = prepare_study(base, definitions, plan.request, rules=rules, reader=reader)
    assert prepared == plan
    from tivelo_backtest.controlled import load_controlled
    from tivelo_backtest.daily import DailyBacktest
    from tivelo_backtest.price_views import StaticPriceView

    for trial in report.windows[0].training:
        graph = graphs[trial.candidate_id]
        locked = trial_lock(base, graph, plan.windows[0])
        reference = DailyBacktest(rules=rules, price_view=StaticPriceView("raw")).run(
            locked, reader, load_controlled(graph)
        )
        actual_lock, actual, metrics = read_result(output / trial.path)
        assert actual_lock == locked and canonical_bytes(actual) == canonical_bytes(reference)
        assert metrics == trial.metrics
    assert (
        evaluate_study(
            base, definitions, plan.request, rules=rules, reader=reader, destination=output
        )
        == manifest
    )


def test_walk_forward_uses_training_only_and_keeps_all_oos_folds(tmp_path):
    _, plan, report, inputs = execute(tmp_path, walk=True)
    choices = {
        candidate.candidate_id: json.loads(candidate.overrides_json)
        for candidate in plan.candidates
    }
    assert [
        choices[window.winner_candidate_id]["weight_per_position"] for window in report.windows
    ] == ["1", "0.5"]
    assert [window.test.metrics.total_return for window in report.windows] == [
        Decimal("-0.5"),
        Decimal("0.2"),
    ]
    assert report.oos_compounded_return == Decimal("-0.4")
    assert report.aggregate_method == "independent_fold_return_product"
    assert [
        (window.train_start.day, window.train_end.day, window.test_start.day, window.test_end.day)
        for window in plan.windows
    ] == [(2, 3, 4, 5), (4, 5, 6, 7)]
    assert all(window.selection_at < window.test_open_at for window in plan.windows)
    other = tmp_path / "changed"
    other.mkdir()
    _, changed_plan, changed, _ = execute(other, walk=True, test_close="30")
    assert changed.windows[0].winner_candidate_id == report.windows[0].winner_candidate_id
    assert [trial.metrics for trial in changed.windows[0].training] == [
        trial.metrics for trial in report.windows[0].training
    ]
    assert changed.windows[0].test.metrics.total_return == Decimal("0.5")
    # The old test interval may legitimately train a later fold after it becomes historical.
    assert choices[changed.windows[1].winner_candidate_id]["weight_per_position"] == "1"
    assert changed_plan.candidates == plan.candidates


def test_tied_metrics_use_content_identity_and_drawdown_minimizes_magnitude(tmp_path):
    _, plan, report, _ = execute(tmp_path)
    trials = report.windows[0].training
    tied = tuple(
        trial.model_copy(
            update={"metrics": trial.metrics.model_copy(update={"total_return": Decimal(0)})}
        )
        for trial in reversed(trials)
    )
    assert select_winner(tied, StudyObjective()) == min(trial.candidate_id for trial in trials)
    changed = tuple(
        trial.model_copy(
            update={
                "metrics": trial.metrics.model_copy(
                    update={"maximum_drawdown": Decimal(index) / 10}
                )
            }
        )
        for index, trial in enumerate(trials)
    )
    assert (
        select_winner(changed, StudyObjective(metric="maximum_drawdown")) == changed[0].candidate_id
    )
    with pytest.raises(TiveloError, match="minimum filled-order"):
        select_winner(trials, StudyObjective(min_filled_orders=1000))
    unfilled = trials[0].model_copy(
        update={
            "metrics": trials[0].metrics.model_copy(
                update={"filled_orders": 0, "total_return": Decimal("100")}
            )
        }
    )
    assert select_winner((unfilled, trials[1]), StudyObjective()) == trials[1].candidate_id


@pytest.mark.parametrize("training", ["rolling", "expanding"])
def test_walk_forward_calendar_gap_and_warmup_keep_execution_windows_separate(tmp_path, training):
    from datetime import date

    from tivelo_contracts.execution import RunLock

    rules, base, reader, definitions = setup(tmp_path, walk=True)
    base = RunLock.create(
        base.contents.model_copy(
            update={
                "spec": base.contents.spec.model_copy(
                    update={"start": date(2025, 1, 3), "warmup_start": date(2025, 1, 2)}
                )
            }
        )
    )
    config = request(
        walk=True,
        walk_forward=WalkForwardWindows(
            train_sessions=2,
            test_sessions=1,
            step_sessions=1,
            gap_sessions=1,
            warmup_sessions=1,
            training=training,
        ),
    )
    output = tmp_path / "study"
    evaluate_study(base, definitions, config, rules=rules, reader=reader, destination=output)
    _, plan, report = read_study(output)
    second_start = 3 if training == "expanding" else 4
    assert [
        (
            window.train_warmup_start.day,
            window.train_start.day,
            window.train_end.day,
            window.test_warmup_start.day,
            window.test_start.day,
            window.test_end.day,
        )
        for window in plan.windows
    ] == [(2, 3, 4, 5, 6, 6), (second_start - 1, second_start, 5, 6, 7, 7)]
    for window, outcome in zip(plan.windows, report.windows, strict=True):
        for trial in outcome.training:
            _, result, _ = read_result(output / trial.path)
            assert result.equity[0].at.date() == window.train_start
            assert result.equity[-1].at.date() == window.train_end
        _, result, _ = read_result(output / outcome.test.path)
        assert len(result.equity) == 1 and result.equity[0].at.date() == window.test_start
        assert outcome.test.metrics.filled_orders == 0
    assert report.oos_compounded_return == 0


def test_oos_aggregate_rejects_decimal128_overflow(tmp_path):
    from tivelo_backtest.studies import compounded_oos

    _, _, report, _ = execute(tmp_path, walk=True)
    windows = tuple(
        window.model_copy(
            update={
                "test": window.test.model_copy(
                    update={
                        "metrics": window.test.metrics.model_copy(
                            update={"total_return": Decimal("100000000000000000000")}
                        )
                    }
                )
            }
        )
        for window in report.windows
    )
    with pytest.raises(TiveloError) as error:
        compounded_oos(windows)
    assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_replay_mismatch_never_publishes_success(tmp_path):
    from tivelo_backtest.studies import _evaluate_prepared

    _, plan, _, inputs = execute(tmp_path)
    rules, base, reader, definitions, _ = inputs
    _, graphs = prepare_study(base, definitions, plan.request, rules=rules, reader=reader)
    output = tmp_path / "replayed"
    with pytest.raises(TiveloError) as error:
        _evaluate_prepared(
            base,
            definitions,
            plan,
            graphs,
            rules=rules,
            reader=reader,
            destination=output,
            _expected_archive_id="f" * 64,
        )
    assert error.value.code == ErrorCode.DATA_CONFLICT
    assert not output.exists()
    assert not list(tmp_path.glob(".tivelo-study-*"))


@pytest.mark.parametrize(
    "values", [(float("nan"),), (float("inf"),), ({"a": 1},), ([1],), (1, 1), ()]
)
def test_search_values_are_finite_immutable_unique_scalars(values):
    with pytest.raises(ValidationError):
        SearchAxis(name="value", values=values)


def test_boolean_integer_float_and_string_parameters_keep_json_types():
    space = SearchSpace(
        target="strategy_parameters", axes=(SearchAxis(name="x", values=(True, 1, 1.0, "1", None)),)
    )
    assert space.expand(5) == ('{"x":true}', '{"x":1}', '{"x":1.0}', '{"x":"1"}', '{"x":null}')
    with pytest.raises(ValueError, match="candidate budget"):
        space.expand(4)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_candidates": 1},
        {"max_executions": 1},
        {"max_archive_bytes": 1},
    ],
)
def test_study_budgets_reject_without_publishing_success(tmp_path, changes):
    limits = StudyLimits(**changes)
    with pytest.raises((TiveloError, ValidationError)):
        execute(tmp_path, req=request(limits=limits))
    assert not (tmp_path / "study").exists()


def test_overlap_partial_windows_and_missing_warmup_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        WalkForwardWindows(train_sessions=2, test_sessions=2, step_sessions=1)
    rules, base, reader, definitions = setup(tmp_path, walk=True)
    for settings in (
        WalkForwardWindows(train_sessions=10, test_sessions=2, step_sessions=2),
        WalkForwardWindows(train_sessions=2, test_sessions=2, step_sessions=2, warmup_sessions=1),
    ):
        with pytest.raises(TiveloError) as error:
            prepare_study(
                base,
                definitions,
                request(walk=True, walk_forward=settings),
                rules=rules,
                reader=reader,
            )
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_invalid_controlled_candidates_fail_before_execution(tmp_path):
    rules, base, reader, definitions = setup(tmp_path)
    for axis in (
        SearchAxis(name="score", values=("close / open", "close")),
        SearchAxis(name="numeric_semantics", values=("different",)),
        SearchAxis(name="weight_per_position", values=("0.5", ".5")),
    ):
        config = request(search=SearchSpace(target="controlled_program", axes=(axis,)))
        with pytest.raises(TiveloError):
            evaluate_study(
                base,
                definitions,
                config,
                rules=rules,
                reader=reader,
                destination=tmp_path / "study",
            )
        assert not (tmp_path / "study").exists()


class CancelAfterTrial:
    stop = False

    def cancelled(self):
        return self.stop

    def progress(self, done, total, phase):
        if phase == "study_trials":
            self.stop = True


def test_cancel_after_completed_trial_leaves_no_success_or_staging_directory(tmp_path):
    with pytest.raises(TiveloError) as error:
        execute(tmp_path, observer=CancelAfterTrial())
    assert error.value.code == ErrorCode.CANCELLED
    assert not (tmp_path / "study").exists()
    assert not list(tmp_path.glob(".tivelo-study-*"))


def test_report_reading_needs_no_fact_source_market_or_original_runtime(tmp_path, monkeypatch):
    manifest, plan, report, inputs = execute(tmp_path)
    import tivelo_backtest.definitions as definitions_module
    import tivelo_backtest.market_loading as market_module
    import tivelo_backtest.runtime as runtime_module

    def forbidden(*args, **kwargs):
        pytest.fail("result inspection tried to load an execution dependency")

    monkeypatch.setattr(definitions_module, "resolve_requirements", forbidden)
    monkeypatch.setattr(market_module, "discover_market_adapter", forbidden)
    monkeypatch.setattr(runtime_module, "verify_runtime", forbidden)
    assert read_study(inputs[-1]) == (manifest, plan, report)


def test_tampered_child_and_changed_request_cannot_reuse_a_published_study(tmp_path):
    _, plan, report, inputs = execute(tmp_path)
    rules, base, reader, definitions, output = inputs
    changed = request(objective=StudyObjective(metric="maximum_drawdown"))
    with pytest.raises(TiveloError) as error:
        evaluate_study(base, definitions, changed, rules=rules, reader=reader, destination=output)
    assert error.value.code == ErrorCode.DATA_CONFLICT
    path = output / report.windows[0].training[0].path / "metrics.json"
    original = path.read_bytes()
    path.write_bytes(original + b"corrupt")
    with pytest.raises(TiveloError) as error:
        read_study(output)
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT
    assert path.read_bytes() == original + b"corrupt"


def test_trusted_parameter_grid_preserves_fixed_parameters_and_resets_instances(tmp_path):
    from tivelo_contracts.execution import RunLock

    from tivelo_backtest.definitions import DefinitionLoader, seal_definition

    rules, base, reader, _ = setup(tmp_path, walk=True)
    source = tmp_path / "python"
    source.mkdir()
    (source / "strategy.py").write_text(
        """
import json
from tivelo_contracts.execution import Signal, SignalBatch
class Strategy:
    def __init__(self, weight):
        self.weight, self.calls = weight, 0
    def decide(self, context):
        self.calls += 1
        return SignalBatch(decision_at=context.now, signals=() if self.calls != 1 else (
            Signal(instrument_id="A", decision_at=context.now, side="buy", score="1",
                   source_id="python.fixture", target_weight=self.weight),))
def build(*, parameters_json, dependencies):
    parameters = json.loads(parameters_json)
    assert parameters["fixed"] == 7
    return Strategy(parameters["weight"])
""",
        encoding="utf-8",
    )
    bundle = seal_definition(
        source,
        definition_id="python.fixture",
        version="1",
        kind="strategy",
        entrypoint="strategy:build",
        files=("strategy.py",),
    )
    (source / "definition.json").write_bytes(canonical_bytes(bundle))
    definitions = DefinitionLoader().inspect(source, parameters_json='{"fixed":7,"weight":"0.2"}')
    base = RunLock.create(
        base.contents.model_copy(
            update={
                "spec": base.contents.spec.model_copy(update={"strategy": definitions.root}),
                "scale_proof": None,
            }
        )
    )
    config = request(
        walk=True,
        search=SearchSpace(
            target="strategy_parameters", axes=(SearchAxis(name="weight", values=("0.5", "1")),)
        ),
    )
    output = tmp_path / "python-study"
    evaluate_study(base, definitions, config, rules=rules, reader=reader, destination=output)
    _, plan, report = read_study(output)
    assert all(
        json.loads(candidate.strategy.parameters_json)["fixed"] == 7
        for candidate in plan.candidates
    )
    assert all(
        trial.metrics.filled_orders == 1
        for window in report.windows
        for trial in (*window.training, window.test)
    )
    assert report.oos_compounded_return == Decimal("-0.4")


def test_rehashed_but_false_winner_is_rejected_by_semantic_reader(tmp_path):
    from tivelo_backtest.archives import _record
    from tivelo_backtest.study_models import StudyArchive

    manifest, plan, report, inputs = execute(tmp_path)
    output = inputs[-1]
    window = report.windows[0]
    wrong = next(
        candidate.candidate_id
        for candidate in plan.candidates
        if candidate.candidate_id != window.winner_candidate_id
    )
    changed = report.model_copy(
        update={"windows": (window.model_copy(update={"winner_candidate_id": wrong}),)}
    )
    (output / "study-report.json").write_bytes(canonical_bytes(changed))
    files = tuple(
        _record(output, item.path) if item.path == "study-report.json" else item
        for item in manifest.contents.files
    )
    altered = StudyArchive.create(manifest.contents.model_copy(update={"files": files}))
    (output / "manifest.json").write_bytes(canonical_bytes(altered))
    with pytest.raises(TiveloError) as error:
        read_study(output)
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


@pytest.mark.parametrize("after_commit", [False, True])
def test_study_commit_boundary_preserves_success_only_after_marker(
    tmp_path, monkeypatch, after_commit
):
    import os

    original = os.link
    destination = tmp_path / "study"

    def link(source, target, *args, **kwargs):
        if target == destination / "manifest.json":
            if after_commit:
                original(source, target, *args, **kwargs)
            raise KeyboardInterrupt()
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    if after_commit:
        manifest, _, _, _ = execute(tmp_path)
        assert read_study(destination)[0] == manifest
    else:
        with pytest.raises(KeyboardInterrupt):
            execute(tmp_path)
        assert not destination.exists()
    assert not list(tmp_path.glob(".tivelo-study-*"))
