"""自定义分析菜单 API。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/analysis-menus", tags=["analysis-menus"])


class AnalysisColumn(BaseModel):
    field: str
    label: str = ""
    type: Literal["string", "number", "percent", "amount", "date"] = "string"
    width: int | None = None
    sortable: bool = False
    precision: int | None = None
    format: str | None = None
    aggregate: Literal["count", "avg", "sum", "min", "max"] | None = None
    visible: bool = True


class DefaultSort(BaseModel):
    field: str
    order: Literal["asc", "desc"] = "desc"


class AnalysisMenu(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=64)
    icon: str = "chart"
    data_source: str = Field(..., min_length=1)
    template: Literal["dimension_rank", "ranking", "table"] = "dimension_rank"
    dimension_field: str | None = None
    rank_field: str | None = None
    group_columns: list[AnalysisColumn] = Field(default_factory=list)
    detail_columns: list[AnalysisColumn] = Field(default_factory=list)
    default_sort: DefaultSort | None = None
    visible: bool = True
    order: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    builtin: bool = False


class UpsertAnalysisMenu(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    icon: str = "chart"
    data_source: str = Field(..., min_length=1)
    template: Literal["dimension_rank", "ranking", "table"] = "dimension_rank"
    dimension_field: str | None = None
    rank_field: str | None = None
    group_columns: list[AnalysisColumn] = Field(default_factory=list)
    detail_columns: list[AnalysisColumn] = Field(default_factory=list)
    default_sort: DefaultSort | None = None
    visible: bool = True
    order: int = 0


class ReorderMenusReq(BaseModel):
    ids: list[str] = Field(..., min_length=1)


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _base_dir(request: Request) -> Path:
    return _data_dir(request) / "analysis_menus"


def _path(request: Request, menu_id: str) -> Path:
    return _base_dir(request) / f"{menu_id}.json"


def _load_saved(request: Request) -> list[AnalysisMenu]:
    base = _base_dir(request)
    if not base.exists():
        return []
    items: list[AnalysisMenu] = []
    for p in sorted(base.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            items.append(AnalysisMenu(**raw))
        except Exception:
            continue
    return items


def _ordered(items: list[AnalysisMenu]) -> list[AnalysisMenu]:
    return sorted(items, key=lambda m: (m.order, m.label, m.id))


def _save(request: Request, menu: AnalysisMenu) -> AnalysisMenu:
    now = datetime.now().isoformat()
    if not menu.created_at:
        menu.created_at = now
    menu.updated_at = now
    menu.builtin = False
    base = _base_dir(request)
    base.mkdir(parents=True, exist_ok=True)
    _path(request, menu.id).write_text(
        json.dumps(menu.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return menu


def _default_menus(request: Request) -> list[AnalysisMenu]:
    """自动生成的默认分析菜单。

    历史上会扫描扩展数据配置,对含「概念」字段的表自动生成一个「概念分析」菜单。
    现已关闭自动生成 —— 内置的概念分析页(/concept-analysis)已覆盖该场景,
    自动菜单会造成导航重复。需要时用户可在「设置 → 扩展页面」手动创建。
    """
    return []


@router.get("")
def list_menus(request: Request):
    saved = _load_saved(request)
    saved_ids = {m.id for m in saved}
    defaults = [m for m in _default_menus(request) if m.id not in saved_ids]
    return {"items": _ordered(saved + defaults)}


@router.get("/{menu_id}")
def get_menu(request: Request, menu_id: str):
    for menu in _ordered(_load_saved(request) + _default_menus(request)):
        if menu.id == menu_id:
            return menu
    raise HTTPException(404, f"分析菜单 '{menu_id}' 不存在")


@router.post("/reorder")
def reorder_menus(request: Request, body: ReorderMenusReq):
    saved = {m.id: m for m in _load_saved(request)}
    defaults = {m.id: m for m in _default_menus(request)}
    for idx, menu_id in enumerate(body.ids):
        menu = saved.get(menu_id) or defaults.get(menu_id)
        if not menu:
            continue
        menu.order = idx
        _save(request, menu)
    return {"items": _ordered(_load_saved(request))}


@router.post("/{menu_id}")
def upsert_menu(request: Request, menu_id: str, body: UpsertAnalysisMenu):
    if not menu_id.replace("_", "").isalnum():
        raise HTTPException(400, "菜单标识只能包含字母、数字和下划线")
    existing = next((m for m in _load_saved(request) if m.id == menu_id), None)
    menu = AnalysisMenu(
        id=menu_id,
        created_at=existing.created_at if existing else None,
        **body.model_dump(),
    )
    return _save(request, menu)


@router.delete("/{menu_id}")
def delete_menu(request: Request, menu_id: str):
    p = _path(request, menu_id)
    if not p.exists():
        raise HTTPException(404, f"分析菜单 '{menu_id}' 不存在或为默认菜单")
    p.unlink()
    return {"status": "deleted"}
