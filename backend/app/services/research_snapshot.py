"""Publish and load one immutable, internally consistent research bundle."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.data_providers.trust import load_latest_audits
from app.file_io import replace_with_retry
from app.services import strategy_cache
from app.services.advisor import evaluate_data_gate

_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = {1, 2}
_SOURCE_DATASETS = ("kline_daily", "kline_daily_enriched")
_VOLATILE_AUDIT_FIELDS = {"recorded_at", "schema_errors"}
_VOLATILE_CACHE_FIELDS = {"updated_at", "enriched_mtime"}


class ResearchSnapshotRejectedError(RuntimeError):
    """The persisted data and strategy cache do not form a publishable bundle."""


class ResearchSnapshotCorruptError(RuntimeError):
    """The published snapshot cannot be trusted or decoded."""


def publish_research_snapshot(data_dir: Path) -> dict:
    """Validate, freeze and atomically publish the latest research inputs."""
    data_dir = Path(data_dir)
    audits = load_latest_audits(data_dir)
    cache = strategy_cache.read_cache(data_dir)
    if not isinstance(cache, dict):
        raise ResearchSnapshotRejectedError("策略缓存缺失或无法读取")

    as_of = str(cache.get("as_of") or "")
    if not as_of:
        raise ResearchSnapshotRejectedError("策略缓存缺少交易日期")

    results = cache.get("results")
    if not isinstance(results, dict) or not results:
        raise ResearchSnapshotRejectedError("策略缓存没有可发布的策略结果")
    wrong_dates = sorted(
        str(strategy_id)
        for strategy_id, result in results.items()
        if not isinstance(result, dict) or str(result.get("as_of") or "") != as_of
    )
    if wrong_dates:
        raise ResearchSnapshotRejectedError(
            f"策略结果日期不一致: {', '.join(wrong_dates)}"
        )

    gate = evaluate_data_gate(audits, as_of)
    if gate["decision"] != "PASS":
        detail = "; ".join(str(reason) for reason in gate["reasons"])
        raise ResearchSnapshotRejectedError(f"数据检查未通过: {detail}")

    frozen_audits = sorted(
        (_json_clone(audit) for audit in audits),
        key=lambda item: str(item.get("dataset") or ""),
    )
    frozen_cache = _json_clone(cache)
    source_evidence = _source_evidence(data_dir, as_of)
    snapshot_id = _snapshot_id(
        as_of,
        frozen_audits,
        frozen_cache,
        source_evidence,
    )
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "published_at": datetime.now(UTC).isoformat(),
        "audits": frozen_audits,
        "strategy_cache": frozen_cache,
        "source_evidence": source_evidence,
    }

    root = data_dir / "research_snapshots"
    archive = root / f"date={as_of}" / f"{snapshot_id}.json"
    latest = root / "latest.json"
    if archive.exists():
        payload = _load_snapshot_file(archive)
        if payload.get("snapshot_id") != snapshot_id:
            raise ResearchSnapshotCorruptError("研究快照归档与文件名不一致")
    else:
        _atomic_write_json(archive, payload)
    _atomic_write_json(latest, payload)
    return payload


def load_latest_research_snapshot(data_dir: Path) -> dict | None:
    """Load the latest published bundle and verify its content hash."""
    path = Path(data_dir) / "research_snapshots" / "latest.json"
    if not path.exists():
        return None
    return _load_snapshot_file(path)


def is_research_date_sealed(data_dir: Path, as_of: str) -> bool:
    """Return whether ``as_of`` has a valid bundle matching persisted sources."""
    try:
        snapshot = load_latest_research_snapshot(data_dir)
    except ResearchSnapshotCorruptError:
        return False
    if not snapshot or str(snapshot.get("as_of") or "") != str(as_of):
        return False
    return research_snapshot_source_problem(Path(data_dir), snapshot) is None


def load_research_snapshot_history(
    data_dir: Path,
    *,
    before_as_of: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Load at most one verified immutable snapshot per prior trading date."""
    root = Path(data_dir) / "research_snapshots"
    if not root.exists() or limit <= 0:
        return []

    dated_snapshots: list[dict] = []
    date_dirs = sorted(
        (
            path
            for path in root.glob("date=*")
            if path.is_dir() and len(path.name) == len("date=YYYY-MM-DD")
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for date_dir in date_dirs:
        partition_as_of = date_dir.name.removeprefix("date=")
        if before_as_of and partition_as_of >= str(before_as_of):
            continue
        valid: list[dict] = []
        for path in date_dir.glob("*.json"):
            try:
                snapshot = _load_snapshot_file(path)
            except ResearchSnapshotCorruptError:
                continue
            if str(snapshot.get("as_of") or "") == partition_as_of:
                valid.append(snapshot)
        if not valid:
            continue
        valid.sort(
            key=lambda value: (
                str(value.get("published_at") or ""),
                str(value.get("snapshot_id") or ""),
            ),
            reverse=True,
        )
        dated_snapshots.append(valid[0])
        if len(dated_snapshots) >= limit:
            break
    dated_snapshots.reverse()
    return dated_snapshots


def research_snapshot_source_problem(data_dir: Path, snapshot: dict) -> dict | None:
    """Fail closed when the same-day source partitions drift after publication."""
    problem = {
        "code": "RESEARCH_SOURCE_DRIFT_AFTER_PUBLICATION",
        "reason": "研究快照发布后, 同日行情源文件又发生变化, 当前候选已失效",
        "next_action": "请重新运行一次盘后刷新, 用最终行情重算策略并发布新快照。",
    }
    as_of = str(snapshot.get("as_of") or "")
    if not as_of:
        return problem

    schema_version = snapshot.get("schema_version")
    if schema_version == 2:
        expected = snapshot.get("source_evidence")
        if not isinstance(expected, dict):
            return problem
        try:
            current = _source_evidence(Path(data_dir), as_of)
        except ResearchSnapshotRejectedError:
            return problem
        for dataset in _SOURCE_DATASETS:
            expected_item = expected.get(dataset)
            current_item = current.get(dataset)
            if not isinstance(expected_item, dict) or expected_item != current_item:
                return problem
        return None

    if schema_version != 1:
        return problem
    try:
        published_at = datetime.fromisoformat(
            str(snapshot.get("published_at") or "").replace("Z", "+00:00")
        )
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        published_timestamp = published_at.timestamp()
    except (TypeError, ValueError, OverflowError):
        return problem
    for dataset in _SOURCE_DATASETS:
        path = _source_partition_path(Path(data_dir), dataset, as_of)
        try:
            modified_timestamp = path.stat().st_mtime
        except OSError:
            return problem
        if modified_timestamp > published_timestamp:
            return problem
    return None


def _load_snapshot_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResearchSnapshotCorruptError("研究快照无法读取") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS
    ):
        raise ResearchSnapshotCorruptError("研究快照结构版本无效")

    as_of = str(payload.get("as_of") or "")
    audits = payload.get("audits")
    cache = payload.get("strategy_cache")
    if not as_of or not isinstance(audits, list) or not isinstance(cache, dict):
        raise ResearchSnapshotCorruptError("研究快照缺少必要字段")
    schema_version = payload.get("schema_version")
    source_evidence = payload.get("source_evidence")
    if schema_version == 2 and not _valid_source_evidence(source_evidence, as_of):
        raise ResearchSnapshotCorruptError("研究快照缺少行情源校验信息")
    expected_id = _snapshot_id(
        as_of,
        audits,
        cache,
        source_evidence if schema_version == 2 else None,
    )
    if payload.get("snapshot_id") != expected_id:
        raise ResearchSnapshotCorruptError("研究快照校验值不匹配")
    return payload


def _snapshot_id(
    as_of: str,
    audits: list[dict],
    cache: dict,
    source_evidence: dict | None = None,
) -> str:
    audit_basis = [
        {
            key: value
            for key, value in audit.items()
            if key not in _VOLATILE_AUDIT_FIELDS
        }
        for audit in audits
    ]
    cache_basis = {
        key: value
        for key, value in cache.items()
        if key not in _VOLATILE_CACHE_FIELDS
    }
    basis = {
        "as_of": as_of,
        "audits": audit_basis,
        "strategy_cache": cache_basis,
    }
    if source_evidence is not None:
        basis["source_evidence"] = source_evidence
    encoded = json.dumps(
        basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_partition_path(data_dir: Path, dataset: str, as_of: str) -> Path:
    return Path(data_dir) / dataset / f"date={as_of}" / "part.parquet"


def _source_evidence(data_dir: Path, as_of: str) -> dict:
    evidence: dict[str, dict] = {}
    for dataset in _SOURCE_DATASETS:
        path = _source_partition_path(data_dir, dataset, as_of)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ResearchSnapshotRejectedError(
                f"缺少 {dataset} 的 {as_of} 源文件, 无法发布可验证研究快照"
            ) from exc
        evidence[dataset] = {
            "path": f"{dataset}/date={as_of}/part.parquet",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return evidence


def _valid_source_evidence(value, as_of: str) -> bool:
    if not isinstance(value, dict) or set(value) != set(_SOURCE_DATASETS):
        return False
    for dataset in _SOURCE_DATASETS:
        item = value.get(dataset)
        if not isinstance(item, dict):
            return False
        if item.get("path") != f"{dataset}/date={as_of}/part.parquet":
            return False
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            return False
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            return False
    return True


def _json_clone(value):
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchSnapshotRejectedError("研究输入包含不可序列化或非有限值") from exc
    return json.loads(encoded)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
