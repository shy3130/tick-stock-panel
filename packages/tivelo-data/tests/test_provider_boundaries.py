import errno
import json

import pytest
from pydantic import ValidationError
from tivelo_contracts import ErrorCode, TiveloError
from tivelo_contracts.data import Coverage

from tivelo_data.providers import FetchPage, FixedSourceResolver, SyncPlan, local_context


def test_plan_does_not_create_any_store_files(make_bar, make_engine, tmp_path):
    root = tmp_path / "not-created"
    engine, provider, plan = make_engine((make_bar(),), directory=root)
    assert plan.request.record_type == "bar"
    assert provider.calls == 0
    assert not root.exists()


def test_changed_source_and_fake_knowledge_basis_are_rejected(make_bar, make_engine):
    engine, provider, plan = make_engine((make_bar(),), knowledge_basis="latest_reconstruction")
    raw = plan.model_dump(mode="json")
    raw["capability"]["knowledge_basis"] = "historical_versions"
    with pytest.raises(ValidationError):
        SyncPlan.model_validate(raw)
    engine.resolver = FixedSourceResolver(provider, "b" * 64)
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert raised.value.code == ErrorCode.DATA_CONFLICT
    assert provider.closed == 1 and provider.calls == 0


def test_cannot_create_staging_closes_source_without_fetching(make_bar, make_engine, monkeypatch):
    engine, provider, plan = make_engine((make_bar(),))

    def no_space():
        raise OSError(errno.ENOSPC, "synthetic full volume")

    monkeypatch.setattr(engine.store, "new_stage", no_space)
    with pytest.raises(OSError) as raised:
        engine.sync(plan)
    assert raised.value.errno == errno.ENOSPC
    assert provider.closed == 1 and provider.calls == 0


def test_staging_directory_sync_failure_preserves_other_candidates(
    make_bar, make_engine, monkeypatch
):
    from tivelo_data import storage

    engine, provider, plan = make_engine((make_bar(),))
    intact = engine.sync(plan)
    store = engine.store
    root = store.path("staging")
    previous = set(root.iterdir())
    original_sync = storage._sync_directory

    def fail(path):
        if path == root:
            raise OSError(errno.ENOSPC, "synthetic directory flush failure")
        original_sync(path)

    monkeypatch.setattr(storage, "_sync_directory", fail)
    with pytest.raises(OSError) as failure:
        engine.sync(plan)
    assert failure.value.errno == errno.ENOSPC and set(root.iterdir()) == previous
    assert provider.closed == 2 and store.candidate(intact) == intact


def test_cli_releases_its_selected_source_on_plan_conflict_and_sync(
    make_bar, make_engine, tmp_path, monkeypatch, capsys
):
    from tivelo_data import cli

    _, provider, plan = make_engine((make_bar(),))
    provider.configuration_digest = "a" * 64
    monkeypatch.setattr(
        cli, "_repository_source", lambda _: (plan.request, provider, local_context())
    )
    prefix = ["--root", str(tmp_path / "lake")]
    flags = ["--capture", "capture.json", "--repository", "archive"]
    assert cli.main([*prefix, "plan-repository", *flags]) == 0
    pin = tmp_path / "plan.json"
    pin.write_text(capsys.readouterr().out)
    assert provider.closed == 1 and provider.calls == 0
    provider.configuration_digest = "b" * 64
    sync = [*prefix, "sync-repository", *flags, "--locked-plan", str(pin)]
    assert cli.main(sync) == 1 and json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT"
    assert provider.closed == 2 and provider.calls == 0
    provider.configuration_digest = "a" * 64
    assert cli.main(sync) == 0
    assert provider.closed == 3 and provider.calls > 0


@pytest.mark.parametrize("previous_failure", [False, True])
def test_close_failure_discards_unreturned_candidate_and_preserves_first_error(
    make_bar, make_engine, previous_failure
):
    engine, provider, plan = make_engine((make_bar(),))
    closed = []

    def fail_close():
        closed.append(True)
        raise RuntimeError("private supplier detail")

    def fail_fetch(*_):
        raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Source authentication failed")

    provider.close = fail_close
    if previous_failure:
        provider.fetch = fail_fetch
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert raised.value.code == (
        ErrorCode.AUTHENTICATION_FAILED if previous_failure else ErrorCode.SOURCE_UNAVAILABLE
    )
    assert "private supplier detail" not in str(raised.value)
    assert closed == [True] and not list((engine.store.root / "staging").iterdir())
    with engine.store.connection() as db:
        assert db.execute("SELECT count(*) FROM versions").fetchone() == (0,)


def test_pagination_cycle_and_oversized_page_fail_without_success(make_bar, make_engine):
    engine, provider, plan = make_engine((make_bar(),))
    provider.fetch = lambda request, cursor, context: FetchPage((), "same")
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert raised.value.code == ErrorCode.CONTRACT_INVALID
    assert not list((engine.store.root / "staging").iterdir())
    engine.max_page_rows = 1
    provider.fetch = lambda request, cursor, context: FetchPage(
        (make_bar(), make_bar()),
        None,
        Coverage(status="complete", requested_units=1, completed_units=1, row_count=2),
    )
    with pytest.raises(TiveloError):
        engine.sync(plan)


def test_source_error_is_not_a_successful_empty_result(make_bar, make_engine):
    engine, provider, plan = make_engine((make_bar(),))

    def fail(*args):
        raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Credentials rejected")

    provider.fetch = fail
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert not raised.value.retryable
    assert raised.value.code == ErrorCode.AUTHENTICATION_FAILED
    assert provider.closed == 1


def test_declared_close_error_retains_public_code_and_retry_delay(make_bar, make_engine):
    engine, provider, plan = make_engine((make_bar(),))

    def busy():
        raise TiveloError(ErrorCode.RESOURCE_BUSY, "Source cleanup is busy", retry_after=3)

    provider.close = busy
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert (raised.value.code, raised.value.retry_after) == (ErrorCode.RESOURCE_BUSY, 3)
    assert not list((engine.store.root / "staging").iterdir())


def test_nonrepeating_endless_pagination_is_also_bounded(make_bar, make_engine):
    engine, provider, plan = make_engine((make_bar(),))
    engine.max_pages = 2
    provider.fetch = lambda request, cursor, context: FetchPage((), str(int(cursor or "0") + 1))
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert raised.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
