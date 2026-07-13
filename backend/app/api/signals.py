"""自定义信号 API 路由 — HTTP 请求 → 调用 custom_signals 模块 → 返回响应。

只做胶水：校验 → 持久化 → 失效缓存。不含表达式编译逻辑。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import custom_signals

router = APIRouter(prefix="/api/custom-signals", tags=["custom-signals"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _invalidate() -> None:
    """失效 pipeline 的自定义信号缓存，下次计算重新加载。"""
    from app.indicators.pipeline import invalidate_custom_signals
    invalidate_custom_signals()


class ConditionModel(BaseModel):
    left: str        # 字段名（须在白名单）
    op: str          # > >= < <= == !=
    right: str       # "field:xxx" 或数字字符串
    leftDays: int = 0    # 左字段取几日前 (0=当日, 默认)
    rightDays: int = 0   # 右字段取几日前 (仅 right 为字段时有意义)


class SignalModel(BaseModel):
    id: str
    name: str
    kind: str        # entry | exit | both
    conditions: list[ConditionModel]
    enabled: bool = True


# ── 字段选项 / 运算符 ───────────────────────────────────


@router.get("/options")
def get_options():
    """返回可选字段与运算符，供前端下拉框使用。"""
    # 字段带中文标签（取自 ENRICHED_COLUMNS，回退为字段名本身）
    from app.indicators.pipeline import ENRICHED_COLUMNS, ENRICHED_COLUMNS_BY_CATEGORY

    allowed = custom_signals.ALLOWED_FIELDS
    fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(allowed)
    ]
    # 字段分组 (只包含白名单内的字段, 供前端 optoptgroup 渲染)
    _GROUP_LABELS = {
        "basic": "基础", "ma": "均线 MA", "ema": "指数均线 EMA",
        "macd": "MACD", "boll": "布林带 BOLL", "kdj": "KDJ",
        "atr": "ATR", "volume": "量价", "extremes": "极值",
        "momentum": "动量", "volatility": "波动率", "rsi": "RSI",
    }
    # 行情类字段不在 ENRICHED_COLUMNS_BY_CATEGORY 里, 单独归一组
    quote_fields = {"open", "high", "low", "close", "volume", "amount",
                    "turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs"}
    groups = [{"key": "quote", "label": "行情",
               "fields": [{"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
                          for f in sorted(allowed & quote_fields)]}]
    for cat, label in _GROUP_LABELS.items():
        cat_fields = [f for f in ENRICHED_COLUMNS_BY_CATEGORY.get(cat, []) if f in allowed]
        if cat_fields:
            groups.append({"key": cat, "label": label,
                           "fields": [{"key": f, "label": ENRICHED_COLUMNS.get(f, f)} for f in cat_fields]})

    return {
        "fields": fields,
        "groups": groups,
        "maxDays": custom_signals.MAX_DAYS,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "kinds": [
            {"key": "entry", "label": "入场"},
            {"key": "exit", "label": "出场"},
            {"key": "both", "label": "出入通用"},
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
