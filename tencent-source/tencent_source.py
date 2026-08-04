"""腾讯财经行情适配服务 —— 为 TSP (tickflow-stock-panel) 自定义数据源提供 HTTP 接口

接口:
  POST /daily       批量日K(前复权, 含成交额)    body: {symbols, start_time(ms), end_time(ms)}
  POST /adj_factor  恒等复权因子 1.0             (daily 已返回前复权价)
  GET  /health      健康检查

单位约定(与 TickFlow 对齐):
  volume = 手   腾讯返回即为手, 不换算
  amount = 元   腾讯 newfqkline 返回万元, 此处 x10000 转元

数据源: 腾讯 proxy.finance.qq.com newfqkline (qfq 前复权, 含成交额)
  注: 该接口忽略 start/end 区间参数, 只认 count(最多 640 根), 故用"count 估算 + 首行日期翻页"覆盖历史区间
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Tencent Finance Adapter")

FQ_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 限速: 并发 8, 每请求最小间隔 0.15s (约 6.7 req/s, 满足 skill 建议 >=100ms 且不至于太慢)
SEM = asyncio.Semaphore(8)
MIN_INTERVAL = 0.15
_last_req = 0.0
_lock = asyncio.Lock()


class BatchReq(BaseModel):
    symbols: list[str]
    start_time: Optional[object] = None  # TSP 发 ISO 字符串(datetime.isoformat), 兼容 int 毫秒
    end_time: Optional[object] = None
    freq: Optional[str] = None  # minute: 1m/5m/15m/30m/60m
    asset_type: Optional[str] = None


def to_qq_code(symbol: str) -> Optional[str]:
    code, _, mkt = symbol.partition(".")
    mkt = mkt.upper()
    if mkt == "SH":
        return "sh" + code
    if mkt == "SZ":
        return "sz" + code
    if mkt == "BJ":
        return "bj" + code
    return None


async def rate_limit() -> None:
    global _last_req
    async with _lock:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _last_req)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_req = time.monotonic()


async def fetch_kline(client: httpx.AsyncClient, qq: str, start_date: str, end_date: str, count: int):
    """拉一段前复权日K, 返回 [(date, open, close, high, low, volume, amount_yuan), ...]"""
    params = {"param": f"{qq},day,{start_date},{end_date},{count},qfq"}
    for attempt in range(3):
        try:
            async with SEM:
                await rate_limit()
                r = await client.get(FQ_URL, params=params, timeout=20)
            if r.status_code != 200:
                await asyncio.sleep(1 + attempt)
                continue
            node = r.json().get("data", {}).get(qq, {})
            rows = node.get("qfqday") or node.get("day") or []
            out = []
            for row in rows:
                if not row or len(row) < 6:
                    continue
                amt = 0.0
                if len(row) >= 9:
                    try:
                        amt = float(row[8]) * 10000.0  # 万元 -> 元
                    except (TypeError, ValueError):
                        amt = 0.0
                try:
                    out.append(
                        (
                            str(row[0]),
                            float(row[1]),
                            float(row[2]),
                            float(row[3]),
                            float(row[4]),
                            float(row[5]),
                            amt,
                        )
                    )
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        except Exception:
            await asyncio.sleep(1 + attempt)
    return None


def estimate_count(start: str, end: str) -> int:
    """按自然日跨度估算交易日根数 (周末+节假日约 32%)"""
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    days = max((d1 - d0).days, 1)
    return min(int(days * 0.68) + 30, 640)


async def fetch_symbol(client: httpx.AsyncClient, symbol: str, start: Optional[str], end: Optional[str]):
    """拉单只股票完整区间日K (自动翻页补历史), 返回 TSP 字段行列表"""
    qq = to_qq_code(symbol)
    if not qq:
        return []
    if start and end:
        count = estimate_count(start, end)
    else:
        count = 320
    rows = await fetch_kline(client, qq, start or "1990-01-01", end or datetime.now().strftime("%Y-%m-%d"), count)
    if not rows:
        return []
    # 翻页: 返回最早日期仍晚于 start 且已达上限 -> 再往前拉一段拼接
    if start and rows[0][0] > start and len(rows) >= count:
        prev_end = (datetime.strptime(rows[0][0], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        prev = await fetch_kline(client, qq, start, prev_end, count)
        if prev:
            prev_last = prev[-1][0]
            rows = prev + [r for r in rows if r[0] > prev_last]
    if start:
        rows = [r for r in rows if r[0] >= start]
    if end:
        rows = [r for r in rows if r[0] <= end]
    return [
        {
            "ts_code": symbol,
            "trade_date": r[0],
            "open": r[1],
            "close": r[2],
            "high": r[3],
            "low": r[4],
            "vol": r[5],
            "amt": r[6],
        }
        for r in rows
    ]


def ms_to_date(ms: object, pad_days: int = 0) -> Optional[str]:
    """时间 -> YYYY-MM-DD。兼容 TSP 的 ISO 字符串(datetime.isoformat)与毫秒整数。
    TSP 发的 end_time 是北京时间当日 00:00, 容器 UTC 时区格式化会早一天,
    故 end 侧 pad +1 天避免漏掉最后交易日。"""
    if ms is None:
        return None
    if isinstance(ms, (int, float)):
        dt = datetime.fromtimestamp(float(ms) / 1000)
    else:
        text = str(ms).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
    return (dt + timedelta(days=pad_days)).strftime("%Y-%m-%d")


@app.post("/daily")
async def daily(req: BatchReq):
    start = ms_to_date(req.start_time)
    end = ms_to_date(req.end_time, pad_days=1)
    async with httpx.AsyncClient(headers=HEADERS, http2=False) as client:
        results = await asyncio.gather(*[fetch_symbol(client, s, start, end) for s in req.symbols])
    rows = [r for sub in results for r in sub]
    return {"data": rows}


MKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
FREQ_MAP = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


async def fetch_minute(client: httpx.AsyncClient, qq: str, freq_key: str, count: int = 320):
    """拉最近 N 根分钟K, 返回 [(datetime, open, close, high, low, volume), ...]"""
    params = {"param": f"{qq},{freq_key},,{count}"}
    for attempt in range(3):
        try:
            async with SEM:
                await rate_limit()
                r = await client.get(MKLINE_URL, params=params, timeout=20)
            if r.status_code != 200:
                await asyncio.sleep(1 + attempt)
                continue
            node = r.json().get("data", {}).get(qq, {})
            rows = node.get(freq_key) or []
            out = []
            for row in rows:
                if not row or len(row) < 6:
                    continue
                try:
                    dt = row[0]
                    # 202608031450 -> 2026-08-03 14:50:00
                    dt_fmt = f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]} {dt[8:10]}:{dt[10:12]}:00"
                    out.append(
                        (
                            dt_fmt,
                            float(row[1]),
                            float(row[2]),
                            float(row[3]),
                            float(row[4]),
                            float(row[5]),
                        )
                    )
                except (TypeError, ValueError, IndexError):
                    continue
            if out:
                return out
        except Exception:
            await asyncio.sleep(1 + attempt)
    return None


@app.post("/minute")
async def minute(req: BatchReq):
    freq_key = FREQ_MAP.get((req.freq or "1m").lower(), "m1")
    start = ms_to_date(req.start_time)
    end = ms_to_date(req.end_time, pad_days=1)

    async def fetch_one(symbol: str):
        qq = to_qq_code(symbol)
        if not qq:
            return []
        rows = await fetch_minute(client, qq, freq_key)
        if not rows:
            return []
        if start:
            rows = [r for r in rows if r[0][:10] >= start]
        if end:
            rows = [r for r in rows if r[0][:10] <= end]
        return [
            {
                "ts_code": symbol,
                "datetime": r[0],
                "open": r[1],
                "close": r[2],
                "high": r[3],
                "low": r[4],
                "vol": r[5],
                # 腾讯分钟K无成交额字段, 用 量(手)x100x收盘价 估算
                "amt": round(r[5] * 100.0 * r[2], 2),
            }
            for r in rows
        ]

    async with httpx.AsyncClient(headers=HEADERS, http2=False) as client:
        results = await asyncio.gather(*[fetch_one(s) for s in req.symbols])
    rows = [r for sub in results for r in sub]
    return {"data": rows}


@app.post("/adj_factor")
async def adj_factor(req: BatchReq):
    end = ms_to_date(req.end_time, pad_days=1) or "1970-01-01"
    return {"data": [{"ts_code": s, "trade_date": end, "factor": 1.0} for s in req.symbols]}


@app.post("/realtime")
async def realtime(req: BatchReq):
    """批量实时行情 (qt.gtimg.cn 行情快照)。

    body: {symbols: [...]}  — 也可不带 symbols, 返回全市场快照 (慢路径)。

    返回字段 (TSP realtime 契约):
      ts_code, last_price, prev_close, open, high, low, volume(手),
      amount(元), change_pct(小数), change_amount, amplitude(小数), turnover_rate(小数)
    """
    qq_codes = []
    for s in req.symbols:
        qq = to_qq_code(s)
        if qq:
            qq_codes.append(qq)
    if not qq_codes:
        return {"data": []}

    # qt.gtimg.cn 单次最多约 600 只, 分片拉取
    chunk_size = 400
    out_rows: list[dict] = []
    async with httpx.AsyncClient(headers=HEADERS, http2=False) as client:
        for i in range(0, len(qq_codes), chunk_size):
            chunk = qq_codes[i:i + chunk_size]
            rows = await _fetch_realtime_chunk(client, chunk)
            out_rows.extend(rows)
    return {"data": out_rows}


REALTIME_URL = "https://qt.gtimg.cn/q="


async def _fetch_realtime_chunk(client: httpx.AsyncClient, qq_codes: list[str]) -> list[dict]:
    """拉一批实时行情。返回 TSP realtime 契约行。"""
    for attempt in range(3):
        try:
            async with SEM:
                await rate_limit()
                r = await client.get(REALTIME_URL + ",".join(qq_codes), timeout=15)
            if r.status_code != 200:
                await asyncio.sleep(1 + attempt)
                continue
            text = r.content.decode("gbk", errors="ignore")
            return [d for d in (_parse_qt_line(line) for line in text.splitlines() if line.strip().startswith("v_")) if d is not None]
        except Exception:
            await asyncio.sleep(1 + attempt)
    return []


def _parse_qt_line(line: str) -> dict | None:
    """解析 qt.gtimg.cn 单行: v_sh601360="1~三六零~601360~9.66~9.50~9.60~...";"""
    try:
        payload = line.split('"')[1]
        f = payload.split("~")
        if len(f) < 40:
            return None
        symbol = f[2]
        # 补全为 TSP 符号: 6 开头 -> .SH, 其余 -> .SZ (北交所 8/4 开头暂按 .BJ)
        code = str(f[2])
        if code.startswith(("6", "9")):
            symbol = f"{code}.SH"
        elif code.startswith(("4", "8")):
            symbol = f"{code}.BJ"
        else:
            symbol = f"{code}.SZ"

        def _f(idx: int) -> float | None:
            try:
                v = float(f[idx])
                return v
            except (TypeError, ValueError, IndexError):
                return None

        last = _f(3)
        prev_close = _f(4)
        volume = _f(6)          # 手
        amount_wan = _f(37)     # 万元
        change_pct_raw = _f(32)  # 百分比 (如 1.68 = +1.68%)
        change_amount = _f(31)   # 元
        turnover_raw = _f(38)    # 百分比
        amplitude_raw = _f(43)   # 百分比

        if last is None or prev_close is None or prev_close <= 0:
            return None
        # 涨跌幅若接口缺失, 用价格差推导 (小数制, 与 TSP 契约一致)
        if change_pct_raw is None and change_amount is not None:
            change_pct_raw = change_amount / prev_close * 100.0
        if change_pct_raw is None:
            change_pct = (last - prev_close) / prev_close
        else:
            change_pct = change_pct_raw / 100.0

        return {
            "ts_code": symbol,
            "last_price": last,
            "prev_close": prev_close,
            "open": _f(5),
            "high": _f(33),
            "low": _f(34),
            "volume": volume,
            "amount": (amount_wan * 10000.0) if amount_wan is not None else None,
            "change_pct": change_pct,
            "change_amount": change_amount,
            "amplitude": (amplitude_raw / 100.0) if amplitude_raw is not None else None,
            "turnover_rate": (turnover_raw / 100.0) if turnover_raw is not None else None,
        }
    except Exception:
        return None


@app.get("/health")
async def health():
    return {"status": "ok"}
