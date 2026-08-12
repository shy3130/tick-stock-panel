"""策略 API 路由 — HTTP 请求 → 调用策略模块 → 返回响应。

只做胶水，不含业务逻辑。
"""
from __future__ import annotations

import ast
import json
import logging
import math
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.backtest.minute_trigger import MINUTE_EXIT_TRIGGER_SIGNALS
from app.strategy import config as strategy_config
from app.strategy.ai_generator import AIStrategyGenerator, find_meta_assignment
from app.strategy.engine import StrategyDef, StrategyEngine
from app.strategy.monitor import StrategyMonitorService
from app.strategy.prompt_builder import build_step1, build_step2

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
logger = logging.getLogger(__name__)

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


def _invalidate_strategy_runtime(request: Request) -> None:
    from app.services import strategy_cache

    strategy_cache.clear_cache(_data_dir(request))
    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    if monitor_engine is not None:
        monitor_engine.invalidate_strategy_state()


def _cleanup_deleted_strategy(request: Request, strategy_id: str) -> list[str]:
    """尽力清理删除后的派生状态, 清理失败不应把已成功的源文件删除变成 500。"""
    from app.services import preferences
    from app.strategy import monitor_rules

    data_dir = _data_dir(request)
    warnings: list[str] = []

    try:
        strategy_config.delete_override(data_dir, strategy_id)
    except Exception as e:
        warnings.append(f"覆盖配置清理失败: {e}")

    try:
        _invalidate_strategy_runtime(request)
    except Exception as e:
        warnings.append(f"运行缓存清理失败: {e}")

    try:
        monitored_ids = preferences.get_strategy_monitor_ids()
        if strategy_id in monitored_ids:
            preferences.set_realtime_monitor_config({
                "strategy_monitor_ids": [sid for sid in monitored_ids if sid != strategy_id],
            })
    except Exception as e:
        warnings.append(f"监控偏好清理失败: {e}")

    try:
        rules_changed = False
        for rule in monitor_rules.load_all(data_dir):
            if (
                rule.get("type") == "strategy"
                and rule.get("strategy_id") == strategy_id
                and rule.get("enabled", True)
            ):
                rule = dict(rule)
                rule["enabled"] = False
                monitor_rules.save_one(data_dir, rule)
                rules_changed = True

        monitor_engine = getattr(request.app.state, "monitor_engine", None)
        if rules_changed and monitor_engine is not None:
            monitor_engine.set_rules(monitor_rules.load_all(data_dir))
    except Exception as e:
        warnings.append(f"关联监控清理失败: {e}")

    for warning in warnings:
        logger.warning("delete strategy %s: %s", strategy_id, warning)
    return warnings


def _safe(result_dict: dict) -> dict:
    rows = result_dict.get("rows", [])
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return result_dict


def _child_meta(engine: StrategyEngine | None, child_id: str) -> dict:
    """查询子策略的可读名称与来源; engine 缺失或子策略不存在时回退为 id/unknown。"""
    if engine is None:
        return {"name": child_id, "source": "unknown"}
    try:
        child = engine.get(child_id)
    except ValueError:
        return {"name": child_id, "source": "unknown"}
    return {"name": str(child.meta.get("name") or child_id), "source": child.source}


def _strategy_detail(
    s: StrategyDef,
    overrides: dict | None = None,
    engine: StrategyEngine | None = None,
) -> dict:
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
        "execution_backend": s.execution_backend,
        "asset_types": s.meta.get("asset_types", ["stock"]),
        "timeframes": s.meta.get("timeframes", ["1d"]),
        "version": s.meta.get("version", "1.0.0"),
        "basic_filter": bf,
        "params": s.meta.get("params", []),
        "params_defaults": params_defaults,
        "scoring": scoring,
        "entry_signals": overrides.get("entry_signals", s.entry_signals) if overrides else s.entry_signals,
        "exit_signals": overrides.get("exit_signals", s.exit_signals) if overrides else s.exit_signals,
        "minute_exit_trigger_supported_signals": sorted(MINUTE_EXIT_TRIGGER_SIGNALS),
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
        # 叠加策略: 子策略列表与权重(供前端展示)。override.children 可覆盖 META 固化值。
        "composite_children": (
            [
                {
                    "id": c["strategy_id"],
                    **_child_meta(engine, c["strategy_id"]),
                    "weight": c.get("weight", 1.0),
                }
                for c in (overrides.get("children") if overrides and isinstance(overrides.get("children"), list) else s.meta.get("children", []))
            ]
            if s.execution_backend == "composite"
            else None
        ),
    }


