"""Versioned export and guarded restore for Sycee-owned user data."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.services.user_storage import path_for
from app.sycee.portfolio import (
    PortfolioTrade,
    _build_portfolio,
)
from app.sycee.portfolio import (
    _lock as portfolio_lock,
)
from app.sycee.portfolio_sell_alert import (
    PortfolioSellAlertUpdate,
)
from app.sycee.portfolio_sell_alert import (
    _lock as sell_alert_lock,
)
from app.sycee.research_ledger import ResearchEntry
from app.sycee.research_ledger import _lock as research_lock
from app.sycee.strategy_tracking import StrategyTrack
from app.sycee.strategy_tracking import _lock as strategy_tracking_lock
from app.sycee.trade_reviews import TradeReview
from app.sycee.trade_reviews import _lock as trade_review_lock

router = APIRouter(prefix="/api/sycee/data-backup", tags=["sycee-data-backup"])

_FILES = {
    "portfolio": "portfolio.json",
    "portfolio_sell_alert": "portfolio_sell_alert.json",
    "trade_reviews": "trade_reviews.json",
    "research_ledger": "research_ledger.json",
    "strategy_tracking": "strategy_tracking.json",
}
_MAX_BACKUP_BYTES = 20 * 1024 * 1024
_TRADE_ID_RE = re.compile(r"^trade_[0-9a-f]{32}$")
_RESEARCH_ID_RE = re.compile(r"^research_[0-9a-f]{32}$")
_CAPTURE_ID_RE = re.compile(r"^capture_[0-9a-f]{32}$")
_RULE_ID_RE = re.compile(r"^sycee_pf_sell_[0-9a-f]{20}$")
_TRACK_ID_RE = re.compile(r"^strategy_track_[0-9a-f]{32}$")
_OBSERVATION_ID_RE = re.compile(r"^strategy_observation_[0-9a-f]{32}$")
_backup_lock = threading.RLock()


class BackupValidationError(ValueError):
    pass


class BackupRestoreError(RuntimeError):
    pass


class SyceeBackupData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio: dict | None
    portfolio_sell_alert: dict | None
    trade_reviews: dict | None
    research_ledger: dict | None
    strategy_tracking: dict | None = None


class SyceeBackupDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["sycee-user-data"]
    version: Literal[1]
    exported_at: str
    data: SyceeBackupData


class SyceeRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["RESTORE_SYCEE_DATA"]
    backup: SyceeBackupDocument


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sycee_dir() -> Path:
    path = path_for(settings.data_dir, "sycee")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_path(key: str) -> Path:
    return _sycee_dir() / _FILES[key]


@contextmanager
def _locked_sources():
    with _backup_lock, ExitStack() as stack:
        for lock in (
            portfolio_lock,
            sell_alert_lock,
            research_lock,
            strategy_tracking_lock,
            trade_review_lock,
        ):
            stack.enter_context(lock)
        yield


def _read_source(key: str) -> dict | None:
    path = _source_path(key)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > _MAX_BACKUP_BYTES:
            raise BackupValidationError(f"{key} 数据超过备份大小限制")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"{key} 数据无法读取") from exc
    except json.JSONDecodeError as exc:
        raise BackupValidationError(f"{key} 数据不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise BackupValidationError(f"{key} 数据结构无效")
    return payload


def _versioned_list(payload: dict | None, key: str, label: str) -> list:
    if payload is None:
        return []
    if payload.get("version") != 1 or not isinstance(payload.get(key), list):
        raise BackupValidationError(f"{label}快照结构无效")
    return payload[key]


def _normalize_portfolio(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    raw_trades = _versioned_list(payload, "trades", "持仓")
    if len(raw_trades) > 100_000:
        raise BackupValidationError("持仓交易记录数量超过限制")
    try:
        trades = [PortfolioTrade.model_validate(item).model_dump() for item in raw_trades]
    except (TypeError, ValueError) as exc:
        raise BackupValidationError("持仓交易记录内容无效") from exc
    ids = [trade["id"] for trade in trades]
    if len(set(ids)) != len(ids) or any(not _TRADE_ID_RE.fullmatch(item) for item in ids):
        raise BackupValidationError("持仓交易记录 ID 无效或重复")
    try:
        _build_portfolio(trades)
    except ValueError as exc:
        raise BackupValidationError(str(exc)) from exc
    return {"version": 1, "trades": trades}


def _normalize_sell_alert(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    config = payload.get("config")
    if payload.get("version") != 1 or not isinstance(config, dict):
        raise BackupValidationError("持仓卖出提醒快照结构无效")
    try:
        update = PortfolioSellAlertUpdate.model_validate(config)
    except (TypeError, ValueError) as exc:
        raise BackupValidationError("持仓卖出提醒配置无效") from exc
    rule_id = config.get("rule_id", "")
    if not isinstance(rule_id, str) or (rule_id and not _RULE_ID_RE.fullmatch(rule_id)):
        raise BackupValidationError("持仓卖出提醒规则 ID 无效")
    return {"version": 1, "config": {**update.model_dump(), "rule_id": rule_id}}


def _normalize_trade_reviews(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    raw_reviews = _versioned_list(payload, "reviews", "交易复盘")
    if len(raw_reviews) > 100_000:
        raise BackupValidationError("交易复盘记录数量超过限制")
    try:
        reviews = [TradeReview.model_validate(item).model_dump() for item in raw_reviews]
    except (TypeError, ValueError) as exc:
        raise BackupValidationError("交易复盘记录内容无效") from exc
    trade_ids = [review["trade_id"] for review in reviews]
    if len(set(trade_ids)) != len(trade_ids):
        raise BackupValidationError("同一交易存在重复复盘")
    return {"version": 1, "reviews": reviews}


def _normalize_research(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    raw_entries = _versioned_list(payload, "entries", "研究账本")
    if len(raw_entries) > 20_000:
        raise BackupValidationError("研究账本记录数量超过限制")
    try:
        entries = [ResearchEntry.model_validate(item).model_dump() for item in raw_entries]
    except (TypeError, ValueError) as exc:
        raise BackupValidationError("研究账本记录内容无效") from exc
    entry_ids = [entry["id"] for entry in entries]
    capture_ids = [capture["id"] for entry in entries for capture in entry["captures"]]
    if len(set(entry_ids)) != len(entry_ids) or any(
        not _RESEARCH_ID_RE.fullmatch(item) for item in entry_ids
    ):
        raise BackupValidationError("研究记录 ID 无效或重复")
    if len(set(capture_ids)) != len(capture_ids) or any(
        not _CAPTURE_ID_RE.fullmatch(item) for item in capture_ids
    ):
        raise BackupValidationError("研究捕获记录 ID 无效或重复")
    return {"version": 1, "entries": entries}


def _normalize_strategy_tracking(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    raw_tracks = _versioned_list(payload, "tracks", "策略跟踪")
    if len(raw_tracks) > 100:
        raise BackupValidationError("策略跟踪计划数量超过限制")
    try:
        tracks = [StrategyTrack.model_validate(item).model_dump(mode="json") for item in raw_tracks]
    except (TypeError, ValueError) as exc:
        raise BackupValidationError("策略跟踪计划内容无效") from exc
    track_ids = [track["id"] for track in tracks]
    observation_ids = [item["id"] for track in tracks for item in track["observations"]]
    if (
        len(set(track_ids)) != len(track_ids)
        or any(not _TRACK_ID_RE.fullmatch(item) for item in track_ids)
        or len(set(observation_ids)) != len(observation_ids)
        or any(not _OBSERVATION_ID_RE.fullmatch(item) for item in observation_ids)
        or any(len(track["observations"]) > 1000 for track in tracks)
    ):
        raise BackupValidationError("策略跟踪计划 ID 无效或重复")
    return {"version": 1, "tracks": tracks}


def _normalize_data(data: SyceeBackupData) -> dict[str, dict | None]:
    normalizers = {
        "portfolio": _normalize_portfolio,
        "portfolio_sell_alert": _normalize_sell_alert,
        "trade_reviews": _normalize_trade_reviews,
        "research_ledger": _normalize_research,
        "strategy_tracking": _normalize_strategy_tracking,
    }
    normalized = {
        key: normalize(getattr(data, key))
        for key, normalize in normalizers.items()
        if key in data.model_fields_set
    }
    size = len(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
    if size > _MAX_BACKUP_BYTES:
        raise BackupValidationError("Sycee 数据快照超过 20 MB 限制")
    return normalized


def export_backup() -> SyceeBackupDocument:
    with _locked_sources():
        data = SyceeBackupData(**{key: _read_source(key) for key in _FILES})
        normalized = _normalize_data(data)
    return SyceeBackupDocument(
        format="sycee-user-data",
        version=1,
        exported_at=_now(),
        data=SyceeBackupData(**normalized),
    )


def _create_safety_backup_unlocked() -> tuple[str, Path]:
    backup_id = f"restore_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    backup_dir = _sycee_dir() / "backups" / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    present: list[str] = []
    try:
        for filename in _FILES.values():
            source = _sycee_dir() / filename
            if source.exists():
                shutil.copy2(source, backup_dir / filename)
                present.append(filename)
        (backup_dir / "manifest.json").write_text(
            json.dumps({"created_at": _now(), "files": present}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise BackupRestoreError("恢复前安全备份创建失败") from exc
    return backup_id, backup_dir


def _rollback_unlocked(backup_dir: Path) -> list[str]:
    errors: list[str] = []
    for filename in _FILES.values():
        target = _sycee_dir() / filename
        saved = backup_dir / filename
        try:
            if saved.exists():
                temp = target.with_name(f".{target.name}.{uuid4().hex}.rollback")
                shutil.copy2(saved, temp)
                os.replace(temp, target)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            errors.append(filename)
    return errors


def _write_restored_data_unlocked(
    normalized: dict[str, dict | None], backup_dir: Path
) -> None:
    prepared: dict[str, Path] = {}
    try:
        for key, payload in normalized.items():
            if payload is None:
                continue
            target = _source_path(key)
            temp = target.with_name(f".{target.name}.{uuid4().hex}.restore")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            prepared[key] = temp
        for key, payload in normalized.items():
            target = _source_path(key)
            if payload is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(prepared[key], target)
    except OSError as exc:
        rollback_errors = _rollback_unlocked(backup_dir)
        detail = "恢复失败,原数据已回滚"
        if rollback_errors:
            detail = f"恢复失败且以下文件回滚失败: {', '.join(rollback_errors)}"
        raise BackupRestoreError(detail) from exc
    finally:
        for temp in prepared.values():
            temp.unlink(missing_ok=True)


def restore_backup(document: SyceeBackupDocument) -> str:
    normalized = _normalize_data(document.data)
    with _locked_sources():
        backup_id, backup_dir = _create_safety_backup_unlocked()
        _write_restored_data_unlocked(normalized, backup_dir)
    return backup_id


@router.get("", response_model=SyceeBackupDocument)
def download_backup():
    try:
        return export_backup()
    except (BackupValidationError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/restore")
def restore_user_data(request: SyceeRestoreRequest):
    try:
        backup_id = restore_backup(request.backup)
    except BackupValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BackupRestoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "safety_backup_id": backup_id}
