"""Trading Review API — 红旗检测 + AI 归因 + 策略变更提案。

独立路由 (prefix=/api/trading, tags=["trading-review"]),
不改 api/trading.py;路由由 main.py 统一注册。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.config import settings
from app.services.trading import autopsy as autopsy_svc
from app.services.trading import cycle_audit as cycle_audit_svc
from app.services.trading import proposals as proposals_svc
from app.services.trading.proposals import ProposalError
from app.services.trading.red_flags import scan_all, scan_trade
from app.services.trading.review_job import run_state_driven_autopsy

router = APIRouter(prefix="/api/trading", tags=["trading-review"])


# ── 红旗 ─────────────────────────────────────────────────
@router.get("/red-flags")
def list_red_flags():
    """汇总全部 trades 的红旗(按 tradeId 分组,仅含有红旗的笔)。"""
    return {"flags": scan_all(settings.data_dir)}


@router.get("/trades/{trade_id}/red-flags")
def get_trade_red_flags(trade_id: str):
    """单笔红旗。trade 不存在时返回空列表(红旗可审计已删除数据)。"""
    return {"tradeId": trade_id, "flags": scan_trade(settings.data_dir, trade_id)}


# ── 周期审计（跨笔聚合） ─────────────────────────────────
@router.get("/cycle-audit")
def get_cycle_audit():
    """执行周期审计：跨所有已平仓 trades 聚合红旗频率、归因分类分布、策略族统计。

    纯代码，不调用 AI。按 YMOS SOP 样本门槛返回 auditLevel。
    """
    return cycle_audit_svc.run_cycle_audit(settings.data_dir)


# ── AI 归因 ──────────────────────────────────────────────
@router.post("/trades/{trade_id}/autopsy")
async def run_autopsy(trade_id: str, profile_id: Annotated[str | None, Query()] = None):
    """跑 AI 归因并返回结果(同时落盘)。AI 调用失败时不落盘,返回友好错误。

    P3: ``profile_id`` 查询参数 (可选) 选择实际使用的 AI profile; 不传走默认 profile。
    """
    return await autopsy_svc.run_autopsy(settings.data_dir, trade_id, profile_id=profile_id)


@router.get("/trades/{trade_id}/autopsy")
def get_autopsy(trade_id: str):
    """读已落盘归因(无则 404)。"""
    result = autopsy_svc.read_autopsy(settings.data_dir, trade_id)
    if result is None:
        raise HTTPException(status_code=404, detail="该笔交易尚未生成归因分析")
    return result


# ── 盘后状态驱动归因 (P6.4 L0/L1/L2) ──────────────────────
@router.post("/review/auto-run")
async def run_auto_review():
    """手动触发盘后状态驱动 AI 归因 (L0/L1)。

    - L0 无候选 → 零 AI 调用, 返回 no_change 语义。
    - L1 有候选 → 仅归因候选 trades; AI 未配置时降级返回 blocked_by_dependency。
    返回 run_state_driven_autopsy 的完整结果 dict。
    """
    return await run_state_driven_autopsy(settings.data_dir)


# ── 策略变更提案 ─────────────────────────────────────────
@router.get("/proposals")
def list_proposals(
    status: Annotated[str | None, Query()] = None,
):
    return {"proposals": proposals_svc.list_proposals(settings.data_dir, status)}


@router.post("/proposals")
def create_proposal(payload: Annotated[dict[str, Any], Body()]):
    try:
        return proposals_svc.create_proposal(settings.data_dir, payload)
    except ProposalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    proposal = proposals_svc.get_proposal(settings.data_dir, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="提案不存在")
    return proposal


@router.patch("/proposals/{proposal_id}")
def patch_proposal(proposal_id: str, payload: Annotated[dict[str, Any], Body()]):
    try:
        return proposals_svc.update_proposal(settings.data_dir, proposal_id, payload)
    except ProposalError as e:
        raise HTTPException(status_code=400, detail=str(e))
