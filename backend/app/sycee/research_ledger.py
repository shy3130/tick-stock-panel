"""Per-user research ledger API and persistence."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.services.user_storage import path_for

router = APIRouter(prefix="/api/sycee/research", tags=["sycee-research"])

SubjectType = Literal["stock", "strategy", "sector", "market"]
ResearchStatus = Literal["draft", "tracking", "validated", "invalidated", "archived"]
ResearchOrigin = Literal["manual", "capture"]
CaptureAction = Literal["created", "appended", "duplicate"]
CaptureValue = str | int | float | bool | None

_ENTRY_ID_RE = re.compile(r"^research_[0-9a-f]{32}$")
_CAPTURE_ID_RE = re.compile(r"^capture_[0-9a-f]{32}$")
_SYMBOL_RE = re.compile(r"^[0-9A-Z._-]{2,32}$")
_lock = threading.RLock()


def _clean_items(values: list[str], *, limit: int, item_limit: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value or value in seen:
            continue
        if len(value) > item_limit:
            raise ValueError(f"单项内容不能超过 {item_limit} 个字符")
        seen.add(value)
        cleaned.append(value)
    if len(cleaned) > limit:
        raise ValueError(f"最多允许 {limit} 项")
    return cleaned


class ResearchEntryCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    subject_type: SubjectType = "stock"
    subject: str = Field(default="", max_length=100)
    thesis: str = Field(default="", max_length=5000)
    evidence: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    invalidation: str = Field(default="", max_length=3000)
    plan: str = Field(default="", max_length=3000)
    status: ResearchStatus = "draft"
    tags: list[str] = Field(default_factory=list)

    @field_validator("evidence", "counter_evidence")
    @classmethod
    def validate_evidence(cls, value: list[str]) -> list[str]:
        return _clean_items(value, limit=20, item_limit=500)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return _clean_items(value, limit=8, item_limit=24)


class ResearchEntryUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=120)
    subject_type: SubjectType | None = None
    subject: str | None = Field(default=None, max_length=100)
    thesis: str | None = Field(default=None, max_length=5000)
    evidence: list[str] | None = None
    counter_evidence: list[str] | None = None
    invalidation: str | None = Field(default=None, max_length=3000)
    plan: str | None = Field(default=None, max_length=3000)
    status: ResearchStatus | None = None
    tags: list[str] | None = None

    @field_validator("evidence", "counter_evidence")
    @classmethod
    def validate_evidence(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _clean_items(value, limit=20, item_limit=500)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _clean_items(value, limit=8, item_limit=24)


class ResearchCaptureRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    symbol: str = Field(min_length=2, max_length=32)
    name: str = Field(default="", max_length=80)
    source: str = Field(min_length=1, max_length=40)
    source_label: str = Field(min_length=1, max_length=40)
    source_key: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    snapshot: dict[str, CaptureValue] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = value.upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("标的代码格式无效")
        return symbol

    @field_validator("snapshot")
    @classmethod
    def validate_snapshot(cls, value: dict[str, CaptureValue]) -> dict[str, CaptureValue]:
        if len(value) > 16:
            raise ValueError("快照字段最多允许 16 项")
        for key, item in value.items():
            if not key or len(key) > 40:
                raise ValueError("快照字段名无效")
            if isinstance(item, str) and len(item) > 500:
                raise ValueError("快照字段内容不能超过 500 个字符")
        return value


class ResearchCapture(BaseModel):
    id: str
    captured_at: str
    source: str
    source_label: str
    source_key: str
    summary: str
    snapshot: dict[str, CaptureValue] = Field(default_factory=dict)


class ResearchEntry(ResearchEntryCreate):
    id: str
    created_at: str
    updated_at: str
    origin: ResearchOrigin = "manual"
    captures: list[ResearchCapture] = Field(default_factory=list)


class ResearchEntryResponse(BaseModel):
    entry: ResearchEntry


class ResearchEntryListResponse(BaseModel):
    entries: list[ResearchEntry]
    total: int


class ResearchCaptureResponse(BaseModel):
    entry: ResearchEntry
    action: CaptureAction
    capture_id: str


class ResearchCaptureUndoResponse(BaseModel):
    ok: bool = True
    entry_deleted: bool
    entry: ResearchEntry | None


def _path():
    path = path_for(settings.data_dir, "sycee/research_ledger.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_unlocked() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("研究账本文件无法读取,请检查数据文件") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("研究账本文件版本无效")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("研究账本文件内容无效")
    return entries


def _write_unlocked(entries: list[dict]) -> None:
    path = _path()
    temp = path.with_suffix(".json.tmp")
    payload = {"version": 1, "entries": entries}
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def list_entries() -> list[dict]:
    with _lock:
        entries = _read_unlocked()
    return sorted(entries, key=lambda row: row.get("updated_at", ""), reverse=True)


def create_entry(data: ResearchEntryCreate) -> dict:
    now = _now()
    entry = {
        "id": f"research_{uuid4().hex}",
        **data.model_dump(),
        "origin": "manual",
        "captures": [],
        "created_at": now,
        "updated_at": now,
    }
    with _lock:
        entries = _read_unlocked()
        entries.insert(0, entry)
        _write_unlocked(entries)
    return entry


def update_entry(entry_id: str, changes: ResearchEntryUpdate) -> dict | None:
    updates = changes.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise ValueError("没有可更新的内容")
    with _lock:
        entries = _read_unlocked()
        for index, entry in enumerate(entries):
            if entry.get("id") != entry_id:
                continue
            updated = {**entry, **updates, "updated_at": _now()}
            if any(key != "status" for key in updates):
                updated["origin"] = "manual"
            entries[index] = updated
            _write_unlocked(entries)
            return updated
    return None


def delete_entry(entry_id: str) -> bool:
    with _lock:
        entries = _read_unlocked()
        remaining = [entry for entry in entries if entry.get("id") != entry_id]
        if len(remaining) == len(entries):
            return False
        from app.sycee.research_sharing import revoke_shares_transactionally

        with revoke_shares_transactionally(entry_id):
            _write_unlocked(remaining)
    return True


def capture_stock(request: ResearchCaptureRequest) -> tuple[dict, CaptureAction, str]:
    now = _now()
    capture = {
        "id": f"capture_{uuid4().hex}",
        "captured_at": now,
        "source": request.source,
        "source_label": request.source_label,
        "source_key": request.source_key,
        "summary": request.summary,
        "snapshot": request.snapshot,
    }
    with _lock:
        entries = _read_unlocked()
        active_entry: dict | None = None
        for entry in entries:
            if (
                entry.get("subject_type") == "stock"
                and str(entry.get("subject", "")).upper() == request.symbol
                and entry.get("status") in {"draft", "tracking"}
            ):
                active_entry = entry
                break

        if active_entry is not None:
            existing_captures = active_entry.get("captures") or []
            for existing in existing_captures:
                if existing.get("source_key") == request.source_key:
                    return active_entry, "duplicate", str(existing["id"])
            updated = {
                **active_entry,
                "captures": [capture, *existing_captures],
                "updated_at": now,
            }
            index = entries.index(active_entry)
            entries[index] = updated
            _write_unlocked(entries)
            return updated, "appended", capture["id"]

        entry = {
            "id": f"research_{uuid4().hex}",
            "title": f"{request.name or request.symbol} · 待整理",
            "subject_type": "stock",
            "subject": request.symbol,
            "thesis": "",
            "evidence": [],
            "counter_evidence": [],
            "invalidation": "",
            "plan": "",
            "status": "draft",
            "tags": [],
            "origin": "capture",
            "captures": [capture],
            "created_at": now,
            "updated_at": now,
        }
        entries.insert(0, entry)
        _write_unlocked(entries)
        return entry, "created", capture["id"]


def undo_capture(entry_id: str, capture_id: str) -> tuple[dict | None, bool] | None:
    with _lock:
        entries = _read_unlocked()
        for index, entry in enumerate(entries):
            if entry.get("id") != entry_id:
                continue
            captures = entry.get("captures") or []
            remaining = [capture for capture in captures if capture.get("id") != capture_id]
            if len(remaining) == len(captures):
                return None
            if entry.get("origin") == "capture" and not remaining:
                from app.sycee.research_sharing import revoke_shares_transactionally

                with revoke_shares_transactionally(entry_id):
                    entries.pop(index)
                    _write_unlocked(entries)
                return None, True
            updated = {**entry, "captures": remaining, "updated_at": _now()}
            entries[index] = updated
            _write_unlocked(entries)
            return updated, False
    return None


def _validate_entry_id(entry_id: str) -> None:
    if not _ENTRY_ID_RE.fullmatch(entry_id):
        raise HTTPException(status_code=400, detail="研究记录 id 非法")


def _validate_capture_id(capture_id: str) -> None:
    if not _CAPTURE_ID_RE.fullmatch(capture_id):
        raise HTTPException(status_code=400, detail="捕获记录 id 非法")


@router.get("", response_model=ResearchEntryListResponse)
def get_research_entries():
    entries = list_entries()
    return {"entries": entries, "total": len(entries)}


@router.post("", status_code=201, response_model=ResearchEntryResponse)
def post_research_entry(request: ResearchEntryCreate):
    return {"entry": create_entry(request)}


@router.post("/capture", response_model=ResearchCaptureResponse)
def post_research_capture(request: ResearchCaptureRequest):
    entry, action, capture_id = capture_stock(request)
    return {"entry": entry, "action": action, "capture_id": capture_id}


@router.patch("/{entry_id}", response_model=ResearchEntryResponse)
def patch_research_entry(entry_id: str, request: ResearchEntryUpdate):
    _validate_entry_id(entry_id)
    try:
        entry = update_entry(entry_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="研究记录不存在")
    return {"entry": entry}


@router.delete("/{entry_id}")
def remove_research_entry(entry_id: str):
    _validate_entry_id(entry_id)
    if not delete_entry(entry_id):
        raise HTTPException(status_code=404, detail="研究记录不存在")
    return {"ok": True}


@router.delete(
    "/{entry_id}/captures/{capture_id}", response_model=ResearchCaptureUndoResponse
)
def remove_research_capture(entry_id: str, capture_id: str):
    _validate_entry_id(entry_id)
    _validate_capture_id(capture_id)
    result = undo_capture(entry_id, capture_id)
    if result is None:
        raise HTTPException(status_code=404, detail="捕获记录不存在")
    entry, entry_deleted = result
    return {"ok": True, "entry_deleted": entry_deleted, "entry": entry}
