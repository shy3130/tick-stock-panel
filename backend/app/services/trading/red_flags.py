"""机械红旗检测 — 纯代码,无 LLM。

输入某 trade 的完整事件流 + 审计流,输出红旗列表。
先按事件顺序 replay 维护每个时点的 costPrice,再逐事件检测机械红旗:

P3 三条基础红旗(事件流 + 审计流驱动,纯函数 scan_trade_events):
1. 放宽止损: adjust 事件新止损距离 > 旧止损距离(向上抬高/收紧不报)
2. 亏损加仓: add(非 planOnly) 加仓价 < 当时成本价
3. 绕过门禁: fill/add/tp/sl/close 事件 gateBypassed=true,或审计断链

P6.1 新增红旗(依赖 profile/账户;无 profile/无账户 → skip 不 fail):
4. horizon_exceeded(单笔级): 持仓天数 > profile.risk.thesisHorizonMonths × 30
5. size_over_limit(单笔级): fill/add 成交后市值 > 账户 maxSingleRatio×NAV
   或 profile.risk.positionLimitPct/100×NAV
6. gate_proliferation(全局级): 用户规则清单总条数 > 15 → scan_all 返回 "global" 分组

赚钱的违规也照记 —— 红旗与盈亏无关。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.json_safe import finite_float_or_none
from app.services.strategy_profile import read_profile
from app.services.trading import accounts as accounts_store
from app.services.trading import gates as gates_store
from app.services.trading import store
from app.services.trading.models import (
    KIND_ADD,
    KIND_ADJUST,
    KIND_CLOSE,
    KIND_FILL,
    KIND_OPEN,
    KIND_SL,
    KIND_TP,
    KIND_TRIM,
    KIND_VOID,
)

# 需要审计留痕的事件类型(审计断链一律告警)
_AUDITED_KINDS = frozenset({
    KIND_FILL, KIND_ADD, KIND_TRIM, KIND_TP, KIND_SL, KIND_ADJUST, KIND_CLOSE, KIND_VOID,
})

# 门禁膨胀阈值(用户规则清单总条数超过即告警)
_GATE_PROLIFERATION_THRESHOLD = 15

_LABELS_LIMIT = {"account": "账户", "strategy": "策略"}


def scan_trade_events(
    events: list[dict[str, Any]],
    audit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """核心检测:输入事件流 + 审计流,输出红旗列表(按事件先后顺序)。

    纯函数,不读磁盘 —— 便于测试与组合。
    """
    # 审计 mode 集合(调用方已按 tradeId 过滤)
    audit_modes: set[str] = {str(a.get("mode", "")) for a in audit}

    flags: list[dict[str, Any]] = []
    cost_price = 0.0
    qty = 0.0
    add_phase = False

    for event in events:
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        ts = str(event.get("ts", ""))

        # ── 放宽止损 ──
        if kind == KIND_ADJUST:
            old_stop = payload.get("oldStopLoss")
            new_stop = payload.get("newStopLoss")
            if old_stop is not None and new_stop is not None and cost_price > 0:
                try:
                    old_dist = (cost_price - float(old_stop)) / cost_price
                    new_dist = (cost_price - float(new_stop)) / cost_price
                except (TypeError, ValueError):
                    old_dist = new_dist = 0.0
                if new_dist > old_dist:
                    flags.append({
                        "type": "stop_loss_widened",
                        "ts": ts,
                        "old": float(old_stop),
                        "new": float(new_stop),
                        "costPrice": round(cost_price, 4),
                    })

        # ── 亏损加仓 ──
        # 新模型中 add 只调大计划，随后的 fill 才是实际加仓；兼容旧 planOnly=false add。
        if kind == KIND_FILL and add_phase:
            price = payload.get("price")
            if price is not None and cost_price > 0:
                try:
                    if float(price) < cost_price:
                        flags.append({
                            "type": "loss_add",
                            "ts": ts,
                            "price": float(price),
                            "costPrice": round(cost_price, 4),
                        })
                except (TypeError, ValueError):
                    pass
        elif kind == KIND_ADD and not payload.get("planOnly"):
            price = payload.get("price")
            if price is not None and cost_price > 0:
                try:
                    if float(price) < cost_price:
                        flags.append({
                            "type": "loss_add",
                            "ts": ts,
                            "price": float(price),
                            "costPrice": round(cost_price, 4),
                        })
                except (TypeError, ValueError):
                    pass

        # ── 绕过门禁 / 审计断链 ──
        if kind in _AUDITED_KINDS:
            if event.get("gateBypassed"):
                flags.append({"type": "gate_bypassed", "ts": ts, "kind": kind})
            if kind not in audit_modes:
                flags.append({"type": "audit_missing", "ts": ts, "kind": kind})

        # ── 更新运行状态(AFTER 检测,保证 fill/adjust 用到的是当时成本) ──
        if kind == KIND_FILL:
            try:
                fill_price = float(payload.get("price", 0))
                fill_qty = float(payload.get("qty", 0))
                new_qty = qty + fill_qty
                if new_qty > 0 and fill_price > 0:
                    cost_price = (qty * cost_price + fill_qty * fill_price) / new_qty
                qty = new_qty
                if payload.get("complete") is True or payload.get("finalizeOnly") is True:
                    add_phase = False
            except (TypeError, ValueError):
                pass
        elif kind == KIND_ADD:
            if payload.get("planOnly"):
                add_phase = True
            else:
                try:
                    add_qty = float(payload.get("qty", 0))
                    add_price = float(payload.get("price", 0))
                    new_qty = qty + add_qty
                    if new_qty > 0:
                        cost_price = (qty * cost_price + add_qty * add_price) / new_qty
                    qty = new_qty
                except (TypeError, ValueError):
                    pass
        elif kind in (KIND_TP, KIND_SL):
            try:
                qty -= float(payload.get("qty", 0))
            except (TypeError, ValueError):
                pass
        elif kind == KIND_CLOSE:
            qty = 0.0

    return flags


# ── P6.1 纯函数检测器(不读磁盘,便于单测) ──────────────────


def _first_event_ts(events: list[dict[str, Any]], kinds: tuple[str, ...]) -> str | None:
    """返回首个匹配 kind 的事件 ts(无则 None)。"""
    for e in events:
        if str(e.get("kind", "")) in kinds:
            ts = str(e.get("ts", "")).strip()
            if ts:
                return ts
    return None


def _parse_ts(ts: str) -> datetime | None:
    """解析 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' / ISO 时间字符串。失败 → None。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def detect_horizon_exceeded(
    events: list[dict[str, Any]],
    horizon_months: float | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """检测持仓超期(单笔级,纯函数)。

    持仓天数 = 结束日 - 起始日:
    - 起始日 = 首个 fill 事件日期;无 fill 则首个 open 日期;都没有 → None
    - 结束日 = close 事件日期;未平仓则 today(可由 now 注入便于测试)
    超期: holding_days > horizon_months × 30
    horizon_months 为 None 或 ≤0 → None(skip:无 profile/无期限不 fail)。
    """
    if horizon_months is None or horizon_months <= 0:
        return None

    start_ts = _first_event_ts(events, (KIND_FILL, KIND_OPEN))
    if start_ts is None:
        return None
    start_dt = _parse_ts(start_ts)
    if start_dt is None:
        return None

    close_ts = _first_event_ts(events, (KIND_CLOSE,))
    if close_ts is not None:
        end_dt = _parse_ts(close_ts)
    else:
        end_dt = now or datetime.now()
    if end_dt is None:
        return None

    holding_days = (end_dt.date() - start_dt.date()).days
    limit_days = horizon_months * 30
    if holding_days <= limit_days:
        return None

    return {
        "type": "horizon_exceeded",
        "ts": _fmt_ts(end_dt),
        "detail": f"持仓 {holding_days} 天 > 声明期限 {horizon_months:g} 月(约 {limit_days:.0f} 天)",
        "holdingDays": holding_days,
        "horizonMonths": horizon_months,
        "limitDays": round(limit_days),
    }


