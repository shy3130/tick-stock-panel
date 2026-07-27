"""Per-user strategy lifecycle plans and backtest observation snapshots."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.config import settings
from app.services.user_storage import path_for

router = APIRouter(prefix="/api/sycee/strategy-tracks", tags=["sycee-strategy-tracking"])

TrackStatus = Literal["tracking", "paused", "closed"]

_TRACK_ID_RE = re.compile(r"^strategy_track_[0-9a-f]{32}$")
_OBSERVATION_ID_RE = re.compile(r"^strategy_observation_[0-9a-f]{32}$")
_STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SYMBOL_RE = re.compile(r"^[0-9A-Z._-]{2,32}$")
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_TRACKS = 100
_MAX_OBSERVATIONS = 1000
_lock = threading.RLock()


class StrategyTrackConflictError(ValueError):
    pass


class StrategyTrackCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", allow_inf_nan=False)

    strategy_id: str = Field(min_length=1, max_length=120)
    strategy_name: str = Field(min_length=1, max_length=120)
    symbols: list[str] = Field(min_length=1, max_length=50)
    start_date: date
    initial_capital: float = Field(gt=0, le=1_000_000_000_000)
    max_positions: int = Field(ge=1, le=100)
    commission_pct: float = Field(default=0.0002, ge=0, le=0.1)
    stamp_tax_pct: float = Field(default=0.001, ge=0, le=0.1)
    slippage_bps: float = Field(default=5, ge=0, le=1000)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    overrides: dict[str, JsonValue] = Field(default_factory=dict)
    note: str = Field(default="", max_length=3000)

    @field_validator("strategy_id")
    @classmethod
    def validate_strategy_id(cls, value: str) -> str:
        if not _STRATEGY_ID_RE.fullmatch(value):
            raise ValueError("策略 ID 格式无效")
        return value

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        symbols = list(dict.fromkeys(value.strip().upper() for value in values))
        if not symbols or any(not _SYMBOL_RE.fullmatch(value) for value in symbols):
            raise ValueError("股票代码格式无效")
        return symbols


class StrategyTrackUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: TrackStatus | None = None
    note: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def require_change(self):
        if self.status is None and self.note is None:
            raise ValueError("没有可更新的内容")
        return self


class StrategyObservationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", allow_inf_nan=False)

    end_date: date
    run_id: str = Field(min_length=1, max_length=160)
    total_return: float | None = None
    annual_return: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    trade_count: int | None = Field(default=None, ge=0)
    ending_equity: float | None = Field(default=None, ge=0)
    elapsed_ms: float = Field(ge=0)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not _RUN_ID_RE.fullmatch(value):
            raise ValueError("回测运行 ID 格式无效")
        return value


class StrategyObservation(StrategyObservationCreate):
    id: str
    observed_at: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _OBSERVATION_ID_RE.fullmatch(value):
            raise ValueError("策略跟踪快照 ID 无效")
        return value


class StrategyTrack(StrategyTrackCreate):
    id: str
    status: TrackStatus
    observations: list[StrategyObservation]
    created_at: str
    updated_at: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _TRACK_ID_RE.fullmatch(value):
            raise ValueError("策略跟踪计划 ID 无效")
        return value

    @model_validator(mode="after")
    def validate_observations(self):
        end_dates = [item.end_date for item in self.observations]
        if len(set(end_dates)) != len(end_dates):
            raise ValueError("同一截止日存在重复策略跟踪快照")
        if any(end_date < self.start_date for end_date in end_dates):
            raise ValueError("策略跟踪快照早于计划起始日")
        return self


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _path() -> Path:
    path = path_for(settings.data_dir, "sycee/strategy_tracking.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_unlocked() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise RuntimeError("策略跟踪文件超过大小限制")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("策略跟踪文件无法读取,请检查数据文件") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("策略跟踪文件版本无效")
    raw_tracks = payload.get("tracks")
    if not isinstance(raw_tracks, list) or len(raw_tracks) > _MAX_TRACKS:
        raise RuntimeError("策略跟踪文件内容无效")
    try:
        tracks = [StrategyTrack.model_validate(item).model_dump(mode="json") for item in raw_tracks]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("策略跟踪文件内容无效") from exc
    track_ids = [track["id"] for track in tracks]
    observation_ids = [item["id"] for track in tracks for item in track["observations"]]
    if (
        len(set(track_ids)) != len(track_ids)
        or any(not _TRACK_ID_RE.fullmatch(item) for item in track_ids)
        or len(set(observation_ids)) != len(observation_ids)
        or any(not _OBSERVATION_ID_RE.fullmatch(item) for item in observation_ids)
        or any(len(track["observations"]) > _MAX_OBSERVATIONS for track in tracks)
    ):
        raise RuntimeError("策略跟踪文件包含无效或重复 ID")
    return tracks


def _write_unlocked(tracks: list[dict]) -> None:
    path = _path()
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    content = json.dumps({"version": 1, "tracks": tracks}, ensure_ascii=False, indent=2)
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        raise StrategyTrackConflictError("策略跟踪文件超过 20 MB 限制")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def list_tracks() -> list[dict]:
    with _lock:
        tracks = _read_unlocked()
    return sorted(tracks, key=lambda item: item["updated_at"], reverse=True)


def create_track(data: StrategyTrackCreate) -> dict:
    now = _now()
    track = StrategyTrack.model_validate(
        {
            **data.model_dump(mode="json"),
            "id": f"strategy_track_{uuid4().hex}",
            "status": "tracking",
            "observations": [],
            "created_at": now,
            "updated_at": now,
        }
    ).model_dump(mode="json")
    with _lock:
        tracks = _read_unlocked()
        if len(tracks) >= _MAX_TRACKS:
            raise StrategyTrackConflictError("策略跟踪计划最多允许 100 个")
        tracks.insert(0, track)
        _write_unlocked(tracks)
    return track


def update_track(track_id: str, changes: StrategyTrackUpdate) -> dict | None:
    updates = changes.model_dump(exclude_unset=True, exclude_none=True)
    with _lock:
        tracks = _read_unlocked()
        for index, track in enumerate(tracks):
            if track["id"] != track_id:
                continue
            updated = StrategyTrack.model_validate(
                {**track, **updates, "updated_at": _now()}
            ).model_dump(mode="json")
            tracks[index] = updated
            _write_unlocked(tracks)
            return updated
    return None


def save_observation(
    track_id: str, data: StrategyObservationCreate
) -> tuple[dict, dict, Literal["created", "replaced"]] | None:
    with _lock:
        tracks = _read_unlocked()
        for track_index, track in enumerate(tracks):
            if track["id"] != track_id:
                continue
            if track["status"] != "tracking":
                raise StrategyTrackConflictError("只有跟踪中的计划可以更新快照")
            if data.end_date < date.fromisoformat(track["start_date"]):
                raise ValueError("快照结束日期不能早于跟踪开始日期")
            observations = track["observations"]
            existing = next(
                (item for item in observations if item["end_date"] == data.end_date.isoformat()),
                None,
            )
            observation = StrategyObservation.model_validate(
                {
                    **data.model_dump(mode="json"),
                    "id": existing["id"] if existing else f"strategy_observation_{uuid4().hex}",
                    "observed_at": _now(),
                }
            ).model_dump(mode="json")
            action: Literal["created", "replaced"] = "replaced" if existing else "created"
            if existing:
                observations[observations.index(existing)] = observation
            else:
                if len(observations) >= _MAX_OBSERVATIONS:
                    raise StrategyTrackConflictError("单个计划最多允许 1000 条快照")
                observations.append(observation)
            observations.sort(key=lambda item: item["end_date"], reverse=True)
            updated = {**track, "observations": observations, "updated_at": _now()}
            tracks[track_index] = updated
            _write_unlocked(tracks)
            return updated, observation, action
    return None


def delete_track(track_id: str) -> bool:
    with _lock:
        tracks = _read_unlocked()
        remaining = [track for track in tracks if track["id"] != track_id]
        if len(remaining) == len(tracks):
            return False
        _write_unlocked(remaining)
    return True


def _valid_track_id(track_id: str) -> str:
    if not _TRACK_ID_RE.fullmatch(track_id):
        raise HTTPException(status_code=400, detail="策略跟踪计划 ID 无效")
    return track_id


@router.get("")
def get_tracks():
    tracks = list_tracks()
    return {"tracks": tracks, "total": len(tracks)}


@router.post("", status_code=201)
def post_track(data: StrategyTrackCreate):
    try:
        return {"track": create_track(data)}
    except StrategyTrackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{track_id}")
def patch_track(track_id: str, changes: StrategyTrackUpdate):
    try:
        track = update_track(_valid_track_id(track_id), changes)
    except StrategyTrackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if track is None:
        raise HTTPException(status_code=404, detail="策略跟踪计划不存在")
    return {"track": track}


@router.post("/{track_id}/observations")
def post_observation(track_id: str, data: StrategyObservationCreate):
    try:
        result = save_observation(_valid_track_id(track_id), data)
    except StrategyTrackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="策略跟踪计划不存在")
    track, observation, action = result
    return {"track": track, "observation": observation, "action": action}


@router.delete("/{track_id}")
def remove_track(track_id: str):
    if not delete_track(_valid_track_id(track_id)):
        raise HTTPException(status_code=404, detail="策略跟踪计划不存在")
    return {"ok": True}
