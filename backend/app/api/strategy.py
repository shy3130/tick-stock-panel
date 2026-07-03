"""策略 API 路由 — HTTP 请求 → 调用策略模块 → 返回响应。

只做胶水，不含业务逻辑。
"""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine, StrategyDef
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.prompt_builder import build_step1, build_step2
from app.strategy.monitor import StrategyMonitorService, StrategyAlert

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

# ── Helpers ──────────────────────────────────────────────────────────


def _get_engine(request: Request) -> StrategyEngine:
    engine = getattr(request.app.state, "strategy_engine", None)
    if not engine:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")
    return engine


def _get_monitor(request: Request) -> StrategyMonitorService:
    mon = getattr(request.app.state, "strategy_monitor", None)
    if not mon:
        raise HTTPException(status_code=503, detail="策略监控未初始化")
    return mon


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _safe(result_dict: dict) -> dict:
    rows = result_dict.get("rows", [])
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return result_dict


def _strategy_detail(s: StrategyDef, overrides: dict | None = None) -> dict:
    """策略详情（含用户覆盖）"""
    bf = {**s.basic_filter}
    scoring = dict(s.meta.get("scoring", {}))
    params_defaults = {p["id"]: p["default"] for p in s.meta.get("params", [])}

    if overrides:
        if overrides.get("basic_filter"):
            bf.update(overrides["basic_filter"])
        if overrides.get("scoring"):
            scoring.update(overrides["scoring"])
        # 用户保存的参数覆盖默认值: 合并进 params_defaults, 前端据此回显
        if overrides.get("params"):
            params_defaults.update(overrides["params"])

    # 名称/描述可被用户覆盖
    name = overrides.get("name", s.meta.get("name", "")) if overrides else s.meta.get("name", "")
    description = overrides.get("description", s.meta.get("description", "")) if overrides else s.meta.get("description", "")

    return {
        "id": s.meta["id"],
        "name": name or s.meta.get("name", ""),
        "description": description or s.meta.get("description", ""),
        "tags": s.meta.get("tags", []),
        "source": s.source,
        "version": s.meta.get("version", "1.0.0"),
        "basic_filter": bf,
        "params": s.meta.get("params", []),
        "params_defaults": params_defaults,
        "scoring": scoring,
        "entry_signals": s.entry_signals,
        "exit_signals": s.exit_signals,
        "stop_loss": overrides.get("stop_loss", s.stop_loss) if overrides else s.stop_loss,
        "take_profit": getattr(s, "take_profit", None),
        "trailing_stop": getattr(s, "trailing_stop", None),
        "trailing_take_profit_activate": getattr(s, "trailing_take_profit_activate", None),
        "trailing_take_profit_drawdown": getattr(s, "trailing_take_profit_drawdown", None),
        "max_hold_days": overrides.get("max_hold_days", s.max_hold_days) if overrides else s.max_hold_days,
        "alerts": s.alerts,
        "order_by": s.meta.get("order_by", "score"),
        "descending": s.meta.get("descending", True),
        "limit": s.meta.get("limit", 30),
        "display_limit": overrides.get("display_limit") if overrides and "display_limit" in overrides else None,
    }


# ── Request Models ───────────────────────────────────────────────────


class RunRequest(BaseModel):
    strategy_id: str
    as_of: date | None = None
    pool: list[str] | None = None
    params: dict | None = None


class RunAllRequest(BaseModel):
    as_of: date | None = None


class SaveConfigRequest(BaseModel):
    strategy_id: str
    overrides: dict


class AIGenerateRequest(BaseModel):
    prompt: str


class AISaveRequest(BaseModel):
    code: str
    strategy_id: str


class MonitorStartRequest(BaseModel):
    strategy_id: str


class ExportRequest(BaseModel):
    target: str
    expression: dict | None = None
    conditions: list[dict] | None = None


# ── 列表 / 详情 ─────────────────────────────────────────────────────


