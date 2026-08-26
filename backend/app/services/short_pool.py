"""AI 短线池 — 固定 preset 的确定性短线观察池服务。

设计契约(研究观察用途, 不产生任何交易指令):

  - 策略完全固定: preset ``short_momentum_quality_v1`` 的条件集与排序在
    本模块内逐字锁定, 调用方(Agent)只能选择 ``limit``(5..12, 默认 8)。
    候选由 QueryService 确定性筛选产生; 模型只解释 evidence, 不得
    生成/删除/重排候选, 不给买卖方向、价格或仓位。
  - 复用 canonical 数据链: 只通过 screener_query.QueryService /
    ScreenerQueryRequest 查询, 不直连 DuckDB、不发 HTTP、无外部 fallback。
  - artifact 不可变且内容寻址: ``user_data/short_pools/{pool_id}.json``,
    pool_id 为规范内容 sha256 前缀(16 hex), 完整 sha256 记入 ``checksum``。
    相同内容幂等复用(不覆写); artifact 被篡改或 hash 碰撞 → fail-closed,
    错误消息不含本地路径。空池同样成功并落盘。
  - 所有进入模型上下文的值只来自 QueryService 返回值(rows)与
    validate_query 的 applied conditions, 不引入第二数据源。

模块导入无副作用: app.* 依赖全部在函数体内延迟导入。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt

# ── 固定 preset(逐字锁定, 测试断言其不可漂移) ─────────────────────
SHORT_POOL_PRESET_ID = "short_momentum_quality_v1"
SHORT_POOL_PRESET_VERSION = 1
SHORT_POOL_PRESET_NAME = "短线动量质量观察"
SHORT_POOL_PRESET_DESCRIPTION = (
    "以流动性、趋势位置、温和动量、波动与涨停风险约束形成的固定研究观察池"
)
SHORT_POOL_SCHEMA_VERSION = 1

MIN_LIMIT = 5
MAX_LIMIT = 12
DEFAULT_LIMIT = 8

SHORT_POOL_DISCLAIMER = "研究观察池，非投资建议"
SHORT_POOL_AI_ROLE = "AI 只解释证据；不得生成、删除或重排候选；不提供买卖方向、价格或仓位建议"


# 条件顺序即 evidence 顺序, 与契约逐字一致。
SHORT_POOL_CONDITIONS: tuple[dict[str, Any], ...] = (
    {"field": "exclude_st", "op": "=", "value": True},
    {"field": "listing_days", "op": ">=", "value": 120},
    {"field": "amount", "op": ">=", "value": 300000000},
    {"field": "turnover_rate", "op": "between", "value": [2, 18]},
    {"field": "above_ma20", "op": "=", "value": True},
    {"field": "momentum_20d", "op": "between", "value": [0.03, 0.25]},
    {"field": "distance_to_60d_high", "op": "between", "value": [-15, 0]},
    {"field": "atr_pct_14", "op": "between", "value": [2, 9]},
    {"field": "vol_ratio_5d", "op": ">=", "value": 1},
    {"field": "change_pct", "op": "between", "value": [-0.03, 0.08]},
    {"field": "limit_up", "op": "=", "value": False},
    {"field": "broken_limit_up", "op": "=", "value": False},
)
SHORT_POOL_ORDER_BY: dict[str, str] = {"field": "momentum_20d", "direction": "desc"}

_EVIDENCE_KEYS = ("field", "label", "actual", "display", "op", "target", "criterion", "unit")
_POOL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_PATH_IN_MSG_RE = re.compile(r"[/\\][^\s\"']*")

_SHORT_POOL_WRITE_LOCK = threading.Lock()


class ShortPoolLimit(BaseModel):
    """preset 分支唯一可调参数: limit(5..12, 默认 8), 其余一律 forbid。"""

    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)


def build_query_request(limit: int = DEFAULT_LIMIT):
    """按固定 preset 构造 ScreenerQueryRequest；越界或非整数输入 fail-closed。"""
    from app.services.screener_query import QueryOrder, ScreenerQueryRequest

    parsed = ShortPoolLimit(limit=limit)
    return ScreenerQueryRequest(
        conditions=[dict(cond) for cond in SHORT_POOL_CONDITIONS],
        order_by=QueryOrder(**dict(SHORT_POOL_ORDER_BY)),
        limit=parsed.limit,
    )


# ── 展示格式化(确定性, 不引入外部数据) ────────────────────────────
def _display(field: str, value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if field in ("momentum_20d", "change_pct"):
        return f"{float(value) * 100:.2f}%"
    if field in ("turnover_rate", "distance_to_60d_high", "atr_pct_14"):
        return f"{float(value):.2f}%"
    if field == "amount":
        return f"{float(value) / 1e8:.2f}亿元"
    if field == "vol_ratio_5d":
        return f"{float(value):.2f}倍"
    if field == "listing_days":
        return f"{float(value):.0f}天"
    return str(value)


def _criterion(label: str, op: str, target: Any) -> str:
    if isinstance(target, list):
        return f"{label} ∈ [{target[0]}, {target[1]}]"
    if isinstance(target, bool):
        return f"{label} = {'是' if target else '否'}"
    return f"{label} {op} {target}"


def _evidence(row: dict[str, Any], applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐股证据: actual 只取 QueryService 返回行, op/target 来自 applied conditions。"""
    from app.services.screener_query import get_field_spec

    entries: list[dict[str, Any]] = []
    for cond in applied:
        spec = get_field_spec(cond["field"])
        label = spec.label if spec is not None else cond["field"]
        unit = spec.unit if spec is not None and spec.unit is not None else ""
        entries.append(
            {
                "field": cond["field"],
                "label": label,
                "actual": row.get(cond["field"]),
                "display": _display(cond["field"], row.get(cond["field"])),
                "op": cond["op"],
                "target": cond["value"],
                "criterion": _criterion(label, cond["op"], cond["value"]),
                "unit": unit,
            }
        )
    return entries


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _checksum_hex(content: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _pool_id_hex(content: dict[str, Any]) -> str:
    return _checksum_hex(content)[:16]


def _short_pools_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "short_pools"


def _sanitize_error(exc: BaseException) -> str:
    message = str(exc) or type(exc).__name__
    return _PATH_IN_MSG_RE.sub("<path>", message)


def _validate_short_pool(pool: Any, pool_id: str) -> dict[str, Any]:
    """防御性校验 artifact(文件可能被外部改动), 失败消息不含路径。"""
    if not isinstance(pool, dict):
        raise ValueError(f"unusable short pool: {pool_id}")
    if pool.get("pool_id") != pool_id:
        raise ValueError(f"short pool pool_id mismatch: {pool_id}")
    if not isinstance(pool.get("data_watermark"), dict):
        raise ValueError(f"unusable short pool watermark: {pool_id}")
    content = {k: v for k, v in pool.items() if k not in ("pool_id", "data_watermark", "checksum")}
    if _pool_id_hex(content) != pool_id:
        raise ValueError(f"short pool checksum mismatch: {pool_id}")
    checksum = pool.get("checksum")
    integrity_payload = {k: v for k, v in pool.items() if k != "checksum"}
    if not isinstance(checksum, str) or checksum != _checksum_hex(integrity_payload):
        raise ValueError(f"short pool checksum mismatch: {pool_id}")
    candidates = pool.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_LIMIT:
        raise ValueError(f"unusable short pool candidates: {pool_id}")
    if pool.get("count") != len(candidates):
        raise ValueError(f"short pool count mismatch: {pool_id}")
    for cand in candidates:
        if not isinstance(cand, dict) or not isinstance(cand.get("evidence"), list):
            raise ValueError(f"unusable short pool evidence: {pool_id}")
        for entry in cand["evidence"]:
            if not isinstance(entry, dict) or any(k not in entry for k in _EVIDENCE_KEYS):
                raise ValueError(f"unusable short pool evidence: {pool_id}")
    return pool


def _load_short_pool(path: Path, pool_id: str) -> dict[str, Any]:
    if not _POOL_ID_RE.fullmatch(pool_id):
        raise ValueError("invalid short pool_id")
    if not path.is_file():
        raise ValueError(f"unknown short pool_id: {pool_id}")
    try:
        pool = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"unreadable short pool: {pool_id}") from exc
    return _validate_short_pool(pool, pool_id)


