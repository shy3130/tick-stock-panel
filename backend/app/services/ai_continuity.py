"""M25: 显式 opt-in 的跨日 AI 分析连续性。

只分析"论点连续性"——判断当前分析帧与上一次成功 artifact 的数据截止/K 线锚点关系，
输出 incremental / full_reanalysis / fresh 判定与失效原因。

**不**包含任何交易执行语义 (order / side / price / direction / entry / stop)。
只从 PA_Agent decision_continuity 借鉴"数据锚点比较 + 跨度断裂检测"机制,
剥离所有交易关系分类 (REL_FLIP / 同向 / cooldown / limit_triggered)。

红线:
    - 只做连续性判定与元数据产出, 不调用 AI provider。
    - 连续性上下文只含"上一轮诊断数据锚点变化", 不含交易行动字段。
    - parent 只读引用旧 artifact, 绝不覆盖 (append-only 由 analysis_artifacts 保证)。
    - 默认关闭, 需显式 opt-in (由 plan_check / API 层门控)。
    - 不 import 交易写入口、不 import provider。
    - 前结论只作"历史模型判断" (model_assessment), 不得升级为程序事实。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.services import analysis_artifacts as artifacts_store
from app.services.ai_structured import AnalysisArtifact

logger = logging.getLogger(__name__)

__all__ = [
    "ContinuityMode",
    "ContinuityVerdict",
    "select_parent",
    "assess_continuity",
    "build_continuity_meta",
    "build_parent_chain",
    "assert_no_forbidden_keys",
    "DEFAULT_MAX_GAP_BARS",
    "FORBIDDEN_KEYS",
]

# ── 配置 ──────────────────────────────────────────────────
# 超过此新增 bar 数 → full_reanalysis (数据跨度过大, 增量失去意义)。
DEFAULT_MAX_GAP_BARS = 60

# 连续性元数据禁止包含的交易行动字段 (与 plan_check._STAGE2_FORBIDDEN_KEYS 同源)。
# 全小写匹配, 确保 case-insensitive 安全。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "order",
        "orderid",
        "side",
        "action",
        "buy",
        "sell",
        "quantity",
        "qty",
        "price",
        "recommendedprice",
        "targetprice",
        "entryprice",
        "limitprice",
        "stopprice",
        "stoploss",
        "amount",
        "position",
        "holdings",
        "signal",
        "direction",
    }
)


# ── 判定模型 ──────────────────────────────────────────────
class ContinuityMode(str, Enum):
    """连续性判定三态。"""

    FRESH = "fresh"
    """无兼容 parent (首次分析或版本/schema 不匹配)。"""

    INCREMENTAL = "incremental"
    """parent 兼容且数据锚点连续 → 增量分析。"""

    FULL_REANALYSIS = "full_reanalysis"
    """parent 存在但失效 (profile 变化/跨度断裂/锚点丢失等) → 全量重分析。"""


class ContinuityVerdict(BaseModel):
    """连续性判定结果 (纯数据, 不含交易行动字段)。"""

    model_config = {"extra": "forbid"}

    mode: ContinuityMode
    parent_attempt_id: str | None = None
    parent_artifact_id: str | None = None
    reason: str = ""
    bars_delta: int = 0
    new_bar_dates: list[str] = Field(default_factory=list)
    parent_data_as_of: str | None = None
    self_data_as_of: str | None = None
    compatibility: dict[str, bool] = Field(default_factory=dict)


# ── parent 选择 ───────────────────────────────────────────
def select_parent(
    data_dir: Path,
    *,
    symbol: str,
    purpose: str,
    schema_version: str,
    program_rules_version: str | None,
) -> AnalysisArtifact | None:
    """选择严格兼容的 parent artifact。

    候选条件 (全部满足):
        - status == "ok" (失败/取消的不做 parent)
        - purpose 匹配
        - symbol 匹配
        - schema_version 匹配
        - program_rules_version 匹配

    profile_id / prompt_version / market / adjustment 不是选择条件,
    而是在 assess_continuity 中判定是否强制 full_reanalysis 的依据。

    返回最新 (created_at 最大) 的匹配 artifact, 或 None。
    """
    return artifacts_store.find_latest_artifact(
        data_dir,
        purpose=purpose,
        status="ok",
        symbol=symbol,
        schema_version=schema_version,
        program_rules_version=program_rules_version,
    )


# ── 连续性评估 ────────────────────────────────────────────
def _to_date(d: date | datetime) -> date:
    """把 date | datetime 归一为 date。"""
    if isinstance(d, datetime):
        return d.date()
    return d


def _full_reanalysis(
    parent: AnalysisArtifact,
    reason: str,
    *,
    self_data_as_of: datetime | None,
    bars_delta: int = 0,
    new_bar_dates: list[str] | None = None,
    compatibility: dict[str, bool] | None = None,
) -> ContinuityVerdict:
    return ContinuityVerdict(
        mode=ContinuityMode.FULL_REANALYSIS,
        parent_attempt_id=parent.attempt_id,
        parent_artifact_id=parent.id,
        reason=reason,
        bars_delta=bars_delta,
        new_bar_dates=list(new_bar_dates or []),
        parent_data_as_of=parent.data_as_of.isoformat() if parent.data_as_of else None,
        self_data_as_of=self_data_as_of.isoformat() if self_data_as_of else None,
        compatibility=dict(compatibility or {}),
    )


def assess_continuity(
    parent: AnalysisArtifact | None,
    frame: Any,
    *,
    profile_id: str | None = None,
    prompt_version: str | None = None,
    max_gap_bars: int = DEFAULT_MAX_GAP_BARS,
) -> ContinuityVerdict:
    """评估当前帧与 parent 的连续性。

    Args:
        parent: select_parent 返回的 parent artifact (或 None)。
        frame: KlineAnalysisFrame (duck-typed: 需 data_as_of, bars, market, adjustment)。
        profile_id: 当前使用的 AI profile id。
        prompt_version: 当前 prompt 版本字符串。
        max_gap_bars: 新增 bar 数超过此值 → full_reanalysis。

    Returns:
        ContinuityVerdict — fresh / incremental / full_reanalysis + reason。
    """
    self_data_as_of: datetime | None = getattr(frame, "data_as_of", None)

    # ── 无 parent → fresh ──
    if parent is None:
        return ContinuityVerdict(
            mode=ContinuityMode.FRESH,
            reason="无兼容 parent artifact (首次分析或版本不匹配)",
            self_data_as_of=self_data_as_of.isoformat() if self_data_as_of else None,
        )

    compatibility: dict[str, bool] = {}

    # ── profile_id 变化 → full_reanalysis ──
    profile_match = (parent.profile_id or None) == (profile_id or None)
    compatibility["profile_match"] = profile_match
    if not profile_match:
        return _full_reanalysis(
            parent,
            f"profile_id 变化 ({parent.profile_id or 'None'} → {profile_id or 'None'})",
            self_data_as_of=self_data_as_of,
            compatibility=compatibility,
        )

    # None 表示调用方未声明 prompt 版本, 不额外制造不兼容;
    # 产品入口会显式传入版本, 因此实际跨轮检查仍 fail-closed。
    prompt_match = (
        True
        if prompt_version is None
        else (parent.prompt_version or "") == prompt_version
    )
    compatibility["prompt_match"] = prompt_match
    if not prompt_match:
        return _full_reanalysis(
            parent,
            f"prompt_version 变化 ({parent.prompt_version or ''} → {prompt_version or ''})",
            self_data_as_of=self_data_as_of,
            compatibility=compatibility,
        )

    # ── market 变化 → full_reanalysis ──
    frame_market = getattr(frame, "market", None)
    market_match = (parent.market or None) == (frame_market or None)
    compatibility["market_match"] = market_match
    if not market_match:
        return _full_reanalysis(
            parent,
            f"market 变化 ({parent.market or 'None'} → {frame_market or 'None'})",
            self_data_as_of=self_data_as_of,
            compatibility=compatibility,
        )

    # ── adjustment 变化 → full_reanalysis ──
    frame_adj = getattr(frame, "adjustment", None)
    adj_match = (parent.adjustment or None) == (frame_adj or None)
    compatibility["adjustment_match"] = adj_match
    if not adj_match:
        return _full_reanalysis(
            parent,
            f"adjustment 变化 ({parent.adjustment or 'None'} → {frame_adj or 'None'})",
            self_data_as_of=self_data_as_of,
            compatibility=compatibility,
        )

    # ── parent 无 data_as_of → full_reanalysis ──
    if parent.data_as_of is None:
        compatibility["has_parent_data_as_of"] = False
        return _full_reanalysis(
            parent,
            "parent 无 data_as_of (无法定位数据锚点)",
            self_data_as_of=self_data_as_of,
            compatibility=compatibility,
        )
    compatibility["has_parent_data_as_of"] = True

    # ── K 线锚点检查 ──
    # parent 的最新数据日期必须出现在当前窗口, 否则跨度断裂。
    parent_anchor_date = _to_date(parent.data_as_of)
    frame_bars = getattr(frame, "bars", [])
    frame_bar_dates = {_to_date(b.date) for b in frame_bars}
    anchor_seen = parent_anchor_date in frame_bar_dates
    compatibility["anchor_seen"] = anchor_seen

    if not anchor_seen:
        # 锚点不在窗口 → 可能在更早的 bar (窗口滚动过去了), 也可能数据源不同。
        oldest = min(frame_bar_dates) if frame_bar_dates else None
        if oldest is not None and parent_anchor_date < oldest:
            reason = "数据锚点已滚出当前窗口 (跨度断裂)"
        else:
            reason = f"数据锚点 {parent_anchor_date} 不在当前窗口 (数据源差异或非交易日)"
        return _full_reanalysis(
            parent,
            reason,
            self_data_as_of=self_data_as_of,
            compatibility=compatibility,
        )

    # ── 计算新增 bar ──
    new_bars = [b for b in frame_bars if _to_date(b.date) > parent_anchor_date]
    bars_delta = len(new_bars)
    new_bar_dates = [str(_to_date(b.date)) for b in new_bars]

    # ── 新增 bar 超阈值 → full_reanalysis ──
    within_gap = bars_delta <= max_gap_bars
    compatibility["within_gap"] = within_gap
    if not within_gap:
        return _full_reanalysis(
            parent,
            f"新增 bar 数 ({bars_delta}) 超过阈值 ({max_gap_bars})",
            self_data_as_of=self_data_as_of,
            bars_delta=bars_delta,
            new_bar_dates=new_bar_dates,
            compatibility=compatibility,
        )

    # ── 全部通过 → incremental ──
    return ContinuityVerdict(
        mode=ContinuityMode.INCREMENTAL,
        parent_attempt_id=parent.attempt_id,
        parent_artifact_id=parent.id,
        reason=f"增量分析: 新增 {bars_delta} 根 bar",
        bars_delta=bars_delta,
        new_bar_dates=new_bar_dates,
        parent_data_as_of=parent.data_as_of.isoformat(),
        self_data_as_of=self_data_as_of.isoformat() if self_data_as_of else None,
        compatibility=compatibility,
    )


# ── 安全元数据产出 ────────────────────────────────────────
def assert_no_forbidden_keys(data: Any, *, _path: str = "") -> None:
    """递归校验 dict 不含禁止的交易行动字段。

    Raises:
        ValueError: 发现禁止键时。
    """
    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = str(key).lower()
            if key_lower in FORBIDDEN_KEYS:
                raise ValueError(f"continuity meta contains forbidden key: {_path}{key}")
            if isinstance(value, (dict, list)):
                assert_no_forbidden_keys(value, _path=f"{_path}{key}.")
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, (dict, list)):
                assert_no_forbidden_keys(item, _path=f"{_path}[{i}].")


def build_continuity_meta(verdict: ContinuityVerdict) -> dict[str, Any]:
    """构建安全的连续性元数据 (嵌入 artifact result)。

    只含连续性判定字段, 递归校验不含禁止键。
    """
    meta: dict[str, Any] = {
        "mode": verdict.mode.value,
        "parent_attempt_id": verdict.parent_attempt_id,
        "parent_artifact_id": verdict.parent_artifact_id,
        "reason": verdict.reason,
        "bars_delta": verdict.bars_delta,
        "new_bar_dates": verdict.new_bar_dates,
        "parent_data_as_of": verdict.parent_data_as_of,
        "self_data_as_of": verdict.self_data_as_of,
        "compatibility": dict(verdict.compatibility),
    }
    assert_no_forbidden_keys(meta)
    return meta


# ── parent 链查询 ─────────────────────────────────────────
def build_parent_chain(
    data_dir: Path,
    attempt_id: str,
    *,
    max_depth: int = 50,
) -> list[dict[str, Any]]:
    """从某 artifact 向上遍历 parent_attempt_id 链。

    返回列表: [self, parent, grandparent, ...] (从当前 artifact 到最早祖先)。
    遇到环 (数据异常) 或超过 max_depth 时安全截断。
    缺失 parent (artifact 已删除) → 截断于最后一个可用节点。
    """
    chain: list[dict[str, Any]] = []
    current_id: str | None = attempt_id
    seen: set[str] = set()

    while current_id and current_id not in seen and len(chain) < max_depth:
        seen.add(current_id)
        artifact = artifacts_store.read(data_dir, current_id)
        if artifact is None:
            break

        result = artifact.result or {}
        continuity = result.get("continuity") if isinstance(result, dict) else None
        if not isinstance(continuity, dict):
            continuity = {}

        chain.append(
            {
                "attempt_id": artifact.attempt_id,
                "artifact_id": artifact.id,
                "status": artifact.status,
                "symbol": artifact.symbol,
                "data_as_of": artifact.data_as_of.isoformat()
                if artifact.data_as_of
                else None,
                "created_at": artifact.created_at.isoformat()
                if artifact.created_at
                else None,
                "parent_attempt_id": artifact.parent_attempt_id,
                "continuity_mode": continuity.get("mode", "unknown"),
                "continuity_reason": continuity.get("reason", ""),
                "bars_delta": continuity.get("bars_delta", 0),
                "usage": {
                    "prompt_tokens": artifact.usage.prompt_tokens,
                    "cached_prompt_tokens": artifact.usage.cached_prompt_tokens,
                    "completion_tokens": artifact.usage.completion_tokens,
                    "total_tokens": artifact.usage.total_tokens,
                },
            }
        )
        current_id = artifact.parent_attempt_id

    return chain