# ── Request Models ───────────────────────────────────────────────────


class RunRequest(BaseModel):
    strategy_id: str
    as_of: date | None = None
    pool: list[str] | None = None
    params: dict | None = None
    asset_type: str = "stock"
    timeframe: str = "1d"


class RunAllRequest(BaseModel):
    as_of: date | None = None
    asset_type: str = "stock"
    timeframe: str = "1d"


class SaveConfigRequest(BaseModel):
    strategy_id: str
    overrides: dict


class AIGenerateRequest(BaseModel):
    prompt: str


class AISaveRequest(BaseModel):
    code: str
    strategy_id: str
    name: str = ""
    description: str = ""


class StrategyCodeValidateRequest(BaseModel):
    code: str
    strategy_id: str = ""
    name: str = ""
    description: str = ""


class StrategyCodeSaveRequest(BaseModel):
    code: str
    strategy_id: str
    target_source: Literal["ai", "custom"] = "custom"
    mode: Literal["create", "update"] = "create"
    name: str = ""
    description: str = ""


class CompositeChildItem(BaseModel):
    strategy_id: str
    weight: float = 1.0


class StrategyCompositeSaveRequest(BaseModel):
    strategy_id: str
    name: str = ""
    description: str = ""
    children: list[CompositeChildItem]
    merge_mode: Literal["union", "intersect"] = "union"
    min_confirm: int = 0
    mode: Literal["create", "update"] = "create"


class MonitorStartRequest(BaseModel):
    strategy_id: str


# ── 列表 / 详情 ─────────────────────────────────────────────────────


