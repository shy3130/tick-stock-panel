"""门禁引擎 — 结构红线 (硬编码, 不可配置/不可关闭) + 用户规则读写。

结构红线 (YMOS §5.1 照搬): 服务端强制判定, 任何前端都绕不过。
用户规则 (gate_rules.json): 勾选清单, 前端决策台渲染, 服务端不强制 —— 只做结构校验后存取。

evaluate_gates 是纯评估 (不落盘): 返回 {passed, gates, missing}。
API 层据结果决定 422 拦截 / confirmed 绕过留痕 / 放行 (见 api/trading.py)。

NAV 口径: 用 portfolio.compute_snapshot(trades, accounts, prices={}) 计算。
价格全 None (stale) 时 NAV = capital + realized (浮动盈亏按 0 保守处理),
这是结构红线想要的保守方向 —— NAV 偏低 → 比例偏高 → 更容易触发拦截。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from app.services.trading import store
from app.services.trading.accounts import read_accounts
from app.services.trading.portfolio import compute_snapshot

SCHEMA_VERSION = 1

# mode → 该模式下需判定的结构红线 id (顺序即输出顺序)
# prepare/revise 无结构红线 (建仓准备不改变资金/仓位事实), 不在此表 → passed=true, gates=[]
_GATE_SPECS: dict[str, list[str]] = {
    "buy_new": ["single_position_ratio", "stop_loss_defined", "stop_loss_distance", "horizon_match"],
    "add": ["single_position_ratio"],
    "adjust": ["stop_loss_distance"],
    "fill": ["fill_reconciliation"],
    "tp": [],
    "sl": [],
    "close": [],
}

_GATE_NAMES: dict[str, str] = {
    "single_position_ratio": "单标的比例",
    "stop_loss_defined": "止损/退出条件已定义",
    "stop_loss_distance": "止损距离为正",
    "horizon_match": "资金期限匹配",
    "fill_reconciliation": "计划/成交对账",
}

_FILL_DEVIATION_THRESHOLD = 0.10  # fill 金额 vs 计划金额允许偏差 10%

_RULE_MODES = ("buy_new", "add", "tp", "sl", "close", "adjust")
_RULE_LISTS = ("all", "any", "discipline")


# ── 结构红线评估 ─────────────────────────────────────────
def evaluate_gates(
    data_dir: Path,
    mode: str,
    *,
    trade: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估指定 mode 的全部结构红线。纯函数, 不落盘。

    返回 {"passed": bool, "gates": [{"id","name","passed","detail"}], "missing": [未过 id]}。
    无适用红线的 mode (tp/sl/close/prepare/revise) → passed=true, gates=[]。
    """
    payload = payload or {}
    gate_ids = _GATE_SPECS.get(mode, [])
    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for gid in gate_ids:
        res = _GATE_FUNCS[gid](data_dir, trade, payload)
        res["id"] = gid
        res["name"] = _GATE_NAMES.get(gid, gid)
        results.append(res)
        if not res["passed"]:
            missing.append(gid)
    passed = all(r["passed"] for r in results) if results else True
    return {"passed": passed, "gates": results, "missing": missing}


