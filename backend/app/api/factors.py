"""因子注册表 API — 因子库 (P1) + 公式校验/试算 (P2) + 自定义/复合因子 CRUD (P3)。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.market_time import cn_today

from app.factors import store
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import all_factors, unregister_factor

router = APIRouter(prefix="/api/factors", tags=["factors"])


@router.get("")
def list_factors(asset_type: str | None = Query(default=None, pattern="^(stock|etf)$")) -> dict:
    """注册表因子列表; asset_type 过滤适用资产 (财务因子仅股票)。"""
    specs = all_factors(asset_type=asset_type)
    return {
        "factors": [
            {
                "id": spec.id,
                "label": spec.label,
                "group": spec.group,
                "kind": spec.kind,
                "version": spec.version,
                "formula": spec.formula_text,
                "direction": spec.direction,
                "unit": spec.unit,
                "warmup_bars": spec.warmup_bars,
                "pit": spec.pit,
                "asset_types": sorted(spec.asset_types),
                "stability": spec.stability,
                "scale_free": spec.scale_free,
                "dependencies": sorted(spec.dependencies),
            }
            for spec in specs
        ]
    }


class FormulaValidateRequest(BaseModel):
    formula: str = Field(..., min_length=1, max_length=2000)


def _compiled_payload(compiled) -> dict:
    return {
        "ok": compiled.ok,
        "errors": [error.to_dict() for error in compiled.errors],
        "dependencies": sorted(compiled.dependencies),
        "referenced_factors": sorted(compiled.referenced_factors),
        "warmup_bars": compiled.warmup_bars,
        "cross_sectional": compiled.cross_sectional,
    }


@router.post("/validate")
def validate_formula(req: FormulaValidateRequest) -> dict:
    """公式校验: 语法/语义/窗口纪律/依赖推导, 编译期 fail-closed。"""
    return _compiled_payload(compile_formula(req.formula))


class FormulaTrialRequest(FormulaValidateRequest):
    asset_type: str = Field(default="stock", pattern="^(stock|etf)$")
    days: int = Field(default=40, ge=20, le=120)


@router.post("/trial")
def trial_formula(req: FormulaTrialRequest, request: Request) -> dict:
    """公式试算: 最近 N 个交易日截面 Rank IC 快照 (复用回测面板与虚拟因子物化路径)。"""
    compiled = compile_formula(req.formula)
    if not compiled.ok:
        raise HTTPException(status_code=400, detail={"errors": [error.to_dict() for error in compiled.errors]})

    from app.api.backtest import _get_engine

    # 交易日 → 自然日换算 (A股年均 243 交易日 ≈ 1.48 自然日/交易日), 留 buffer
    calendar_days = int((compiled.warmup_bars + req.days) * 1.6) + 15
    start = cn_today() - timedelta(days=calendar_days)
    # 面板基础物理列 (load_panel 只返回 parquet 物理列, 因子列由补算路径生成)
    base_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    if "consecutive_limit_ups" in compiled.dependencies:
        base_columns.append("consecutive_limit_ups")
    engine = _get_engine(request)
    panel = engine.load_panel(None, start, cn_today(), columns=base_columns, asset_type=req.asset_type)
    if panel.is_empty():
        raise HTTPException(status_code=400, detail="当前数据目录无可用历史数据, 无法试算")

    # 复用检验引擎同一条补算路径 (compute_indicators + 虚拟因子物化), 禁止第二套计算逻辑
    from app.backtest.factor import FactorBacktestService

    physical = set(panel.columns)
    to_compute = set(compiled.referenced_factors) | {
        dep for dep in compiled.dependencies if dep not in physical
    }
    if to_compute:
        panel = FactorBacktestService._compute_missing_factors(panel, to_compute)

    if compiled.frame_transform is None:
        raise HTTPException(status_code=500, detail="编译产物缺少帧变换")
    prepared = compiled.frame_transform(panel)
    if prepared is None:
        raise HTTPException(
            status_code=400,
            detail={"errors": [{
                "code": "E013", "message": "依赖列不可用: 面板缺少公式所需列",
                "position": {"offset": 0, "line": 1},
                "detail": {"missing": sorted((compiled.dependencies | compiled.referenced_factors) - set(panel.columns))},
            }]},
        )

    total_rows = panel.height
    frame = (
        prepared
        .with_columns(
            (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1.0).alias("_next_return")
        )
        .filter(pl.col(FACTOR_COLUMN).is_not_null())
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
    )
    non_null_rows = frame.height
    if non_null_rows == 0:
        return {
            "ok": True, "n_dates": 0, "null_ratio": 1.0,
            "ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None,
            "ic_series": [], "message": "试算区间内公式输出全为空 (检查预热窗口与数据范围)",
        }

    ic_frame = (
        frame.filter(pl.col("_next_return").is_not_null())
        .group_by("date")
        .agg(
            pl.corr(pl.col(FACTOR_COLUMN).rank(method="average"), pl.col("_next_return").rank(method="average")).alias("ic"),
            pl.len().alias("n_symbols"),
        )
        # 截面值全相同时 pl.corr 返回 NaN (非 null), 与 FactorBacktestService 同口径剔除
        .filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
        .sort("date")
        .tail(req.days)
    )
    ic_series = [
        {"date": str(row["date"]), "ic": round(row["ic"], 4), "n_symbols": row["n_symbols"]}
        for row in ic_frame.to_dicts()
    ]
    if ic_frame.is_empty():
        return {
            "ok": True, "n_dates": 0, "null_ratio": round(1.0 - non_null_rows / max(total_rows, 1), 4),
            "ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None,
            "ic_series": [], "message": "无有效 IC 截面 (需每期 ≥2 只标的)",
        }
    stats = ic_frame.select(
        pl.col("ic").mean().alias("mean"),
        pl.col("ic").std(ddof=0).alias("std"),
        (pl.col("ic") > 0).mean().alias("win"),
    ).row(0, named=True)
    ic_std = stats["std"]
    # Newey-West t (lag=1): 与检验页同源口径, 样本过少时不给 (fail-closed)
    t_newey_west = None
    if ic_frame.height >= 5:
        from app.backtest.stats_v2 import newey_west_t

        values = ic_frame["ic"].to_numpy()
        nw = newey_west_t(values, lag=1)
        if nw is not None:
            t_newey_west = round(float(nw[0]), 3)
    return {
        "ok": True,
        "n_dates": ic_frame.height,
        "null_ratio": round(1.0 - non_null_rows / max(total_rows, 1), 4),
        "ic_mean": round(stats["mean"], 4),
        "ic_std": None if ic_std is None else round(ic_std, 4),
        "ir": None if not ic_std or ic_std == 0 else round(stats["mean"] / ic_std, 3),
        "ic_win_rate": round(stats["win"], 4),
        "t_newey_west": t_newey_west,
        "ic_series": ic_series,
    }


# ── 自定义/复合因子 CRUD (P3) ──────────────────────────────


class CustomFactorCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="自定义", max_length=16)
    formula: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


class CompositeFactorCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="组合", max_length=16)
    members: dict[str, float] = Field(..., min_length=2, max_length=8)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


def _data_dir(request: Request):
    from pathlib import Path

    data_dir = getattr(getattr(request.app.state, "repo", None), "store", None)
    root = getattr(data_dir, "data_dir", None) if data_dir is not None else None
    if root is None:
        raise HTTPException(status_code=500, detail="数据目录不可用")
    return Path(root)


def _slugify_id(label: str, prefix: str) -> str:
    base = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in label.lower())
    candidate = f"{prefix}_{base}".strip("_")[:44]
    import re

    candidate = re.sub(r"_+", "_", candidate)
    return candidate or f"{prefix}_f"


def _resolve_id(requested: str | None, label: str, prefix: str) -> str:
    return requested.strip() if requested and requested.strip() else _slugify_id(label, prefix)


def _next_version(data_dir, factor_id: str) -> int:
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            return int(definition.get("version", 1)) + 1
    return 1


def _trial_nonempty(request: Request, formula: str, asset_type: str = "stock") -> None:
    """保存前置校验: 公式在最近 40 个交易日有非空输出 (设计 §3.5, fail-closed)。"""
    compiled = compile_formula(formula)
    if not compiled.ok:
        raise HTTPException(status_code=400, detail={"errors": [e.to_dict() for e in compiled.errors]})
    from app.api.backtest import _get_engine
    from app.backtest.factor import FactorBacktestService

    calendar_days = int((compiled.warmup_bars + 40) * 1.6) + 15
    base_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    engine = _get_engine(request)
    panel = engine.load_panel(None, cn_today() - timedelta(days=calendar_days), cn_today(), columns=base_columns, asset_type=asset_type)
    if panel.is_empty():
        raise HTTPException(status_code=400, detail="当前无历史数据, 无法完成保存前试算 (fail-closed)")
    physical = set(panel.columns)
    to_compute = set(compiled.referenced_factors) | {d for d in compiled.dependencies if d not in physical}
    if to_compute:
        panel = FactorBacktestService._compute_missing_factors(panel, to_compute)
    prepared = compiled.frame_transform(panel) if compiled.frame_transform else None
    if prepared is None or prepared[FACTOR_COLUMN].is_not_null().sum() == 0:
        raise HTTPException(status_code=400, detail="公式在最近 40 个交易日输出全为空, 拒绝保存")


@router.post("/custom")
def create_custom_factor(req: CustomFactorCreateRequest, request: Request) -> dict:
    """保存自定义公式因子: 编译通过 + 服务端试算非空 (fail-closed)。"""
    data_dir = _data_dir(request)
    factor_id = _resolve_id(req.id, req.label, "uf")
    definition = {
        "id": factor_id,
        "kind": "custom",
        "version": _next_version(data_dir, factor_id),
        "label": req.label,
        "group": req.group,
        "formula": req.formula,
        "description": req.description,
        "direction": req.direction,
        "status": "draft",
        "created_at": store._now(),
        "updated_at": store._now(),
    }
    try:
        store.to_spec(definition)  # 先做 schema/id/编译校验
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _trial_nonempty(request, req.formula)
    try:
        store.register_definition(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, definition)
    return {"ok": True, "id": factor_id, "version": definition["version"]}


@router.post("/composite")
def create_composite_factor(req: CompositeFactorCreateRequest, request: Request) -> dict:
    """保存复合因子: 成员校验 + 循环引用检查 (无需试算, 值由成员物化路径计算)。"""
    data_dir = _data_dir(request)
    factor_id = _resolve_id(req.id, req.label, "cf")
    definition = {
        "id": factor_id,
        "kind": "composite",
        "version": _next_version(data_dir, factor_id),
        "label": req.label,
        "group": req.group,
        "members": req.members,
        "description": req.description,
        "direction": req.direction,
        "status": "draft",
        "created_at": store._now(),
        "updated_at": store._now(),
    }
    try:
        store.register_definition(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, definition)
    return {"ok": True, "id": factor_id, "version": definition["version"]}


class CustomFactorUpdateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="自定义", max_length=16)
    formula: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


@router.post("/custom/{factor_id}/update")
def update_custom_factor(factor_id: str, req: CustomFactorUpdateRequest, request: Request) -> dict:
    """编辑已有自定义因子: 编译校验 + 试算非空 (与创建同一门禁) → 版本提升注册。

    公式变化时状态回 draft (生命周期语义: 编辑后需重新检验激活); 仅改名称/分组保留状态。
    """
    data_dir = _data_dir(request)
    target = None
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            target = definition
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
    if str(target.get("kind", "custom")) != "custom":
        raise HTTPException(status_code=400, detail=f"仅自定义因子支持公式编辑 (kind={target.get('kind')})")
    formula_changed = str(target.get("formula")) != req.formula
    if formula_changed:
        _trial_nonempty(request, req.formula)
    target.update({
        "label": req.label,
        "group": req.group,
        "formula": req.formula,
        "description": req.description,
        "direction": req.direction,
        "version": int(target.get("version", 1)) + 1,  # 版本提升 → 注册表允许覆盖
        "status": "draft" if formula_changed else str(target.get("status", "draft")),
        "updated_at": store._now(),
    })
    try:
        store.register_definition(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, target)
    return {"ok": True, "id": factor_id, "version": target["version"], "status": target["status"]}


def _find_references(data_dir, factor_id: str) -> list[str]:
    """扫描策略与复合因子定义中的引用 (删除前 fail-closed 检查)。"""
    references: list[str] = []
    strategies_dir = data_dir / "strategies"
    if strategies_dir.is_dir():
        for file in strategies_dir.glob("*.json"):
            try:
                text = file.read_text(encoding="utf-8")
                if factor_id in text:
                    references.append(f"strategies/{file.name}")
            except OSError:
                continue
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            continue
        members = definition.get("members")
        if isinstance(members, dict) and factor_id in members:
            references.append(f"custom_factors/{definition.get('id')}.json")
    return references


@router.delete("/custom/{factor_id}")
def delete_custom_factor(factor_id: str, request: Request, force: bool = Query(default=False)) -> dict:
    """删除自定义/复合因子; 有引用时列出引用方并拒绝 (需 force)。"""
    data_dir = _data_dir(request)
    from app.factors.registry import get_factor

    # 引用检查必须排在存在性判定之前: 下面用来探测「盘上是否有定义」的
    # store.delete_one 本身就会删文件, 反过来会出现「拒绝删除」但定义已被删掉。
    references = _find_references(data_dir, factor_id)
    if references and not force:
        raise HTTPException(
            status_code=409,
            detail={"message": "该因子仍有引用, 拒绝删除 (可带 force=true 强制)", "references": references},
        )
    if get_factor(factor_id) is None and not store.delete_one(data_dir, factor_id):
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    try:
        unregister_factor(factor_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.delete_one(data_dir, factor_id)
    return {"ok": True, "id": factor_id, "removed_references": references}


class FactorStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(draft|active|watch|retired)$")


@router.post("/custom/{factor_id}/status")
def update_factor_status(factor_id: str, req: FactorStatusRequest, request: Request) -> dict:
    """生命周期状态迁移 (P4): draft->active->watch->retired, 编辑后回 draft。"""
    data_dir = _data_dir(request)
    target = None
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            target = definition
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
    target["status"] = req.status
    target["updated_at"] = store._now()
    try:
        # 动态因子先注销再注册: 元数据变更 (status/group) 不提升版本,
        # 直接 register 会因"版本未提升"被拒 (启动加载后的真实路径)
        unregister_factor(factor_id)
        store.register_definition(target)  # 状态与 stability 联动
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, target)
    return {"ok": True, "id": factor_id, "status": req.status}


class FactorGroupRequest(BaseModel):
    group: str = Field(..., min_length=1, max_length=24)


@router.post("/custom/{factor_id}/group")
def update_factor_group(factor_id: str, req: FactorGroupRequest, request: Request) -> dict:
    """修改单个自定义/复合因子的分组 (内置因子分组与快照/预设绑定, 不可改)。"""
    data_dir = _data_dir(request)
    group = req.group.strip()
    if not group:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    target = None
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            target = definition
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
    target["group"] = group
    target["updated_at"] = store._now()
    try:
        unregister_factor(factor_id)
        store.register_definition(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, target)
    return {"ok": True, "id": factor_id, "group": group}
