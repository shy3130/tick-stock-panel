"""持仓分析 Agent 的审计式自进化闭环。

学习范围刻意收窄为三情景的主观先验概率。价格异常、数据新鲜度、L1/L2
权限和资金流口径属于硬安全门，永远不从反馈自动修改。反馈只保存脱敏后的
情景结果；至少 10 个去重观察且覆盖 5 个不同交易日、时间顺序留出验证，且
Brier score 不劣于当前 profile 后，候选才进入 validated。验证不会自动部署：
只能经显式 API 应用，并保留不删除审计文件的显式回滚路径。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_OUTCOMES = ("weak", "repair", "strong_attack")
_MIN_SAMPLES = 10
_MIN_DISTINCT_TRADE_DAYS = 5
_MIN_VALIDATION = 2
DEFAULT_SCENARIO_PRIORS = {"weak": 0.55, "repair": 0.30, "strong_attack": 0.15}
_OBSERVATION_RE = re.compile(r"^pa-[0-9a-f]{16,32}$")
_SYMBOL_RE = re.compile(r"^[0-9A-Z]{1,8}\.(SH|SZ|BJ|HK|ETF)$")
_LOCK = threading.Lock()


class PositionLearningFeedback(BaseModel):
    """单次收盘后事实反馈；不接收数量、成本、账户或自由格式行情。"""

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    trade_date: date
    symbol: str
    outcome: Literal["weak", "repair", "strong_attack"]
    evidence_grade: Literal["A", "B"]
    note: str = Field(default="", max_length=200)

    @field_validator("observation_id")
    @classmethod
    def _observation_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _OBSERVATION_RE.fullmatch(normalized):
            raise ValueError("observation_id 必须来自持仓分析结果")
        return normalized

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _SYMBOL_RE.fullmatch(normalized):
            raise ValueError("symbol 必须是规范证券代码")
        return normalized

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        return " ".join(value.split())


class PositionLearningCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["candidate", "validated", "applied", "rejected", "rolled_back"]
    kind: Literal["scenario_priors"] = "scenario_priors"
    sample_size: int
    training_size: int
    validation_size: int
    current_priors: dict[str, float]
    proposed_priors: dict[str, float]
    distinct_trade_days: int
    baseline_brier: float | None
    candidate_brier: float | None
    reason: str
    evidence_fingerprint: str
    first_trade_date: str
    last_trade_date: str
    created_at: str
    applied_at: str | None = None
    rolled_back_at: str | None = None


def record_feedback(data_dir: Path, feedback: PositionLearningFeedback) -> dict[str, Any]:
    """幂等追加一条反馈，并重新评估最新候选；不会自动应用。"""
    root = _root(data_dir)
    path = root / "feedback.jsonl"
    payload = feedback.model_dump(mode="json")
    note = str(payload.pop("note", ""))
    row = {
        **payload,
        "note_digest": hashlib.sha256(note.encode()).hexdigest() if note else None,
        "recorded_at": _now(),
    }
    key = (feedback.observation_id, feedback.symbol)
    with _LOCK:
        existing = _read_feedback(path)
        if any((item.get("observation_id"), item.get("symbol")) == key for item in existing):
            return {
                "recorded": False,
                "reason": "duplicate_observation",
                "candidate": _latest_candidate(root),
            }
        _append_jsonl(path, row)
        candidate = _evaluate_candidate(root, [*existing, row])
    return {"recorded": True, "candidate": candidate}


def list_candidates(data_dir: Path) -> list[dict[str, Any]]:
    directory = _root(data_dir) / "candidates"
    if not directory.exists():
        return []
    output: list[dict[str, Any]] = []
    for path in sorted(directory.glob("pa-learn-*.json"), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            output.append(value)
    return output


def apply_candidate(data_dir: Path, candidate_id: str) -> dict[str, Any]:
    """显式人工批准 validated 候选；原子更新唯一 active profile。"""
    if not re.fullmatch(r"pa-learn-[0-9a-f]{16}", candidate_id):
        raise ValueError("invalid candidate_id")
    root = _root(data_dir)
    candidate_path = root / "candidates" / f"{candidate_id}.json"
    with _LOCK:
        candidate = _read_object(candidate_path)
        profile = _read_object(root / "active_profile.json")
        if candidate is None:
            raise ValueError("candidate not found")
        status = candidate.get("status")
        if (
            status == "applied"
            and profile is not None
            and profile.get("active") is True
            and profile.get("candidate_id") == candidate_id
        ):
            return candidate
        if status not in {"validated", "applied"}:
            raise ValueError("only validated candidates can be applied")
        applied_at = str(candidate.get("applied_at") or _now())
        candidate["status"] = "applied"
        candidate["applied_at"] = applied_at
        _atomic_write(candidate_path, candidate)
        _atomic_write(
            root / "active_profile.json",
            {
                "schema_version": 1,
                "active": True,
                "candidate_id": candidate_id,
                "scenario_priors": candidate["proposed_priors"],
                "applied_at": applied_at,
            },
        )
    return candidate


def rollback_candidate(data_dir: Path, candidate_id: str) -> dict[str, Any]:
    """显式回滚当前 active 候选；保留 candidate/profile 审计事实，不删除文件。"""
    if not re.fullmatch(r"pa-learn-[0-9a-f]{16}", candidate_id):
        raise ValueError("invalid candidate_id")
    root = _root(data_dir)
    path = root / "candidates" / f"{candidate_id}.json"
    with _LOCK:
        candidate = _read_object(path)
        profile = _read_object(root / "active_profile.json")
        if candidate is None:
            raise ValueError("candidate not found")
        status = candidate.get("status")
        if (
            status == "rolled_back"
            and profile is not None
            and profile.get("active") is False
            and profile.get("rolled_back_candidate_id") == candidate_id
        ):
            return candidate
        if (
            status != "applied"
            or profile is None
            or profile.get("candidate_id") != candidate_id
            or profile.get("active") is not True
        ):
            raise ValueError("candidate is not the active applied profile")
        rolled_back_at = str(candidate.get("rolled_back_at") or _now())
        candidate["status"] = "rolled_back"
        candidate["rolled_back_at"] = rolled_back_at
        _atomic_write(path, candidate)
        _atomic_write(
            root / "active_profile.json",
            {
                "schema_version": 1,
                "active": False,
                "rolled_back_candidate_id": candidate_id,
                "rolled_back_at": rolled_back_at,
            },
        )
    return candidate


def reject_candidate(data_dir: Path, candidate_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"pa-learn-[0-9a-f]{16}", candidate_id):
        raise ValueError("invalid candidate_id")
    path = _root(data_dir) / "candidates" / f"{candidate_id}.json"
    with _LOCK:
        candidate = _read_object(path)
        if candidate is None:
            raise ValueError("candidate not found")
        if candidate.get("status") in {"applied", "rolled_back"}:
            raise ValueError("applied or rolled-back candidate cannot be rejected")
        if candidate.get("status") != "rejected":
            candidate["status"] = "rejected"
            candidate["reason"] = "人工拒绝；不进入 active profile。"
            _atomic_write(path, candidate)
    return candidate


def active_scenario_priors(data_dir: Path | None) -> dict[str, float] | None:
    if data_dir is None:
        return None
    root = _root(data_dir, create=False)
    profile = _read_object(root / "active_profile.json")
    if profile is None or profile.get("active") is not True:
        return None
    candidate_id = profile.get("candidate_id")
    if not isinstance(candidate_id, str):
        return None
    candidate = _read_object(root / "candidates" / f"{candidate_id}.json")
    if candidate is None or candidate.get("status") != "applied":
        return None
    return _valid_priors(profile.get("scenario_priors"))


def _evaluate_candidate(root: Path, feedback: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = _deduplicated_rows(feedback)
    if len(rows) < _MIN_SAMPLES:
        return None
    distinct_days = len({str(item["trade_date"]) for item in rows})
    if distinct_days < _MIN_DISTINCT_TRADE_DAYS:
        return None
    validation_size = max(_MIN_VALIDATION, len(rows) // 5)
    training = rows[:-validation_size]
    validation = rows[-validation_size:]
    if len(training) < _MIN_SAMPLES - _MIN_VALIDATION:
        return None

    proposed = _estimate_priors(training)
    current = active_scenario_priors(root.parent.parent.parent) or dict(DEFAULT_SCENARIO_PRIORS)
    baseline_brier = _brier(validation, current)
    candidate_brier = _brier(validation, proposed)
    passed = candidate_brier <= baseline_brier
    evidence_fingerprint = hashlib.sha256(
        json.dumps(
            [
                {
                    key: item.get(key)
                    for key in (
                        "observation_id",
                        "trade_date",
                        "symbol",
                        "outcome",
                        "evidence_grade",
                    )
                }
                for item in rows
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    payload = {
        "kind": "scenario_priors",
        "sample_size": len(rows),
        "training_size": len(training),
        "validation_size": len(validation),
        "distinct_trade_days": distinct_days,
        "current_priors": current,
        "proposed_priors": proposed,
        "evidence_fingerprint": evidence_fingerprint,
        "first_trade_date": str(rows[0]["trade_date"]),
        "last_trade_date": str(rows[-1]["trade_date"]),
        "baseline_brier": baseline_brier,
        "candidate_brier": candidate_brier,
        "status": "validated" if passed else "rejected",
        "reason": (
            "时间顺序留出集 Brier score 不劣于当前 profile；等待人工批准。"
            if passed
            else "时间顺序留出集未优于当前 profile；候选已拒绝。"
        ),
    }
    candidate_id = "pa-learn-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    candidate = PositionLearningCandidate(
        id=candidate_id,
        created_at=_now(),
        **payload,
    ).model_dump(mode="json")
    directory = root / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{candidate_id}.json"
    if not path.exists():
        _atomic_write(path, candidate)
    return candidate


def _deduplicated_rows(feedback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 同一分析/标的一票；按交易日排序，避免把后验数据泄露进训练段。
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in feedback:
        if item.get("evidence_grade") not in {"A", "B"} or item.get("outcome") not in _OUTCOMES:
            continue
        key = (str(item.get("observation_id")), str(item.get("symbol")))
        unique.setdefault(key, item)
    return sorted(
        unique.values(),
        key=lambda item: (
            str(item.get("trade_date") or ""),
            str(item.get("observation_id") or ""),
            str(item.get("symbol") or ""),
        ),
    )


def _estimate_priors(rows: list[dict[str, Any]]) -> dict[str, float]:
    counts = {name: 1 for name in _OUTCOMES}  # Laplace smoothing
    for item in rows:
        counts[str(item["outcome"])] += 1
    total = float(sum(counts.values()))
    return {name: counts[name] / total for name in _OUTCOMES}


def _brier(rows: list[dict[str, Any]], priors: dict[str, float]) -> float:
    scores = []
    for item in rows:
        outcome = str(item["outcome"])
        scores.append(sum((priors[name] - (1.0 if name == outcome else 0.0)) ** 2 for name in _OUTCOMES))
    return sum(scores) / len(scores)


def _valid_priors(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict) or set(value) != set(_OUTCOMES):
        return None
    try:
        priors = {name: float(value[name]) for name in _OUTCOMES}
    except (TypeError, ValueError):
        return None
    if any(number < 0 or number > 1 for number in priors.values()):
        return None
    if abs(sum(priors.values()) - 1.0) > 1e-6:
        return None
    return priors


def _root(data_dir: Path, *, create: bool = True) -> Path:
    root = Path(data_dir) / "user_data" / "position_analysis_agent" / "learning"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _read_feedback(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    output: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            output.append(item)
    return output


def _latest_candidate(root: Path) -> dict[str, Any] | None:
    candidates = list_candidates(root.parent.parent.parent)
    return candidates[0] if candidates else None


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, encoded.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
