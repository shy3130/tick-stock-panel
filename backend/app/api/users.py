"""Administrator account and invite management API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.services import users

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not user.is_admin:
        raise HTTPException(403, "仅管理员可执行此操作")
    return user


class CreateInviteIn(BaseModel):
    label: str = Field(default="", max_length=64)


class DisableUserIn(BaseModel):
    disabled: bool


@router.get("/users")
def list_accounts(request: Request) -> dict:
    _require_admin(request)
    return {"users": users.list_users(settings.data_dir)}


@router.put("/users/{user_id}/disabled")
def update_account(user_id: str, req: DisableUserIn, request: Request) -> dict:
    admin = _require_admin(request)
    if user_id == admin.id and req.disabled:
        raise HTTPException(400, "不能停用当前账户")
    try:
        users.set_user_disabled(settings.data_dir, user_id, req.disabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if req.disabled:
        registry = getattr(request.app.state, "user_runtime_registry", None)
        if registry:
            registry.remove(user_id)
    return {"ok": True}


@router.get("/invites")
def list_invite_codes(request: Request) -> dict:
    _require_admin(request)
    return {"invites": users.list_invites(settings.data_dir)}


@router.post("/invites")
def create_invite_code(req: CreateInviteIn, request: Request) -> dict:
    _require_admin(request)
    code = users.create_invite(settings.data_dir, req.label)
    return {"ok": True, "code": code}
