import errno
import hashlib
import os
import stat
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from test_daily import case
from tivelo_contracts.artifacts import ArchiveContents, ResultArchive
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import RunLock, RunLockContents

from tivelo_backtest.archives import (
    publish_result,
    publish_run,
    read_result,
    read_result_inputs,
    read_run,
)
from tivelo_backtest.definitions import DefinitionLoader, seal_definition


def inputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "strategy.py").write_text(
        "def build(*, parameters_json, dependencies):\n    raise RuntimeError('never execute while archiving')\n",
        encoding="utf-8",
    )
    bundle = seal_definition(
        source,
        definition_id="strategy.fixture",
        version="1",
        kind="strategy",
        entrypoint="strategy:build",
        files=("strategy.py",),
    )
    (source / "definition.json").write_bytes(canonical_bytes(bundle))
    definitions = DefinitionLoader().inspect(source)
    runner, lock, reader, binding = case()
    values = lock.contents.model_dump()
    values["spec"]["strategy"] = definitions.root.model_dump()
    lock = RunLock.create(RunLockContents.model_validate(values))
    result = runner.run(lock, reader, replace(binding, reference=definitions.root))
    return lock, definitions, result


def test_archive_preserves_definitions_and_exact_result_without_original_source(tmp_path):
    lock, definitions, result = inputs(tmp_path)
    destination = tmp_path / "run"
    manifest = publish_run(lock, definitions, destination)
    assert publish_run(lock, definitions, destination) == manifest
    (tmp_path / "source/strategy.py").write_text("edited", encoding="utf-8")
    restored_lock, restored_definitions = read_run(destination)
    assert restored_lock == lock
    assert restored_definitions == definitions
    output = tmp_path / "result"
    published = publish_result(lock, definitions, result, output)
    assert publish_result(lock, definitions, result, output) == published
    assert read_result_inputs(output) == (published, lock, definitions)
    actual_lock, actual_result, metrics = read_result(output)
    assert actual_lock == lock
    assert canonical_bytes(actual_result) == canonical_bytes(result)
    assert metrics.final_equity == Decimal("11000")
    assert metrics.total_return == Decimal("0.1")
    assert metrics.maximum_drawdown == 0
    assert metrics.filled_orders == 2
    assert pl.read_parquet(output / "orders.parquet").schema["raw_fill_price"] == pl.Decimal(38, 12)
    assert pl.read_parquet(output / "equity.parquet").schema["at"] == pl.Datetime("us", "UTC")
    assert not (output / "result.json").exists()  # Large event tables are columnar.
    assert (published.format_version, published.encoding) == ("1.1", "parquet-v2")
    assert {order.order_type for order in actual_result.orders} == {"market"}


