"""Revocable, read-only snapshots for sharing research entries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.config import settings
from app.services.user_storage import path_for
from app.sycee.research_ledger import ResearchEntry, list_entries

router = APIRouter(prefix="/api/sycee/research", tags=["sycee-research-sharing"])
public_router = APIRouter(
    prefix="/api/public/sycee/research", tags=["sycee-public-research"]
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_SHARE_ID_RE = re.compile(r"^research_share_[0-9a-f]{32}$")
_lock = threading.RLock()


class ResearchShare(BaseModel):
    id: str
    entry_id: str
    token: str
    created_at: str
    refreshed_at: str
    entry_updated_at: str


class ResearchShareResponse(BaseModel):
    share: ResearchShare | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _index_path() -> Path:
    path = path_for(settings.data_dir, "sycee/research_shares.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _public_dir() -> Path:
    path = settings.data_dir / "sycee_public" / "research_shares"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _public_path(token: str) -> Path:
    return _public_dir() / f"{_token_hash(token)}.json"


def _atomic_write(path: Path, payload: dict) -> None:
    temp = path.with_suffix(f"{path.suffix}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_index_unlocked() -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("研究分享索引无法读取") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("研究分享索引版本无效")
    raw_shares = payload.get("shares")
    if not isinstance(raw_shares, list):
        raise RuntimeError("研究分享索引内容无效")
    try:
        return [ResearchShare.model_validate(item).model_dump() for item in raw_shares]
    except ValueError as exc:
        raise RuntimeError("研究分享索引内容无效") from exc


def _write_index_unlocked(shares: list[dict]) -> None:
    _atomic_write(_index_path(), {"version": 1, "shares": shares})


def _entry(entry_id: str) -> dict:
    entry = next((item for item in list_entries() if item.get("id") == entry_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="研究记录不存在")
    return ResearchEntry.model_validate(entry).model_dump()


def _public_entry(entry: dict) -> dict:
    return {
        "title": entry["title"],
        "subject_type": entry["subject_type"],
        "subject": entry["subject"],
        "thesis": entry["thesis"],
        "evidence": entry["evidence"],
        "counter_evidence": entry["counter_evidence"],
        "invalidation": entry["invalidation"],
        "plan": entry["plan"],
        "status": entry["status"],
        "tags": entry["tags"],
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        "captures": [
            {
                "captured_at": capture["captured_at"],
                "source_label": capture["source_label"],
                "summary": capture["summary"],
            }
            for capture in entry["captures"]
        ],
    }


def _public_document(share: dict, entry: dict) -> dict:
    return {
        "version": 1,
        "share_id": share["id"],
        "published_at": share["created_at"],
        "refreshed_at": share["refreshed_at"],
        "entry": _public_entry(entry),
    }


def _find_share(shares: list[dict], entry_id: str) -> dict | None:
    return next((share for share in shares if share.get("entry_id") == entry_id), None)


def get_share(entry_id: str) -> dict | None:
    with _lock:
        shares = _read_index_unlocked()
        share = _find_share(shares, entry_id)
        if share is None:
            return None
        if not _public_path(share["token"]).exists():
            shares = [item for item in shares if item.get("id") != share["id"]]
            _write_index_unlocked(shares)
            return None
        return share


def create_share(entry_id: str) -> dict:
    entry = _entry(entry_id)
    with _lock:
        shares = _read_index_unlocked()
        existing = _find_share(shares, entry_id)
        if existing and _public_path(existing["token"]).exists():
            return existing
        if existing:
            shares = [item for item in shares if item.get("id") != existing["id"]]

        now = _now()
        share = {
            "id": f"research_share_{uuid4().hex}",
            "entry_id": entry_id,
            "token": secrets.token_urlsafe(32),
            "created_at": now,
            "refreshed_at": now,
            "entry_updated_at": entry["updated_at"],
        }
        public_path = _public_path(share["token"])
        _atomic_write(public_path, _public_document(share, entry))
        try:
            _write_index_unlocked([share, *shares])
        except Exception:
            public_path.unlink(missing_ok=True)
            raise
        return share


def refresh_share(entry_id: str) -> dict:
    entry = _entry(entry_id)
    with _lock:
        shares = _read_index_unlocked()
        existing = _find_share(shares, entry_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="研究记录尚未分享")
        refreshed = {
            **existing,
            "refreshed_at": _now(),
            "entry_updated_at": entry["updated_at"],
        }
        _atomic_write(_public_path(refreshed["token"]), _public_document(refreshed, entry))
        updated = [refreshed if item.get("id") == refreshed["id"] else item for item in shares]
        _write_index_unlocked(updated)
        return refreshed


def revoke_shares_for_entry(entry_id: str) -> bool:
    with _lock:
        shares = _read_index_unlocked()
        matched = [share for share in shares if share.get("entry_id") == entry_id]
        if not matched:
            return False
        for share in matched:
            _public_path(share["token"]).unlink(missing_ok=True)
        _write_index_unlocked(
            [share for share in shares if share.get("entry_id") != entry_id]
        )
        return True


def read_public_share(token: str) -> dict:
    if not _TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=404, detail="分享不存在或已撤销")
    path = _public_path(token)
    if not path.exists():
        raise HTTPException(status_code=404, detail="分享不存在或已撤销")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="分享不存在或已撤销") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not _SHARE_ID_RE.fullmatch(str(payload.get("share_id", "")))
        or not isinstance(payload.get("entry"), dict)
    ):
        raise HTTPException(status_code=404, detail="分享不存在或已撤销")
    return payload


@router.get("/{entry_id}/share", response_model=ResearchShareResponse)
def get_research_share(entry_id: str):
    _entry(entry_id)
    return {"share": get_share(entry_id)}


@router.post("/{entry_id}/share", status_code=201, response_model=ResearchShareResponse)
def post_research_share(entry_id: str):
    return {"share": create_share(entry_id)}


@router.put("/{entry_id}/share", response_model=ResearchShareResponse)
def put_research_share(entry_id: str):
    return {"share": refresh_share(entry_id)}


@router.delete("/{entry_id}/share")
def delete_research_share(entry_id: str):
    _entry(entry_id)
    if not revoke_shares_for_entry(entry_id):
        raise HTTPException(status_code=404, detail="研究记录尚未分享")
    return {"ok": True}


@public_router.get("/{token}")
def get_public_research_share(token: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return read_public_share(token)
