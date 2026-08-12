"""监控规则 API 路由 — HTTP 请求 → 调用 monitor_rules 模块 → 同步引擎内存态。

只做胶水: 校验 → 持久化 → 失效引擎内存态。不含评估逻辑。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import monitor_rules

router = APIRouter(prefix="/api/monitor-rules", tags=["monitor-rules"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _sync_engine(request: Request) -> None:
    """保存/删除后,把最新规则集 reload 到引擎内存态。"""
    engine = getattr(request.app.state, "monitor_engine", None)
    if engine is not None:
        rules = monitor_rules.load_all(_data_dir(request))
        engine.set_rules(rules)


# ── Pydantic 模型 ───────────────────────────────────────
class ConditionModel(BaseModel):
    field: str
    op: str            # truth | > >= < <= == !=
    value: float | None = None   # op 非 truth 时必填


class RuleModel(BaseModel):
    id: str
    name: str
    enabled: bool = True
    type: str          # strategy | signal | price | market
    scope: str = "symbols"   # symbols | all  (sector 已禁用: validate 拒绝保存, 引擎 fail-closed)
    symbols: list[str] = []
    sector: str | None = None
    strategy_id: str | None = None
    direction: str = "entry"  # entry | exit | both
    conditions: list[ConditionModel] = []
    logic: str = "and"        # and | or
    cooldown_seconds: int = 3600
    severity: str = "info"    # info | warn | critical
    webhook_url: str = ""     # Webhook 推送地址 (推送到 QMT 等外部软件, 待定)
    webhook_enabled: bool = False
    message: str = ""


# ── 字段选项 ─────────────────────────────────────────────
@router.get("/options")
def get_options(request: Request):
    """返回可选字段、信号列、运算符、枚举,供前端表单使用。"""
    from app.indicators.pipeline import ENRICHED_COLUMNS
    from app.strategy.custom_signals import ALLOWED_FIELDS, load_all as load_csg

    # 阈值字段 (带中文标签)
    threshold_fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(ALLOWED_FIELDS)
    ]
    # 内置信号列 (布尔, 用于 op=truth)
    builtin_signals = [
        {"key": k, "label": v}
        for k, v in ENRICHED_COLUMNS.items()
        if k.startswith("signal_")
    ]
    # 自定义信号列 (csg_)
    custom_sigs = []
    try:
        for cs in load_csg(_data_dir(request)):
            if cs.get("enabled") is not False:
                custom_sigs.append({
                    "key": f"csg_{cs['id']}",
                    "label": cs.get("name", cs["id"]),
                })
    except Exception:
        pass

    return {
        "threshold_fields": threshold_fields,
        "builtin_signals": builtin_signals,
        "custom_signals": custom_sigs,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "types": [
            {"key": "signal", "label": "个股信号"},
            {"key": "price", "label": "价格/涨跌"},
            {"key": "market", "label": "市场异动"},
            {"key": "strategy", "label": "策略监控"},
        ],
        "scopes": [
            {"key": "symbols", "label": "指定股票"},
            {"key": "all", "label": "全市场"},
            # sector 已移除: 板块精确过滤未实现, 不向新规则提供。
            # 编辑历史 sector 规则时, 前端额外渲染一个 disabled 选项。
        ],
        "logics": [
            {"key": "and", "label": "全部满足 (AND)"},
            {"key": "or", "label": "任一满足 (OR)"},
        ],
        "severities": [
            {"key": "info", "label": "普通"},
            {"key": "warn", "label": "警告"},
            {"key": "critical", "label": "重要"},
        ],
        "directions": [
            {"key": "entry", "label": "买入"},
            {"key": "exit", "label": "卖出"},
            {"key": "both", "label": "买卖都报"},
        ],
    }


# ── 列表 ───────────────────────────────────────────────
@router.get("")
def list_rules(request: Request):
    rules = monitor_rules.load_all(_data_dir(request))
    # 按 created_at 倒序
    rules.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"rules": rules}


# ── 新建 / 更新 ────────────────────────────────────────
@router.post("")
def save_rule(req: RuleModel, request: Request):
    rule = monitor_rules.normalize(req.model_dump())
    # 编辑现有规则时, 保留原 created_at (避免按时间排序时位置跳动)
    existing = monitor_rules.load_one(_data_dir(request), rule["id"])
    if existing and existing.get("created_at"):
        rule["created_at"] = existing["created_at"]
    try:
        monitor_rules.validate(rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    monitor_rules.save_one(_data_dir(request), rule)
    _sync_engine(request)
    return {"ok": True, "rule": rule}


# ── 删除 ───────────────────────────────────────────────
@router.delete("/{rule_id}")
def delete_rule(rule_id: str, request: Request):
    if not monitor_rules.ID_RE.match(rule_id):
        raise HTTPException(status_code=400, detail="规则 id 非法")
    deleted = monitor_rules.delete_one(_data_dir(request), rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="规则不存在")
    _sync_engine(request)
    return {"ok": True}


# ── 演示数据生成 (仅 Dev 页用) ─────────────────────────

import time as _time
from datetime import datetime, timezone


def _demo_rule(rule_id: str, name: str, rtype: str, scope: str, symbols: list[str],
               conditions: list[dict], logic: str = "or", cooldown: int = 3600,
               severity: str = "info", message: str = "",
               strategy_id: str | None = None, direction: str = "entry") -> dict:
    rule = monitor_rules.normalize({
        "id": rule_id,
        "name": name,
        "type": rtype,
        "scope": scope,
        "symbols": symbols,
        "conditions": conditions,
        "logic": logic,
        "cooldown_seconds": cooldown,
        "severity": severity,
        "message": message,
        "enabled": True,
    })
    if rtype == "strategy":
        rule["strategy_id"] = strategy_id
        rule["direction"] = direction
    return rule


_DEMO_RULES_TEMPLATE = [
    ("个股信号 · 茅台放量突破", "signal", "symbols", ["600519.SH"],
     [{"field": "signal_volume_surge", "op": "truth"},
      {"field": "signal_n_day_high", "op": "truth"}], "or", "info"),
    ("个股信号 · 宁德金叉", "signal", "symbols", ["300750.SZ"],
     [{"field": "signal_ma_golden_5_20", "op": "truth"}], "or", "info"),
    ("价格 · 平安跌幅监控", "price", "symbols", ["000001.SZ"],
     [{"field": "change_pct", "op": "<", "value": -0.03}], "or", "warn", "warn"),
    ("价格 · 比亚迪RSI超卖", "price", "symbols", ["002594.SZ"],
     [{"field": "rsi_14", "op": "<", "value": 30}], "and", "warn", "warn"),
    ("市场异动 · 全市场涨停", "market", "all", [],
     [{"field": "signal_limit_up", "op": "truth"}], "or", "critical", "critical"),
    ("市场异动 · 全市场炸板", "market", "all", [],
     [{"field": "signal_broken_limit_up", "op": "truth"}], "or", "warn", "warn"),
    ("市场异动 · 跌幅超5%", "market", "all", [],
     [{"field": "change_pct", "op": "<", "value": -0.05}], "or", "warn", "warn"),
    ("个股信号 · 茅台跌破MA20", "signal", "symbols", ["600519.SH"],
     [{"field": "signal_ma20_breakdown", "op": "truth"}], "or", "info"),
]

# 策略类型单独声明 (格式不同: 含 strategy_id + direction)
_DEMO_STRATEGY_RULES: list[dict] = [
    {"name": "策略监控 · 趋势突破", "strategy_id": "trend_breakout", "direction": "entry"},
    {"name": "策略监控 · MACD金叉", "strategy_id": "macd_golden", "direction": "both"},
]


@router.post("/seed")
def seed_demo_rules(request: Request):
    """生成演示监控规则 (Dev 页用)。覆盖 signal/price/market/strategy 四类。"""
    ts = int(_time.time() * 1000)
    created = []
    i = 0
    for (name, rtype, scope, symbols, conditions, logic, severity, sev) in _DEMO_RULES_TEMPLATE:
        rule_id = f"demo_{ts}_{i}"
        rule = _demo_rule(rule_id, name, rtype, scope, symbols, conditions, logic, 3600, sev)
        monitor_rules.save_one(_data_dir(request), rule)
        created.append(rule_id)
        i += 1
    # 策略类型规则
    for sr in _DEMO_STRATEGY_RULES:
        rule_id = f"demo_{ts}_{i}"
        rule = _demo_rule(
            rule_id, sr["name"], "strategy", "all", [], [], "and", 3600, "info",
            strategy_id=sr["strategy_id"], direction=sr.get("direction", "entry"),
        )
        monitor_rules.save_one(_data_dir(request), rule)
        created.append(rule_id)
        i += 1
    _sync_engine(request)
    return {"ok": True, "generated": len(created), "ids": created}