def run_short_pool(
    app_state: Any,
    limit: int = DEFAULT_LIMIT,
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """运行固定 preset 的确定性短线观察池并落盘 artifact; 返回完整证据封套。"""
    from app.services.agent_research_tools import (
        _atomic_write_json,
        _data_watermark,
        _require_repo,
    )
    from app.services.screener_query import (
        QueryService,
        ScreenerDataUnavailableError,
        ScreenerSemanticError,
        validate_query,
    )

    parsed = ShortPoolLimit(limit=limit)
    req = build_query_request(parsed.limit)
    applied, _order = validate_query(req)
    repo = _require_repo(app_state)
    try:
        result = QueryService(repo).query(req)
    except ScreenerDataUnavailableError as exc:
        raise ValueError(f"短线池数据不可用(字段: {','.join(exc.fields)})") from exc
    except ScreenerSemanticError as exc:
        raise ValueError(f"短线池条件无效 {exc.location}: {exc.reason}") from exc

    rows = [row for row in (result.get("rows") or []) if row.get("symbol")][: parsed.limit]
    applied = list(result.get("applied") or applied)
    as_of = str(result["as_of"])
    total = int(result.get("total") or len(rows))
    count = len(rows)
    candidates = [
        {
            "rank": rank,
            "symbol": str(row["symbol"]),
            "name": str(row.get("name") or row["symbol"]),
            "evidence": _evidence(row, applied),
        }
        for rank, row in enumerate(rows, start=1)
    ]


    content = {
        "schema_version": SHORT_POOL_SCHEMA_VERSION,
        "preset": {
            "preset_id": SHORT_POOL_PRESET_ID,
            "version": SHORT_POOL_PRESET_VERSION,
            "name": SHORT_POOL_PRESET_NAME,
            "description": SHORT_POOL_PRESET_DESCRIPTION,
        },
        "as_of": as_of,
        "count": count,
        "total": total,
        "limit": parsed.limit,
        "conditions": applied,
        "order_by": dict(SHORT_POOL_ORDER_BY),
        "candidates": candidates,
    }
    pool_id = _pool_id_hex(content)
    storage_root = Path(repo.store.data_dir) if artifact_root is None else Path(artifact_root)
    pool_path = _short_pools_dir(storage_root) / f"{pool_id}.json"
    with _SHORT_POOL_WRITE_LOCK:
        if pool_path.is_file():
            existing = _load_short_pool(pool_path, pool_id)
            if any(existing.get(key) != value for key, value in content.items()):
                raise ValueError(f"short pool hash collision: {pool_id}")
        else:
            pool = {
                "pool_id": pool_id,
                **content,
                "data_watermark": _data_watermark(repo),
            }
            pool["checksum"] = _checksum_hex(pool)
            try:
                _atomic_write_json(pool_path, pool)
            except Exception as exc:  # noqa: BLE001 — 对外统一打码路径
                raise ValueError(_sanitize_error(exc)) from exc

    return {
        "status": "success",
        "summary": (
            f"{SHORT_POOL_PRESET_NAME}池(确定性筛选): 命中 {total} 只, "
            f"输出 {count} 只, as_of={as_of}; AI 只解释证据"
        ),
        "pool_id": pool_id,
        "as_of": as_of,
        "count": count,
        "total": total,
        "preset": dict(content["preset"]),
        "candidates": candidates,
        "disclaimer": SHORT_POOL_DISCLAIMER,
        "selection_basis": {
            "conditions": applied,
            "order_by": dict(SHORT_POOL_ORDER_BY),
            "limit": parsed.limit,
            "deterministic": True,
        },
        "ai_role": SHORT_POOL_AI_ROLE,
        "next_actions": (
            ["view_stock_detail", "add_to_watchlist", "stage_strategy_backtest"]
            if candidates
            else []
        ),
        "artifacts": [
            {
                "kind": "short_pool",
                "pool_id": pool_id,
                "as_of": as_of,
                "count": count,
                "location": f"user_data/short_pools/{pool_id}.json",
            }
        ],
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "SHORT_POOL_AI_ROLE",
    "SHORT_POOL_CONDITIONS",
    "SHORT_POOL_DISCLAIMER",
    "SHORT_POOL_ORDER_BY",
    "SHORT_POOL_PRESET_ID",
    "SHORT_POOL_PRESET_NAME",
    "SHORT_POOL_PRESET_DESCRIPTION",
    "SHORT_POOL_PRESET_VERSION",
    "ShortPoolLimit",
    "build_query_request",
    "run_short_pool",
]
