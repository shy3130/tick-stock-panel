"""守护档案持久化: 每策略一份 JSON (基线 + 复验历史 + 状态)。

目录: data/user_data/strategy_guardian/{strategy_id}.json
设计见 docs/auto-strategy-lab-plan.md §3.1。档案文件由守护功能自身创建,
drop 只删除本目录内的档案 (fail-closed 路径校验), 不触碰策略源码。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.backtest.guardian import (
    BASELINE_KINDS,
    GUARDIAN_STATUSES,
    MAX_CHECKS,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LOCK = threading.RLock()


class GuardianStoreError(RuntimeError):
    pass


# ── 组合建议 (P2d): 健康冠军间低相关可组合对, 由守护运行时扫描写入 ──────
_MAX_COMBINATION_SUGGESTIONS = 10


def combination_path(data_dir: Path | str) -> Path:
    root = Path(data_dir).resolve() / "user_data" / "strategy_guardian"
    root.mkdir(parents=True, exist_ok=True)
    return root / "combinations.json"


def read_combinations(data_dir: Path | str) -> dict[str, Any] | None:
    path = combination_path(data_dir)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_combinations(
    data_dir: Path | str,
    *,
    window_start: str,
    window_end: str,
    suggestions: list[dict[str, Any]],
    pairs_evaluated: int,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "window_start": window_start,
        "window_end": window_end,
        "pairs_evaluated": pairs_evaluated,
        "suggestions": suggestions[:_MAX_COMBINATION_SUGGESTIONS],
    }
    path = combination_path(data_dir)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise GuardianStoreError("failed to write combinations") from exc
    return payload


class GuardianStoreValidationError(GuardianStoreError, ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class GuardianStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.root = (self.data_dir / "user_data" / "strategy_guardian").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ── 基线 ───────────────────────────────────────────────────────

    def create_baseline(
        self,
        strategy_id: str,
        *,
        kind: str,
        data_as_of: str,
        objective: str,
        metrics: Mapping[str, Any],
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建守护档案; 档案已存在时幂等返回现有档案 (重复发布不覆盖历史)。"""
        if kind not in BASELINE_KINDS:
            raise GuardianStoreValidationError(f"unsupported baseline kind: {kind!r}")
        _validate_iso_date(data_as_of, "data_as_of")
        if not isinstance(objective, str) or not objective:
            raise GuardianStoreValidationError("baseline objective must be a string")
        with _LOCK:
            existing = self.get(strategy_id)
            if existing is not None:
                return existing
            profile = _new_profile(strategy_id)
            profile["baseline"] = {
                "kind": kind,
                "data_as_of": data_as_of,
                "objective": objective,
                "metrics": _copy_json(metrics),
                "provenance": _copy_json(provenance or {}),
                "created_at": _now_iso(),
            }
            self._write(profile)
            return profile

    # ── 读取 ───────────────────────────────────────────────────────

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        path = self._profile_path(strategy_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GuardianStoreError(
                f"failed to read guardian profile: {strategy_id}"
            ) from exc
        if not isinstance(value, dict):
            raise GuardianStoreError(f"invalid guardian profile: {strategy_id}")
        return _normalize_profile(value, strategy_id)

    def list_profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            # combinations.json 是 P2d 的组合建议文件, 不是策略档案
            if path.name == combination_path(self.data_dir).name:
                continue
            strategy_id = path.stem
            try:
                profile = self.get(strategy_id)
            except GuardianStoreError:
                logger.warning("skipping unreadable guardian profile: %s", path.name)
                continue
            if profile is not None:
                profiles.append(profile)
        profiles.sort(key=lambda item: str(item.get("updated_at") or ""))
        return profiles

    # ── 复验与状态 ─────────────────────────────────────────────────

    def append_check(self, strategy_id: str, check: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条复验记录 (滚动保留最近 MAX_CHECKS 条) 并刷新 updated_at。"""
        clean = _copy_json(check)
        with _LOCK:
            profile = self._required(strategy_id)
            checks = list(profile.get("checks") or [])
            checks.append(clean)
            profile["checks"] = checks[-MAX_CHECKS:]
            profile["updated_at"] = _now_iso()
            self._write(profile)
            return profile

    def set_status(
        self,
        strategy_id: str,
        status: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if status not in GUARDIAN_STATUSES:
            raise GuardianStoreValidationError(f"unsupported guardian status: {status!r}")
        with _LOCK:
            profile = self._required(strategy_id)
            if profile.get("status") != status:
                profile["status"] = status
                profile["status_reason"] = reason
                profile["status_since"] = _now_iso()
            profile["updated_at"] = _now_iso()
            self._write(profile)
            return profile

    def set_baseline_metrics(
        self,
        strategy_id: str,
        metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        """trailing_reference 档案首跑标定后回填基线指标。"""
        with _LOCK:
            profile = self._required(strategy_id)
            baseline = profile.get("baseline")
            if not isinstance(baseline, dict):
                raise GuardianStoreValidationError("profile has no baseline")
            if baseline.get("kind") != "trailing_reference":
                raise GuardianStoreValidationError(
                    "only trailing_reference baselines can be calibrated"
                )
            baseline["metrics"] = _copy_json(metrics)
            baseline["calibrated_at"] = _now_iso()
            profile["updated_at"] = _now_iso()
            self._write(profile)
            return profile

    def drop(self, strategy_id: str) -> bool:
        """删除守护档案 (仅本目录内文件)。返回是否实际删除。"""
        with _LOCK:
            path = self._profile_path(strategy_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    # ── 内部 ───────────────────────────────────────────────────────

    def _required(self, strategy_id: str) -> dict[str, Any]:
        profile = self.get(strategy_id)
        if profile is None:
            raise KeyError(strategy_id)
        return profile

    def _profile_path(self, strategy_id: str) -> Path:
        if not isinstance(strategy_id, str) or not _SAFE_ID.fullmatch(strategy_id):
            raise GuardianStoreValidationError(
                "strategy_id contains unsafe characters for guardian profiles"
            )
        candidate = (self.root / f"{strategy_id}.json").resolve()
        if not candidate.is_relative_to(self.root):
            raise GuardianStoreValidationError("guardian profile path escapes root")
        return candidate

    def _write(self, profile: dict[str, Any]) -> None:
        path = self._profile_path(str(profile["strategy_id"]))
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            text = json.dumps(
                profile, ensure_ascii=False, allow_nan=False, indent=2
            ) + "\n"
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise GuardianStoreError(f"failed to write guardian profile: {path}") from exc


def _new_profile(strategy_id: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": _SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "baseline": None,
        "status": "pending",
        "status_reason": None,
        "status_since": now,
        "checks": [],
        "created_at": now,
        "updated_at": now,
    }


def _normalize_profile(value: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    status = value.get("status", "pending")
    if status not in GUARDIAN_STATUSES:
        raise GuardianStoreError(f"invalid status in guardian profile: {strategy_id}")
    baseline = value.get("baseline")
    if baseline is not None and (
        not isinstance(baseline, dict)
        or baseline.get("kind") not in BASELINE_KINDS
    ):
        raise GuardianStoreError(f"invalid baseline in guardian profile: {strategy_id}")
    checks = value.get("checks")
    if not isinstance(checks, list):
        checks = []
    normalized = dict(value)
    normalized.update(
        {
            "schema_version": value.get("schema_version", _SCHEMA_VERSION),
            "strategy_id": strategy_id,
            "status": status,
            "checks": checks[-MAX_CHECKS:],
        }
    )
    return normalized


def _copy_json(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise GuardianStoreValidationError(
            "guardian payload is not JSON serializable"
        ) from exc


def _validate_iso_date(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise GuardianStoreValidationError(f"{field} must be an ISO date string")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise GuardianStoreValidationError(f"{field} is not a valid ISO date") from exc
