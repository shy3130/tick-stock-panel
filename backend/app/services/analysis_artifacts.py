"""M15+M16：隔离的 append-only 分析 artifact / 失败队列存储与显式重放计划。

研究 / 审计 artifact 存储，**不是**订单或策略事实源。

布局::

    {data_dir}/user_data/ai_attempts/
    ├── attempts/{attempt_id}.json   # 每次 attempt（ok/failed/cancelled）的完整 artifact
    ├── failed/{attempt_id}.json     # 仅失败 artifact 的副本（失败待处理队列）
    └── index.jsonl                  # append-only 索引（安全元数据投影）

安全边界（产品红线）：
- 安全 metadata 仅来自 ``StructuredAIResult`` 与 audit 投影，**绝不**保存完整 prompt、
  API key、账户 / 持仓或完整交易流水；不写 ``raw_text``。
- append-only index + 原子单 artifact 写；旧结果不可覆盖；重放必须生成新 attempt_id 并带
  ``parent_attempt_id`` 关联。
- 公开 replay 仅返回计划 / 提示，**不**执行 provider 调用，**不**写入交易事件 —— 本模块不
  import 任何 provider / trade store，结构上保证（见 ``test_no_provider_or_trade_dependency``）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.services.ai_structured import (
    AnalysisArtifact,
    AnalysisTraceNode,
    StructuredAIResult,
    new_attempt_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AnalysisArtifact",
    "ArtifactError",
    "ArtifactExistsError",
    "ReplayPlan",
    "build_artifact",
    "is_replayable",
    "list_artifacts",
    "list_failed",
    "new_artifact_id",
    "read",
    "read_index",
    "record",
    "record_result",
    "replay_plan",
    "store_root",
    "validate",
]

# ── 错误分类语义 ──────────────────────────────────────────
# AIErrorCategory 无独立 "auth" 类；凭据 / 鉴权失败归入 "provider"。
# 重放不可达集合 = 运行时类（quota / cancelled / provider），与 RetryPolicy「不内容重试」一致。
_NOT_REPLAYABLE_CATEGORIES: frozenset[str] = frozenset({"quota", "cancelled", "provider"})

# id 仅允许 [A-Za-z0-9_-]，杜绝路径穿越（attempt_id / artifact_id 均由工厂生成，天然匹配）。
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_lock = threading.Lock()


class ArtifactError(Exception):
    """artifact 存储错误基类（路径穿越、文件损坏等）。"""


class ArtifactExistsError(ArtifactError):
    """attempt 已存在 —— append-only 语义下拒绝覆盖历史。"""


def new_artifact_id() -> str:
    """artifact id: ``art_<uuid4_hex>``。"""
    return f"art_{uuid4().hex}"


# ── 路径 / 布局 ───────────────────────────────────────────
def store_root(data_dir: Path) -> Path:
    """ai_attempts 存储根目录。"""
    return Path(data_dir) / "user_data" / "ai_attempts"


def _attempts_dir(data_dir: Path) -> Path:
    d = store_root(data_dir) / "attempts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _failed_dir(data_dir: Path) -> Path:
    d = store_root(data_dir) / "failed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path(data_dir: Path) -> Path:
    p = store_root(data_dir) / "index.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _safe_id(value: str) -> str:
    if not value or not _SAFE_ID_RE.match(value):
        raise ArtifactError(f"unsafe id rejected: {value!r}")
    return value


def _checked_path(directory: Path, name: str) -> Path:
    """返回 ``directory/<name>.json``，并校验解析后仍位于 directory 内（路径穿越防御）。"""
    safe = _safe_id(name)
    target = (directory / f"{safe}.json").resolve()
    if target.parent != directory.resolve():
        raise ArtifactError(f"path escape rejected: {name!r}")
    return target


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# ── 安全 builder ──────────────────────────────────────────
def build_artifact(
    result: StructuredAIResult,
    *,
    symbol: str | None = None,
    market: str | None = None,
    adjustment: Literal["qfq", "hfq", "none"] | None = None,
    source_refs: list[str] | None = None,
    data_as_of: datetime | None = None,
    schema_version: str = "v1",
    prompt_version: str = "",
    program_rules_version: str | None = None,
    trace: list[AnalysisTraceNode] | None = None,
    parent_attempt_id: str | None = None,
    artifact_id: str | None = None,
    include_result: bool = True,
) -> AnalysisArtifact:
    """从 ``StructuredAIResult`` 构建安全的 ``AnalysisArtifact``。

    仅读取结果字段（provider / model / usage / error / warnings / data），**不**接收
    messages、api key 或账户 / 持仓 / 完整交易流水；不写 ``raw_text``。``data`` 仅在为 dict
    时保留（结构化输出投影），否则丢弃 —— 杜绝把任意非结构化对象落盘。
    """
    data: dict[str, Any] | None = None
    if include_result and isinstance(result.data, dict):
        data = result.data
    return AnalysisArtifact(
        id=artifact_id or new_artifact_id(),
        attempt_id=result.attempt_id,
        request_id=result.request_id,
        purpose=result.purpose,
        status=result.status,
        schema_version=schema_version,
        prompt_version=prompt_version,
        program_rules_version=program_rules_version,
        created_at=result.created_at,
        data_as_of=data_as_of,
        symbol=symbol,
        market=market,
        adjustment=adjustment,
        source_refs=list(source_refs) if source_refs else [],
        provider=result.provider,
        profile_id=result.profile_id,
        model=result.model,
        result=data,
        error=result.error,
        trace=list(trace) if trace else [],
        warnings=list(result.warnings),
        usage=result.usage,
        parent_attempt_id=parent_attempt_id,
    )


def validate(artifact: AnalysisArtifact) -> AnalysisArtifact:
    """显式 schema 校验：dump → 再解析，确保 artifact 可持久化且结构合法。"""
    return AnalysisArtifact.model_validate(artifact.model_dump(mode="json"))


# ── 原子写 / append-only 索引 ─────────────────────────────
def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子单文件写：tmp + os.replace。仅写新内容，不整文件覆盖历史 index。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _index_line(artifact: AnalysisArtifact) -> dict[str, Any]:
    """index.jsonl 的安全元数据投影 —— 不含 result / error 正文 / raw_text / messages。"""
    usage = artifact.usage
    return {
        "artifact_id": artifact.id,
        "attempt_id": artifact.attempt_id,
        "request_id": artifact.request_id,
        "purpose": artifact.purpose,
        "status": artifact.status,
        "schema_version": artifact.schema_version,
        "provider": artifact.provider,
        "profile_id": artifact.profile_id,
        "model": artifact.model,
        "symbol": artifact.symbol,
        "market": artifact.market,
        "created_at": _iso(artifact.created_at),
        "data_as_of": _iso(artifact.data_as_of),
        "error_category": artifact.error.category if artifact.error else None,
        "parent_attempt_id": artifact.parent_attempt_id,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "cached_prompt_tokens": usage.cached_prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        },
        "has_result": artifact.result is not None,
    }


def _append_index(data_dir: Path, line: dict[str, Any]) -> None:
    """append-only：永远在文件尾追加，不覆盖已有行。"""
    p = _index_path(data_dir)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def record(data_dir: Path, artifact: AnalysisArtifact) -> AnalysisArtifact:
    """持久化 artifact：原子写 attempts 文件 →（失败时）failed 副本 → append index。

    - append-only：同 attempt_id 已存在则 ``ArtifactExistsError``，绝不覆盖历史。
    - failed 副本仅对 status == "failed" 写入（失败队列隔离）。
    - index 永远 append，不覆盖已有行。
    - 全程持 ``_lock``，写序为「文件 → 索引」，读到的索引行必有对应文件。
    """
    validate(artifact)
    payload = artifact.model_dump(mode="json")
    with _lock:
        apath = _checked_path(_attempts_dir(data_dir), artifact.attempt_id)
        if apath.exists():
            raise ArtifactExistsError(f"attempt already recorded: {artifact.attempt_id}")
        _atomic_write_json(apath, payload)
        if artifact.status == "failed":
            fpath = _checked_path(_failed_dir(data_dir), artifact.attempt_id)
            _atomic_write_json(fpath, payload)
        _append_index(data_dir, _index_line(artifact))
    return artifact


def record_result(
    data_dir: Path,
    result: StructuredAIResult,
    **ctx: Any,
) -> AnalysisArtifact:
    """便捷入口：``build_artifact(result, **ctx)`` → ``record``。"""
    return record(data_dir, build_artifact(result, **ctx))


# ── 读 / 列 ──────────────────────────────────────────────
def read(data_dir: Path, attempt_id: str) -> AnalysisArtifact | None:
    """读取单个 artifact（按 attempt_id）。不存在返回 None；损坏抛 ``ArtifactError``。"""
    with _lock:
        p = _checked_path(_attempts_dir(data_dir), attempt_id)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ArtifactError(f"corrupt artifact {attempt_id}: {e}") from e
    return AnalysisArtifact.model_validate(raw)


def _iter_dir(directory: Path) -> list[AnalysisArtifact]:
    """读取目录下全部 artifact；损坏 / 不合法的文件被跳过并告警（不阻断读取）。"""
    out: list[AnalysisArtifact] = []
    for p in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("skipping corrupt artifact file: %s", p)
            continue
        try:
            out.append(AnalysisArtifact.model_validate(raw))
        except Exception as e:  # pydantic ValidationError 等
            logger.warning("skipping invalid artifact %s: %s", p, e)
            continue
    return out


def list_artifacts(
    data_dir: Path,
    *,
    status: str | None = None,
    purpose: str | None = None,
    limit: int | None = None,
) -> list[AnalysisArtifact]:
    """列出全部 artifact（按 created_at 倒序）。可按 status / purpose 过滤。"""
    items = _iter_dir(_attempts_dir(data_dir))
    if status:
        items = [a for a in items if a.status == status]
    if purpose:
        items = [a for a in items if a.purpose == purpose]
    items.sort(key=lambda a: a.created_at, reverse=True)
    if limit is not None:
        items = items[:limit]
    return items


def list_failed(
    data_dir: Path,
    *,
    purpose: str | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[AnalysisArtifact]:
    """失败待处理队列（仅 failed/ 目录下的 artifact）。可按 purpose / error.category 过滤。"""
    items = _iter_dir(_failed_dir(data_dir))
    if purpose:
        items = [a for a in items if a.purpose == purpose]
    if category:
        items = [a for a in items if a.error is not None and a.error.category == category]
    items.sort(key=lambda a: a.created_at, reverse=True)
    if limit is not None:
        items = items[:limit]
    return items


def read_index(
    data_dir: Path,
    *,
    status: str | None = None,
    purpose: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读取 index.jsonl 的安全元数据投影（轻量目录，按追加顺序；limit 取最近 N 条）。

    不含 result / error 正文；用于检索而非事实回放。
    """
    p = _index_path(data_dir)
    out: list[dict[str, Any]] = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if status:
        out = [e for e in out if e.get("status") == status]
    if purpose:
        out = [e for e in out if e.get("purpose") == purpose]
    if limit is not None:
        out = out[-limit:]
    return out