def test_result_archive_accepts_different_path_and_handle_ctime(tmp_path, monkeypatch):
    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    publish_result(lock, definitions, result, output)
    original_stat = Path.stat

    def stat_with_creation_time(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path != output / "orders.parquet":
            return info
        # Windows Python 3.12 can return creation time for stat(path)
        # and metadata change time for fstat(fd).
        fields = list(info)
        fields[stat.ST_CTIME] -= 10
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", stat_with_creation_time)
    actual_lock, actual_result, _ = read_result(output)
    assert actual_lock == lock
    assert canonical_bytes(actual_result) == canonical_bytes(result)


def test_old_result_layout_remains_readable_without_inventing_an_order_type(tmp_path):
    lock, definitions, result = inputs(tmp_path)
    historical = result.model_copy(
        update={
            "orders": tuple(
                order.model_copy(update={"order_type": None}) for order in result.orders
            )
        }
    )
    output = tmp_path / "historical-result"
    published = publish_result(lock, definitions, historical, output)
    assert (published.format_version, published.encoding) == ("1.0", "parquet-v1")
    assert "order_type" not in pl.read_parquet(output / "orders.parquet").columns
    _, actual, _ = read_result(output)
    assert canonical_bytes(actual) == canonical_bytes(historical)
    assert all(order.order_type is None for order in actual.orders)


def test_limit_orders_roundtrip_raw_limits_as_exact_decimals(tmp_path):
    lock, definitions, result = inputs(tmp_path)
    limit_result = result.model_copy(
        update={
            "orders": tuple(
                order.model_copy(
                    update={
                        "order_type": "limit",
                        "raw_limit_price": Decimal("10" if order.side == "buy" else "11"),
                    }
                )
                for order in result.orders
            )
        }
    )
    output = tmp_path / "limit-result"
    publish_result(lock, definitions, limit_result, output)
    assert pl.read_parquet(output / "orders.parquet").schema["raw_limit_price"] == pl.Decimal(
        38, 12
    )
    _, actual, _ = read_result(output)
    assert canonical_bytes(actual) == canonical_bytes(limit_result)


@pytest.mark.parametrize("fault", ["fill_exceeds_limit", "order_type_changes", "wrong_layout"])
def test_resealed_result_cannot_hide_inconsistent_order_metadata(tmp_path, fault):
    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    publish_result(lock, definitions, result, output)
    path = "orders.parquet"
    table = pl.read_parquet(output / path)
    if fault == "wrong_layout":
        table = table.drop("order_type", "raw_limit_price")
    else:
        selected = pl.col("side") == "buy"
        if fault == "order_type_changes":
            selected &= pl.col("status") == "created"
        table = table.with_columns(
            pl.when(selected)
            .then(pl.lit("limit"))
            .otherwise(pl.col("order_type"))
            .alias("order_type"),
            pl.when(selected)
            .then(pl.lit(Decimal("9" if fault == "fill_exceeds_limit" else "10")))
            .otherwise(pl.col("raw_limit_price"))
            .cast(pl.Decimal(38, 12))
            .alias("raw_limit_price"),
        )
    table.write_parquet(output / path)
    reseal_file(output, path)
    with pytest.raises(TiveloError) as error:
        read_result(output)
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


@pytest.mark.parametrize("relative", ["run-lock.json", "orders.parquet", "metrics.json"])
def test_changed_published_files_are_rejected_without_overwriting(tmp_path, relative):
    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    publish_result(lock, definitions, result, output)
    original = (output / relative).read_bytes()
    (output / relative).write_bytes(original + b"tampered")
    with pytest.raises(TiveloError) as error:
        read_result(output)
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT
    with pytest.raises(TiveloError):
        publish_result(lock, definitions, result, output)
    assert (output / relative).read_bytes() == original + b"tampered"


def test_missing_marker_and_unknown_destination_are_never_overwritten(tmp_path):
    lock, definitions, _ = inputs(tmp_path)
    output = tmp_path / "unknown"
    output.mkdir()
    (output / "keep.txt").write_bytes(b"mine")
    with pytest.raises(TiveloError):
        publish_run(lock, definitions, output)
    assert (output / "keep.txt").read_bytes() == b"mine"
    assert not (output / "manifest.json").exists()
    with pytest.raises(TiveloError):
        read_run(output)


def test_failed_publication_has_no_marker_and_preserves_unrelated_files(tmp_path, monkeypatch):
    import tivelo_backtest.archives as module

    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    original = module.os.link

    def failing_link(source, destination, *args, **kwargs):
        if str(destination).endswith("manifest.json"):
            (output / "keep.txt").write_bytes(b"arrived during publication")
            raise OSError(errno.ENOSPC, "synthetic disk full")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", failing_link)
    with pytest.raises(TiveloError) as error:
        publish_result(lock, definitions, result, output)
    assert error.value.code == ErrorCode.PUBLICATION_FAILED
    assert sorted(p.name for p in output.iterdir()) == ["keep.txt"]


def test_wrong_lock_or_forged_definition_never_publishes(tmp_path):
    lock, definitions, result = inputs(tmp_path)
    wrong = result.model_copy(update={"lock_id": "1" * 64})
    with pytest.raises(TiveloError):
        publish_result(lock, definitions, wrong, tmp_path / "result")
    forged = replace(
        definitions, nodes=(replace(definitions.nodes[0], files=(("strategy.py", b"bad"),)),)
    )
    with pytest.raises(TiveloError):
        publish_run(lock, forged, tmp_path / "run")
    assert not (tmp_path / "result/manifest.json").exists()
    assert not (tmp_path / "run/manifest.json").exists()


def test_replay_mismatch_never_publishes_changed_result(tmp_path):
    lock, definitions, result = inputs(tmp_path)
    expected = publish_result(lock, definitions, result, tmp_path / "original")
    changed = result.model_copy(update={"diagnostics": (*result.diagnostics, "different")})
    output = tmp_path / "replay"
    with pytest.raises(TiveloError) as error:
        publish_result(lock, definitions, changed, output, expected_archive=expected)
    assert error.value.code == ErrorCode.DATA_CONFLICT
    assert not output.exists()
    assert publish_result(lock, definitions, result, output, expected_archive=expected) == expected


def test_symlinked_archive_and_declared_file_are_rejected(tmp_path):
    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    publish_result(lock, definitions, result, output)
    link = tmp_path / "alias"
    try:
        link.symlink_to(output, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    with pytest.raises(TiveloError):
        read_result(link)
    outside = tmp_path / "outside"
    (output / "metrics.json").rename(outside)
    (output / "metrics.json").symlink_to(outside)
    with pytest.raises(TiveloError):
        read_result(output)


def reseal_file(output, path):
    manifest = ResultArchive.model_validate_json((output / "manifest.json").read_bytes())
    payload = (output / path).read_bytes()
    contents = ArchiveContents(
        lock_id=manifest.contents.lock_id,
        files=tuple(
            file.model_copy(
                update={
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
            if file.path == path
            else file
            for file in manifest.contents.files
        ),
    )
    (output / "manifest.json").write_bytes(
        canonical_bytes(ResultArchive.create(contents, encoding=manifest.encoding))
    )


@pytest.mark.parametrize("change", ["schema", "order", "metrics"])
def test_result_reader_validates_structure_beyond_file_checksums(tmp_path, change):
    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    publish_result(lock, definitions, result, output)
    if change == "metrics":
        path = "metrics.json"
        payload = (
            (output / path).read_text().replace('"final_equity":"11000"', '"final_equity":"11001"')
        )
        (output / path).write_text(payload, encoding="utf-8")
    else:
        path = "equity.parquet"
        table = pl.read_parquet(output / path)
        table = (
            table.with_columns(pl.col("equity").cast(pl.Float64))
            if change == "schema"
            else table.reverse()
        )
        table.write_parquet(output / path)
    reseal_file(output, path)
    with pytest.raises(TiveloError) as error:
        read_result(output)
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


@pytest.mark.parametrize("kind", ["run", "result"])
def test_cancellation_during_publication_never_creates_success_marker(tmp_path, monkeypatch, kind):
    import tivelo_backtest.archives as module

    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    original = module.os.link
    cancelled = False

    class Observer:
        def cancelled(self):
            return cancelled

    def cancel_after_link(*args, **kwargs):
        nonlocal cancelled
        original(*args, **kwargs)
        cancelled = True

    monkeypatch.setattr(module.os, "link", cancel_after_link)
    with pytest.raises(TiveloError) as error:
        if kind == "run":
            publish_run(lock, definitions, output, observer=Observer())
        else:
            publish_result(lock, definitions, result, output, observer=Observer())
    assert error.value.code == ErrorCode.CANCELLED
    assert not output.exists()


def test_interrupt_after_commit_keeps_successful_complete_result(tmp_path, monkeypatch):
    import tivelo_backtest.archives as module

    lock, definitions, result = inputs(tmp_path)
    output = tmp_path / "result"
    original = module.os.link

    def interrupt_after_marker(source, target):
        original(source, target)
        if str(target).endswith("manifest.json"):
            raise KeyboardInterrupt()

    monkeypatch.setattr(module.os, "link", interrupt_after_marker)
    published = publish_result(lock, definitions, result, output)
    assert published.contents.lock_id == lock.lock_id
    assert read_result(output)[1] == result


def test_link_permission_failure_cannot_publish_success(tmp_path, monkeypatch):
    import tivelo_backtest.archives as module

    lock, definitions, _ = inputs(tmp_path)

    def unsupported(*args):
        raise PermissionError(errno.EACCES, "synthetic unavailable filesystem capability")

    monkeypatch.setattr(module.os, "link", unsupported)
    with pytest.raises(TiveloError) as error:
        publish_run(lock, definitions, tmp_path / "run")
    assert error.value.code == ErrorCode.PUBLICATION_FAILED
    assert not (tmp_path / "run").exists()


def test_parquet_writer_failure_is_safe_and_does_not_publish(tmp_path, monkeypatch):
    lock, definitions, result = inputs(tmp_path)

    def failing_write(*args, **kwargs):
        raise pl.exceptions.ComputeError("synthetic native writer failure")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", failing_write)
    with pytest.raises(TiveloError) as error:
        publish_result(lock, definitions, result, tmp_path / "result")
    assert error.value.code == ErrorCode.PUBLICATION_FAILED
    assert not (tmp_path / "result").exists()
