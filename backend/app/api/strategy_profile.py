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
from app.services.ai_budgets import resolve_budget
from app.services.ai_provider import ai_configured, generate_ai_text_with_meta
from app.services.ai_provider import profile_configured as _profile_configured
from app.services.ai_structured import CancellationToken, build_ai_meta, run_structured_ai
from app.services.ai_usage_snapshot import record_structured_usage
from app.services.strategy_profile import (
    SCHEMA_VERSION,
    delete_profile,
    read_profile,
    validate_profile,
    write_profile,
)
from app.services.strategy_validator import validate_strategy
from app.services.trade_journal import store as journal_store
from app.services.trading import proposals as proposals_svc

router = APIRouter(prefix="/api/strategies", tags=["strategy-profile"])

def profile_configured(profile_id: str | None = None) -> bool:
    """按指定 profile 检查；默认路径复用本模块可注入的兼容 seam。"""
    return _profile_configured(profile_id) if profile_id else ai_configured()


@router.get("/{strategy_id}/profile/validate")
async def validate(
    strategy_id: str,
    ai: Annotated[bool, Query()] = False,
    profile_id: Annotated[str | None, Query()] = None,
):
    """机械体检: profile + 台账 ledger + 关联提案。



    ai=true 时追加 AI 深度体检报告 (对照 7 结构不变量 + 可证伪性语义判断);
    AI 未配置/调用失败 → aiReport=None 且附加 aiError, 不抛 500。
    P3: ``profile_id`` (可选) 选择实际使用的 AI profile; 响应 additive 追加
    ``ai_meta`` (实际 profile / fallback / usage)。ai=false 时与现状一致。
    """
    profile = read_profile(settings.data_dir, strategy_id)
    ledger = journal_store.read_ledger(settings.data_dir)
    proposals = proposals_svc.list_proposals(settings.data_dir)
    result = validate_strategy(strategy_id, profile, ledger, proposals)
    if not ai:
        return result
    report, error, ai_meta = await _ai_deep_review(
        strategy_id, profile, ledger, ai_profile_id=profile_id, include_meta=True
    )
    result["aiReport"] = report
    if error is not None:
        result["aiError"] = error
    if ai_meta is not None:
        result["ai_meta"] = ai_meta
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


async def generate_ai_text(messages, **kwargs):
    """兼容生成 seam；生产默认返回带实际 profile/usage 的元数据响应。"""
    return await generate_ai_text_with_meta(messages, **kwargs)


async def _default_generate(messages, **kwargs):
    """P3: 默认走 metadata 路径 (真实 fallback + usage 回传)。"""
    return await generate_ai_text(messages, **kwargs)


async def _ai_deep_review(
    strategy_id: str,
    profile: dict[str, Any] | None,
    ledger: dict[str, Any] | None,
    *,
    ai_profile_id: str | None = None,
    cancel_token: CancellationToken | None = None,
    on_event: Any | None = None,
    include_meta: bool = False,
) -> tuple[Any, ...]:
    """结构化体检并渲染为兼容的可读报告。

    P3: 返回 ``(report, error, ai_meta)``; ``ai_meta`` 为 None 表示未触发 AI
    (未配置 / 调用失败时仍尽量回填，便于前端展示实际 profile)。
    """
    def response(
        report: str | None, error: str | None, ai_meta: dict[str, Any] | None
    ) -> tuple[Any, ...]:
        return (report, error, ai_meta) if include_meta else (report, error)

    if not profile_configured(ai_profile_id):
        return response(None, "AI 未配置, 深度体检不可用", None)
    budget = resolve_budget("strategy_profile_deep_review")
    try:
        result = await run_structured_ai(
            messages=_build_ai_review_messages(strategy_id, profile, ledger),
            output_model=_DeepReview,
            purpose="strategy_profile_deep_review",
            profile_id=ai_profile_id,
            invariants=(_review_invariant,),
            cancel_token=cancel_token,
            on_event=on_event,
            generate=_default_generate,
            temperature=budget.temperature,
            max_tokens=budget.max_tokens,
            timeout=budget.timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return response(None, f"AI 深度体检调用失败: {exc}", None)
    record_structured_usage("strategy_profile_deep_review", result)
    ai_meta = build_ai_meta(result)
    if result.status == "cancelled":
        raise asyncio.CancelledError
    if result.status != "ok" or not result.data:
        category = getattr(getattr(result, "error", None), "category", "malformed")
        message = getattr(getattr(result, "error", None), "message", "结构化输出校验失败")
        return response(None, f"AI 深度体检调用失败 [{category}]: {message}", ai_meta)
    try:
        return response(_render_review(result.data), None, ai_meta)
    except Exception as exc:
        return response(None, f"AI 深度体检调用失败: {exc}", ai_meta)
