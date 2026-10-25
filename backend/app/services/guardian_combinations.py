"""P2d 守护冠军组合: composite 策略生成 (用户确认后调用)。

只做"低相关健康冠军 → composite 策略文件 + 守护入护"这一件事;
候选对的发现由守护运行时的组合扫描产出 (guardian_runtime._run_combination_scan)。
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from app.services.guardian_store import GuardianStore, read_combinations

logger = logging.getLogger(__name__)

_MAX_NAME = 60
_GUARDIAN_COMBO_MAX = 50  # 累计生成的守护组合上限, 防无限膨胀


def render_composite_source(
    strategy_id: str,
    name: str,
    children: list[dict[str, Any]],
) -> str:
    meta = {
        "id": strategy_id,
        "name": name,
        "description": "守护冠军低相关组合 (自动生成, 来源见守护页)",
        "asset_types": ["stock"],
        "timeframes": ["1d"],
        "params": [
            {
                "id": "merge_mode",
                "label": "合并模式",
                "type": "select",
                "options": ["union", "intersect"],
                "default": "union",
            },
            {"id": "min_confirm", "label": "交集最少确认数", "type": "int",
             "default": 1, "min": 0},
        ],
        "scoring": {},
        "order_by": "score",
        "descending": True,
        "limit": 100,
        "children": children,
    }
    return (
        '"""守护组合策略 (自动生成, 请勿手改业务逻辑)。"""\n'
        f"META = {meta!r}\n"
        'EXECUTION_BACKEND = "composite"\n'
    )


def create_guardian_composite(
    data_dir: Path,
    strategy_engine: Any,
    *,
    left: str,
    right: str,
    latest_enriched: Any,
) -> dict[str, Any]:
    """按组合建议创建 composite 策略并纳入守护; 幂等 (已存在直接返回)。

    fail-closed: 建议对必须在最近一次组合扫描结果中 (健康 + 相关性 ≤0.7)。
    """
    suggestions = read_combinations(data_dir) or {}
    pair = next(
        (
            item for item in suggestions.get("suggestions") or []
            if {item.get("a"), item.get("b")} == {left, right}
        ),
        None,
    )
    if pair is None:
        raise ValueError(
            "该策略对不在最新组合建议中 — 请先运行组合扫描, 且双方需为健康守护策略"
        )
    if latest_enriched is None:
        raise ValueError("enriched 数据不可用, 无法建立守护基线")
    # 与 _custom_strategy_path 同口径: 先 resolve 再比较,
    # 避免 /tmp 等符号链接目录被误判为逃逸 data_dir
    data_dir = Path(data_dir).resolve()

    strategy_id = _combo_strategy_id(left, right)
    if strategy_engine.has(strategy_id):
        _ensure_guardian_profile(data_dir, strategy_id, latest_enriched)
        return {"ok": True, "strategy_id": strategy_id, "created": False}

    composite_dir = data_dir / "strategies" / "composite"
    unresolved = composite_dir
    unresolved.mkdir(parents=True, exist_ok=True)
    if unresolved.is_symlink():
        raise ValueError("composite strategy directory must not be a symlink")
    root = unresolved.resolve()
    if not root.is_relative_to(data_dir):
        raise ValueError("composite directory escapes data_dir")
    existing = sorted(root.glob("guardian_combo_*.py"))
    if len(existing) >= _GUARDIAN_COMBO_MAX:
        raise ValueError(
            f"守护组合数量已达上限 ({_GUARDIAN_COMBO_MAX}); 请删除不再使用的组合"
        )
    path = (root / f"{strategy_id}.py").resolve(strict=False)
    if path.parent != root:
        raise ValueError("composite path escapes its directory")

    left_name = _strategy_display_name(strategy_engine, left)
    right_name = _strategy_display_name(strategy_engine, right)
    name = f"{left_name}+{right_name}"[:_MAX_NAME]
    children = [
        {"strategy_id": left, "weight": 0.5},
        {"strategy_id": right, "weight": 0.5},
    ]
    source = render_composite_source(strategy_id, name, children)

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("failed to write composite strategy source") from exc

    strategy_engine.reload()
    strategy = strategy_engine.get(strategy_id)
    if strategy.execution_backend != "composite":
        raise RuntimeError("生成的组合策略未通过引擎校验")
    for child in children:
        strategy_engine.get(str(child["strategy_id"]))

    _ensure_guardian_profile(data_dir, strategy_id, latest_enriched)
    _invalidate_caches(data_dir)
    return {"ok": True, "strategy_id": strategy_id, "created": True}


def _ensure_guardian_profile(data_dir: Path, strategy_id: str, latest: Any) -> None:
    GuardianStore(data_dir).create_baseline(
        strategy_id,
        kind="trailing_reference",
        data_as_of=latest.isoformat(),
        objective="sharpe",
        metrics={},
        provenance={"kind": "guardian_combination"},
    )


def _invalidate_caches(data_dir: Path) -> None:
    try:
        from app.services.strategy_cache import clear_cache

        clear_cache(data_dir)
    except Exception:  # noqa: BLE001 — 缓存失效失败不阻断创建
        logger.warning("strategy cache invalidation failed", exc_info=True)


def _combo_strategy_id(left: str, right: str) -> str:
    payload = "\\0".join(sorted((left, right))).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).hexdigest()
    return f"guardian_combo_{digest}"


def _strategy_display_name(strategy_engine: Any, strategy_id: str) -> str:
    try:
        name = str(strategy_engine.get(strategy_id).meta.get("name") or "")
    except ValueError:
        name = ""
    return name or strategy_id
