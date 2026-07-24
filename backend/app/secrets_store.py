"""Key / 凭据本地存储(§14)。

存储位置:`data/user_data/secrets.json`,权限 0600。
优先级:secrets.json > .env > 空(Free 模式)。

UI 改 Key 时只动这个文件,不动 .env。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("secrets.json malformed: %s", e)
    return {}


def save(updates: dict) -> dict:
    """合并写入(不会清掉未提及的字段)。返回新内容。"""
    current = load()
    current.update({k: v for k, v in updates.items() if v is not None})
    p = _path()
    p.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return current


def clear(*keys: str) -> dict:
    """清掉指定字段(留空清全部)。"""
    p = _path()
    if not p.exists():
        return {}
    if not keys:
        p.unlink()
        return {}
    current = load()
    for k in keys:
        current.pop(k, None)
    p.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


def get_tickflow_key() -> str:
    """取当前 TickFlow Key:secrets.json 优先,否则 .env。"""
    val = load().get("tickflow_api_key")
    if val:
        return val
    from app.config import settings
    return settings.tickflow_api_key or ""


def get_ai_key() -> str:
    """取当前 AI Key:secrets.json 优先,否则 .env。"""
    val = load().get("ai_api_key")
    if val:
        return val
    from app.config import settings
    return settings.ai_api_key or ""


def get_ai_config(key: str, default: str = "") -> str:
    """取 AI 配置项:secrets.json 优先,否则 config。"""
    val = load().get(key)
    if val:
        return val
    from app.config import settings
    return getattr(settings, key, default) or default


def mask(key: str, prefix: int = 4, suffix: int = 4) -> str:
    """脱敏显示。"""
    if not key:
        return ""
    if len(key) <= prefix + suffix:
        return "•" * len(key)
    return f"{key[:prefix]}{'•' * 6}{key[-suffix:]}"
