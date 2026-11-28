"""Explicitly save a new price-view input alongside the original immutable run."""

import os
import tempfile
from pathlib import Path

from pydantic import Field
from tivelo_backtest.archives import inspect_archive, publish_run
from tivelo_backtest.variants import PriceVariantRequest, derive_price_variant
from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.base import Digest, RelativePath
from tivelo_extensions.store import EnginePackageStore

from .artifacts import read_document, read_input_run
from .catalog import run_summary
from .price_views import JobPriceViews
from .storage import JobError


class SavePriceVariant(PriceVariantRequest):
    run: RelativePath
    snapshot: RelativePath
    source_lock_id: Digest = Field(
        description="Run lock displayed when choosing the original input"
    )


def save_price_variant(store, request):
    original, source = read_input_run(store, request.run)
    bundle = SnapshotBundle.model_validate_json(
        read_document(store.path(f"inputs/{request.snapshot}/bundle.json"))
    )
    if source.lock_id != request.source_lock_id or bundle.snapshot != source.contents.snapshot:
        raise JobError("DATA_CONFLICT", "原归档或快照已变化，请重新选择输入")
    view = request.price_view
    registry = None
    if view.mode == "extension":
        choices = EnginePackageStore(store.data_dir).price_choices()
        choice = next(
            (entry for entry in choices if entry["provider_id"] == view.provider_id), None
        )
        if not choice or choice["state"] != "available":
            state = choice and choice["state"]
            code = {"needs_license": "LICENSE_REQUIRED", "license_expired": "LICENSE_EXPIRED"}.get(
                state, "EXTENSION_INCOMPATIBLE"
            )
            raise JobError(code, "请在导入中心检查所选扩展的活动版本和本机凭证")
        registry = JobPriceViews(store.data_dir, 1)
    lock, definitions = derive_price_variant(
        store.path("inputs/" + request.run),
        request,
        extension_runtime=registry,
        expected_lock_id=source.lock_id,
    )
    if lock.lock_id == source.lock_id:
        return {"input": run_summary(request.run, original, source), "snapshot": request.snapshot}
    path = f"price-variants/{lock.lock_id}"
    target = store.path("inputs/" + path)
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=store.root, prefix=".price-input-") as temporary:
        staged = Path(temporary) / "run"
        manifest = publish_run(lock, definitions, staged)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = store.path("inputs/" + path)
            if target.exists():
                previous, _ = inspect_archive(target)
                if previous != manifest:
                    raise JobError("DATA_CONFLICT", "目标位置已有不同的回测输入")
            else:
                os.rename(staged, target)
            if os.name != "nt":
                for directory in (target.parent, target.parent.parent):
                    handle = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(handle)
                    finally:
                        os.close(handle)
            db.commit()
    return {"input": run_summary(path, manifest, lock), "snapshot": request.snapshot}
