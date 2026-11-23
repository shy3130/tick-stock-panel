import pytest
from tivelo_contracts.data import Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data.jsonl import JSONLinesProvider
from tivelo_data.providers import FixedSourceResolver


def provider(path, request, coverage=None):
    return JSONLinesProvider(
        path,
        request,
        source_id="fixture",
        knowledge_basis="historical_versions",
        bar_convention_id="synthetic.day" if request.record_type == "bar" else None,
        coverage=coverage,
    )


def test_explicit_empty_event_coverage_is_locked_and_not_inferred(make_engine, tmp_path):
    engine, _, base = make_engine((), record_type="corporate_action")
    file = tmp_path / "events.jsonl"
    file.write_text("", encoding="utf-8")
    coverage = Coverage(
        status="empty",
        requested_units=1,
        completed_units=1,
        row_count=0,
        issues=(
            CoverageIssue(
                instrument_id="security-1",
                reason="no_events",
                detail="Source query confirms no events in the requested range",
            ),
        ),
    )
    source = provider(file, base.request, coverage)
    engine.resolver = FixedSourceResolver(source, source.configuration_digest)
    planned = engine.plan_sync(base.request)
    candidate = engine.sync(planned)
    assert candidate.version.contents.coverage == coverage
    version = engine.publish(candidate)
    assert engine.store.dataset_version(version.version_id).contents.coverage == coverage
    unknown = provider(file, base.request)
    engine.resolver = FixedSourceResolver(unknown, unknown.configuration_digest)
    assert engine.plan_sync(base.request).plan_id != planned.plan_id
    with pytest.raises(TiveloError) as error:
        engine.sync(planned)
    assert error.value.code == ErrorCode.DATA_CONFLICT
    unproven = engine.sync(engine.plan_sync(base.request))
    assert unproven.version.contents.coverage.completed_units == 0
    assert unproven.version.contents.coverage.issues[0].reason != "no_events"


def test_declared_counts_cannot_contradict_records(make_engine, make_bar, tmp_path):
    engine, _, base = make_engine((make_bar(),))
    file = tmp_path / "bars.jsonl"
    file.write_text(make_bar().model_dump_json(), encoding="utf-8")
    source = provider(
        file,
        base.request,
        Coverage(status="complete", requested_units=1, completed_units=1, row_count=2),
    )
    engine.resolver = FixedSourceResolver(source, source.configuration_digest)
    with pytest.raises(TiveloError) as error:
        engine.sync(engine.plan_sync(base.request))
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    assert not list((engine.store.root / "staging").iterdir())


def test_coverage_cannot_describe_another_instrument(make_engine, tmp_path):
    _, _, base = make_engine((), record_type="corporate_action")
    file = tmp_path / "events.jsonl"
    file.write_text("", encoding="utf-8")
    coverage = Coverage(
        status="empty",
        requested_units=1,
        completed_units=1,
        row_count=0,
        issues=(
            CoverageIssue(instrument_id="other", reason="no_events", detail="Different query"),
        ),
    )
    with pytest.raises(ValueError):
        provider(file, base.request, coverage)
