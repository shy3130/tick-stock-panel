"""Published repository daily/factor snapshots available to the strategy backtester."""

from dataclasses import dataclass
from datetime import date, time, timedelta

from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.base import Contract, Digest, fingerprint

from app.market_time import CN_TZ

from .artifacts import read_document, verify_completed
from .models import NodeId
from .preparation_models import LockedCapture, RepositoryDailyInput, RepositoryFactorInput
from .storage import JobError


class DailySnapshotRef(Contract):
    job_id: Digest
    node_id: NodeId
    snapshot_id: Digest


@dataclass(frozen=True)
class DailySnapshotScope:
    daily: RepositoryDailyInput
    factors: RepositoryFactorInput
    start: date
    end: date

    @property
    def symbols(self) -> dict[str, str]:
        return {entry.instrument_id: entry.symbol for entry in self.daily.symbols}


def read_published_snapshot(store, job_id, node_id):
    plan = store.plan(job_id)
    locked = next(
        (item.preparation for item in plan.inputs if item.request.node_id == node_id), None
    )
    if locked is None:
        raise JobError("NOT_FOUND", "任务中没有这个快照节点")
    stage = node_id + ".snapshot"
    artifact = verify_completed(store, plan, stage)
    bundle = SnapshotBundle.model_validate_json(
        read_document(store.path(artifact.path + "/bundle.json"))
    )
    if fingerprint(bundle) != artifact.identity:
        raise JobError("ARTIFACT_CORRUPT", "快照清单与已发布记录不符")
    return artifact, bundle, locked


def daily_snapshot_scope(
    locked: LockedCapture, bundle: SnapshotBundle,
) -> DailySnapshotScope | None:
    if len(locked.sources) != 2 or len(bundle.datasets) != 2:
        return None
    sources = [item.source for item in locked.sources]
    daily = next((s for s in sources if isinstance(s, RepositoryDailyInput)), None)
    factors = next((s for s in sources if isinstance(s, RepositoryFactorInput)), None)
    if daily is None or factors is None:
        return None
    if (
        daily.layout != "daily_raw_v1"
        or daily.request.price_basis != "raw"
        or factors.representation != "cumulative_forward"
        or daily.symbols != factors.symbols
        or daily.expected_provider != factors.expected_provider
        or daily.request.start != factors.request.start
        or daily.request.end != factors.request.end
    ):
        return None
    coverages = {
        dataset.contents.dataset_id: dataset.contents.coverage.status for dataset in bundle.datasets
    }
    if (
        coverages.get(daily.request.dataset_id) not in ("complete", "partial")
        or coverages.get(factors.request.dataset_id) != "complete"
    ):
        return None
    start = daily.request.start.astimezone(CN_TZ)
    end = daily.request.end.astimezone(CN_TZ)
    if start.time() != time.min or end.time() != time.min:
        return None
    return DailySnapshotScope(daily, factors, start.date(), end.date() - timedelta(days=1))
