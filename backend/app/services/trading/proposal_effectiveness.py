"""提案生效验证 (移植自 YMOS 判断层变更提案 / 周期审计 / 裁判长规则)。

纯函数: 对单个策略变更提案做只读的「是否当前生效」判定。只验证,不批准、
不修改策略内核、不迁移提案状态、不落盘。

五条验证维度 (照搬 YMOS):
1. 人工批准 — 提案必须经 Human 批准 (status ≥ approved 且 human_approved)。
   Agent 不得自动批准 (内核审计 §三硬规矩; P12 裁判长 §结果边界)。
2. 版本/策略族匹配 — 提案所依据的 Strategy Profile (profileId/version/
   strategyFamilies) 必须与当前 Profile 一致; 不一致说明 Profile 已演进,
   提案的 before 快照失效 (周期审计 Step 7 反馈回路; 一切结论放回策略族看)。
3. 生效时间 — now 必须落在 effective_from..effective_until 窗口内。
4. 反证条件 — 提案必带 falsifier (无反证条件的提案不予受理); 若已观察到
   反证信号命中, 提案被证伪, 失效 (内核审计 §三硬规矩 第 2 条)。
5. 效果观察窗口 — 试运行 (trial) 须累积足够样本并渡过观察期; 观察达标且
   outcome_met=True → effective; outcome_met=False → not_effective;
   窗口未满或效果未评估 → pending_observation (周期审计 Step 8 到期复核)。

判定优先级 (先命中先返回, 全程 fail-closed):
  insufficient_data → not_approved → version_conflict → not_yet_effective
  → expired → counter_evidence_hit → pending_observation → not_effective → effective

安全不变量:
1. 永不自动批准 — verdict == effective 的必要条件是 human_approved=True。
2. 反证命中即失效 — 即使 status=verified, 命中 falsifier 也判 counter_evidence_hit。
3. 观察未满不宣称有效 — trial 窗口未满或 outcome 未评估 → pending, 不得判 effective。
4. now 可注入 — 相同输入 (含相同 now) 必定产出相同 verdict (确定性)。
5. 全部输出 JSON 可序列化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── verdict 常量 ────────────────────────────────────────────
VERDICT_EFFECTIVE = "effective"                    # 全部门禁通过, 提案当前生效
VERDICT_NOT_EFFECTIVE = "not_effective"            # 观察窗口已满但效果未达标, 应驳回/回滚
VERDICT_PENDING_OBSERVATION = "pending_observation"  # 试运行观察窗口未满或效果未评估
VERDICT_EXPIRED = "expired"                        # 超过 effective_until 硬截止
VERDICT_NOT_YET_EFFECTIVE = "not_yet_effective"    # 早于 effective_from, 计划生效但未到时
VERDICT_VERSION_CONFLICT = "version_conflict"      # 与当前 Profile 的版本/策略族不匹配
VERDICT_COUNTER_EVIDENCE_HIT = "counter_evidence_hit"  # 反证信号命中, 提案被证伪
VERDICT_NOT_APPROVED = "not_approved"              # 未经人工批准 / 已驳回
VERDICT_INSUFFICIENT_DATA = "insufficient_data"    # 缺 falsifier 等必填项, 无法判定

VERDICTS = (
    VERDICT_EFFECTIVE,
    VERDICT_NOT_EFFECTIVE,
    VERDICT_PENDING_OBSERVATION,
    VERDICT_EXPIRED,
    VERDICT_NOT_YET_EFFECTIVE,
    VERDICT_VERSION_CONFLICT,
    VERDICT_COUNTER_EVIDENCE_HIT,
    VERDICT_NOT_APPROVED,
    VERDICT_INSUFFICIENT_DATA,
)

# ── required_action 常量 ────────────────────────────────────
ACTION_NONE = "none"                            # effective: 无需动作
ACTION_AWAIT_HUMAN_APPROVAL = "await_human_approval"      # not_approved: 等待 Human 批准
ACTION_RESOLVE_VERSION = "resolve_version_conflict"       # version_conflict: 重新对齐当前 Profile
ACTION_SCHEDULE = "await_effective_time"                  # not_yet_effective: 等待生效时间
ACTION_REMOVE_EXPIRED = "remove_or_renew_expired"         # expired: 移除或续期
ACTION_ROLLBACK = "rollback"                              # counter_evidence_hit: 回滚到改前
ACTION_REVIEW = "review_outcome"                          # pending_observation: 复核效果
ACTION_REJECT = "reject_or_rollback"                      # not_effective: 驳回并回滚
ACTION_COLLECT_DATA = "collect_missing_data"              # insufficient_data: 补齐必填项

# ── check 状态 ──────────────────────────────────────────────
_CHECK_PASS = "pass"
_CHECK_FAIL = "fail"
_CHECK_SKIP = "skip"

# 默认最小观察样本 (周期审计: 建议至少 10 笔已归档平仓证据)
DEFAULT_MIN_OBSERVATION_SAMPLES = 10

MODULE_VERSION = "proposal-effectiveness-v1"


# ── 输入数据结构 ────────────────────────────────────────────
@dataclass(frozen=True)
class CurrentProfile:
    """当前生效的 Strategy Profile 快照 (用于版本/策略族匹配)。

    Attributes:
        profile_id: Profile 标识; 与提案 target_profile_id 比较。
        version: Profile 版本号; 与提案 target_profile_version 比较。
        strategy_families: 当前 Profile 声明的策略族列表; 与提案目标族求交集。
        status: Profile 状态 (draft/active/paused/retired); active 才是有效基准。
    """

    profile_id: str | None = None
    version: str | None = None
    strategy_families: list[str] = field(default_factory=list)
    status: str | None = None


@dataclass(frozen=True)
class ProposalContext:
    """待验证的提案上下文 (从 proposal dict + 观测数据归一化而来)。

    所有时间字段: ISO 字符串 / datetime / None (naive 视为 UTC)。
    """

    proposal_id: str
    status: str                                   # draft|approved|rejected|trial|verified
    falsifier: str | None = None                  # 反证条件描述 (必填非空)
    # 人工批准
    human_approved: bool = False
    approved_at: datetime | str | None = None
    # 版本/策略族目标
    target_profile_id: str | None = None
    target_profile_version: str | None = None
    target_strategy_families: list[str] = field(default_factory=list)
    # 生效时间窗口
    effective_from: datetime | str | None = None
    effective_until: datetime | str | None = None
    review_after: datetime | str | None = None    # 试运行复核截止 (观察期下限)
    # 观察窗口
    min_observation_samples: int = DEFAULT_MIN_OBSERVATION_SAMPLES
    observation_samples: int = 0
    outcome_met: bool | None = None               # 效果是否达标 (None=未评估)
    # 反证命中
    falsifier_hits: list[dict[str, Any]] = field(default_factory=list)


# ── 输出数据结构 ────────────────────────────────────────────
@dataclass(frozen=True)
class EffectivenessVerdict:
    """提案生效判定结果 (不可变, 可 JSON 序列化)。

    Attributes:
        proposal_id: 提案标识回显。
        verdict: 生效判定 (见 VERDICTS)。
        reason: 人可读的判定理由。
        evidence: 各门禁检查的逐条证据 [{check, passed, detail}]。
        required_action: Human 下一步动作 (见 ACTION_* 常量)。
        pending_conditions: 判定生效前仍需满足的条件列表 (effective 时为空)。
    """

    proposal_id: str
    verdict: str
    reason: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    required_action: str = ACTION_NONE
    pending_conditions: list[str] = field(default_factory=list)

    @property
    def is_effective(self) -> bool:
        """提案是否当前生效 — 仅 effective 为 True。"""
        return self.verdict == VERDICT_EFFECTIVE

    def to_dict(self) -> dict[str, Any]:
        """序列化为 plain dict (JSON 可序列化)。"""
        return asdict(self)


# ── 工具函数 ────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clean_str(v: Any) -> str | None:
    """非空字符串 → strip 后返回; 否则 None。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_dt(raw: Any) -> datetime | None:
    """解析时间字段为 timezone-aware datetime。

    - datetime 对象: naive → 视为 UTC; aware → 原样。
    - ISO 字符串: fromisoformat (Python 3.11+ 支持 'Z')。
    - 其他 / 解析失败 → None。
    """
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _check(check_id: str, status: str, detail: str) -> dict[str, Any]:
    """构造一条门禁检查证据。"""
    return {"check": check_id, "passed": status == _CHECK_PASS, "detail": detail}