def detect_size_over_limit(
    events: list[dict[str, Any]],
    capital: float | None,
    max_single_ratio: float | None,
    position_limit_pct: float | None,
) -> list[dict[str, Any]]:
    """检测单笔仓位超限(逐 fill/add 成交事件,纯函数)。

    replay 维护 qty / cost_price / realized_pnl;每当 fill 或 add(非 planOnly)成交后:
    - 市值 = qty × 当时事件价
    - NAV = capital + realized_pnl(静态口径:不含浮动盈亏,避免引行情,离线可复现;
      与 portfolio.py 一致,不含 changes 增资)
    超限口径(任一命中即红旗):
    1. 账户: 市值/NAV > max_single_ratio
    2. 策略: 市值/NAV > position_limit_pct/100
    capital ≤ 0(含无账户默认 capital=0)或无正数限额 → 全程 skip(不 fail)。
    """
    if capital is None or capital <= 0:
        return []
    has_account_limit = max_single_ratio is not None and max_single_ratio > 0
    has_strategy_limit = position_limit_pct is not None and position_limit_pct > 0
    if not has_account_limit and not has_strategy_limit:
        return []

    flags: list[dict[str, Any]] = []
    qty = 0.0
    cost_price = 0.0
    realized_pnl = 0.0

    for event in events:
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        ts = str(event.get("ts", ""))

        # ── 卖出:累计已实现盈亏 + 扣减持仓(先于后续买入的 NAV 计算) ──
        if kind == KIND_CLOSE:
            sell_price = finite_float_or_none(payload.get("price"))
            if sell_price is not None and cost_price > 0 and qty > 0:
                realized_pnl += (sell_price - cost_price) * qty
            qty = 0.0
            continue
        if kind in (KIND_TP, KIND_SL):
            sell_qty = finite_float_or_none(payload.get("qty")) or 0.0
            sell_price = finite_float_or_none(payload.get("price"))
            if sell_qty > 0 and sell_price is not None and cost_price > 0:
                realized_pnl += (sell_price - cost_price) * sell_qty
            qty = max(0.0, qty - sell_qty)
            continue

        # ── fill/add 成交:更新持仓后检测超限 ──
        if kind == KIND_FILL or (kind == KIND_ADD and not payload.get("planOnly")):
            price = finite_float_or_none(payload.get("price")) or 0.0
            add_qty = finite_float_or_none(payload.get("qty")) or 0.0
            new_qty = qty + add_qty
            if new_qty > 0 and price > 0:
                cost_price = (qty * cost_price + add_qty * price) / new_qty
            qty = new_qty

            if qty > 0 and price > 0:
                market_value = qty * price
                nav = capital + realized_pnl
                if nav > 0:
                    ratio = market_value / nav
                    breached: list[str] = []
                    if has_account_limit and ratio > max_single_ratio:
                        breached.append("account")
                    if has_strategy_limit and ratio > position_limit_pct / 100:
                        breached.append("strategy")
                    if breached:
                        flag: dict[str, Any] = {
                            "type": "size_over_limit",
                            "ts": ts,
                            "kind": kind,
                            "detail": _size_detail(market_value, nav, ratio, breached),
                            "marketValue": round(market_value, 2),
                            "nav": round(nav, 2),
                            "exposure": round(ratio, 4),
                            "breached": breached,
                        }
                        if has_account_limit:
                            flag["maxSingleRatio"] = max_single_ratio
                        if has_strategy_limit:
                            flag["positionLimitPct"] = position_limit_pct
                        flags.append(flag)

    return flags