# ── 各结构红线 ───────────────────────────────────────────
def _gate_single_position_ratio(
    data_dir: Path, trade: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    amount = _buy_amount(payload)
    if amount is None or amount <= 0:
        return {"passed": True, "detail": "未提供买入金额,跳过比例校验"}
    accounts = read_accounts(data_dir)
    trades = store.list_trades(data_dir)
    snap = compute_snapshot(trades, accounts, prices={})
    nav = snap["nav"]
    max_single = snap["maxSingleRatio"]
    if nav <= 0:
        return {"passed": False, "detail": f"NAV={nav},无可用资金,买入金额 {amount} 超限"}
    ratio = amount / nav
    if ratio <= max_single:
        return {"passed": True, "detail": f"买入金额 {amount:.0f} / NAV {nav:.0f} = {ratio:.1%} ≤ maxSingleRatio {max_single:.0%}"}
    return {"passed": False, "detail": f"买入金额 {amount:.0f} / NAV {nav:.0f} = {ratio:.1%} > maxSingleRatio {max_single:.0%}"}


def _gate_stop_loss_defined(
    data_dir: Path, trade: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    stop = _positive(payload.get("stopLoss")) or (trade and _positive(trade.get("stopLoss")))
    if stop:
        return {"passed": True, "detail": f"止损价 {stop}"}
    rule = _str(payload.get("exitRule")) or (trade and _str(trade.get("exitRule")))
    if rule:
        return {"passed": True, "detail": f"逻辑退出条件: {rule}"}
    thesis = payload.get("thesis")
    if not isinstance(thesis, dict) and trade:
        thesis = trade.get("thesis")
    invalidation = _str(thesis.get("invalidation")) if isinstance(thesis, dict) else ""
    if invalidation:
        return {"passed": True, "detail": "失效信号作为退出条件 (thesis.invalidation)"}
    return {"passed": False, "detail": "缺少止损价或逻辑退出条件 (stopLoss/exitRule/thesis.invalidation)"}


def _gate_stop_loss_distance(
    data_dir: Path, trade: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    # adjust 优先取 newStopLoss; 否则取 stopLoss (buy_new)
    stop = _positive(payload.get("newStopLoss"))
    if stop is None:
        stop = _positive(payload.get("stopLoss")) or (trade and _positive(trade.get("stopLoss")))
    if stop is None:
        return {"passed": True, "detail": "无价格止损,跳过距离校验"}
    # 参考价: 买入价 (price/plannedPrice) 优先, 否则回退成本价 (adjust 用现有成本)
    ref = _positive(payload.get("price")) or _positive(payload.get("plannedPrice"))
    if ref is None and trade:
        pos = trade.get("position") or {}
        ref = _positive(pos.get("costPrice"))
    if ref is None or ref <= 0:
        return {"passed": True, "detail": f"止损价 {stop},无参考买入价/成本,跳过距离校验"}
    distance = (ref - stop) / ref
    if distance > 0:
        return {"passed": True, "detail": f"止损 {stop} < 参考 {ref},距离 {distance:.1%}"}
    return {"passed": False, "detail": f"止损 {stop} ≥ 参考 {ref},距离 {distance:.1%} 非正"}


def _gate_horizon_match(
    data_dir: Path, trade: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    declared = payload.get("thesisHorizonMonths")
    if declared is None:
        return {"passed": True, "detail": "未声明 thesisHorizonMonths,跳过期限校验"}
    declared = _positive(declared)
    if declared is None:
        return {"passed": True, "detail": "thesisHorizonMonths 非正数值,跳过期限校验"}
    accounts = read_accounts(data_dir)
    accs = accounts.get("accounts") or []
    limit = accs[0].get("horizonFundMonths") if accs else 12
    if declared <= limit:
        return {"passed": True, "detail": f"声明期限 {declared:g} 月 ≤ 账户期限 {limit} 月"}
    return {"passed": False, "detail": f"声明期限 {declared:g} 月 > 账户期限 {limit} 月"}


def _gate_fill_reconciliation(
    data_dir: Path, trade: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    if not trade:
        return {"passed": True, "detail": "无单笔上下文,跳过对账"}

    plan = trade.get("plan") or {}
    plan_amount = _positive(plan.get("total"))
    if plan_amount is None:
        planned_qty = _positive(plan.get("qty"))
        planned_price = _positive(plan.get("price"))
        if planned_qty and planned_price:
            plan_amount = planned_qty * planned_price
    if plan_amount is None:
        events = store.read_events(data_dir, trade.get("tradeId"))
        for event in reversed(events):
            if event.get("kind") not in ("prepare", "revise"):
                continue
            event_payload = event.get("payload") or {}
            planned_qty = _positive(event_payload.get("plannedQty"))
            planned_price = _positive(event_payload.get("plannedPrice"))
            if planned_qty and planned_price:
                plan_amount = planned_qty * planned_price
                break
    if plan_amount is None or plan_amount <= 0:
        return {"passed": True, "detail": "无建仓计划金额,跳过对账"}

    build = trade.get("build") or {}
    filled_before = _positive(build.get("filledAmount")) or 0.0
    finalize_only = payload.get("finalizeOnly") is True
    if finalize_only:
        fill_amount = 0.0
    else:
        qty = _positive(payload.get("qty"))
        price = _positive(payload.get("price"))
        if not (qty and price):
            return {"passed": True, "detail": "未提供 fill qty/price,跳过对账"}
        fill_amount = qty * price
    cumulative = filled_before + fill_amount
    deviation = abs(cumulative - plan_amount) / plan_amount

    if payload.get("complete") is not True and not finalize_only:
        if cumulative <= plan_amount * (1 + _FILL_DEVIATION_THRESHOLD):
            return {
                "passed": True,
                "detail": f"累计成交进度 {cumulative / plan_amount:.1%}，尚未收口",
            }
    if deviation <= _FILL_DEVIATION_THRESHOLD:
        return {"passed": True, "detail": f"累计偏差 {deviation:.1%} ≤ {_FILL_DEVIATION_THRESHOLD:.0%}"}
    reason = _str(payload.get("reconcileReason"))
    if reason:
        return {
            "passed": True,
            "detail": f"累计偏差 {deviation:.1%} > {_FILL_DEVIATION_THRESHOLD:.0%},已填写对账原因",
        }
    return {
        "passed": False,
        "detail": f"累计偏差 {deviation:.1%} > {_FILL_DEVIATION_THRESHOLD:.0%},需填写 reconcileReason",
    }


_GATE_FUNCS: dict[str, Callable[..., dict[str, Any]]] = {
    "single_position_ratio": _gate_single_position_ratio,
    "stop_loss_defined": _gate_stop_loss_defined,
    "stop_loss_distance": _gate_stop_loss_distance,
    "horizon_match": _gate_horizon_match,
    "fill_reconciliation": _gate_fill_reconciliation,
}


# ── 用户规则 (gate_rules.json) — 结构校验后存取,不强制判定 ──
_lock = threading.Lock()


def _rules_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "trading" / "gate_rules.json"


def _default_rules() -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "rules": {m: {"all": [], "any": [], "discipline": []} for m in _RULE_MODES},
    }


def read_gate_rules(data_dir: Path) -> dict[str, Any]:
    """读取用户规则。文件缺失/损坏 → 返回全空清单的默认结构。"""
    p = _rules_path(data_dir)
    if not p.exists():
        return _default_rules()
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {}
    except (OSError, json.JSONDecodeError):
        return _default_rules()
    if not isinstance(data, dict):
        return _default_rules()
    rules = data.get("rules")
    if not isinstance(rules, dict):
        rules = {}
    return {"schemaVersion": SCHEMA_VERSION, "rules": _normalize_rules(rules)}


def write_gate_rules(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """校验结构并落盘用户规则。校验失败抛 ValueError (调用方转 HTTP 400)。"""
    if not isinstance(payload, dict):
        raise ValueError("gate_rules 必须是对象")
    rules = payload.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("rules 必须是对象")
    normalized = _normalize_rules(rules)
    out = {"schemaVersion": SCHEMA_VERSION, "rules": normalized}
    p = _rules_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    return out


def _normalize_rules(rules: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in _RULE_MODES:
        section = rules.get(mode)
        out[mode] = _normalize_section(section if isinstance(section, dict) else {})
    return out


def _normalize_section(section: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _RULE_LISTS:
        items = section.get(key)
        out[key] = _normalize_items(items) if isinstance(items, list) else []
    return out


def _normalize_items(items: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "").strip()
        text = str(it.get("text") or "").strip()
        if iid and text:
            out.append({"id": iid, "text": text})
    return out


# ── 工具 ─────────────────────────────────────────────────
def _positive(v: Any) -> float | None:
    """正数值 → float; None/非数值/≤0 → None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _str(v: Any) -> str:
    return str(v or "").strip()


def _buy_amount(payload: dict[str, Any]) -> float | None:
    """从 payload 推断本次买入金额: amount → qty×price → plannedQty×plannedPrice。"""
    amt = _positive(payload.get("amount"))
    if amt:
        return amt
    for qk, pk in (("qty", "price"), ("plannedQty", "plannedPrice")):
        q = _positive(payload.get(qk))
        p = _positive(payload.get(pk))
        if q and p:
            return q * p
    return None