def _coerce_profile(profile: CurrentProfile | dict[str, Any] | None) -> CurrentProfile | None:
    """dict → CurrentProfile; None → None。"""
    if profile is None:
        return None
    if isinstance(profile, CurrentProfile):
        return profile
    if isinstance(profile, dict):
        return CurrentProfile(
            profile_id=_clean_str(profile.get("profile_id") or profile.get("profileId")),
            version=_clean_str(profile.get("version")),
            strategy_families=list(profile.get("strategy_families")
                                   or profile.get("strategyFamilies")
                                   or []),
            status=_clean_str(profile.get("status")),
        )
    raise TypeError(f"CurrentProfile | dict | None expected, got {type(profile).__name__}")


def _coerce_proposal(proposal: ProposalContext | dict[str, Any]) -> ProposalContext:
    """dict → ProposalContext; 已是 dataclass 则原样返回。

    接受 snake_case 与 camelCase 双写法, 兼容 YMOS JSON 契约。
    """
    if isinstance(proposal, ProposalContext):
        return proposal
    if not isinstance(proposal, dict):
        raise TypeError(f"ProposalContext | dict expected, got {type(proposal).__name__}")

    def _get(*keys: str) -> Any:
        for k in keys:
            if k in proposal and proposal[k] is not None:
                return proposal[k]
        return None

    raw_hits = _get("falsifier_hits", "falsifierHits") or []
    if not isinstance(raw_hits, list):
        raw_hits = [raw_hits] if raw_hits else []

    return ProposalContext(
        proposal_id=str(_get("proposal_id", "id") or ""),
        status=str(_get("status") or "").strip(),
        falsifier=_clean_str(_get("falsifier")),
        human_approved=bool(_get("human_approved", "humanApproval", "humanApproved")),
        approved_at=_get("approved_at", "approvedAt"),
        target_profile_id=_clean_str(_get("target_profile_id", "targetProfileId")),
        target_profile_version=_clean_str(_get("target_profile_version", "targetProfileVersion")),
        target_strategy_families=list(_get("target_strategy_families",
                                            "targetStrategyFamilies") or []),
        effective_from=_get("effective_from", "effectiveFrom"),
        effective_until=_get("effective_until", "effectiveUntil"),
        review_after=_get("review_after", "reviewAfter"),
        min_observation_samples=int(_get("min_observation_samples",
                                          "minObservationSamples")
                                    or DEFAULT_MIN_OBSERVATION_SAMPLES),
        observation_samples=int(_get("observation_samples", "observationSamples") or 0),
        outcome_met=_get("outcome_met", "outcomeMet"),
        falsifier_hits=[h for h in raw_hits if h],
    )


