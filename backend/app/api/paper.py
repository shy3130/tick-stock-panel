"""虚拟账户(模拟盘) API — 薄胶水: 校验/映射在 paper 域模块, 这里只做 HTTP 壳。

多账户: 所有端点接受 ?account=<id> (缺省 "default"); 帐户 ID 校验在域模块。

路由挂载在 app/main.py; 域模块自带进程内写锁, API 层不再加第二把锁
(镜像 lots API 的跨请求互斥语义, 但锁下沉到域)。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.strategy import paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _acc(request: Request, account: str) -> str:
    try:
        return paper.validate_account_id(account)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class AccountModel(BaseModel):
    initial_cash: float
    account_id: str = paper.DEFAULT_ACCOUNT_ID
    name: str | None = None
    commission_pct: float = paper.DEFAULT_COMMISSION_PCT
    stamp_tax_pct: float = paper.DEFAULT_STAMP_TAX_PCT
    slippage_bps: float = paper.DEFAULT_SLIPPAGE_BPS
    queue_limit_orders: bool = False


class OrderModel(BaseModel):
    symbol: str
    side: str                       # buy / sell
    qty: int | None = None
    amount: float | None = None
    order_type: str = "market"      # market / next_open / close
    ref_price: float | None = None  # amount 模式折算 & 买入资金预检


class SettingsModel(BaseModel):
    queue_limit_orders: bool | None = None
    commission_pct: float | None = None   # 佣金率 (0.00025 = 万2.5)
    stamp_tax_pct: float | None = None    # 印花税 (0.001 = 千1, 仅卖出)
    slippage_bps: float | None = None     # 滑点 (5 = 5bps)


def _resolve_asset_type(request: Request, symbol: str) -> str:
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


def _last_close(data_dir: Path, symbol: str, asset_type: str) -> float | None:
    """最近收盘价 (amount 折算参考价)。读最新分区日 raw close。"""
    base = paper._daily_path(data_dir, asset_type)
    if not base.exists():
        return None
    try:
        import polars as pl
        df = (
            pl.scan_parquet((base / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol") == symbol)
            .sort("date")
            .select(pl.col("close").last())
            .collect()
        )
    except Exception:
        return None
    if df.is_empty() or df["close"][0] is None:
        return None
    return float(df["close"][0])


@router.get("/accounts")
def list_accounts(request: Request):
    """账户列表 (带最新净值摘要, 供前端切换器)。"""
    return {"accounts": paper.list_accounts(_data_dir(request))}


@router.get("/account")
def get_account(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    acc = paper.get_account(_data_dir(request), _acc(request, account))
    return {"account": acc}


@router.post("/account")
def create_account(request: Request, body: AccountModel):
    try:
        acc = paper.create_account(
            _data_dir(request),
            body.initial_cash,
            account_id=_acc(request, body.account_id),
            name=body.name,
            commission_pct=body.commission_pct,
            stamp_tax_pct=body.stamp_tax_pct,
            slippage_bps=body.slippage_bps,
            queue_limit_orders=body.queue_limit_orders,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


@router.get("/overview")
def overview(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """总览: 现金 + 持仓估值 + 账户状态。

    盘中(交易时段且实时快照就绪)用当日实时 raw_close 估算 (estimating=true),
    其余时刻用最近日线收盘价 (与收盘定版口径一致)。
    """
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        return {"initialized": False, "account_id": acc_id}
    ov = paper.overview(data_dir, account_id=acc_id)
    ov["fees"] = {k: acc[k] for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")}

    # 盘中实时估算: 从行情服务的当日 enriched 快照取 raw_close (软失败, 降级日线)
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None and acc.get("status") == "active":
        try:
            enriched, enriched_date = qs.get_enriched_today()
            from app.market_time import cn_today
            if not enriched.is_empty() and enriched_date == cn_today() and ov.get("holdings"):
                snapshot = dict(zip(enriched["symbol"].to_list(), enriched["raw_close"].to_list(), strict=False))
                live = paper.overview(data_dir, snapshot, account_id=acc_id)
                live["fees"] = ov["fees"]
                return live
        except Exception as e:
            logger.warning("paper overview 实时估算失败, 降级日线口径: %s", e)
    return ov


@router.post("/orders")
def create_order(request: Request, body: OrderModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    asset_type = _resolve_asset_type(request, body.symbol)
    ref_price = body.ref_price
    if ref_price is None:
        ref_price = _last_close(data_dir, body.symbol, asset_type)
    order, err = paper.create_order(
        data_dir, body.symbol, body.side,
        account_id=acc_id,
        qty=body.qty, amount=body.amount,
        order_type=body.order_type, asset_type=asset_type, ref_price=ref_price,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"order": order}


@router.get("/orders")
def list_orders(request: Request, status: str | None = None, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    orders = paper.load_orders(_data_dir(request), _acc(request, account))
    if status:
        orders = [o for o in orders if o["status"] == status]
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    return {"orders": orders}


@router.delete("/orders/{order_id}")
def cancel_order(request: Request, order_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    order, err = paper.cancel_order(_data_dir(request), order_id, _acc(request, account))
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"order": order}


@router.get("/trades")
def list_trades(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    fills = paper.load_fills(_data_dir(request), _acc(request, account))
    fills.sort(key=lambda f: f.get("seq", 0), reverse=True)
    return {"fills": fills}


@router.get("/positions")
def list_positions(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    ov = paper.overview(data_dir, account_id=_acc(request, account))
    return {"holdings": ov.get("holdings", []), "initialized": ov.get("initialized", False)}


@router.get("/nav")
def list_nav(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return {"nav": paper.load_nav(_data_dir(request), _acc(request, account))}


@router.get("/stats")
def get_stats(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return paper.stats(_data_dir(request), _acc(request, account))


@router.get("/compare")
def compare_accounts(request: Request):
    """横向对比全部账户: 概览 + 回合统计 + 定版净值 (供对比表与净值叠加图)。

    净值给全量定版序列, 前端做归一化 (起点=1) 后多账户叠加。
    """
    data_dir = _data_dir(request)
    rows = []
    for acc_id in paper.list_account_ids(data_dir):
        acc = paper.get_account(data_dir, acc_id)
        if acc is None:
            continue
        ov = paper.overview(data_dir, account_id=acc_id)
        if not ov.get("initialized"):
            continue  # 空壳目录 (懒创建) 不进对比
        st = paper.stats(data_dir, acc_id)
        initial = float(acc.get("initial_cash") or 0)
        rows.append({
            "account": acc_id,
            "name": acc.get("name") or acc_id,
            "status": acc.get("status"),
            "initial_cash": initial,
            "fees": {k: acc.get(k) for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")},
            "total": ov.get("total"),
            "cash": ov.get("cash"),
            "market_value": ov.get("market_value"),
            "total_pnl": ov.get("total_pnl"),
            "pnl_pct": round((ov.get("total_pnl") or 0) / initial * 100, 2) if initial > 0 else None,
            "rounds": st.get("rounds"),
            "win_rate": st.get("win_rate"),
            "profit_loss_ratio": st.get("profit_loss_ratio"),
            "avg_holding_days": st.get("avg_holding_days"),
            "realized_pnl": st.get("realized_pnl"),
            "max_drawdown": st.get("max_drawdown"),
            "nav": [{"date": n["date"], "nav": n["nav"]} for n in paper.load_nav(data_dir, acc_id)],
        })
    return {"accounts": rows}


@router.post("/rebuild")
def rebuild(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """由成交台账重建物化持仓与现金 (修复兜底)。"""
    positions = paper.rebuild_positions(_data_dir(request), _acc(request, account))
    return {"symbols": len(positions)}


@router.post("/freeze")
def freeze(request: Request, frozen: bool = True, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """冻结/解冻账户: 冻结后拒绝新订单, 持仓只读。"""
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        raise HTTPException(status_code=400, detail="尚未创建模拟账户")
    acc["status"] = "frozen" if frozen else "active"
    paper.save_account(data_dir, acc, acc_id)
    return {"account": acc}


@router.post("/settings")
def update_settings(request: Request, body: SettingsModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """更新账户设置 (涨跌停排队等开关)。"""
    try:
        acc = paper.update_settings(_data_dir(request), _acc(request, account), **body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


# ===== 自动跟单规则 (V2) =====
class AutoRuleModel(BaseModel):
    name: str
    match_kind: str                 # strategy / rule
    match_id: str
    side: str = "buy"
    size_mode: str = "fixed_amount" # fixed_amount / pct_equity
    size_value: float
    order_type: str = "next_open"
    cooldown_days: int = 5
    enabled: bool = True


@router.get("/auto_rules")
def list_auto_rules(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    return {"rules": paper_auto.load_auto_rules(_data_dir(request), _acc(request, account))}


@router.post("/auto_rules")
def create_auto_rule(request: Request, body: AutoRuleModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    try:
        rule = paper_auto.create_auto_rule(_data_dir(request), body.model_dump(), _acc(request, account))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"rule": rule}


@router.post("/auto_rules/{rule_id}/enabled")
def set_auto_rule_enabled(request: Request, rule_id: str, enabled: bool, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    rule = paper_auto.set_enabled(_data_dir(request), rule_id, enabled, _acc(request, account))
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"rule": rule}


@router.delete("/auto_rules/{rule_id}")
def delete_auto_rule(request: Request, rule_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    if not paper_auto.delete_auto_rule(_data_dir(request), rule_id, _acc(request, account)):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"ok": True}
