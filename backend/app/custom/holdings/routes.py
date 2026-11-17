"""Holdings extension HTTP boundary; no provider calls or independent quote cache."""
import hashlib
import json
import logging
import math
import re
from datetime import timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.indicators.levels import compute_chip_distribution
from app.market_time import cn_today

from .ledger import LOCK, Trade, load, replay, save

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/custom/holdings", tags=["holdings"])


def revision(trades):
    return hashlib.sha256(json.dumps([t.model_dump(mode="json") for t in trades], sort_keys=True).encode()).hexdigest()


def read(request):
    try:
        return load(request.app.state.repo.store.data_dir)
    except (ValueError, KeyError, OSError) as exc:
        logger.warning("holdings ledger unavailable", exc_info=True)
        raise HTTPException(409, "账本损坏或版本不支持，请检查备份；已拒绝写入") from exc


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    trade: Trade


@router.get("")
def overview(request: Request):
    with LOCK:
        trades = read(request)
        try:
            result = replay(trades, cn_today())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    repo = request.app.state.repo
    symbols = sorted({p["symbol"] for p in result["positions"]})
    names = repo.get_name_map(symbols) if symbols else {}
    prices = {}
    market_error = None
    if symbols:
        try:
            df, _ = repo.get_enriched_latest(symbols=symbols)
            if not df.is_empty() and "raw_close" in df.columns:
                for row in df.to_dicts():
                    value = row.get("raw_close")
                    if value is not None and math.isfinite(float(value)) and float(value) > 0:
                        prices[row["symbol"]] = (float(value), str(row.get("date", "")))
        except Exception:  # noqa: BLE001
            logger.warning("holdings quotes unavailable", exc_info=True)
            market_error = "行情暂不可用"
    for p in result["positions"]:
        price, as_of = prices.get(p["symbol"], (None, None))
        p.update(name=names.get(p["symbol"], ""), last_price=price, quote_date=as_of,
                 market_value=price * p["qty"] if price is not None and p["qty"] else None,
                 unrealized=(price * p["qty"] - p["cost"]) if price is not None and p["qty"] else None)
    return {**result, "revision": revision(trades), "trades": [t.model_dump(mode="json") for t in trades],
            "market_error": market_error, "version": 1}


@router.put("/trade")
def put_trade(change: Change, request: Request):
    with LOCK:
        trades = read(request)
        existing = next((t for t in trades if t.id == change.trade.id), None)
        if change.revision != revision(trades):
            if existing is not None and existing == change.trade.model_copy(update={"order": existing.order}):
                return {"ok": True}
            raise HTTPException(409, "账本已变化，请刷新后重试")
        if request.app.state.repo.resolve_asset_type(change.trade.symbol) != "stock":
            raise HTTPException(400, "本期仅支持股票交易")
        # Entry sequence belongs to the server; corrections keep their original position.
        sequence = existing.order if existing else max((t.order for t in trades), default=0) + 1
        changed = change.trade.model_copy(update={"order": sequence})
        updated = [changed if t.id == changed.id else t for t in trades]
        if existing is None:
            updated.append(changed)
        try:
            save(request.app.state.repo.store.data_dir, updated, cn_today())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.get("/chips")
def chips(symbol: str, request: Request):
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise HTTPException(400, "证券代码非法")
    repo = request.app.state.repo
    df = repo.get_daily_asset("stock", symbol, cn_today() - timedelta(days=760), cn_today())
    if not df.is_empty():
        df = df.sort("date").tail(250)
    required = {"high", "low", "close", "volume", "turnover_rate"}
    if df.height < 20 or not required.issubset(df.columns):
        return {"bins": [], "message": "至少需要20个交易日行情及换手率数据"}
    if df.select(pl.any_horizontal([pl.col(c).is_null() | ~pl.col(c).is_finite() for c in required]).any()).item():
        return {"bins": [], "message": "行情或换手率有缺失，无法估算筹码分布"}
    if df.filter((pl.col("turnover_rate") < 0) | (pl.col("turnover_rate") > 100)).height:
        return {"bins": [], "message": "换手率异常，无法估算筹码分布"}
    if df.filter((pl.col("low") <= 0) | (pl.col("high") < pl.col("low")) | (pl.col("volume") < 0)).height:
        return {"bins": [], "message": "价格或成交量异常，无法估算筹码分布"}
    return {"bins": compute_chip_distribution(df), "as_of": str(df["date"][-1]),
            "message": f"市场筹码估算 · 最近{df.height}个交易日 · 前复权价格 · 换手率衰减模型"}


class Restore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    revision: str
    trades: list[Trade]


@router.post("/restore")
def restore(payload: Restore, request: Request):
    if payload.version != 1:
        raise HTTPException(400, "账本版本不支持")
    with LOCK:
        trades = read(request)
        if revision(trades) != payload.revision:
            raise HTTPException(409, "账本已变化，请刷新后重试")
        for t in payload.trades:
            if request.app.state.repo.resolve_asset_type(t.symbol) != "stock":
                raise HTTPException(400, "本期仅支持股票交易")
        try:
            save(request.app.state.repo.store.data_dir, payload.trades, cn_today())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