# ── 各门禁检查 (纯函数, 返回 (status, detail)) ──────────────
def _check(check_id: str, status: str, detail: str) -> dict[str, Any]:
    """构造一条门禁检查证据。

    passed=True 表示该门禁未阻断生效 (pass 或 skip); 仅 fail 为 False。
    """
    return {"check": check_id, "passed": status != _CHECK_FAIL, "detail": detail}

def _check_falsifier_defined(ctx: ProposalContext) -> tuple[str, str]:
    """反证条件是否定义且非空 (内核审计 §三硬规矩 第 2 条)。"""
    if _clean_str(ctx.falsifier):
        return _CHECK_PASS, f"反证条件已定义: {ctx.falsifier!r}"
    return _CHECK_FAIL, "提案缺少反证条件 (falsifier), 无法判定是否被证伪"


def _check_human_approval(ctx: ProposalContext) -> tuple[str, str]:
    """是否经人工批准 (status ≥ approved 且 human_approved)。

    Agent 不得自动批准 — human_approved 必须为 True。
    """
    approved_statuses = {"approved", "trial", "verified"}
    if ctx.status not in approved_statuses:
        return _CHECK_FAIL, (
            f"status={ctx.status!r} 未达批准态 (draft/rejected), "
            f"需 Human 批准后方可生效"
        )
    if not ctx.human_approved:
        return _CHECK_FAIL, (
            f"status={ctx.status!r} 但 human_approved=False, "
            f"缺少 Human 批准记录 — Agent 不得自动批准"
        )
    extra = f", 批准时间 {ctx.approved_at}" if ctx.approved_at else ""
    return _CHECK_PASS, f"已获 Human 批准 (status={ctx.status}{extra})"


