"""Trading 计划台 + 门禁 API — gate-rules 读写 / gate 预检 / 计划 CRUD / 偏差 / 结构化计划检查。

prefix=/api/trading, tags=["trading-plans"], 与 trading.py 同前缀不冲突 (路径不重叠)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.config import settings
from app.services.ai_attempts import get_registry
from app.services.ai_structured import CancellationToken, new_attempt_id, new_request_id
from app.services.trading import store
from app.services.trading.gates import evaluate_gates, read_gate_rules, write_gate_rules
from app.services.trading.plans import deviation as plan_deviation
from app.services.trading.plans import read_plan, write_plan

router = APIRouter(prefix="/api/trading", tags=["trading-plans"])


# ── 用户门禁规则 ─────────────────────────────────────────
@router.get("/gate-rules")
def get_gate_rules():
    return read_gate_rules(settings.data_dir)


@router.put("/gate-rules")
def put_gate_rules(payload: Annotated[dict[str, Any], Body()]):
    try:
        return write_gate_rules(settings.data_dir, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── 门禁预检 (不落盘, 供前端决策台实时预览) ─────────────
@router.post("/gates/evaluate")
def evaluate(payload: Annotated[dict[str, Any], Body()]):
    mode = str(payload.get("mode") or "").strip()
    if not mode:
        raise HTTPException(status_code=400, detail="mode 必填")
    trade_id = str(payload.get("tradeId") or "").strip() or None
    inner = payload.get("payload") or {}
    if not isinstance(inner, dict):
        raise HTTPException(status_code=400, detail="payload 必须是对象")
    trade = None
    if trade_id:
        trade = store.read_trade(settings.data_dir, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="单笔交易不存在")
    return evaluate_gates(settings.data_dir, mode, trade=trade, payload=inner)


# ── 交易计划 CRUD ────────────────────────────────────────
@router.get("/plans/{date}")
def get_plan(date: str):
    plan = read_plan(settings.data_dir, date)
    if plan is None:
        return {"schemaVersion": 1, "date": date, "entries": [], "actualNotes": ""}
    return plan


@router.put("/plans/{date}")
def put_plan(date: str, payload: Annotated[dict[str, Any], Body()]):
    if not (len(date) == 8 and date.isdigit()):
        raise HTTPException(status_code=400, detail="date 必须是 yyyymmdd")
    try:
        return write_plan(settings.data_dir, date, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/plans/{date}/deviation")
def get_deviation(date: str):
    if not (len(date) == 8 and date.isdigit()):
        raise HTTPException(status_code=400, detail="date 必须是 yyyymmdd")
    return plan_deviation(settings.data_dir, date)


# ── 结构化计划检查 (P4 默认关闭; 只读消费 plan_check 模块) ─────────
# 协议: NDJSON, 每行一个 JSON 事件。
#   {"type":"meta", ...}     首事件, 暴露 overall attempt_id / request_id
#   {"type":"progress", ...} 进度透传 (绝不含 prompt)
#   {"type":"result", ...}   安全 artifact 投影
#   {"type":"error", ...}    失败
#   {"type":"done", ...}     终止
# 关闭开关 → HTTP 403, 零 AI。AI profile 不可用 → HTTP 503。


def _require_feature_enabled() -> None:
    from app.services import preferences

    if not preferences.get_structured_plan_check_enabled():
        raise HTTPException(status_code=403, detail="结构化计划检查未开启")


def _require_ai_available(profile_id: str | None) -> None:
    from app.services.ai_provider import profile_configured

    if not profile_configured(profile_id):
        raise HTTPException(status_code=503, detail="AI profile 不可用")


def _find_plan_entry(date: str, entry_id: str) -> dict[str, Any]:
    """读取已保存的计划条目; 不存在计划或条目 → 404 (fail-closed)。"""
    if not (len(date) == 8 and date.isdigit()):
        raise HTTPException(status_code=400, detail="date 必须是 yyyymmdd")
    plan = read_plan(settings.data_dir, date)
    if plan is None:
        raise HTTPException(status_code=404, detail="当日计划不存在")
    entries = plan.get("entries") or []
    for entry in entries:
        if str(entry.get("id") or "") == entry_id:
            return entry
    raise HTTPException(status_code=404, detail="计划条目不存在")


@router.post("/plans/{date}/entries/{entry_id}/check")
async def check_plan_entry(
    request: Request,
    date: str,
    entry_id: str,
    profile_id: Annotated[str | None, Query()] = None,
):
    """结构化计划检查 — NDJSON 流式返回。

    只检查已保存的用户计划条目; 不生成/执行订单, 不写 trade event。
    关闭开关 → 403; AI profile 不可用 → 503; 缺计划/条目 → 404。
    """
    _require_feature_enabled()
    _find_plan_entry(date, entry_id)  # 预检存在性 (核心模块也会读)
    _require_ai_available(profile_id)

    from app.services.trading import plan_check

    repo = request.app.state.repo
    data_dir = settings.data_dir
    attempt_id = new_attempt_id()
    request_id = new_request_id()
    token = CancellationToken()
    registry = get_registry()

    async def stream_gen():
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("plan check stream task unavailable")
        registry.register(attempt_id=attempt_id, request_id=request_id, task=task, token=token)
        # 首事件: 暴露 overall IDs (不含 prompt)
        yield (
            json.dumps(
                {
                    "type": "meta",
                    "attempt_id": attempt_id,
                    "request_id": request_id,
                    "date": date,
                    "entry_id": entry_id,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

        progress_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def on_event(kind: str, payload: dict[str, Any] | None = None) -> None:
            # 进度采用显式 allowlist, 避免未来 runtime 新字段把 prompt/raw 嵌套透传。
            allowed = {
                "attempt_id",
                "request_id",
                "attempt_index",
                "stage",
                "status",
                "ok",
                "data_as_of",
                "source",
                "adjustment",
                "warnings",
                "error_category",
                "elapsed_ms",
                "profile_id",
                "provider",
                "model",
                "usage",
                "prompt_budget",
            }
            safe = {k: v for k, v in (payload or {}).items() if k in allowed}
            with contextlib.suppress(asyncio.QueueFull):
                progress_queue.put_nowait({"type": "progress", "kind": kind, **safe})

        async def drain_progress():
            while True:
                item = await progress_queue.get()
                if item is None:
                    return
                yield json.dumps(item, ensure_ascii=False) + "\n"

        async def run_check():
            try:
                artifact = await plan_check.run_plan_check(
                    repo,
                    data_dir,
                    date=date,
                    entry_id=entry_id,
                    profile_id=profile_id,
                    cancel_token=token,
                    on_event=on_event,
                    attempt_id=attempt_id,
                    request_id=request_id,
                )
            except asyncio.CancelledError:
                await progress_queue.put(
                    {"type": "error", "code": "cancelled", "message": "已取消"}
                )
                raise
            except Exception:
                await progress_queue.put(
                    {"type": "error", "code": "internal", "message": "计划检查失败"}
                )
            else:
                await progress_queue.put(
                    {
                        "type": "result",
                        "attempt_id": artifact.attempt_id,
                        "request_id": artifact.request_id,
                        "status": artifact.status,
                        "result": artifact.result,
                        "trace": [n.model_dump(mode="json") for n in artifact.trace],
                        "usage": artifact.usage.model_dump(mode="json"),
                        "warnings": artifact.warnings,
                    }
                )
            finally:
                await progress_queue.put(None)

        check_task = asyncio.create_task(run_check())
        try:
            async for chunk in drain_progress():
                yield chunk
            await check_task
        except asyncio.CancelledError:
            registry.cancel(attempt_id)
            if not check_task.done():
                check_task.cancel()
            raise
        finally:
            registry.unregister(attempt_id)
            if not check_task.done():
                check_task.cancel()

        yield (
            json.dumps(
                {"type": "done", "attempt_id": attempt_id, "request_id": request_id},
                ensure_ascii=False,
            )
            + "\n"
        )

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-AI-Attempt-ID": attempt_id,
            "X-AI-Request-ID": request_id,
        },
    )


# ── 计划检查 artifact 查询 (只读安全投影) ──────────────────
@router.get("/plan-checks")
def list_plan_checks(
    symbol: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
):
    """列出计划检查 artifact (安全投影)。可按 symbol 过滤。"""
    from app.services import analysis_artifacts
    from app.services.trading import plan_check

    _require_feature_enabled()
    items = analysis_artifacts.list_artifacts(
        settings.data_dir,
        purpose=plan_check.PURPOSE,
        limit=limit,
    )
    if symbol:
        symbol_up = symbol.strip().upper()
        items = [a for a in items if (a.symbol or "").upper() == symbol_up]
    return {
        "items": [
            {
                "id": a.id,
                "attempt_id": a.attempt_id,
                "request_id": a.request_id,
                "status": a.status,
                "symbol": a.symbol,
                "market": a.market,
                "profile_id": a.profile_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "result_status": (a.result or {}).get("status") if a.result else None,
            }
            for a in items
        ],
    }


def _read_plan_check_artifact(attempt_id: str):
    """读取 plan_check artifact; 路径穿越/不存在 → 404。"""
    from app.services import analysis_artifacts
    from app.services.trading import plan_check

    try:
        artifact = analysis_artifacts.read(settings.data_dir, attempt_id)
    except analysis_artifacts.ArtifactError:
        raise HTTPException(status_code=404, detail="计划检查 artifact 不存在") from None
    if artifact is None or artifact.purpose != plan_check.PURPOSE:
        raise HTTPException(status_code=404, detail="计划检查 artifact 不存在")
    return artifact


@router.get("/plan-checks/{attempt_id}")
def get_plan_check(attempt_id: str):
    """读取单个计划检查 artifact (安全投影)。"""
    _require_feature_enabled()
    artifact = _read_plan_check_artifact(attempt_id)
    return artifact.model_dump(mode="json")


@router.get("/plan-checks/{attempt_id}/export")
def export_plan_check(
    attempt_id: str,
    format: Annotated[str, Query()] = "json",
):
    """导出计划检查 artifact (安全投影)。

    format=json → application/json 内联; format=markdown → text/markdown 附件。
    只接受安全 attempt_id, 不接受用户文件名/路径。
    """
    from app.services.trading import plan_check

    _require_feature_enabled()
    if format not in ("json", "markdown"):
        raise HTTPException(status_code=400, detail="format 必须是 json 或 markdown")
    artifact = _read_plan_check_artifact(attempt_id)

    if format == "json":
        return artifact.model_dump(mode="json")

    md = plan_check.artifact_to_markdown(artifact)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in attempt_id)
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="plan-check-{safe_name}.md"'},
    )