# ── 显式重放计划（纯数据，不执行）────────────────────────
class ReplayPlan(BaseModel):
    """显式重放计划 —— 仅返回计划 / 提示，不执行 provider 调用，不写入交易事件。

    ``replayable`` 标记是否建议重放；quota / auth(→provider) / cancelled 不可达。
    重放生成新 attempt_id（``new_attempt_id``），通过 ``parent_attempt_id`` 关联原 attempt，
    旧结果保留不覆盖。
    """

    model_config = ConfigDict(extra="ignore")

    new_attempt_id: str
    parent_attempt_id: str
    artifact_id: str
    purpose: str
    replayable: bool
    reason: str | None = None
    profile_id: str | None = None
    model: str
    profile_change_hint: str
    must_refresh_data: bool = True
    data_as_of: str | None = None  # 原始（已过期），重放前必须刷新
    symbol: str | None = None
    warnings: list[str] = Field(default_factory=list)
    note: str = "重放计划：不执行 AI 调用，不写入交易事件；需用户/管理员显式触发。"


def is_replayable(artifact: AnalysisArtifact) -> bool:
    """失败且错误分类属内容类（syntax/missing/invalid/plaintext）才可重放。

    quota / auth(→provider) / cancelled 不可自动重放；非 failed 不可重放。
    """
    if artifact.status != "failed" or artifact.error is None:
        return False
    return artifact.error.category not in _NOT_REPLAYABLE_CATEGORIES