def _check_version_match(
    ctx: ProposalContext, profile: CurrentProfile | None
) -> tuple[str, str]:
    """版本/策略族是否与当前 Profile 匹配。

    - profile 为 None → 跳过 (无基准可比)。
    - profileId 不一致 → fail (提案针对的是别的 Profile)。
    - version 不一致 → fail (Profile 已演进, before 快照失效)。
    - 策略族无交集 → fail (提案目标族不在当前 Profile 范围内)。
    """
    if profile is None:
        return _CHECK_SKIP, "未提供当前 Profile, 跳过版本/策略族匹配"

    # profileId 比较
    if (ctx.target_profile_id and profile.profile_id
            and ctx.target_profile_id != profile.profile_id):
        return _CHECK_FAIL, (
            f"profileId 不匹配: 提案 {ctx.target_profile_id!r} "
            f"vs 当前 {profile.profile_id!r}"
        )

    # version 比较
    if (ctx.target_profile_version and profile.version
            and ctx.target_profile_version != profile.version):
        return _CHECK_FAIL, (
            f"profileVersion 不匹配: 提案 {ctx.target_profile_version!r} "
            f"vs 当前 {profile.version!r} — Profile 已演进, before 快照失效"
        )

    # 策略族交集
    if ctx.target_strategy_families and profile.strategy_families:
        proposal_set = {f.strip() for f in ctx.target_strategy_families if f and f.strip()}
        current_set = {f.strip() for f in profile.strategy_families if f and f.strip()}
        if proposal_set and current_set and not (proposal_set & current_set):
            return _CHECK_FAIL, (
                f"策略族无交集: 提案 {sorted(proposal_set)} "
                f"vs 当前 {sorted(current_set)}"
            )

    matched = []
    if ctx.target_profile_id:
        matched.append(f"profileId={ctx.target_profile_id!r}")
    if ctx.target_profile_version:
        matched.append(f"version={ctx.target_profile_version!r}")
    if ctx.target_strategy_families:
        matched.append(f"families={ctx.target_strategy_families}")
    detail = "版本/策略族匹配通过"
    if matched:
        detail += f" ({', '.join(matched)})"
    return _CHECK_PASS, detail


def _check_effective_time(
    ctx: ProposalContext, now: datetime
) -> tuple[str, str, str | None]:
    """生效时间窗口检查。

    Returns:
        (status, detail, early_verdict) — early_verdict 为 not_yet_effective
        时调用方应据此短路; 否则 None。
    """
    effective_from = _parse_dt(ctx.effective_from)
    effective_until = _parse_dt(ctx.effective_until)

    if effective_from and now < effective_from:
        return (
            _CHECK_FAIL,
            f"当前 {now.isoformat()} 早于生效时间 {effective_from.isoformat()}",
            VERDICT_NOT_YET_EFFECTIVE,
        )
    if effective_until and now > effective_until:
        return (
            _CHECK_FAIL,
            f"当前 {now.isoformat()} 晚于截止时间 {effective_until.isoformat()}",
            VERDICT_EXPIRED,
        )
    parts = []
    if effective_from:
        parts.append(f"from {effective_from.isoformat()}")
    if effective_until:
        parts.append(f"until {effective_until.isoformat()}")
    detail = "生效时间窗口内" + (f" ({', '.join(parts)})" if parts else "")
    return _CHECK_PASS, detail, None


