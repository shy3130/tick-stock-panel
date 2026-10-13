"""AI 助手会话历史服务端存储 — 单用户, 前端为主写入方, 后端只存取与裁剪。

动机: 会话历史是用户产生的内容而非设备习惯, 原先只存浏览器 localStorage,
换设备即不可见; 2026-10 起同步到服务端, localStorage 降级为启动缓存/离线兜底。

文件: data/user_data/assistant_sessions.json
  {"sessions": [...], "activeId": "..."}   (字段名与前端 store.ts 载荷一致)

容量纪律 (与前端 store.ts 相同): ≤20 会话 × 每会话 ≤200 条消息, 超限裁尾部。
写走 fs_utils.atomic_write_text 原子替换; 读带 mtime 签名缓存 — GET 只在应用
加载时调用一次、写经前端防抖, 磁盘 IO 可忽略, 不影响其他任何请求路径。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

MAX_SESSIONS = 20
MAX_MESSAGES_PER_SESSION = 200
_FILENAME = "assistant_sessions.json"

_cache: dict[str, Any] | None = None
_cache_sig: tuple[int, int] | None = None


def _path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / _FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _signature(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _sanitize_sessions(data: Any) -> list[dict]:
    """结构校验 + 容量裁剪: 只信 id 为字符串的会话, 消息截尾至 200 条。"""
    if not isinstance(data, dict):
        return []
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return []
    out: list[dict] = []
    for s in sessions:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"]:
            continue
        msgs = s.get("messages")
        if not isinstance(msgs, list):
            msgs = []
        out.append({**s, "messages": msgs[-MAX_MESSAGES_PER_SESSION:]})
    return out[:MAX_SESSIONS]


def _sanitize_active(data: Any) -> str:
    if isinstance(data, dict):
        active = data.get("activeId")
        if isinstance(active, str):
            return active
    return ""


def load_sessions(data_dir: Path) -> dict[str, Any]:
    """读取 {sessions, activeId}; 文件不存在/损坏返回空结构 (不抛错)。"""
    global _cache, _cache_sig
    p = _path(data_dir)
    sig = _signature(p)
    if sig is None:
        return {"sessions": [], "activeId": ""}
    if _cache is not None and sig == _cache_sig:
        return _cache
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — 损坏文件视为无历史, 不阻断
        logger.warning("assistant sessions 文件损坏, 视为空: %s", e)
        return {"sessions": [], "activeId": ""}
    result = {
        "sessions": _sanitize_sessions(data),
        "activeId": _sanitize_active(data),
    }
    _cache, _cache_sig = result, sig
    return result


def save_sessions(data_dir: Path, payload: dict[str, Any]) -> int:
    """校验 + 裁剪 + 原子写盘; 返回保留的会话数。"""
    global _cache, _cache_sig
    doc = {
        "sessions": _sanitize_sessions(payload),
        "activeId": _sanitize_active(payload),
    }
    p = _path(data_dir)
    atomic_write_text(p, json.dumps(doc, ensure_ascii=False))
    _cache, _cache_sig = doc, _signature(p)
    return len(doc["sessions"])