def replay_plan(artifact: AnalysisArtifact) -> ReplayPlan | None:
    """为失败 artifact 构建显式重放计划；非 failed 或缺少错误分类返回 None。

    **不**执行 provider 调用，**不**写入交易事件（本模块无 provider / trade 依赖）。
    重放生成新 attempt_id，通过 ``parent_attempt_id`` 关联原 attempt（旧结果不可覆盖）。
    """
    if artifact.status != "failed" or artifact.error is None:
        return None
    category = artifact.error.category
    replayable = category not in _NOT_REPLAYABLE_CATEGORIES
    if replayable:
        reason = f"内容类失败（{category}），可由用户显式重放（生成新 attempt，旧结果保留）。"
        warnings: list[str] = []
    else:
        mapping = {
            "quota": "配额/quota 失败，需恢复额度或更换 profile 后由用户显式重放。",
            "cancelled": "已取消的 attempt 不自动重放。",
            "provider": "provider/auth 类失败（鉴权/网络/上游），不自动内容重放。",
        }
        reason = mapping.get(category, "不可自动重放。")
        warnings = ["当前分类不建议自动重放；如需重放请用户显式触发并先排查根因。"]
    return ReplayPlan(
        new_attempt_id=new_attempt_id(),
        parent_attempt_id=artifact.attempt_id,
        artifact_id=artifact.id,
        purpose=artifact.purpose,
        replayable=replayable,
        reason=reason,
        profile_id=artifact.profile_id,
        model=artifact.model,
        profile_change_hint=(
            f"原 model={artifact.model!r}；重放将使用当前 profile，请确认 model 是否已变化。"
        ),
        must_refresh_data=True,
        data_as_of=_iso(artifact.data_as_of),
        symbol=artifact.symbol,
        warnings=warnings,
    )