def _check_counter_evidence(ctx: ProposalContext) -> tuple[str, str, str | None]:
    """反证信号是否命中 (内核审计 §三硬规矩 第 2 条; 周期审计 Step 8)。

    Returns:
        (status, detail, short_verdict) — 命中时 short_verdict=counter_evidence_hit。
    """
    hits = ctx.falsifier_hits
    if hits:
        names = []
        for h in hits:
            if isinstance(h, dict):
                names.append(str(h.get("name") or h.get("signal") or h))
            else:
                names.append(str(h))
        return (
            _CHECK_FAIL,
            f"反证条件已命中 {len(hits)} 条: {names}",
            VERDICT_COUNTER_EVIDENCE_HIT,
        )
    return _CHECK_PASS, "未观察到反证信号命中", None


def _check_observation_window(
    ctx: ProposalContext, now: datetime
) -> tuple[str, str, str | None, list[str]]:
    """效果观察窗口检查 (周期审计 Step 8 到期复核)。

    Returns:
        (status, detail, short_verdict, pending) — short_verdict 非 None 时短路;
        pending 为仍未满足的条件列表。
    """
    pending: list[str] = []

    # 已 verified 的提案观察已通过, 不重复要求 (除非反证命中, 已由前序门禁拦截)
    if ctx.status == "verified":
        return _CHECK_PASS, "已通过观察复核 (verified), 无需重复观察", None, []

    # approved 但未进入 trial → 等待激活试运行
    if ctx.status == "approved":
        pending.append("进入试运行 (status: approved → trial)")
        return (
            _CHECK_FAIL,
            "已批准但尚未进入试运行 (trial), 修改尚未生效观察",
            VERDICT_PENDING_OBSERVATION,
            pending,
        )

    # trial: 检查样本数 + 观察期 + 效果
    samples_met = ctx.observation_samples >= ctx.min_observation_samples
    review_after = _parse_dt(ctx.review_after)
    period_elapsed = review_after is None or now >= review_after

    if not samples_met:
        pending.append(
            f"积累观察样本: 当前 {ctx.observation_samples} "
            f"< 最低 {ctx.min_observation_samples}"
        )
    if review_after is not None and not period_elapsed:
        pending.append(
            f"观察期未满: 当前 {now.isoformat()} 早于复核截止 "
            f"{review_after.isoformat()}"
        )

    if pending:
        return (
            _CHECK_FAIL,
            "试运行观察窗口未满: " + "; ".join(pending),
            VERDICT_PENDING_OBSERVATION,
            pending,
        )

    # 窗口已满, 评估效果
    if ctx.outcome_met is None:
        pending.append("评估效果是否达标 (outcome_met 未知)")
        return (
            _CHECK_FAIL,
            "观察窗口已满但效果未评估 (outcome_met=None), 不得宣称有效",
            VERDICT_PENDING_OBSERVATION,
            pending,
        )
    if ctx.outcome_met is True:
        return (
            _CHECK_PASS,
            f"观察达标: {ctx.observation_samples} 样本 ≥ "
            f"{ctx.min_observation_samples}, 效果满足预期",
            None,
            [],
        )
    # outcome_met is False
    return (
        _CHECK_FAIL,
        f"观察达标但效果未满足预期 (outcome_met=False)",
        VERDICT_NOT_EFFECTIVE,
        [],
    )


