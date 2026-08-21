"""用户选股方案 (screener screens) 的 JSON 存储。

方案持久化在 ``{data_dir}/user_data/screener_screens.json``, 单文件原子写
(tmp + os.replace)。conditions/order_by/limit 复用 ScreenerQueryRequest 的
pydantic 结构校验; 字段语义 (unknown field 等) 留给查询期 validate_query。
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.services.screener_query import ScreenerQueryRequest

MAX_SCREENS = 50
_NAME_MAX_CHARS = 40
_ID_HEX_CHARS = 12


class ScreenStoreError(Exception):
    """方案校验失败 (API 层映射 400)。code: invalid_name | invalid_conditions | screen_limit"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ScreenNotFoundError(Exception):
    """方案 id 不存在 (API 层映射 404)。"""


def store_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "user_data" / "screener_screens.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 坏文件按空处理: 读失败不阻断接口, 下一次成功写入即恢复
        return []
    screens = payload.get("screens", []) if isinstance(payload, dict) else payload
    if not isinstance(screens, list):
        return []
    return [s for s in screens if isinstance(s, dict) and "id" in s]


def _atomic_save(path: Path, screens: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"screens": screens}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _validate(
    name: Any,
    conditions: Any,
    order_by: Any,
    limit: Any,
    group_logic: Any = None,
) -> dict[str, Any]:
    """名称范围校验 + 查询结构校验 (复用 ScreenerQueryRequest), 返回可存字段。"""
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= _NAME_MAX_CHARS:
        raise ScreenStoreError("invalid_name", f"方案名称需为 1..{_NAME_MAX_CHARS} 个字符")
    try:
        req = ScreenerQueryRequest(
            conditions=conditions,
            order_by=order_by,
            limit=limit if limit is not None else 100,
            group_logic=group_logic or "and",
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        raise ScreenStoreError(
            "invalid_conditions", f"查询结构校验失败: {loc} {first.get('msg', '')}"
        ) from None
    return {
        "name": name.strip(),
        # group=None 不落盘, 旧方案的存储形状逐字节不变
        "conditions": [c.model_dump(exclude_none=True) for c in req.conditions],
        "order_by": req.order_by.model_dump() if req.order_by is not None else None,
        "limit": limit,
        "group_logic": req.group_logic,
    }


def _new_id(existing: set[str]) -> str:
    while True:
        candidate = secrets.token_hex(_ID_HEX_CHARS // 2)
        if candidate not in existing:
            return candidate


def list_screens(data_dir: Path | str) -> list[dict[str, Any]]:
    return _load(store_path(data_dir))


def create_screen(
    data_dir: Path | str,
    *,
    name: str,
    conditions: list[Any],
    order_by: dict[str, Any] | None = None,
    limit: int | None = None,
    group_logic: str | None = None,
) -> dict[str, Any]:
    fields = _validate(name, conditions, order_by, limit, group_logic)
    path = store_path(data_dir)
    screens = _load(path)
    if len(screens) >= MAX_SCREENS:
        raise ScreenStoreError("screen_limit", f"选股方案数量已达上限 {MAX_SCREENS}")
    now = _now()
    record = {
        "id": _new_id({s["id"] for s in screens}),
        **fields,
        "created_at": now,
        "updated_at": now,
    }
    screens.append(record)
    _atomic_save(path, screens)
    return record


def update_screen(
    data_dir: Path | str,
    screen_id: str,
    *,
    name: str,
    conditions: list[Any],
    order_by: dict[str, Any] | None = None,
    limit: int | None = None,
    group_logic: str | None = None,
) -> dict[str, Any]:
    fields = _validate(name, conditions, order_by, limit, group_logic)
    path = store_path(data_dir)
    screens = _load(path)
    for screen in screens:
        if screen["id"] == screen_id:
            screen.update(fields)
            screen["updated_at"] = _now()
            _atomic_save(path, screens)
            return screen
    raise ScreenNotFoundError(screen_id)


def delete_screen(data_dir: Path | str, screen_id: str) -> None:
    path = store_path(data_dir)
    screens = _load(path)
    remaining = [s for s in screens if s["id"] != screen_id]
    if len(remaining) == len(screens):
        raise ScreenNotFoundError(screen_id)
    _atomic_save(path, remaining)
