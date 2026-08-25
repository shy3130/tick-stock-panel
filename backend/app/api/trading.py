"""Trading API — 单笔交易生命周期 + 决策审计。

铁律 (照搬 YMOS):
1. 门禁未通过而用户仍确认动作 → 记 passed=false 审计 + 事件标记 gateBypassed
   (不得把被拦截误记成正常事件)
2. 只有事件成功落盘后才写 passed=true 审计
3. 审计写失败 → API 返回显式 500,不得静默吞掉
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.config import settings
from app.data_providers.registry import get_active_provider_name, get_provider
from app.services.trading import accounts as accounts_store
from app.services.trading import fhold_client, red_flag_webhook, red_flags, store
from app.services.trading.accounts import read_accounts, write_accounts
from app.services.trading.gates import evaluate_gates
from app.services.trading.lifecycle import apply_event, has_prepare_or_revise, new_trade, now_str
from app.services.trading.models import (
    EVENT_KINDS,
    KIND_CLOSE,
    KIND_FILL,
    KIND_OPEN,
    SCHEMA_VERSION,
    STATUS_BUILDING,
    STATUS_CLOSED,
    STATUS_HOLDING,
    LifecycleError,
)
from app.services.trading.portfolio import compute_risk_snapshot, compute_snapshot

router = APIRouter(prefix="/api/trading", tags=["trading"])
logger = logging.getLogger(__name__)

# event kind → 决策审计 mode (与 gates.py 的 _GATE_SPECS key 对齐)
_KIND_TO_MODE = {
    "fill": "fill",
    "add": "add",
    "trim": "trim",
    "tp": "tp",
    "sl": "sl",
    "adjust": "adjust",
    "close": "close",
    "void": "void",
}


def _audit_entry(mode: str, trade_id: str, symbol: str, passed: bool, gate: dict | None) -> dict:
    gate = gate or {}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ts": now_str(),
        "mode": mode,
        "tradeId": trade_id,
        "symbol": symbol,
        "passed": passed,
        "gates": gate.get("gates") or [],
        "missing": gate.get("missing") or [],
        "note": str(gate.get("note") or ""),
    }


def _write_audit(entry: dict) -> None:
    """审计写失败必须显式暴露 (铁律 3)。"""
    try:
        store.append_audit(settings.data_dir, entry)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"决策审计写入失败,本次动作未生效: {e}")


def _merge_gate(mode: str, trade: dict | None, eval_payload: dict[str, Any], client_gate: dict[str, Any] | None = None) -> dict[str, Any]:
    """服务端结构红线评估 + 客户端 gate 合并。结构红线失败 → passed=false。

    eval_payload: 事件级字段 (qty/price/stopLoss/reconcileReason 等), 喂给 evaluate_gates。
    client_gate: 外层 payload.gate (含 confirmed/gates/missing), 用户提供。
    返回的 gate 用于: passed=false 且未 confirmed → 422; confirmed → 落盘 + gateBypassed。
    gates/missing 以服务端结构红线评估结果为准 (客户端清单不覆盖结构判定)。
    """
    server = evaluate_gates(settings.data_dir, mode, trade=trade, payload=eval_payload)
    client = client_gate if isinstance(client_gate, dict) else {}
    confirmed = bool(client.get("confirmed"))
    # 服务端结构红线失败时以服务端结果为准; 全过时保留客户端 gates (用户规则清单)
    if not server["passed"]:
        merged = dict(server)
    else:
        merged = {
            "passed": True,
            "gates": client.get("gates") or server["gates"],
            "missing": client.get("missing") or [],
        }
    merged["confirmed"] = confirmed
    return merged


@router.post("/trades")
def open_trade(payload: Annotated[dict[str, Any], Body()]):
    """建档 (open): 买入论点 + 可观察失效信号。"""
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol 必填")
    day = datetime.now().strftime("%Y%m%d")
    trade_id = f"{symbol}_{day}_{store.next_trade_seq(settings.data_dir, symbol, day)}"
    ts = now_str()
    try:
        trade = new_trade(trade_id, symbol, payload, ts)
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    gate = _merge_gate("buy_new", trade, payload, payload.get("gate"))
    if not gate["passed"]:
        _write_audit(_audit_entry("buy_new", trade_id, symbol, False, gate))
        if not gate.get("confirmed"):
            raise HTTPException(status_code=422, detail="门禁未通过,动作未执行(已记入决策审计)")

    event = {
        "schemaVersion": SCHEMA_VERSION,
        "tradeId": trade_id,
        "kind": KIND_OPEN,
        "ts": ts,
        "payload": {
            "name": trade["name"],
            "strategy": trade["strategy"],
            "thesis": trade["thesis"],
            "stopLoss": trade["stopLoss"],
        },
        "note": str(payload.get("note") or ""),
        "gateBypassed": bool(gate.get("confirmed") and not gate["passed"]),
    }
    _persist(trade, event)
    _write_audit(_audit_entry("buy_new", trade_id, symbol, True, gate))
    return trade


@router.post("/trades/{trade_id}/events")
def append_event(trade_id: str, payload: Annotated[dict[str, Any], Body()]):
    """追加生命周期事件。

    支持分批 fill、add/trim 计划变更、零成交 void 与 close 平仓结转。
    payload.gate.confirmed=true 仍只表示用户确认绕过门禁，所有结构不变量不可关闭。
    """
    kind = str(payload.get("kind") or "").strip()
    if kind not in EVENT_KINDS or kind == KIND_OPEN:
        raise HTTPException(status_code=400, detail=f"kind 必须是 {'/'.join(k for k in EVENT_KINDS if k != KIND_OPEN)}")
    trade = store.read_trade(settings.data_dir, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="单笔交易不存在")

    # close 事件已落盘但账户结转失败时，客户端可原请求重试。
    # 此路径只修复幂等 settlement，不重复事件或审计。
    if kind == KIND_CLOSE and trade.get("status") == STATUS_CLOSED:
        try:
            accounts_store.settle_trade(settings.data_dir, trade, now_str())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"平仓结转写入失败: {e}")
        return trade

    mode = _KIND_TO_MODE.get(kind, kind)
    gate = _merge_gate(mode, trade, payload.get("payload") or {}, payload.get("gate"))
    if not gate["passed"]:
        _write_audit(_audit_entry(kind, trade_id, trade["symbol"], False, gate))
        if not gate.get("confirmed"):
            raise HTTPException(status_code=422, detail="门禁未通过,动作未执行(已记入决策审计)")

    if kind == KIND_FILL:
        events = store.read_events(settings.data_dir, trade_id)
        if not has_prepare_or_revise(events):
            raise HTTPException(status_code=400, detail="fill 之前必须已有 prepare/revise 建仓准备")

    ts = now_str()
    event_payload = dict(payload.get("payload") or {})
    try:
        updated = apply_event(trade, kind, event_payload, ts)
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    event = {
        "schemaVersion": SCHEMA_VERSION,
        "tradeId": trade_id,
        "kind": kind,
        "ts": ts,
        "payload": event_payload,
        "note": str(payload.get("note") or ""),
        "gateBypassed": bool(gate.get("confirmed") and not gate["passed"]),
    }
    _persist(updated, event)
    _write_audit(_audit_entry(kind, trade_id, trade["symbol"], True, gate))
    if kind == KIND_CLOSE:
        try:
            accounts_store.settle_trade(settings.data_dir, updated, ts)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"平仓已落盘，但结转失败: {e}")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"平仓已落盘，但结转写入失败: {e}")
    _notify_red_flags(trade_id)
    return updated


@router.get("/trades")
def list_trades(status: Annotated[str | None, Query()] = None):
    return {"trades": store.list_trades(settings.data_dir, status)}


@router.get("/trades/{trade_id}")
def get_trade(trade_id: str):
    trade = store.read_trade(settings.data_dir, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="单笔交易不存在")
    return {"trade": trade, "events": store.read_events(settings.data_dir, trade_id)}


@router.get("/audit")
def list_audit(
    trade_id: Annotated[str | None, Query()] = None,
    passed: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
):
    return {"audit": store.read_audit(settings.data_dir, trade_id, passed, limit)}


# ── P1: 账户模型 + 组合快照 ──────────────────────────────
@router.get("/accounts")
def get_accounts():
    return read_accounts(settings.data_dir)


@router.put("/accounts")
def put_accounts(payload: Annotated[dict[str, Any], Body()]):
    try:
        return write_accounts(settings.data_dir, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/portfolio")
def get_portfolio():
    """组合快照: 实时计算 NAV / 持仓 / 健康度。provider 不可用不报错。

    fhold 段: 来自 ../fhold (fhold-cli) 的真实券商持仓,只读接入;
    fhold 不可用时 available=False,不影响生命周期持仓快照。
    """
    accounts = read_accounts(settings.data_dir)
    trades = store.list_trades(settings.data_dir)
    prices = _fetch_prices(trades)
    snapshot = compute_snapshot(trades, accounts, prices)
    snapshot["fhold"] = fhold_client.fetch_holdings()
    return snapshot


@router.get("/portfolio/risk")
def get_portfolio_risk(
    request: Request,
    lookback_days: Annotated[int, Query(ge=20, le=500)] = 120,
):
    """基于 canonical 日 K 的当前持仓静态权重风险透视。"""
    trades = store.list_trades(settings.data_dir)
    return compute_risk_snapshot(
        request.app.state.repo,
        trades,
        lookback_days=lookback_days,
    )


def _fetch_prices(trades: list[dict[str, Any]]) -> dict[str, float | None]:
    """收集持仓中 symbols,经 registry provider 拉最新价。失败/不可用 → 全 None (stale)。"""
    symbols = sorted({
        str(t["symbol"])
        for t in trades
        if t.get("status") in (STATUS_BUILDING, STATUS_HOLDING) and t.get("symbol")
    })
    if not symbols:
        return {}
    try:
        name = get_active_provider_name(capability="realtime")
        provider = get_provider(name)
        if not getattr(provider.capabilities, "realtime", False):
            return {s: None for s in symbols}
        df = provider.get_realtime(symbols=symbols)
        if df is None or getattr(df, "is_empty", lambda: True)():
            return {s: None for s in symbols}
        prices: dict[str, float | None] = {s: None for s in symbols}
        cols = getattr(df, "columns", [])
        sym_col = "symbol" if "symbol" in cols else None
        price_col = None
        for cand in ("last_price", "price", "close", "last"):
            if cand in cols:
                price_col = cand
                break
        if sym_col and price_col:
            rows = df.to_dicts()
            for row in rows:
                sym = str(row.get("symbol") or "").strip().upper()
                px = row.get(price_col)
                if sym and px is not None:
                    prices[sym] = float(px)
        return prices
    except Exception:
        return {s: None for s in symbols}


def _persist(trade: dict, event: dict) -> None:
    try:
        store.persist_trade_with_event(settings.data_dir, trade, event)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"交易事件落盘失败: {e}")


def _notify_red_flags(trade_id: str) -> None:
    """事件与审计均成功落盘后推送新红旗；失败不得阻断交易。"""
    try:
        flags = red_flags.scan_trade(settings.data_dir, trade_id)
        red_flag_webhook.push_new_flags(settings.data_dir, trade_id, flags)
    except Exception as exc:  # noqa: BLE001 — 推送不是交易事实
        logger.warning("纪律红旗扫描/推送失败: trade=%s error=%s", trade_id, exc)
