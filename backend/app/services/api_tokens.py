"""API Token 域模块 — 外部调用方的第二认证通道 (与访问密码并行)。

设计 (docs/open-platform-plan.md §4.1):
  - 存储: data/user_data/api_tokens.json (0600), 只存 SHA-256 哈希;
    明文 Token (tsp_ + 32B hex) 仅在创建响应中出现一次。
  - scope: 固定五档, 见 SCOPES; 管理接口无对应 scope, Token 永远进不去。
  - last_used_at 节流落盘 (≥60s 才写一次), 避免热路径每请求写文件。
进程内写互斥沿用域模块惯例 (PAPER_LOCK 模式); 读取无锁 (json 小文件)。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 对外开放的 scope 全集 (admin 刻意不存在 — 管理面永不开放给 Token)
SCOPES = (
    "read:market",
    "read:ext",
    "write:ext",
    "read:analysis",
    "run:backtest",
    "paper:trade",
)

TOKEN_PREFIX = "tsp_"

_LOCK = threading.Lock()
# last_used_at 落盘节流: (token_id, 上次落盘时间); 内存即可, 重启丢失无害
_last_persist: dict[str, datetime] = {}


def _tokens_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "api_tokens.json"


def load_tokens(data_dir: Path) -> list[dict]:
    p = _tokens_path(data_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw.get("tokens", [])
    except Exception as e:
        logger.warning("api_tokens.json 解析失败 (视为无 Token): %s", e)
        return []


def _save(data_dir: Path, tokens: list[dict]) -> None:
    p = _tokens_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2))
    # Windows 权限模型不同, 尽力而为
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def list_tokens(data_dir: Path) -> list[dict]:
    """管理列表视图: 去掉哈希, 附加 scope 有效性 (未知 scope 标 invalid)。"""
    out = []
    for t in load_tokens(data_dir):
        out.append({
            "id": t["id"],
            "name": t["name"],
            "scopes": t["scopes"],
            "created_at": t["created_at"],
            "last_used_at": t.get("last_used_at"),
            "revoked": bool(t.get("revoked")),
        })
    return out


def create_token(data_dir: Path, name: str, scopes: list[str]) -> tuple[dict, str]:
    """创建 Token。返回 (记录视图, 明文) — 明文只此一次, 调用方负责展示。"""
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"未知 scope: {bad} (可用: {list(SCOPES)})")
    plaintext = TOKEN_PREFIX + secrets.token_hex(32)
    record = {
        "id": f"tok_{secrets.token_hex(4)}",
        "name": name.strip() or "未命名",
        "token_hash": _hash(plaintext),
        "scopes": sorted(set(scopes)),
        "created_at": cn_now().isoformat(timespec="seconds"),
        "last_used_at": None,
        "revoked": False,
    }
    with _LOCK:
        tokens = load_tokens(data_dir)
        tokens.append(record)
        _save(data_dir, tokens)
    view = {k: v for k, v in record.items() if k != "token_hash"}
    return view, plaintext


def revoke_token(data_dir: Path, token_id: str) -> bool:
    with _LOCK:
        tokens = load_tokens(data_dir)
        hit = False
        for t in tokens:
            if t["id"] == token_id:
                t["revoked"] = True
                hit = True
        if hit:
            _save(data_dir, tokens)
        return hit


def verify_token(data_dir: Path, plaintext: str) -> dict | None:
    """明文 → 有效记录 (revoked/不匹配/哈希对不上 → None)。

    命中时节流更新 last_used_at (内存立即, 落盘 ≥60s 一次)。
    """
    if not plaintext.startswith(TOKEN_PREFIX):
        return None
    h = _hash(plaintext)
    for t in load_tokens(data_dir):
        if t.get("token_hash") == h and not t.get("revoked"):
            now = cn_now()
            if _should_persist(t["id"], now):
                _touch(data_dir, t, now)
            return t
    return None


def _should_persist(token_id: str, now: datetime) -> bool:
    last = _last_persist.get(token_id)
    if last is None or (now - last).total_seconds() >= 60:
        _last_persist[token_id] = now
        return True
    return False


def _touch(data_dir: Path, record: dict, now: datetime) -> None:
    record["last_used_at"] = now.isoformat(timespec="seconds")
    try:
        with _LOCK:
            tokens = load_tokens(data_dir)
            for t in tokens:
                if t["id"] == record["id"]:
                    t["last_used_at"] = record["last_used_at"]
            _save(data_dir, tokens)
    except Exception as e:
        logger.warning("api_tokens last_used_at 落盘失败 (忽略): %s", e)
