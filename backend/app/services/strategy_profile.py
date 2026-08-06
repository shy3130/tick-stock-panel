"""策略风险声明 (strategy profile) — 失效信号 / 风险预算 / 期限声明。

存储 (与 strategy overrides 同目录, 文件名带 .profile 后缀, 与 {id}.json 互不覆盖):
    data/user_data/strategy_overrides/{strategy_id}.profile.json

Schema (schemaVersion=1):
    {
      "schemaVersion": 1,
      "strategyId": "...",
      "invalidation": [{"name", "observable", "action"}],
      "risk": {"positionLimitPct", "lossBudgetPct", "thesisHorizonMonths"},
      "cadence": {"review": "weekly|monthly|quarterly"}
    }

职责: 纯读写 + 结构校验 (validate_profile)。
不知道: 策略引擎 / 回测 / 前端 / 行情。机械校验规则见 docstring 末尾。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# 策略坐标卡 (family) 合法值 — 见 ymos-diagnosis strategy_family_map.md
FAMILY_VALUES = (
    "value", "growth", "trend", "event",
    "short_horizon", "relative_value", "mixed",
)
# family=mixed 时必须显式裁决的四要素 (入场裁判/失效权/仓位期限/冲突裁决)
FAMILY_MIX_KEYS = ("entryJudge", "invalidationAuthority", "sizingHorizon", "conflictResolution")
# playbook 文本字段 (均可缺省; 出现则必须为字符串)
PLAYBOOK_KEYS = ("scope", "entry", "exit")


def _overrides_dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "strategy_overrides"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(strategy_id: str) -> str:
    # strategy_id 由引擎 META.id 产生; 做一次路径穿越防御 (与 trading.store 一致)
    return strategy_id.replace("/", "_").replace("\\", "_").replace("..", "_")


def profile_path(data_dir: Path, strategy_id: str) -> Path:
    return _overrides_dir(data_dir) / f"{_safe_id(strategy_id)}.profile.json"


def read_profile(data_dir: Path, strategy_id: str) -> dict[str, Any] | None:
    """读取策略风险声明; 不存在或损坏返回 None (损坏只告警, 不抛)。"""
    p = profile_path(data_dir, strategy_id)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("read strategy profile %s failed: %s", strategy_id, e)
        return None


def write_profile(data_dir: Path, profile: dict[str, Any]) -> None:
    """全量覆盖写 (调用前应先 validate_profile)。strategyId 必填。"""
    p = profile_path(data_dir, str(profile["strategyId"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_profile(data_dir: Path, strategy_id: str) -> bool:
    """删除声明; 返回是否确实删除了文件。"""
    p = profile_path(data_dir, strategy_id)
    if p.exists():
        p.unlink()
        return True
    return False


def validate_profile(profile: dict[str, Any]) -> list[str]:
    """返回问题清单 (空列表 = 通过)。纯结构校验, 不触达行情/台账。

    规则:
    - invalidation: 非空数组, 每项 name + observable + action 三要素齐全且非空。
    - risk.positionLimitPct / lossBudgetPct: 数值 ∈ (0, 100]。
    - risk.thesisHorizonMonths: 正整数 (bool 不算)。
    """
    problems: list[str] = []
    if not isinstance(profile, dict):
        return ["profile 必须是对象"]

    # ── invalidation 三要素 ──
    inv = profile.get("invalidation")
    if inv is None:
        problems.append("invalidation 缺失: 必须声明至少一个可观察的失效信号")
    elif not isinstance(inv, list) or not inv:
        problems.append("invalidation 必须是非空数组")
    else:
        for i, item in enumerate(inv):
            if not isinstance(item, dict):
                problems.append(f"invalidation[{i}] 必须是对象")
                continue
            for key in ("name", "observable", "action"):
                val = item.get(key)
                if not isinstance(val, str) or not val.strip():
                    problems.append(
                        f"invalidation[{i}].{key} 缺失或为空 (需 name+observable+action 三要素齐全)"
                    )

    # ── risk 数值边界 ──
    risk = profile.get("risk")
    if not isinstance(risk, dict):
        problems.append("risk 缺失: 必须声明 positionLimitPct / lossBudgetPct / thesisHorizonMonths")
    else:
        _check_pct(problems, risk, "positionLimitPct")
        _check_pct(problems, risk, "lossBudgetPct")
        horizon = risk.get("thesisHorizonMonths")
        # bool 是 int 的子类, 必须显式排除
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
            problems.append("risk.thesisHorizonMonths 必须是正整数 (月)")

    # ── 策略坐标卡 family (P6.3, 可选; 旧 profile 无此字段则跳过) ──
    family = profile.get("family")
    if family is not None:
        if not isinstance(family, str) or family not in FAMILY_VALUES:
            problems.append(
                f"family={family!r} 非法, 需为 {list(FAMILY_VALUES)} 之一"
            )
        elif family == "mixed":
            _check_family_mix(problems, profile)

    # ── playbook (P6.3, 可选; 出现则结构必须合法) ──
    playbook = profile.get("playbook")
    if playbook is not None:
        if not isinstance(playbook, dict):
            problems.append("playbook 必须是对象")
        else:
            for key in PLAYBOOK_KEYS:
                if key in playbook and not isinstance(playbook[key], str):
                    problems.append(f"playbook.{key} 若声明必须是字符串")

    return problems


def _check_pct(problems: list[str], risk: dict[str, Any], key: str) -> None:
    val = risk.get(key)
    # bool 是 int 的子类, 排除
    if isinstance(val, bool) or not isinstance(val, int | float):
        problems.append(f"risk.{key} 必须是数值")
        return
    if not (0 < val <= 100):
        problems.append(f"risk.{key}={val} 越界, 需 ∈ (0, 100]")


def _check_family_mix(problems: list[str], profile: dict[str, Any]) -> None:
    """family=mixed 时校验 familyMix 四要素均非空字符串。

    混合策略须显式裁决: 入场裁判 / 失效权 / 仓位期限 / 冲突裁决 (见 strategy_family_map.md)。
    """
    mix = profile.get("familyMix")
    if not isinstance(mix, dict):
        problems.append(
            "family=mixed 时 familyMix 必须是对象且含四要素: "
            + "/".join(FAMILY_MIX_KEYS)
        )
        return
    for key in FAMILY_MIX_KEYS:
        val = mix.get(key)
        if not isinstance(val, str) or not val.strip():
            problems.append(
                f"familyMix.{key} 缺失或为空 (mixed 须显式裁决入场裁判/失效权/仓位期限/冲突裁决)"
            )
