"""自定义信号 API 路由 — HTTP 请求 → 调用 custom_signals 模块 → 返回响应。

只做胶水：校验 → 持久化 → 失效缓存。不含表达式编译逻辑。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import custom_signals
from app.services.ai_signal_draft import (
    CustomSignalDraftError,
    CustomSignalDraftRequest,
    _ensure_unique_draft_id,
    generate_custom_signal_draft,
)

router = APIRouter(prefix="/api/custom-signals", tags=["custom-signals"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _invalidate() -> None:
    """失效 pipeline 的自定义信号缓存，下次计算重新加载。"""
    from app.indicators.pipeline import invalidate_custom_signals

    invalidate_custom_signals()


class ConditionModel(BaseModel):
    left: str  # 字段名（须在白名单）
    op: str  # > >= < <= == !=
    right: str  # "field:xxx" 或数字字符串


class SignalModel(BaseModel):
    id: str
    name: str
    kind: str  # entry | exit | both
    conditions: list[ConditionModel]
    enabled: bool = True


# ── 字段选项 / 运算符 ───────────────────────────────────


@router.get("/options")
def get_options():
    """返回可选字段与运算符，供前端下拉框使用。"""
    # 字段带中文标签（取自 ENRICHED_COLUMNS，回退为字段名本身）
    from app.indicators.pipeline import ENRICHED_COLUMNS

    fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(custom_signals.ALLOWED_FIELDS)
    ]
    return {
        "fields": fields,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "kinds": [
            {"key": "entry", "label": "买入"},
            {"key": "exit", "label": "卖出"},
            {"key": "both", "label": "买卖通用"},
        ],
    }


# ── 列表 ───────────────────────────────────────────────


@router.get("")
def list_signals(request: Request):
    sigs = custom_signals.load_all(_data_dir(request))
    return {"signals": sigs}


# ── 新建 / 更新 ────────────────────────────────────────


@router.post("")
def save_signal(req: SignalModel, request: Request):
    sig = req.model_dump()
    try:
        custom_signals.validate(sig)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    custom_signals.save_one(_data_dir(request), sig)
    _invalidate()
    return {"ok": True, "signal": sig}


# ── 删除 ───────────────────────────────────────────────


@router.delete("/{signal_id}")
def delete_signal(signal_id: str, request: Request):
    if not custom_signals.ID_RE.match(signal_id):
        raise HTTPException(status_code=400, detail="信号 id 非法")
    deleted = custom_signals.delete_one(_data_dir(request), signal_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="信号不存在")
    _invalidate()
    return {"ok": True}


# ── AI 草稿（仅返回，不保存，不执行）────────────────────────────────


@router.post("/ai-draft")
async def ai_draft_custom_signal(req: CustomSignalDraftRequest, request: Request):
    """仅生成草稿。provider/auth/quota 不可用映射 503；结构非法 422；冲突 id 仅后缀化返回的 draft。"""
    try:
        res = await generate_custom_signal_draft(req.text, req.profile_id)
    except CustomSignalDraftError as exc:
        msg = str(exc)
        if "provider_unavailable" in msg or "unavailable" in msg:
            raise HTTPException(
                status_code=503, detail={"code": "custom_signal_draft_unavailable"}
            ) from None
        raise HTTPException(
            status_code=422, detail={"code": "invalid_draft", "message": msg}
        ) from None

    draft = dict(res.get("draft") or {})
    # 只改返回 draft 的 id 做安全唯一后缀；绝不写盘、不调用 save/invalidate
    try:
        stored = custom_signals.load_all(_data_dir(request))
        existing = {
            signal["id"]
            for signal in stored
            if isinstance(signal, dict) and isinstance(signal.get("id"), str)
        }
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={"code": "custom_signal_store_unavailable"},
        ) from None
    draft = _ensure_unique_draft_id(draft, existing)

    return {
        "draft": draft,
        "rationale": res.get("rationale"),
        "ai_meta": res.get("ai_meta"),
    }