@router.get("")
def list_strategies(
    request: Request,
    asset_type: str | None = None,
    timeframe: str | None = None,
):
    engine = _get_engine(request)
    data_dir = _data_dir(request)
    all_overrides = strategy_config.list_overrides(data_dir)

    result = []
    for meta in engine.list_strategies():
        if asset_type and asset_type not in meta.get("asset_types", ["stock"]):
            continue
        if timeframe and timeframe not in meta.get("timeframes", ["1d"]):
            continue
        sid = meta["id"]
        s = engine.get(sid)
        overrides = all_overrides.get(sid)
        result.append(_strategy_detail(s, overrides, engine))
    return {"strategies": result, "load_errors": engine.load_errors()}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str, request: Request):
    engine = _get_engine(request)
    try:
        s = engine.get(strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    overrides = strategy_config.load_override(_data_dir(request), strategy_id)
    return _strategy_detail(s, overrides or None, engine)


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
        svc = ScreenerService(request.app.state.repo, asset_type=req.asset_type)
        as_of = svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    try:
        from app.services.screener import ScreenerService
        svc = ScreenerService(request.app.state.repo, asset_type=req.asset_type)
        context = svc.build_strategy_context(
            engine,
            as_of,
            [req.strategy_id],
            timeframe=req.timeframe,
            params_map={req.strategy_id: params},
            overrides_map={req.strategy_id: overrides or {}},
        )
        result = engine.run(
            req.strategy_id,
            context,
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
        svc = ScreenerService(request.app.state.repo, asset_type=req.asset_type)
        as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "results": {}}

    all_overrides = strategy_config.list_overrides(data_dir)
    strategy_ids = [
        meta["id"]
        for meta in engine.list_strategies()
        if req.asset_type in meta.get("asset_types", ["stock"])
        and req.timeframe in meta.get("timeframes", ["1d"])
    ]
    from app.services.screener import ScreenerService
    svc = ScreenerService(request.app.state.repo, asset_type=req.asset_type)
    params_map = {
        sid: dict((all_overrides.get(sid) or {}).get("params") or {})
        for sid in strategy_ids
    }
    context = svc.build_strategy_context(
        engine,
        as_of,
        strategy_ids,
        timeframe=req.timeframe,
        params_map=params_map,
        overrides_map={sid: all_overrides.get(sid, {}) for sid in strategy_ids},
    )
    results: dict[str, dict] = {}
    for sid, result in engine.run_all(
        context,
        params_map=params_map,
        overrides_map={sid: all_overrides.get(sid, {}) for sid in strategy_ids},
        strategy_ids=strategy_ids,
    ).items():
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
    execution_backend: Literal["polars_expr", "matrix_native"] = "polars_expr"
    # step2 字段
    current_code: str = ""
    instruction: str = ""


def _py_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _find_meta_dict(code: str) -> ast.Dict:
    found = find_meta_assignment(code)
    if found is None:
        raise ValueError("找不到 META 字典")
    return found[1]


def _set_meta_string_field(block: str, field: str, value: str) -> str:
    pattern = re.compile(
        rf"(?m)^(\s*[\"']{re.escape(field)}[\"']\s*:\s*)([\"'])(?:\\.|[^\n\\])*?\2"
    )
    next_block, count = pattern.subn(
        lambda m: f"{m.group(1)}{_py_string(value)}",
        block,
        count=1,
    )
    if count:
        return next_block

    lines = block.splitlines(keepends=True)
    key_indent = None
    for line in lines:
        m = re.match(r"^(\s*)[\"'][^\"']+[\"']\s*:", line)
        if m:
            key_indent = m.group(1)
            break
    if key_indent is None:
        first_indent = re.match(r"^(\s*)", lines[0] if lines else "")
        key_indent = (first_indent.group(1) if first_indent else "") + "    "

    insert_at = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("}"):
            insert_at = i
            break
    for i in range(insert_at - 1, -1, -1):
        if not lines[i].strip():
            continue
        body = lines[i].rstrip("\r\n")
        if body.rstrip() and not body.rstrip().endswith((",", "{")):
            newline = lines[i][len(body):]
            lines[i] = body.rstrip() + "," + newline
        break
    lines.insert(insert_at, f'{key_indent}"{field}": {_py_string(value)},\n')
    return "".join(lines)


def _normalize_strategy_meta(code: str, strategy_id: str,
                             name: str | None = None,
                             description: str | None = None) -> str:
    """Force persisted strategy identity to match the caller-owned identity."""
    found = find_meta_assignment(code)
    if found is None:
        raise ValueError("找不到 META 字典")
    target, meta_node = found
    if target.id != "META":
        lines = code.splitlines(keepends=True)
        index = target.lineno - 1
        raw_line = lines[index].encode("utf-8")
        lines[index] = (
            raw_line[:target.col_offset]
            + b"META"
            + raw_line[target.end_col_offset:]
        ).decode("utf-8")
        code = "".join(lines)
        meta_node = _find_meta_dict(code)

    lines = code.splitlines(keepends=True)
    start = meta_node.lineno - 1
    end = meta_node.end_lineno or meta_node.lineno
    block = "".join(lines[start:end])

    fields = {"id": strategy_id}
    if name:
        fields["name"] = name
    if description:
        fields["description"] = description
    for field, value in fields.items():
        block = _set_meta_string_field(block, field, value)

    lines[start:end] = block.splitlines(keepends=True)
    return "".join(lines)


def _normalize_build_result(result: dict, strategy_id: str, name: str = "",
                            description: str = "") -> dict:
    if not result.get("valid") or not strategy_id:
        return result
    try:
        code = _normalize_strategy_meta(
            result.get("code", ""),
            strategy_id,
            name.strip() or None,
            description.strip() or None,
        )
        return {**result, "code": code, "meta": AIStrategyGenerator._extract_meta(code)}
    except Exception as e:
        return {**result, "valid": False, "error": f"规范化 META 失败: {e}"}


def _validate_strategy_id(strategy_id: str) -> str:
    sid = (strategy_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise ValueError("strategy_id 仅允许字母、数字、下划线、短横线")
    return sid


def _target_dir(data_dir: Path, source: str) -> Path:
    if source not in {"ai", "custom", "composite"}:
        raise ValueError("target_source 必须是 ai、custom 或 composite")
    return data_dir / "strategies" / source


def _prepare_strategy_code(req: StrategyCodeValidateRequest | StrategyCodeSaveRequest) -> dict:
    sid = _validate_strategy_id(req.strategy_id) if req.strategy_id else ""
    code = req.code
    if sid:
        current_meta = AIStrategyGenerator._extract_meta(code)
        needs_normalize = (
            current_meta.get("id") != sid
            or bool(req.name.strip())
            or bool(req.description.strip())
        )
        if needs_normalize:
            code = _normalize_strategy_meta(
                code,
                sid,
                req.name.strip() or None,
                req.description.strip() or None,
            )
    # 安全校验始终执行 (此前 strict 字段可被客户端设 false 绕过, 已移除)
    AIStrategyGenerator._validate_safety(code)
    meta = AIStrategyGenerator._extract_meta(code)
    AIStrategyGenerator._validate_meta_semantics(code, meta)
    return {"code": code, "meta": meta}


def _restore_strategy_file(path: Path, previous_code: str | None) -> None:
    if previous_code is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(previous_code, encoding="utf-8")


def _save_strategy_code(req: StrategyCodeSaveRequest, request: Request, *, legacy_ai_path: bool = False) -> dict:
    sid = _validate_strategy_id(req.strategy_id)
    if legacy_ai_path:
        if not (sid.startswith("ai_") or sid.startswith("custom_")):
            raise ValueError("策略 ID 必须以 ai_ 或 custom_ 开头")

    engine = _get_engine(request)
    data_dir = _data_dir(request)
    existing: StrategyDef | None = None
    try:
        existing = engine.get(sid)
    except ValueError:
        existing = None

    if not legacy_ai_path and req.mode == "create":
        if req.target_source == "ai" and not sid.startswith("ai_"):
            raise ValueError("AI 策略 ID 必须以 ai_ 开头")
        if req.target_source == "custom" and not sid.startswith("custom_"):
            raise ValueError("自定义策略 ID 必须以 custom_ 开头")

    if legacy_ai_path:
        out_dir = _target_dir(data_dir, "ai")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{sid}.py"
        expected_source = "ai"
    elif req.mode == "update":
        if existing is None:
            raise ValueError(f"策略 {sid} 不存在")
        if existing.source == "builtin":
            raise ValueError("内置策略不可覆盖，请另存为自定义策略")
        path = existing.file_path
        expected_source = existing.source
    else:
        if existing is not None:
            raise ValueError(f"策略 {sid} 已存在，请改用修改模式或换一个策略 ID")
        source_dir = "ai" if legacy_ai_path else req.target_source
        out_dir = _target_dir(data_dir, source_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{sid}.py"
        expected_source = "ai" if legacy_ai_path else req.target_source

    if path is None:
        raise ValueError("策略源文件不存在")
    path.parent.mkdir(parents=True, exist_ok=True)

    prepared = _prepare_strategy_code(req)
    previous_code = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(prepared["code"], encoding="utf-8")

    try:
        engine.reload()
        loaded = engine.get(sid)
        if loaded.file_path is None or loaded.file_path.resolve() != path.resolve():
            raise ValueError("策略加载到了非预期文件，请检查是否存在重复 strategy_id")
        if loaded.source != expected_source:
            raise ValueError(f"策略来源异常: 期望 {expected_source}, 实际 {loaded.source}")
    except Exception as e:
        _restore_strategy_file(path, previous_code)
        engine.reload()
        raise ValueError(f"策略保存失败: {e}") from e

    _invalidate_strategy_runtime(request)

    return {
        "ok": True,
        "strategy_id": sid,
        "source": expected_source,
        "path": str(path),
        "meta": prepared["meta"],
    }


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
        return {
            "ok": False,
            "error": str(e) or repr(e),
            "error_type": type(e).__name__,
        }


def _build_prompt(req: BuildRequest) -> str:
    if req.step == 1:
        return build_step1(
            req.name,
            req.description,
            req.direction,
            req.rules,
            req.strategy_id,
            req.execution_backend,
        )
    if req.step == 2:
        return build_step2(req.current_code, req.instruction)
    raise ValueError(f"无效步骤: {req.step}")


@router.post("/build")
async def build_strategy(req: BuildRequest, request: Request):
    """两步策略构建。
    step1: name + description + direction + rules → 完整策略
    step2: current_code + instruction → 修改任意部分
    """
    gen = AIStrategyGenerator()

    try:
        prompt = _build_prompt(req)
        result = await gen.generate(prompt)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if req.step == 1:
        result = _normalize_build_result(result, req.strategy_id, req.name, req.description)
    elif req.strategy_id:
        result = _normalize_build_result(result, req.strategy_id)
    return result


@router.post("/build/stream")
async def build_strategy_stream(req: BuildRequest, request: Request):
    try:
        prompt = _build_prompt(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    async def event_generator():
        gen = AIStrategyGenerator()
        chunks: list[str] = []
        yield json.dumps({"type": "meta", "strategy_id": req.strategy_id, "step": req.step}, ensure_ascii=False) + "\n"
        try:
            async for chunk in gen.stream(prompt):
                chunks.append(chunk)
                yield json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False) + "\n"
            result = gen.validate_code("".join(chunks))
            if gen.needs_structural_repair(result):
                result = await gen.repair_code(result["code"], result["error"])
            if req.step == 1:
                result = _normalize_build_result(result, req.strategy_id, req.name, req.description)
            elif req.strategy_id:
                result = _normalize_build_result(result, req.strategy_id)
            yield json.dumps({"type": "result", **result}, ensure_ascii=False) + "\n"
        except RuntimeError as e:
            yield json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": f"AI生成失败: {e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")



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


@router.post("/code/validate")
def validate_strategy_code(req: StrategyCodeValidateRequest, request: Request):
    try:
        prepared = _prepare_strategy_code(req)
        return {"valid": True, "error": None, **prepared}
    except Exception as e:
        return {"valid": False, "error": str(e), "code": req.code, "meta": {}}


@router.post("/code/save")
def save_strategy_code(req: StrategyCodeSaveRequest, request: Request):
    try:
        return _save_strategy_code(req, request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _render_composite_code(
    sid: str,
    name: str,
    description: str,
    children: list[dict],
    merge_mode: str,
    min_confirm: int,
) -> str:
    """渲染声明式 composite 策略 .py 文件内容。

    composite 不含业务代码, 仅通过 META.children 引用子策略 + EXECUTION_BACKEND 声明。
    权重固化在 META; merge_mode/min_confirm 作为 params(可经 override 轻量调整)。
    """
    import json as _json

    children_json = ",\n        ".join(
        _json.dumps({"strategy_id": c["strategy_id"], "weight": c["weight"]}, ensure_ascii=False)
        for c in children
    )
    safe_name = name or sid
    return f'''"""叠加策略 {sid}（自动生成, 请勿手改业务逻辑）。"""
META = {{
    "id": {sid!r},
    "name": {safe_name!r},
    "description": {description!r},
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {{"id": "merge_mode", "label": "合并模式", "type": "select",
          "options": ["union", "intersect"], "default": {merge_mode!r}}},
        {{"id": "min_confirm", "label": "交集最少确认数", "type": "int",
          "default": {int(min_confirm)!r}, "min": 0}},
    ],
    "scoring": {{}},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "children": [
        {children_json}
    ],
}}
EXECUTION_BACKEND = "composite"
'''


def _save_composite_strategy(req: StrategyCompositeSaveRequest, request: Request) -> dict:
    """保存叠加策略: 渲染声明式 .py → 写盘 → reload → 校验。"""
    sid = _validate_strategy_id(req.strategy_id)
    if not sid.startswith("composite_"):
        raise ValueError("叠加策略 ID 必须以 composite_ 开头")

    engine = _get_engine(request)
    data_dir = _data_dir(request)

    existing: StrategyDef | None = None
    try:
        existing = engine.get(sid)
    except ValueError:
        existing = None

    if req.mode == "create":
        if existing is not None:
            raise ValueError(f"策略 {sid} 已存在，请改用修改模式或换一个策略 ID")
    else:  # update
        if existing is None:
            raise ValueError(f"策略 {sid} 不存在")
        if existing.source == "builtin":
            raise ValueError("内置策略不可覆盖")
        # 只允许覆盖 composite 策略(防止把普通策略覆盖成 composite)
        if existing.execution_backend != "composite":
            raise ValueError("目标策略不是叠加策略，无法以叠加模式覆盖")

    if not req.children:
        raise ValueError("叠加策略至少需要一个子策略")

    children = [{"strategy_id": c.strategy_id, "weight": c.weight} for c in req.children]
    # 子策略存在性预检(给出清晰错误, 而非等到 reload 后孤儿移除的笼统报错)。
    for c in children:
        if not engine.has(c["strategy_id"]):
            raise ValueError(f"子策略 {c['strategy_id']!r} 不存在")
        try:
            child_def = engine.get(c["strategy_id"])
            if child_def.execution_backend == "composite":
                raise ValueError(f"子策略 {c['strategy_id']!r} 也是叠加策略; 首版禁止嵌套叠加")
        except ValueError:
            raise

    code = _render_composite_code(
        sid, req.name, req.description, children, req.merge_mode, req.min_confirm
    )

    out_dir = _target_dir(data_dir, "composite")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sid}.py"
    previous_code = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(code, encoding="utf-8")

    try:
        engine.reload()
        loaded = engine.get(sid)
        if loaded.file_path is None or loaded.file_path.resolve() != path.resolve():
            raise ValueError("策略加载到了非预期文件，请检查是否存在重复 strategy_id")
        if loaded.source != "composite":
            raise ValueError(f"策略来源异常: 期望 composite, 实际 {loaded.source}")
        if loaded.execution_backend != "composite":
            raise ValueError("策略后端异常: 期望 composite")
    except Exception as e:
        _restore_strategy_file(path, previous_code)
        engine.reload()
        raise ValueError(f"叠加策略保存失败: {e}") from e

    _invalidate_strategy_runtime(request)

    return {
        "ok": True,
        "strategy_id": sid,
        "source": "composite",
        "path": str(path),
    }


@router.post("/composite/save")
def save_composite_strategy(req: StrategyCompositeSaveRequest, request: Request):
    try:
        return _save_composite_strategy(req, request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/ai/save")
async def ai_save(req: AISaveRequest, request: Request):
    try:
        save_req = StrategyCodeSaveRequest(
            code=req.code,
            strategy_id=req.strategy_id,
            target_source="ai",
            mode="create",
            name=req.name,
            description=req.description,
            strict=True,
        )
        result = _save_strategy_code(save_req, request, legacy_ai_path=True)
        return {"ok": True, "path": result["path"]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request):
    """删除自定义策略 — 清除源文件、运行时注册和关联状态。内置策略不可删除。"""

    engine = _get_engine(request)
    try:
        s = engine.get(strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 不存在") from e

    if s.source == "builtin":
        raise HTTPException(status_code=403, detail="内置策略不可删除")

    # 删除被引用的子策略会令叠加策略加载失败; 删除前 fail-closed 阻止。
    dependents = engine.find_dependents(strategy_id)
    if dependents:
        raise HTTPException(
            status_code=409,
            detail=f"该策略被叠加策略 {dependents} 引用，请先解除引用后再删除",
        )

    path = s.file_path
    data_dir = _data_dir(request)
    if path is None or s.source not in {"custom", "ai", "composite"}:
        raise HTTPException(status_code=400, detail="策略源文件路径无效, 无法删除")

    try:
        allowed_dir = (data_dir / "strategies" / s.source).resolve()
        resolved_path = path.resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=409, detail=f"无法访问策略文件: {e}") from e
    if not resolved_path.is_relative_to(allowed_dir):
        raise HTTPException(status_code=400, detail="策略源文件不在用户策略目录, 拒绝删除")

    try:
        resolved_path.unlink(missing_ok=True)
    except OSError as e:
        reason = e.strerror or str(e)
        raise HTTPException(
            status_code=409,
            detail=(
                f"无法删除策略文件 {resolved_path.name}: {reason}。"
                "请确认数据目录可写; Docker 部署请检查数据卷不是只读挂载。"
            ),
        ) from e

    # 删除只影响当前策略, 全量 reload 会让其他损坏或重复 ID 的文件阻塞本次删除。
    engine.unregister(strategy_id)
    warnings = _cleanup_deleted_strategy(request, strategy_id)
    return {"ok": True, "warnings": warnings}


# ── 监控 ─────────────────────────────────────────────────────────────
# 注: 策略监控已统一迁移到 MonitorRuleEngine (监控通知页), 旧的 start/stop/status
# 路由已移除。StrategyMonitorService 类保留 (其 _check_signals 被 MonitorRuleEngine 复用)。


# ── 热重载 ───────────────────────────────────────────────────────────


@router.post("/reload")
def reload_strategies(request: Request):
    engine = _get_engine(request)
    try:
        engine.reload()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _invalidate_strategy_runtime(request)
    return {"ok": True, "count": len(engine.list_strategies())}