# ── 核心判定 ────────────────────────────────────────────────
def evaluate_proposal_effectiveness(
    proposal: ProposalContext | dict[str, Any],
    *,
    current_profile: CurrentProfile | dict[str, Any] | None = None,
    now: datetime | None = None,
) -> EffectivenessVerdict:
    """判定单个提案是否当前生效。

    纯函数, 只读: 不批准提案、不修改策略、不迁移状态、不落盘。
    相同输入 (含相同 ``now``) 必定产出相同 verdict (确定性)。

    判定优先级 (先命中先返回, fail-closed):
      1. insufficient_data  — 缺 falsifier 等必填项
      2. not_approved       — 未经人工批准 / 已驳回
      3. version_conflict   — 与当前 Profile 版本/策略族不匹配
      4. not_yet_effective  — 早于 effective_from
      5. expired            — 晚于 effective_until
      6. counter_evidence_hit — 反证信号命中
      7. pending_observation — 观察窗口未满 / 效果未评估
      8. not_effective      — 窗口已满但效果未达标
      9. effective          — 全部门禁通过

    Args:
        proposal: 提案上下文 (ProposalContext 或等价 dict)。
        current_profile: 当前 Strategy Profile (用于版本/策略族匹配); None 则跳过该门禁。
        now: 当前时间 (默认 UTC now; 测试注入以保证确定性)。

    Returns:
        EffectivenessVerdict — verdict / reason / evidence /
        required_action / pending_conditions。
    """
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    ctx = _coerce_proposal(proposal)
    profile = _coerce_profile(current_profile)
    evidence: list[dict[str, Any]] = []
    pid = ctx.proposal_id or "(unknown)"

    def _emit(check_id: str, status: str, detail: str) -> None:
        evidence.append(_check(check_id, status, detail))

    # 1. falsifier 必填 — insufficient_data
    st, detail = _check_falsifier_defined(ctx)
    _emit("falsifier_defined", st, detail)
    if st == _CHECK_FAIL:
        return _build(
            pid, VERDICT_INSUFFICIENT_DATA, detail, evidence,
            ACTION_COLLECT_DATA, ["补齐反证条件 (falsifier)"],
        )

    # 2. 人工批准 — not_approved
    st, detail = _check_human_approval(ctx)
    _emit("human_approval", st, detail)
    if st == _CHECK_FAIL:
        pending = ["获得 Human 批准"] if ctx.status in {"draft"} else []
        return _build(
            pid, VERDICT_NOT_APPROVED, detail, evidence,
            ACTION_AWAIT_HUMAN_APPROVAL, pending,
        )

    # 3. 版本/策略族匹配 — version_conflict
    st, detail = _check_version_match(ctx, profile)
    _emit("version_match", st, detail)
    if st == _CHECK_FAIL:
        return _build(
            pid, VERDICT_VERSION_CONFLICT, detail, evidence,
            ACTION_RESOLVE_VERSION,
            ["重新对齐当前 Profile 的版本/策略族, 或基于当前 Profile 重写提案"],
        )

    # 4-5. 生效时间窗口 — not_yet_effective / expired
    st, detail, early = _check_effective_time(ctx, now)
    _emit("effective_time", st, detail)
    if early == VERDICT_NOT_YET_EFFECTIVE:
        return _build(
            pid, VERDICT_NOT_YET_EFFECTIVE, detail, evidence,
            ACTION_SCHEDULE, [f"等待生效时间 {_parse_dt(ctx.effective_from)}"],
        )
    if early == VERDICT_EXPIRED:
        return _build(
            pid, VERDICT_EXPIRED, detail, evidence,
            ACTION_REMOVE_EXPIRED, ["移除过期提案或基于当前状态续期"],
        )

    # 6. 反证命中 — counter_evidence_hit
    st, detail, short = _check_counter_evidence(ctx)
    _emit("counter_evidence", st, detail)
    if short == VERDICT_COUNTER_EVIDENCE_HIT:
        return _build(
            pid, VERDICT_COUNTER_EVIDENCE_HIT, detail, evidence,
            ACTION_ROLLBACK, ["按反证条件回滚到改前状态"],
        )

    # 7-8. 观察窗口 + 效果
    st, detail, short, pending = _check_observation_window(ctx, now)
    _emit("observation_window", st, detail)
    if short == VERDICT_PENDING_OBSERVATION:
        return _build(
            pid, VERDICT_PENDING_OBSERVATION, detail, evidence,
            ACTION_REVIEW, pending,
        )
    if short == VERDICT_NOT_EFFECTIVE:
        return _build(
            pid, VERDICT_NOT_EFFECTIVE, detail, evidence,
            ACTION_REJECT, ["驳回提案并回滚到改前状态"],
        )

    # 9. 全部通过
    return _build(
        pid, VERDICT_EFFECTIVE,
        f"提案 {pid!r} 全部门禁通过, 当前生效",
        evidence, ACTION_NONE, [],
    )


def _build(
    proposal_id: str,
    verdict: str,
    reason: str,
    evidence: list[dict[str, Any]],
    action: str,
    pending: list[str],
) -> EffectivenessVerdict:
    return EffectivenessVerdict(
        proposal_id=proposal_id,
        verdict=verdict,
        reason=reason,
        evidence=evidence,
        required_action=action,
        pending_conditions=list(pending),
    )
