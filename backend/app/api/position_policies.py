"""用户自定义仓位策略 API — 定义页「仓位策略」与回测设置的数据面。

GET    /api/backtest/position-policies               → 列表 (含坏文件错误表)
GET    /api/backtest/position-policies/templates     → 新建模板 (空白/波动率目标/回撤自适应)
GET    /api/backtest/position-policies/{id}          → 单条 (META + 用户代码区, 编辑回显)
POST   /api/backtest/position-policies/validate      → 三关校验 (安全/结构/冒烟), 不落盘
POST   /api/backtest/position-policies               → 保存 (三关全过才写盘)
DELETE /api/backtest/position-policies/{id}          → 删除
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.backtest import position_policies as pp
from app.config import settings

router = APIRouter(prefix="/api/backtest/position-policies", tags=["backtest"])


def _data_dir(request: Request) -> Path:
    override = getattr(request.app.state, "data_dir", None)
    return Path(override) if override else Path(settings.data_dir)


class PolicySaveRequest(BaseModel):
    id: str | None = Field(default=None, pattern=r"^up_[a-z0-9_]{1,40}$")
    label: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)
    params: list[dict] = Field(default_factory=list)
    # 用户代码区: def compute(ctx) (+ 可选 import/辅助函数)。编辑已有文件时
    # 可整体改; 留空且提供 id 时表示未改动, 由服务端从文件解析。
    code: str = Field(min_length=1, max_length=pp.MAX_CODE_CHARS)


@router.get("")
def list_position_policies(request: Request) -> dict:
    policies, errors = pp.list_policies(_data_dir(request))
    return {"policies": policies, "errors": errors}


@router.get("/templates")
def list_templates() -> dict:
    return {"templates": pp.TEMPLATES}


@router.get("/{policy_id}")
def get_position_policy(policy_id: str, request: Request) -> dict:
    d = _data_dir(request)
    path = d / pp.POLICY_DIR / f"{policy_id}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"仓位策略不存在: {policy_id}")
    try:
        code = path.read_text(encoding="utf-8")
        meta, user_code = pp.parse_policy_file(code)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"策略文件解析失败: {e}") from e
    return {"id": policy_id, "meta": meta, "code": user_code}


@router.post("/validate")
def validate_position_policy(req: PolicySaveRequest, request: Request) -> dict:
    """保存前预检: 安全校验 + 结构解析 + 合成冒烟, 不落盘。"""
    policy_id = req.id or ""
    existing_code = None
    if policy_id:
        path = _data_dir(request) / pp.POLICY_DIR / f"{policy_id}.py"
        if path.exists():
            existing_code = path.read_text(encoding="utf-8")
    if existing_code is not None and req.code.strip() == "__unchanged__":
        code = existing_code
    else:
        meta = {"id": policy_id or "up_preview", "label": req.label,
                "description": req.description, "params": req.params}
        code = pp.render_policy_file(meta, req.code)
    result = pp.validate_policy_code(code)
    return {"ok": result["ok"], "issues": result["issues"], "smoke": result["smoke"]}


@router.post("")
def save_position_policy(req: PolicySaveRequest, request: Request) -> dict:
    meta = {"id": req.id or "", "label": req.label,
            "description": req.description, "params": req.params}
    if req.code.strip() == "__unchanged__" and req.id:
        path = _data_dir(request) / pp.POLICY_DIR / f"{req.id}.py"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"仓位策略不存在: {req.id}")
        return {"ok": True, "id": req.id, "issues": []}
    result = pp.save_policy(_data_dir(request), meta, req.code)
    if not result["ok"]:
        raise HTTPException(status_code=422, detail={"message": "校验未通过", "issues": result["issues"]})
    return result


@router.delete("/{policy_id}")
def delete_position_policy(policy_id: str, request: Request) -> dict:
    if not pp.delete_policy(_data_dir(request), policy_id):
        raise HTTPException(status_code=404, detail=f"仓位策略不存在: {policy_id}")
    return {"ok": True}
