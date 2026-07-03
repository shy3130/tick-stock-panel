"""行情状态 / SSE 推送 API。

盘中选股相关端点已迁移至策略页面，此处仅保留全局行情基础设施。
SSE 推送三种事件 (使用标准 SSE event 字段):
  - quotes_updated: 行情数据刷新，前端 invalidate 对应 query
  - strategy_alert: 策略监控/告警触发，前端弹通知
  - depth_updated: 五档盘口修正完成，前端刷新连板梯队/看板封单数据
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/intraday", tags=["quotes"])


def _get_quote_service(request: Request):
    """获取全局 QuoteService。"""
    return getattr(request.app.state, "quote_service", None)


def _fallback_index_quotes_from_daily(request: Request, symbols: list[str] | None = None) -> list[dict]:
    """实时指数缓存为空时，从本地指数日 K 取最近收盘价作为兜底。"""
    repo = getattr(request.app.state, "repo", None)
    if not repo:
        return []

    params: list[str] = []
    symbol_filter = ""
    if symbols:
        placeholders = ", ".join("?" for _ in symbols)
        symbol_filter = f"WHERE symbol IN ({placeholders})"
        params.extend(symbols)

    try:
        rows = repo.execute_all(
            f"""
            WITH ranked AS (
                SELECT symbol, date, close,
                       row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM kline_index_daily
                {symbol_filter}
            ), latest AS (
                SELECT symbol,
                       max(CASE WHEN rn = 1 THEN date END) AS date,
                       max(CASE WHEN rn = 1 THEN close END) AS last_price,
                       max(CASE WHEN rn = 2 THEN close END) AS prev_close
                FROM ranked
                WHERE rn <= 2
                GROUP BY symbol
            )
            SELECT latest.symbol, latest.date, latest.last_price, latest.prev_close
            FROM latest
            ORDER BY latest.symbol
            """,
            params,
        )
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for symbol, dt, last_price, prev_close in rows:
        change_amount = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_amount = float(last_price) - float(prev_close)
            change_pct = change_amount / float(prev_close) * 100
        out.append({
            "symbol": symbol,
            "name": None,
            "date": str(dt) if dt else None,
            "last_price": float(last_price) if last_price is not None else None,
            "close": float(last_price) if last_price is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "source": "index_daily",
        })
    return out


@router.get("/status")
def status(request: Request):
    """行情状态 (来自全局 QuoteService)。"""
    qs = _get_quote_service(request)
    if qs:
        return qs.status()
    return {"enabled": False, "running": False, "symbol_count": 0, "index_symbol_count": 0,
            "quote_age_ms": None, "is_trading_hours": False, "last_fetch_ms": None}


@router.get("/indices")
def index_quotes(
    request: Request,
    symbols: str | None = Query(None, description="逗号分隔的指数 symbol 列表"),
):
    """返回实时指数行情缓存，不触发上游实时请求。"""
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    qs = _get_quote_service(request)
    if not qs:
        rows = _fallback_index_quotes_from_daily(request, symbol_list)
        return {"rows": rows, "count": len(rows), "source": "index_daily"}
    df = qs.get_index_quotes(symbol_list)
    rows = df.to_dicts() if not df.is_empty() else []
    if not rows:
        rows = _fallback_index_quotes_from_daily(request, symbol_list)
        return {"rows": rows, "count": len(rows), "source": "index_daily"}
    return {"rows": rows, "count": len(rows), "source": "realtime"}


@router.get("/stream")
async def quote_stream(request: Request):
    """SSE 端点: 行情更新 + 告警推送 + 五档修正。

    使用 sse-starlette EventSourceResponse:
    - 标准 SSE event 字段，前端按 event name 监听
    - 内置断线检测，客户端断开立即终止 generator
    - 内置 ping 心跳，保持连接活跃
    """
    qs = _get_quote_service(request)

    async def event_generator():
        while True:
            # 同时等待三类信号: 行情更新 / 告警 / 五档修正
            tasks: dict[str, asyncio.Future] = {
                "quote": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_update, timeout=5.0) if qs else asyncio.sleep(5)
                ),
                "alert": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_alert, timeout=5.0) if qs else asyncio.sleep(5)
                ),
                "depth": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_depth_update, timeout=5.0) if qs else asyncio.sleep(5)
                ),
                "review": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_review, timeout=5.0) if qs else asyncio.sleep(5)
                ),
            }

            done, pending = await asyncio.wait(
                list(tasks.values()),
                timeout=30.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            # 先推送告警 (如果有)
            if qs:
                alerts = qs.pop_alerts()
                if alerts:
                    for chunk_start in range(0, len(alerts), 20):
                        chunk = alerts[chunk_start:chunk_start + 20]
                        yield {
                            "event": "strategy_alert",
                            "data": json.dumps({
                                "ts": int(time.time() * 1000),
                                "alerts": chunk,
                            }, ensure_ascii=False),
                        }

                # 推送复盘进度 (定时复盘流式生成时) — 前端 reviewStore 直接消费
                # 事件已是 recap_market_stream 产出的 JSON 字符串, 逐条转发
                for evt_json in qs.pop_review_events():
                    yield {
                        "event": "review_progress",
                        "data": evt_json,
                    }

            # 推送行情更新 (行情信号触发)
            if tasks["quote"] in done:
                try:
                    update_result = tasks["quote"].result()
                except Exception:  # noqa: BLE001
                    update_result = False
                if update_result:
                    yield {
                        "event": "quotes_updated",
                        "data": json.dumps({
                            "ts": int(time.time() * 1000),
                            "symbol_count": qs._symbol_count if qs else 0,
                        }),
                    }

            # 推送五档修正完成 (depth 信号触发) — 前端刷新连板梯队封单数据
            if tasks["depth"] in done:
                try:
                    depth_result = tasks["depth"].result()
                except Exception:  # noqa: BLE001
                    depth_result = False
                if depth_result:
                    yield {
                        "event": "depth_updated",
                        "data": json.dumps({
                            "ts": int(time.time() * 1000),
                        }),
                    }

    return EventSourceResponse(event_generator())


@router.post("/refresh")
def refresh_quotes(request: Request):
    """手动刷新一次行情数据。"""
    qs = _get_quote_service(request)
    if qs:
        return qs.refresh()
    return {"error": "QuoteService not available"}
