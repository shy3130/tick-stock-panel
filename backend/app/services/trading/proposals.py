"""策略变更提案 — 一提案一文件,带反证条件与状态迁移门禁。

防线(照搬 YMOS):单笔结果不改内核;提案必带反证条件;放宽类修改需额外举证。

Schema (data/user_data/trading/proposals/{id}.json):
{
  "id": "prop_{yyyymmdd}_{seq}",
  "title": "...",
  "target": "strategy 配置或 gate_rules",
  "evidence": [...],
  "before": {}, "after": {},
  "falsifier": "如果改错了,我会在什么情况下看到",  // 必填非空
  "sampleSize": 12,                                   // <10 不可批准
  "status": "draft|approved|rejected|trial|verified",
  "createdAt": "...", "updatedAt": "...",
  "history": [{"ts": "...", "from": "...", "to": "...", "note": "..."}],
  "relaxationAfterLoss": false   // P6.1:属放宽 && 近30天有亏损平仓 → true,审批警示
}
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.trading import store
from app.services.trading.lifecycle import now_str
from app.services.trading.models import STATUS_CLOSED

PROPOSAL_STATUSES = ("draft", "approved", "rejected", "trial", "verified")

# 合法状态迁移(draft→approved|rejected, approved→trial, trial→verified|rejected)
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"approved", "rejected"},
    "approved": {"trial"},
    "trial": {"verified", "rejected"},
    "rejected": set(),  # 终态
    "verified": set(),  # 终态
}

_MIN_SAMPLE_SIZE_FOR_APPROVAL = 10


class ProposalError(ValueError):
    """提案校验 / 状态迁移违规。"""


# ── 路径 ─────────────────────────────────────────────────
def _proposals_dir(data_dir: Path) -> Path:
    d = store.trading_dir(data_dir) / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(proposal_id: str) -> str:
    return proposal_id.replace("/", "_").replace("\\", "_").replace("..", "_")


def _proposal_path(data_dir: Path, proposal_id: str) -> Path:
    return _proposals_dir(data_dir) / f"{_safe_id(proposal_id)}.json"


def _next_seq(data_dir: Path, day: str) -> int:
    prefix = f"prop_{day}_"
    n = 0
    for p in _proposals_dir(data_dir).glob(f"{prefix}*.json"):
        suffix = p.stem[len(prefix):]
        if suffix.isdigit():
            n = max(n, int(suffix))
    return n + 1


def _write(data_dir: Path, proposal: dict[str, Any]) -> None:
    p = _proposal_path(data_dir, proposal["id"])
    p.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 校验 ─────────────────────────────────────────────────
def _require_falsifier(payload: dict[str, Any]) -> None:
    if not str(payload.get("falsifier") or "").strip():
        raise ProposalError("falsifier 必填且非空(无反证条件的提案不予受理)")


def _validate_transition(current: str, target: str, sample_size: int) -> None:
    allowed = _VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ProposalError(f"非法状态迁移: {current} → {target}")
    if target == "approved" and sample_size < _MIN_SAMPLE_SIZE_FOR_APPROVAL:
        raise ProposalError(
            f"sampleSize={sample_size} < {_MIN_SAMPLE_SIZE_FOR_APPROVAL},"
            f"证据不足不予批准(只登记)"
        )



# ── 放宽判定(P6.1:亏损后放松规则标记,纯函数便于单测) ────
_RECENT_LOSS_WINDOW_DAYS = 30


def is_relaxation(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    """判定提案是否属于「放宽」类修改。

    - 数值上调:after 侧 limit/budget/max/loss/stop 等数值大于 before 侧(更宽松)。
    - 数值下调(下限放宽):after 侧 min/lower/阈值类数值小于 before 侧。
    - invalidation 失效信号条目减少:after 条目数 < before 条目数。
    - before/after 均非 dict 或为空 → 判 False(非放宽)。
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not before and not after:
        return False

    # invalidation 失效信号数组条目数减少 = 放宽(失效条件被删减)
    if _list_shorter(after, before, ("invalidation",)):
        return True

    # 数值上调类 key: limit/budget/max/upper/ratio/loss/stop 相关
    # (stopLossPct 上调 = 止损更宽松; lossBudgetPct 上调 = 风险预算放宽)
    if _num_increase(after, before, ("limit", "budget", "max", "upper", "ratio", "loss", "stop")):
        return True

    # 数值下调类 key: min/lower/floor 相关(下限放宽)
    if _num_decrease(after, before, ("min", "lower", "floor")):
        return True

    return False


def _num_increase(after: dict[str, Any], before: dict[str, Any], suffixes: tuple[str, ...]) -> bool:
    """after 中任一命中 suffix 的数值 > before 对应数值 → True。"""
    for key, av in after.items():
        if not _match_suffix(key, suffixes):
            continue
        bv = before.get(key)
        af, bf = _to_num(av), _to_num(bv)
        if af is not None and bf is not None and af > bf:
            return True
    return False


