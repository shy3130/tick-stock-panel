"""告警触发记录 API — 查询/清空/生成演示数据 alerts.jsonl。"""
from __future__ import annotations

import random
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.services import alert_store

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


@router.get("")
def list_alerts(
    request: Request,
    days: int = 7,
    limit: int = 5000,
    source: str | None = None,
    type: str | None = None,
    ext_columns: str | None = None,
):
    """查询触发记录 (时间倒序)。

    ext_columns: 逗号分隔的 "configId.fieldName", 传入后按 symbol 富化行业/概念等 ext 字段,
    每条记录附带 {configId}__{fieldName} 键 (与 watchlist/screener 一致)。
    """
    events = alert_store.list_recent(
        _data_dir(request), days=days, limit=limit, source=source, type=type,
    )
    if ext_columns and events:
        try:
            from app.api.screener import _load_ext_value_maps, _rows_with_ext
            repo = request.app.state.repo
            value_maps = _load_ext_value_maps(repo, ext_columns)
            if value_maps:
                events = _rows_with_ext(events, value_maps)
        except Exception:  # noqa: BLE001
            pass
    total = alert_store.count(_data_dir(request))
    return {"alerts": events, "total": total}


@router.delete("")
def clear_alerts(request: Request):
    """清空全部触发记录。"""
    n = alert_store.clear(_data_dir(request))
    return {"ok": True, "cleared": n}


@router.delete("/{ts}")
def delete_alert(ts: int, request: Request):
    """删除单条触发记录 (按 ts 毫秒时间戳)。"""
    deleted = alert_store.delete_one(_data_dir(request), ts)
    if not deleted:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True}


# ── 演示数据生成 (仅 Dev 页用) ─────────────────────────

_DEMO_STOCKS = [
    ("600519.SH", "贵州茅台"), ("000001.SZ", "平安银行"), ("300750.SZ", "宁德时代"),
    ("002594.SZ", "比亚迪"), ("000858.SZ", "五粮液"), ("601318.SH", "中国平安"),
    ("002475.SZ", "立讯精密"), ("600036.SH", "招商银行"), ("000725.SZ", "京东方A"),
    ("300059.SZ", "东方财富"),
]
_DEMO_TEMPLATES = [
    ("signal", "MA金叉触发", ["signal_ma_golden_5_20"], "info"),
    ("signal", "放量突破新高", ["signal_volume_surge", "signal_n_day_high"], "warn"),
    ("signal", "MACD金叉", ["signal_macd_golden"], "info"),
    ("signal", "跌破MA20", ["signal_ma20_breakdown"], "info"),
    ("price", "涨幅超 5%", [], "warn"),
    ("price", "RSI 极度超卖", [], "warn"),
    ("price", "跌幅超 3%", [], "info"),
    ("market", "涨停封板", ["signal_limit_up"], "critical"),
    ("market", "连板异动", ["signal_limit_up"], "warn"),
    ("market", "炸板", ["signal_broken_limit_up"], "warn"),
    # 新策略变更格式
    ("strategy", "策略「趋势突破」进入 贵州茅台 +2.3%", ["signal_n_day_high", "signal_volume_surge"], "info"),
    ("strategy", "策略「趋势突破」移出 五粮液 -1.5%", ["signal_ma20_breakdown"], "info"),
    ("strategy", "策略「新低反转」进入 平安银行 +1.1%", ["signal_n_day_low"], "warn"),
    ("strategy", "策略「MACD金叉」移出 比亚迪 -0.8%", ["signal_macd_golden"], "info"),
    # 批量变更
    ("strategy", "策略「趋势突破」进入 6 只：平安银行、宁德时代、比亚迪、东方财富、招商银行、立讯精密", [], "info"),
    ("strategy", "策略「MACD金叉」移出 7 只：京东方A、平安银行、五粮液、立讯精密、招商银行、东方财富、比亚迪", [], "warn"),
]


@router.post("/seed")
def seed_demo_alerts(request: Request, count: int = 12, recent: bool = True):
    """生成演示触发记录 (Dev 页用)。

    Args:
        count: 生成条数 (1-50)
        recent: True=时间戳设为"刚刚"(用于测试闪烁效果); False=分散在近3天
    """
    count = max(1, min(50, count))
    now_ms = int(time.time() * 1000)
    events = []
    for i in range(count):
        source, message, signals, severity = _DEMO_TEMPLATES[i % len(_DEMO_TEMPLATES)]
        sym, name = _DEMO_STOCKS[i % len(_DEMO_STOCKS)]
        # 策略类型按消息推导 type: new_entry / dropped, 否则沿用 source
        if source == "strategy":
            if "进入" in message:
                ev_type = "new_entry"
            elif "移出" in message:
                ev_type = "dropped"
            else:
                ev_type = "strategy"
        else:
            ev_type = source
        # recent 模式: 时间戳从现在往前每条错开 30 秒 (最新在前)
        ts = now_ms - (i * 30000) if recent else now_ms - random.randint(60, 4320) * 60 * 1000
        events.append({
            "ts": ts,
            "rule_id": f"demo_rule_{i}",
            "rule_name": message,
            "source": source,
            "type": ev_type,
            "symbol": "" if source == "strategy" and ("只：" in message) else sym,
            "name": name,
            "message": message,
            "price": round(random.uniform(8, 1800), 2) if not (source == "strategy" and "只：" in message) else None,
            "change_pct": round(random.uniform(-0.06, 0.098), 4) if not (source == "strategy" and "只：" in message) else None,
            "signals": signals,
            "severity": severity,
        })
    alert_store.append_many(_data_dir(request), events)

    # 同步推入 SSE 队列, 让所有连着 SSE 的客户端实时收到 (不依赖轮询)
    qs = getattr(request.app.state, "quote_service", None)
    if qs:
        # 转成 SSE 推送格式 (和 _evaluate_monitors 一致)
        sse_alerts = [{
            "source": ev["source"],
            "type": ev["type"],
            "rule_id": ev.get("rule_id"),
            "symbol": ev["symbol"],
            "name": ev["name"],
            "message": ev["message"],
            "price": ev["price"],
            "change_pct": ev["change_pct"],
            "signals": ev["signals"],
            "severity": ev.get("severity", "info"),
        } for ev in events]
        qs.push_alerts(sse_alerts)

    return {"ok": True, "generated": len(events)}

