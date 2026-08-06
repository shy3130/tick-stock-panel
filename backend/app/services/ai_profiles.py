"""AI profile store backed by secrets.json."""
from __future__ import annotations

import shlex
import shutil
import uuid

from app import secrets_store

OPENAI_COMPAT_PROVIDER = "openai_compat"
ACP_PROVIDER = "acp"
CODEX_CLI_PROVIDER = "codex_cli"

_PROFILE_FIELDS = (
    "id", "name", "provider", "base_url", "api_key", "model",
    "codex_command", "launch_command", "user_agent",
)


def _all() -> list[dict]:
    migrate_legacy_if_needed()
    return list(secrets_store.load().get("ai_profiles") or [])


def _persist(profiles: list[dict], default_id: str | None) -> None:
    secrets_store.save({"ai_profiles": profiles, "ai_default_profile_id": default_id or ""})


def list_profiles() -> list[dict]:
    return _all()


def list_profiles_masked() -> list[dict]:
    default_id = get_default_profile_id()
    out = []
    for p in _all():
        item = {k: p.get(k) for k in _PROFILE_FIELDS if k != "api_key"}
        item["has_api_key"] = bool(p.get("api_key"))
        item["api_key_masked"] = secrets_store.mask(p.get("api_key") or "")
        item["is_default"] = p.get("id") == default_id
        item["available"] = is_available(p)
        out.append(item)
    return out


def get_profile(profile_id: str) -> dict | None:
    return next((p for p in _all() if p.get("id") == profile_id), None)


def get_default_profile_id() -> str:
    data = secrets_store.load()
    did = data.get("ai_default_profile_id") or ""
    profiles = _all()
    if did and any(p.get("id") == did for p in profiles):
        return did
    return profiles[0]["id"] if profiles else ""


def resolve_profile(profile_id: str | None) -> dict | None:
    if profile_id:
        profile = get_profile(profile_id)
        if profile:
            return profile
    default_id = get_default_profile_id()
    return get_profile(default_id) if default_id else None


def create_profile(**fields) -> dict:
    provider = fields.get("provider") or OPENAI_COMPAT_PROVIDER
    profile = {
        "id": f"p_{uuid.uuid4().hex[:8]}",
        "name": fields.get("name") or provider,
        "provider": provider,
        "base_url": fields.get("base_url") or "",
        "api_key": fields.get("api_key") or "",
        "model": fields.get("model") or "",
        "codex_command": fields.get("codex_command") or "codex",
        "launch_command": fields.get("launch_command") or "",
        "user_agent": fields.get("user_agent") or "",
    }
    profiles = _all()
    profiles.append(profile)
    _persist(profiles, get_default_profile_id() or profile["id"])
    return profile


def update_profile(profile_id: str, **fields) -> dict:
    profiles = _all()
    for profile in profiles:
        if profile.get("id") != profile_id:
            continue
        for key, value in fields.items():
            if key in _PROFILE_FIELDS and key != "id" and value is not None:
                profile[key] = value
        _persist(profiles, get_default_profile_id())
        return profile
    raise KeyError(profile_id)


def delete_profile(profile_id: str) -> None:
    profiles = [p for p in _all() if p.get("id") != profile_id]
    default_id = get_default_profile_id()
    if default_id == profile_id:
        default_id = profiles[0]["id"] if profiles else ""
    _persist(profiles, default_id)


def set_default(profile_id: str) -> None:
    if not get_profile(profile_id):
        raise KeyError(profile_id)
    _persist(_all(), profile_id)


def is_available(profile: dict) -> bool:
    provider = profile.get("provider")
    if provider == CODEX_CLI_PROVIDER:
        return shutil.which("codex") is not None
    if provider == ACP_PROVIDER:
        parts = shlex.split(profile.get("launch_command") or "")
        return bool(parts and shutil.which(parts[0]))
    return True


def migrate_legacy_if_needed() -> None:
    data = secrets_store.load()
    if data.get("ai_profiles"):
        return
    if not any(data.get(k) for k in ("ai_provider", "ai_api_key", "ai_codex_command", "ai_model")):
        return
    profile = {
        "id": f"p_{uuid.uuid4().hex[:8]}",
        "name": "默认",
        "provider": data.get("ai_provider") or OPENAI_COMPAT_PROVIDER,
        "base_url": data.get("ai_base_url") or "",
        "api_key": data.get("ai_api_key") or "",
        "model": data.get("ai_model") or "",
        "codex_command": data.get("ai_codex_command") or "codex",
        "launch_command": "",
        "user_agent": data.get("ai_user_agent") or "",
    }
    _persist([profile], profile["id"])



def list_profile_ids() -> list[str]:
    """返回所有已注册 profile id，用于 route policy 校验（保持顺序）。"""
    return [p.get("id") for p in list_profiles() if p.get("id")]