"""策略风险声明 API — prefix=/api/strategies, tags=["strategy-profile"]。

与 api/strategy.py 共享 /api/strategies 前缀 (路径段不冲突); 路由由主会话统一注册,
本模块不修改 main.py。

端点:
    GET    /api/strategies/{strategy_id}/profile          读取声明 (无则 404)
    PUT    /api/strategies/{strategy_id}/profile          写前跑 validate_profile, 有问题 422
    DELETE /api/strategies/{strategy_id}/profile          删除声明 (无则 404)
    GET    /api/strategies/{strategy_id}/profile/validate 跑 strategy_validator (ledger 从 trade_journal 读)
"""
import asyncio
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from app.config import settings
from app.services.ai_structured import CancellationToken, run_structured_ai
from app.services.strategy_profile import (
    SCHEMA_VERSION,
    delete_profile,
    read_profile,
    validate_profile,
    write_profile,
)
from app.services.strategy_validator import validate_strategy
from app.services.trade_journal import store as journal_store
from app.services.ai_provider import ai_configured, generate_ai_text
from app.services.trading import proposals as proposals_svc

router = APIRouter(prefix="/api/strategies", tags=["strategy-profile"])


@router.get("/{strategy_id}/profile/validate")
async def validate(strategy_id: str, ai: Annotated[bool, Query()] = False):
    """机械体检: profile + 台账 ledger + 关联提案。

    ai=true 时追加 AI 深度体检报告 (对照 7 结构不变量 + 可证伪性语义判断);
    AI 未配置/调用失败 → aiReport=None 且附加 aiError, 不抛 500。
    ai=false 时响应结构与现状一致 (前端向后兼容)。
    """
    profile = read_profile(settings.data_dir, strategy_id)
    ledger = journal_store.read_ledger(settings.data_dir)
    proposals = proposals_svc.list_proposals(settings.data_dir)
    result = validate_strategy(strategy_id, profile, ledger, proposals)
    if not ai:
        return result
    report, error = await _ai_deep_review(strategy_id, profile, ledger)
    result["aiReport"] = report
    if error is not None:
        result["aiError"] = error
    return result


@router.get("/{strategy_id}/profile")
def get_profile(strategy_id: str):
    profile = read_profile(settings.data_dir, strategy_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未声明风险 profile")
    return {"profile": profile}


@router.put("/{strategy_id}/profile")
def put_profile(strategy_id: str, payload: Annotated[dict[str, Any], Body()]):
    """写声明; 写前强制结构校验, 有问题返回 422 + 问题清单。"""
    profile = dict(payload)
    # schemaVersion / strategyId 由服务端钉死, 不信任客户端传值
    profile["schemaVersion"] = SCHEMA_VERSION
    profile["strategyId"] = strategy_id
    problems = validate_profile(profile)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={"code": "profile_invalid", "problems": problems},
        )
    write_profile(settings.data_dir, profile)
    return {"ok": True, "profile": profile}


@router.delete("/{strategy_id}/profile")
def remove_profile(strategy_id: str):
    deleted = delete_profile(settings.data_dir, strategy_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未声明风险 profile")
    return {"ok": True}


# ── AI 深度体检 (P6.2; 仅 ai=true 时触发, 不替代机械 checks) ──
_AI_REVIEW_SYSTEM = """\
你是一位严格的策略结构诊断师。请对照以下 7 项结构不变量逐项评价该策略风险声明，
并对失效信号做可证伪性语义判断。必须返回 JSON 对象，包含 items（恰好 7 项）、
falsifiability 和 overall，禁止 markdown 或额外字段。
每项 item 为 {index,name,conclusion,reason}，conclusion 只能是 满足/部分满足/不满足。"""


class _ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=7)
    name: str
    conclusion: Literal["满足", "部分满足", "不满足"]
    reason: str


class _DeepReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_ReviewItem]
    falsifiability: str
    overall: str


def _review_invariant(data: dict[str, Any]) -> Any:
    from app.services.ai_structured import AIValidationIssue

    items = data.get("items")
    indexes = [item.get("index") for item in items] if isinstance(items, list) else []
    if len(items or []) != 7 or sorted(indexes) != list(range(1, 8)):
        return AIValidationIssue(
            category="invalid",
            path="items",
            message="必须提供恰好 7 项且 index 唯一覆盖 1..7",
        )
    return None


def _build_ai_review_messages(
    strategy_id: str,
    profile: dict[str, Any] | None,
    ledger: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """构建 AI 深度体检 prompt (OpenAI 兼容 messages)。"""
    profile_text = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "(未声明)"
    summary = ledger.get("summary") if isinstance(ledger, dict) else None
    ledger_text = json.dumps(summary, ensure_ascii=False) if summary else "(无)"
    user = (
        f"策略: {strategy_id}\n\n风险声明 profile:\n{profile_text}\n\n"
        f"台账摘要:\n{ledger_text}\n\n请逐项评价 7 项结构不变量并给出可证伪性判断。"
    )
    return [{"role": "system", "content": _AI_REVIEW_SYSTEM}, {"role": "user", "content": user}]


def _render_review(data: dict[str, Any]) -> str:
    lines = ["AI 深度体检："]
    for item in sorted(data["items"], key=lambda value: value["index"]):
        lines.append(f"{item['index']}. {item['name']}：{item['conclusion']} — {item['reason']}")
    lines.append(f"整体可证伪性：{data['falsifiability']}")
    lines.append(f"整体结论：{data['overall']}")
    return "\n".join(lines)


async def _ai_deep_review(
    strategy_id: str,
    profile: dict[str, Any] | None,
    ledger: dict[str, Any] | None,
    *,
    cancel_token: CancellationToken | None = None,
    on_event: Any | None = None,
) -> tuple[str | None, str | None]:
    """结构化体检并渲染为兼容的可读报告。"""
    if not ai_configured():
        return None, "AI 未配置, 深度体检不可用"
    try:
        result = await run_structured_ai(
            messages=_build_ai_review_messages(strategy_id, profile, ledger),
            output_model=_DeepReview,
            purpose="strategy_profile_deep_review",
            invariants=(_review_invariant,),
            cancel_token=cancel_token,
            on_event=on_event,
            generate=generate_ai_text,
            temperature=0.2,
            max_tokens=2000,
            timeout=60,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"AI 深度体检调用失败: {exc}"
    if result.status == "cancelled":
        raise asyncio.CancelledError
    if result.status != "ok" or not result.data:
        category = getattr(getattr(result, "error", None), "category", "malformed")
        message = getattr(getattr(result, "error", None), "message", "结构化输出校验失败")
        return None, f"AI 深度体检调用失败 [{category}]: {message}"
    try:
        return _render_review(result.data), None
    except Exception as exc:
        return None, f"AI 深度体检调用失败: {exc}"