def _size_detail(market_value: float, nav: float, ratio: float, breached: list[str]) -> str:
    labels = "/".join(_LABELS_LIMIT.get(b, b) for b in breached)
    return f"持仓市值 {market_value:.0f} / NAV {nav:.0f} = {ratio:.1%} 超 {labels}上限"


def detect_gate_proliferation(
    rule_count: int,
    threshold: int = _GATE_PROLIFERATION_THRESHOLD,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """检测门禁膨胀(全局级,纯函数)。

    用户规则清单总条数 > threshold → 红旗;否则 None。
    """
    if rule_count <= threshold:
        return None
    return {
        "type": "gate_proliferation",
        "ts": _fmt_ts(now or datetime.now()),
        "detail": f"用户规则清单共 {rule_count} 条 > {threshold} 条上限,存在门禁膨胀风险",
        "ruleCount": rule_count,
        "threshold": threshold,
    }


def count_gate_rules(rules_payload: dict[str, Any]) -> int:
    """统计用户规则清单总条数(all + any + discipline 三类求和)。"""
    total = 0
    sections = rules_payload.get("rules") or {}
    if not isinstance(sections, dict):
        return total
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        for key in ("all", "any", "discipline"):
            items = section.get(key)
            if isinstance(items, list):
                total += len(items)
    return total


# ── P6.1 磁盘读取辅助 ───────────────────────────────────
def _read_horizon_months(data_dir: Path, trade: dict[str, Any] | None) -> float | None:
    """trade.strategy → profile.risk.thesisHorizonMonths。无 strategy/profile/期限 → None。"""
    if not trade:
        return None
    strategy = str(trade.get("strategy") or "").strip()
    if not strategy:
        return None
    profile = read_profile(data_dir, strategy)
    if not isinstance(profile, dict):
        return None
    risk = profile.get("risk") or {}
    h = finite_float_or_none(risk.get("thesisHorizonMonths"))
    return h if (h is not None and h > 0) else None


def _read_position_limit_pct(data_dir: Path, trade: dict[str, Any] | None) -> float | None:
    """trade.strategy → profile.risk.positionLimitPct(百分比)。无 → None。"""
    if not trade:
        return None
    strategy = str(trade.get("strategy") or "").strip()
    if not strategy:
        return None
    profile = read_profile(data_dir, strategy)
    if not isinstance(profile, dict):
        return None
    risk = profile.get("risk") or {}
    pct = finite_float_or_none(risk.get("positionLimitPct"))
    return pct if (pct is not None and pct > 0) else None


def _scan_size_over_limit(
    data_dir: Path, trade: dict[str, Any] | None, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """读取账户 + profile,调用纯函数检测单笔仓位超限。无账户/无 profile → skip。"""
    accs = accounts_store.read_accounts(data_dir).get("accounts") or []
    acc = accs[0] if accs else {}
    capital = finite_float_or_none(acc.get("capital"))
    max_single = finite_float_or_none(acc.get("maxSingleRatio"))
    position_limit = _read_position_limit_pct(data_dir, trade)
    return detect_size_over_limit(events, capital, max_single, position_limit)


# ── 磁盘入口 ─────────────────────────────────────────────
def scan_trade(data_dir: Path, trade_id: str) -> list[dict[str, Any]]:
    """读取磁盘数据并检测单笔红旗(含 P6.1 新增 horizon_exceeded / size_over_limit)。"""
    events = store.read_events(data_dir, trade_id)
    # limit 取大值,保证单笔审计不被截断
    audit = store.read_audit(data_dir, trade_id, limit=10000)
    flags = scan_trade_events(events, audit)

    trade = store.read_trade(data_dir, trade_id)
    # 期限漂移:持仓天数 > 声明月数(无 profile/无期限 → skip)
    horizon = _read_horizon_months(data_dir, trade)
    hf = detect_horizon_exceeded(events, horizon)
    if hf:
        flags.append(hf)
    # 仓位超限:逐 fill/add 成交检测(无账户/无 profile → skip)
    flags.extend(_scan_size_over_limit(data_dir, trade, events))
    return flags


def scan_all(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """遍历全部 trades,按 tradeId 分组汇总红旗(仅含有红旗的笔)。

    新增 ``"global"`` 键承载全局级红旗(如 gate_proliferation);无全局红旗时不出现该键。
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for trade in store.list_trades(data_dir):
        flags = scan_trade(data_dir, trade["tradeId"])
        if flags:
            result[trade["tradeId"]] = flags

    # 全局级红旗:门禁膨胀
    rules = gates_store.read_gate_rules(data_dir)
    gp = detect_gate_proliferation(count_gate_rules(rules))
    if gp:
        result["global"] = [gp]

    return result