@router.get("")
def list_strategies(request: Request):
    engine = _get_engine(request)
    data_dir = _data_dir(request)
    all_overrides = strategy_config.list_overrides(data_dir)

    result = []
    for meta in engine.list_strategies():
        sid = meta["id"]
        s = engine.get(sid)
        overrides = all_overrides.get(sid)
        result.append(_strategy_detail(s, overrides))
    return {"strategies": result}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str, request: Request):
    engine = _get_engine(request)
    try:
        s = engine.get(strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    overrides = strategy_config.load_override(_data_dir(request), strategy_id)
    return _strategy_detail(s, overrides or None)


@router.post("/{strategy_id}/export")
def export_strategy(strategy_id: str, req: ExportRequest, request: Request):
    engine = _get_engine(request)
    try:
        s = engine.get(strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if req.target not in {"tdx", "ths"}:
        raise HTTPException(status_code=400, detail=f"不支持的导出目标: {req.target}")

    from app.services.strategy_export import export_strategy_formula

    return export_strategy_formula(
        s,
        req.target,
        expression=req.expression,
        conditions=req.conditions,
    ).to_dict()


# ── 执行选股 ─────────────────────────────────────────────────────────


@router.post("/run")
def run_strategy(req: RunRequest, request: Request):
    engine = _get_engine(request)
    data_dir = _data_dir(request)

    # 读取用户覆盖配置
    overrides = strategy_config.load_override(data_dir, req.strategy_id)
    params = req.params or {}
    # 合并用户保存的策略参数
    if overrides.get("params"):
        merged = dict(overrides["params"])
        merged.update(params)  # 请求里的优先
        params = merged

    # 确定日期
    as_of = req.as_of
    if not as_of:
        from app.services.screener import ScreenerService
        svc = ScreenerService(request.app.state.repo)
        as_of = svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    try:
        result = engine.run(
            req.strategy_id, as_of,
            pool=req.pool,
            params=params,
            overrides=overrides or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    return _safe(asdict(result))


@router.post("/run-all")
def run_all(req: RunAllRequest, request: Request):
    engine = _get_engine(request)
    data_dir = _data_dir(request)

    as_of = req.as_of
    if not as_of:
        from app.services.screener import ScreenerService
        svc = ScreenerService(request.app.state.repo)
        as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "results": {}}

    all_overrides = strategy_config.list_overrides(data_dir)
    results: dict[str, dict] = {}
    for sid, result in engine.run_all(as_of, overrides_map=all_overrides).items():
        results[sid] = {"total": result.total, "as_of": str(as_of)}

    return {"as_of": str(as_of), "results": results}


# ── 配置持久化 ───────────────────────────────────────────────────────


@router.post("/config")
def save_config(req: SaveConfigRequest, request: Request):
    engine = _get_engine(request)
    if not engine.has(req.strategy_id):
        raise HTTPException(status_code=404, detail=f"策略 {req.strategy_id} 不存在")

    # 剥离与策略默认值相同的字段，只保存用户真正修改过的值
    overrides = _strip_defaults(req.strategy_id, req.overrides, engine)

    strategy_config.save_override(_data_dir(request), req.strategy_id, overrides)
    return {"ok": True}


def _strip_defaults(strategy_id: str, overrides: dict, engine) -> dict:
    """剥离与策略默认值相同的字段，避免默认值被固化到 override 中。

    核心问题: 前端把策略的默认 basic_filter 全量发回后端保存，
    导致隐含的默认过滤条件 (如 market_cap_min, amount_min) 被写入 override 文件。
    即使前端 UI 不展示这些字段，它们仍会在策略运行时生效。
    """
    s = engine.get(strategy_id)
    result = dict(overrides)

    # 处理 basic_filter: 只保留与策略默认值不同的键
    bf = result.get("basic_filter")
    if bf and isinstance(bf, dict):
        default_bf = s.basic_filter if s else {}
        stripped_bf = {}
        for k, v in bf.items():
            default_val = default_bf.get(k)
            # 保留与默认值不同的键，以及没有默认值的键
            if k not in default_bf or v != default_val:
                stripped_bf[k] = v
        if stripped_bf:
            result["basic_filter"] = stripped_bf
        else:
            del result["basic_filter"]

    return result


@router.delete("/config/{strategy_id}")
def reset_config(strategy_id: str, request: Request):
    strategy_config.delete_override(_data_dir(request), strategy_id)
    return {"ok": True}


# ── AI 生成 ───────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    """两步策略构建请求"""
    step: int  # 1 / 2
    # step1 字段
    name: str = ""
    description: str = ""
    direction: str = "long"
    rules: str = ""
    strategy_id: str = ""
    # step2 字段
    current_code: str = ""
    instruction: str = ""


@router.get("/ai/status")
def ai_status(request: Request):
    """Check whether the selected AI provider is configured."""
    from app import secrets_store
    from app.services.ai_provider import ai_configured, current_ai_model, current_ai_provider

    has_key = bool(secrets_store.get_ai_key())
    model = current_ai_model()
    provider = current_ai_provider()
    return {
        "configured": ai_configured(provider) and bool(model or provider == "codex_cli"),
        "has_key": has_key,
        "has_model": bool(model),
        "provider": provider,
    }


@router.get("/{strategy_id}/source")
def get_strategy_source(strategy_id: str, request: Request):
    """获取策略源文件内容（用于 AI 修改）"""
    from pathlib import Path

    # 先查 StrategyEngine 获取文件路径
    engine = _get_engine(request)
    try:
        s = engine.get(strategy_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 不存在")

    path = s.file_path
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="策略源文件不存在")

    return {"code": path.read_text(encoding="utf-8"), "source": s.source}


@router.post("/ai/test")
async def ai_test(request: Request):
    """Send a small prompt through the selected AI provider."""
    from app.services.ai_provider import current_ai_model, current_ai_provider, generate_ai_text

    try:
        text = await generate_ai_text(
            [{"role": "user", "content": "Reply exactly: OK"}],
            temperature=0,
            max_tokens=8,
            timeout=15,
        )
        return {"ok": True, "model": current_ai_model() or current_ai_provider(), "response": text[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/build")
async def build_strategy(req: BuildRequest, request: Request):
    """两步策略构建。
    step1: name + description + direction + rules → 完整策略
    step2: current_code + instruction → 修改任意部分
    """
    gen = AIStrategyGenerator()

    if req.step == 1:
        prompt = build_step1(req.name, req.description, req.direction, req.rules, req.strategy_id)
    elif req.step == 2:
        prompt = build_step2(req.current_code, req.instruction)
    else:
        raise HTTPException(status_code=400, detail=f"无效步骤: {req.step}")

    try:
        result = await gen.generate(prompt)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result



@router.post("/ai/generate")
async def ai_generate(req: AIGenerateRequest, request: Request):
    try:
        gen = AIStrategyGenerator()
        result = await gen.generate(req.prompt)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI生成失败: {e}") from e
    return result


@router.post("/ai/save")
async def ai_save(req: AISaveRequest, request: Request):
    data_dir = _data_dir(request)
    out_dir = data_dir / "strategies" / "ai"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{req.strategy_id}.py"
    previous_code = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(req.code, encoding="utf-8")

    # 热重载，并确认保存的策略真的被引擎加载。
    engine = _get_engine(request)
    engine.reload()
    if not engine.has(req.strategy_id):
        if previous_code is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous_code, encoding="utf-8")
        engine.reload()
        raise HTTPException(
            status_code=400,
            detail=f"策略保存成功但加载失败: {req.strategy_id}，请检查代码语法和 META.id 是否一致",
        )
    return {"ok": True, "path": str(path)}


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request):
    """删除自定义策略 — 清除 .py 文件 + overrides + 热重载。内置策略不可删除。"""
    from pathlib import Path

    engine = _get_engine(request)
    try:
        s = engine.get(strategy_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 不存在")

    if s.source == "builtin":
        raise HTTPException(status_code=403, detail="内置策略不可删除")

    # 删除策略文件
    if s.file_path and s.file_path.exists():
        s.file_path.unlink()

    # 删除 overrides
    data_dir = _data_dir(request)
    override_path = data_dir / "user_data" / "strategy_overrides" / f"{strategy_id}.json"
    if override_path.exists():
        override_path.unlink()

    # 热重载
    engine.reload()
    return {"ok": True}


# ── 监控 ─────────────────────────────────────────────────────────────
# 注: 策略监控已统一迁移到 MonitorRuleEngine (监控通知页), 旧的 start/stop/status
# 路由已移除。StrategyMonitorService 类保留 (其 _check_signals 被 MonitorRuleEngine 复用)。


# ── 热重载 ───────────────────────────────────────────────────────────


@router.post("/reload")
def reload_strategies(request: Request):
    engine = _get_engine(request)
    engine.reload()
    return {"ok": True, "count": len(engine.list_strategies())}