def _num_decrease(after: dict[str, Any], before: dict[str, Any], suffixes: tuple[str, ...]) -> bool:
    """after 中任一命中 suffix 的数值 < before 对应数值 → True(下限放宽)。"""
    for key, av in after.items():
        if not _match_suffix(key, suffixes):
            continue
        bv = before.get(key)
        af, bf = _to_num(av), _to_num(bv)
        if af is not None and bf is not None and af < bf:
            return True
    return False


def _list_shorter(after: dict[str, Any], before: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """after 中任一 key 的列表条目数少于 before 对应 → True。"""
    for key in keys:
        bv = before.get(key)
        av = after.get(key)
        if isinstance(bv, list) and isinstance(av, list) and len(av) < len(bv):
            return True
    return False


def _match_suffix(key: str, suffixes: tuple[str, ...]) -> bool:
    """key 中是否包含任一 token(大小写无关;stopLossPct 命中 loss、lossBudgetPct 命中 budget)。"""
    kl = key.lower()
    return any(tok in kl for tok in suffixes)


def _to_num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def has_recent_loss(
    trades: list[dict[str, Any]],
    now: datetime | None = None,
    window_days: int = _RECENT_LOSS_WINDOW_DAYS,
) -> bool:
    """是否存在「近 window_days 天内的亏损平仓」。

    判定: trade.status == 已平仓 且 realizedPnl < 0 且 closedAt 落在 [now-window, now]。
    无 closedAt 或解析失败 → False。纯函数,不读磁盘。
    """
    if not trades:
        return False
    ref = (now or datetime.now())
    cutoff = ref - timedelta(days=window_days)
    for t in trades:
        if str(t.get("status")) != STATUS_CLOSED:
            continue
        pnl = _to_num(t.get("realizedPnl"))
        if pnl is None or pnl >= 0:
            continue
        closed = _parse_dt(t.get("closedAt"))
        if closed is None:
            continue
        if closed >= cutoff:
            return True
    return False


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def compute_relaxation_after_loss(
    data_dir: Path, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> bool:
    """组合判定:属放宽 && 近 30 天有亏损平仓 → True;否则 False。读磁盘获取 trades。"""
    if not is_relaxation(before, after):
        return False
    trades = store.list_trades(data_dir)
    return has_recent_loss(trades)

# ── CRUD ─────────────────────────────────────────────────
def create_proposal(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    _require_falsifier(payload)
    day = datetime.now().strftime("%Y%m%d")
    proposal_id = f"prop_{day}_{_next_seq(data_dir, day)}"
    ts = now_str()
    proposal: dict[str, Any] = {
        "schemaVersion": 1,
        "id": proposal_id,
        "title": str(payload.get("title") or "").strip(),
        "target": str(payload.get("target") or "").strip(),
        "evidence": list(payload.get("evidence") or []),
        "before": payload.get("before") or {},
        "after": payload.get("after") or {},
        "falsifier": str(payload.get("falsifier") or "").strip(),
        "sampleSize": int(payload.get("sampleSize") or 0),
        "status": "draft",
        "createdAt": ts,
        "updatedAt": ts,
        "history": [],
        "relaxationAfterLoss": compute_relaxation_after_loss(
            data_dir, payload.get("before"), payload.get("after")
        ),
    }
    _write(data_dir, proposal)
    return proposal


def get_proposal(data_dir: Path, proposal_id: str) -> dict[str, Any] | None:
    p = _proposal_path(data_dir, proposal_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_proposals(data_dir: Path, status: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(_proposals_dir(data_dir).glob("*.json")):
        try:
            proposal = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status and proposal.get("status") != status:
            continue
        out.append(proposal)
    out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    return out


def update_proposal(
    data_dir: Path, proposal_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """PATCH: 字段更新 + 状态迁移(走校验,非法迁移抛 ProposalError)。"""
    proposal = get_proposal(data_dir, proposal_id)
    if proposal is None:
        raise ProposalError("提案不存在")

    old_status = proposal.get("status", "draft")
    new_status = payload.get("status")

    # 先更新可变字段
    for field in ("title", "target", "evidence", "before", "after", "falsifier", "sampleSize"):
        if field not in payload:
            continue
        if field == "falsifier":
            val = str(payload[field] or "").strip()
            if not val:
                raise ProposalError("falsifier 必填且非空(无反证条件的提案不予受理)")
            proposal[field] = val
        elif field == "evidence":
            proposal[field] = list(payload[field] or [])
        elif field in ("before", "after"):
            proposal[field] = payload[field] or {}
        elif field == "sampleSize":
            proposal[field] = int(payload[field] or 0)
        else:
            proposal[field] = str(payload[field] or "").strip()

    # 再做状态迁移(用更新后的 sampleSize 判断证据门槛)
    if new_status is not None and new_status != old_status:
        _validate_transition(old_status, new_status, proposal.get("sampleSize", 0))
        proposal["status"] = new_status
        proposal.setdefault("history", []).append({
            "ts": now_str(),
            "from": old_status,
            "to": new_status,
            "note": str(payload.get("note") or ""),
        })

    proposal["updatedAt"] = now_str()
    _write(data_dir, proposal)
    return proposal
