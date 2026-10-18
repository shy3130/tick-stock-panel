"""TdxQuant(通达信量化数据服务)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
插件被 loader 扫描注册后, 各 service 无需改动即可路由。

实现数据集:
  - realtime    A 股全市场快照(五市场股票池, 600 只/批)
  - depth5      五档盘口(快照 Buyp/Buyv/Sellp/Sellv)
  - daily       A 股日K, 原始价(dividend=none), count 模式单标的拉取
  - adj_factor  除权因子: 事件接口 + 本地K线配价, 交易所公式推导, 涨跌停自检
  - minute      分钟K(period=1m count 模式, 单标的 240 根/日, 原始价)
  - full_minute 全量分钟: 修复轮 get_intraday_batch(当日窗口) +
                稳态增量 get_intraday_latest(每股最新 count 根)。
未声明 financial → provider_has_dataset 为 False, 回退 tickflow。

单位与口径 (CONTRIBUTING §3, 不可凭字段名推断; 2026-10 实测):
  - 快照 Volume 已是手, 直接透传; Amount 为万元 → ×10000 转元。
  - 快照无涨跌幅字段 → change_pct = (Now-LastClose)/LastClose, 小数制。
  - 日K Volume 为股 → floor(/100) 转手; Amount 为万元 → ×10000 转元。
  - 分钟K与日K同源同口径 (实测茅台全天 240 根 sum(Volume)=3511100 ≈ 日K
    3511051 股): Volume 股 → floor(/100) 转手; Amount 万元 → ×10000 转元;
    datetime = Date+Time 拼北京墙钟 naive (契约 §分钟K)。
  - 日K取数 dividend="none" 锁定原始价, 前复权由 indicators.pipeline 用本地因子计算。
  - 除权事件 Bonus/ShareBonus/Allotment 均为每 10 股口径 → ÷10 转每股再进公式。
  - ex_factor 为单事件比值(非累积), 累积链由 pipeline._apply_adj_factor 构建。
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from app.plugins.tdxquant.client import SNAPSHOT_FIELDS, TdxQuantClient, TdxQuantError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("realtime", "depth5", "daily", "adj_factor", "minute", "full_minute")

API_KEY_ENV = "TDXQUANT_API_KEY"
SECRETS_FIELD = "tdxquant_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env

_SNAPSHOT_BATCH = 600  # 实测 600 只/批正常返回
_HIST_INTERVAL_S = 0.08  # 单标的 K 线/事件请求节流
_HIST_MAX_COUNT = 2600  # count 模式单次上限(≈10.6 年日K, 项目深窗口即 10 年)
_ADJ_BACKDAYS = 30  # 推导因子时向前找"除权日前收盘"的回看天数(容忍长期停牌)
_ADJ_FUTURE_DAYS = 15  # 未来已公告事件窗口: 无前收盘自然跳过, 滚动增量轮补上
_BEIJING = ZoneInfo("Asia/Shanghai")

# 内部指数代码 → TDX 快照代码(实测 2026-10: 上证指数走 TDX 999999 别名)
_INDEX_CODE_MAP = {"000001.SH": "999999.SH"}

_DAILY_SCHEMA = {
    "symbol": pl.String, "date": pl.Date,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Int64, "amount": pl.Float64,
}
_ADJ_SCHEMA = {
    "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
    "dividend": pl.Float64, "bonus": pl.Float64, "allot": pl.Float64,
    "allot_price": pl.Float64, "prev_close": pl.Float64,
}
_MINUTE_SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}
_MINUTE_BARS_PER_DAY = 240  # A 股 1m 一交易日根数(含收盘集合竞价分钟)
_MINUTE_MAX_COUNT = 9600    # 实测单次 count=9600(40 交易日)正常返回
_MINUTE_INTERVAL_S = 0.08   # 单标的分钟请求节流(与日K同档)


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def _module_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("tdxaidata") is not None
    except Exception:  # noqa: BLE001 — find_spec 对损坏安装也会抛
        return False


def availability() -> tuple[bool, str]:
    """loader 启动自检: tdxaidata 已安装 + Key 已配置才注册。不抛异常。"""
    if not _module_available():
        return False, "tdxaidata 未安装(默认依赖, 请检查 uv sync)"
    if not get_api_key():
        return False, f"缺少 API Key(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"
    return True, "ok"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """候选 Key 探测: 格式校验 + 子进程连通性实探。

    TDX 会话按 IP 复用(同 IP 活跃好 Key 会话时错误 Key 也能拉到日K, 实测),
    "无效 Key 拒绝"不可靠 — 格式明显不对直接拒绝, 连通性通过即保存,
    Key 真伪由首次实拉(test_dataset / 日常轮询)暴露, 卡片可一键试拉复核。
    """
    key = (api_key or "").strip()
    if not key.startswith("TDX-") or len(key) < 12:
        return False, "Key 格式应为 TDX- 开头(通达信商城创建的数据服务 Key)"
    from app.plugins.tdxquant import probe

    return probe.run(key)


@dataclass
class _TdxQuantConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tdx"
    display_name: str = "TdxQuant"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_date(value) -> date | None:
    """原生 Date 'YYYYMMDD'(或 DataFrame 归一后的 ISO 'YYYY-MM-DD…')→ date。

    非法值返回 None, 不伪造。
    """
    try:
        text = str(value).strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            text = text[:10].replace("-", "")
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (TypeError, ValueError):
        return None


def _snapshot_ms(refresh_time, fallback_ms: int) -> int:
    """RefreshTime 'HHMMSS'(服务端北京墙钟) + 今日北京日期 → epoch ms。

    解析失败退回本地时间; A 股时段无跨日歧义。
    """
    try:
        text = str(refresh_time).strip().zfill(6)
        hh, mm, ss = int(text[:2]), int(text[2:4]), int(text[4:6])
        if hh > 23 or mm > 59 or ss > 59:
            raise ValueError(text)
        now_bj = datetime.now(_BEIJING)
        stamp = datetime(now_bj.year, now_bj.month, now_bj.day, hh, mm, ss, tzinfo=_BEIJING)
        return int(stamp.timestamp() * 1000)
    except (TypeError, ValueError):
        return fallback_ms


def _map_snapshot_row(symbol: str, row: dict, fallback_ms: int) -> dict | None:
    """TDX 快照行 → 内部 realtime record。字段缺失按口径推导, 不伪造数据。"""
    last = _to_float(row.get("Now"))
    prev = _to_float(row.get("LastClose"))
    if last is None and prev is None:
        return None

    change_amount = last - prev if (last is not None and prev is not None) else None
    # 与 quote_service 推导同口径: 小数制, 不乘 100
    change_pct = change_amount / prev if (change_amount is not None and prev) else None

    volume = _to_float(row.get("Volume"))  # 快照已为手, 零换算
    amount = _to_float(row.get("Amount"))  # 万元 → 元

    return {
        "symbol": symbol,
        "name": None,  # 快照无名称, 由下游维表关联
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("Open")),
        "high": _to_float(row.get("Max")),
        "low": _to_float(row.get("Min")),
        "volume": math.floor(volume) if volume is not None else None,
        "amount": amount * 10_000.0 if amount is not None else None,
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,  # 快照未提供, 不启发式计算
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道
        "timestamp": _snapshot_ms(row.get("RefreshTime"), fallback_ms),
        "session": None,
    }


def _ref_price(
    prev_close: float, dividend: float, bonus: float, allot: float, allot_price: float
) -> float | None:
    """交易所除权参考价: (P - D + AR·AP) / (1 + S + AR), 四舍五入(half-up)保留 2 位。"""
    denom = 1.0 + bonus + allot
    if denom <= 0:
        return None
    x = (prev_close - dividend + allot * allot_price) / denom
    return math.floor(x * 100 + 0.5) / 100


def _price_limit(symbol: str) -> float:
    """按代码前缀给涨跌停幅度(自检容差用): 创业板/科创板 20%, 北交所 30%, 主板 10%。"""
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _kline_rows(symbol: str, raw: dict, start_d: date | None, end_d: date) -> list[dict]:
    """原生 count 模式 K 线 dict → 内部行(窗口过滤, volume 股→手, amount 万元→元)。"""

    def _col(name: str, i: int) -> float | None:
        values = raw.get(name)
        return _to_float(values[i]) if values and i < len(values) else None

    dates = raw.get("Date") or []
    out: list[dict] = []
    for i, compact in enumerate(dates):
        d = _compact_date(compact)
        if d is None or d > end_d:
            continue
        if start_d is not None and d < start_d:
            continue
        volume = _col("Volume", i)
        amount = _col("Amount", i)
        out.append({
            "symbol": symbol,
            "date": d,
            "open": _col("Open", i),
            "high": _col("High", i),
            "low": _col("Low", i),
            "close": _col("Close", i),
            "volume": math.floor(volume / 100.0) if volume is not None else None,
            "amount": amount * 10_000.0 if amount is not None else None,
        })
    return out


def _bars_for_window(span_days: int) -> int:
    """窗口自然日跨度 → count 模式请求根数(交易日≈自然日×5/7, 加停牌冗余)。"""
    return max(30, min(_HIST_MAX_COUNT, int(span_days * 5 / 7) + 20))


def _minute_dt(compact_date, hhmmss) -> datetime | None:
    """Date 'YYYYMMDD' + Time 'HHMMSS' → 北京墙钟 naive datetime; 非法返回 None。"""
    d = _compact_date(compact_date)
    if d is None:
        return None
    t = str(hhmmss or "").strip()
    if len(t) < 6 or not t[:6].isdigit():
        return None
    try:
        return datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:4]), int(t[4:6]))
    except ValueError:
        return None


def _minute_rows(
    symbol: str, raw: dict, start_dt: datetime | None, end_dt: datetime | None,
    today_only: date | None = None,
) -> list[dict]:
    """原生 1m K 线 dict → 内部分钟行(datetime 北京墙钟, volume 股→手, amount 万元→元)。

    today_only 非空时仅保留该交易日的行(修复轮用: count 模式尾部会带前一日余量)。
    """
    dates = raw.get("Date") or []
    times = raw.get("Time") or []
    out: list[dict] = []
    for i, compact in enumerate(dates):
        dt = _minute_dt(compact, times[i] if i < len(times) else None)
        if dt is None:
            continue
        if today_only is not None and dt.date() != today_only:
            continue
        if start_dt is not None and dt < start_dt:
            continue
        if end_dt is not None and dt > end_dt:
            continue

        def _col(name: str, idx: int = i) -> float | None:
            values = raw.get(name)
            return _to_float(values[idx]) if values and idx < len(values) else None

        close = _col("Close")
        if close is None:
            continue  # 停牌/无成交分钟, 不伪造
        volume = _col("Volume")
        amount = _col("Amount")
        out.append({
            "symbol": symbol, "datetime": dt,
            "open": _col("Open"), "high": _col("High"),
            "low": _col("Low"), "close": close,
            "volume": float(math.floor(volume / 100.0)) if volume is not None else None,
            "amount": amount * 10_000.0 if amount is not None else None,
        })
    return out


def _minute_count_for_window(start_dt: datetime | None, end_dt: datetime) -> int:
    """窗口 → 单次 count 请求根数(交易日×240 + 两日余量, 封顶 _MINUTE_MAX_COUNT)。"""
    span_days = (end_dt.date() - start_dt.date()).days if start_dt else 30
    return max(240, min(_MINUTE_MAX_COUNT, int(span_days * 5 / 7 * _MINUTE_BARS_PER_DAY) + 480))


class TdxQuantProvider:
    """tdx 数据源。realtime = 五市场全 A 快照批拉; depth5 复用同一快照的五档数组。"""

    name = "tdxquant"
    builtin = True

    def __init__(self) -> None:
        self.config = _TdxQuantConfig()
        self._client: TdxQuantClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> TdxQuantClient:
        if self._client is None:
            self._client = TdxQuantClient(api_key=get_api_key())
        return self._client

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        client = self._get_client()
        try:
            universe = client.stock_universe()
        except TdxQuantError as e:
            logger.warning("TdxQuant 股票池枚举失败: %s", e)
            return []

        fallback_ms = int(time.time() * 1000)
        records: list[dict] = []
        dropped = failed_batches = 0
        for i in range(0, len(universe), _SNAPSHOT_BATCH):
            codes = universe[i : i + _SNAPSHOT_BATCH]
            try:
                rows = client.snapshot_batch(codes)
            except TdxQuantError as e:
                failed_batches += 1
                logger.warning("TdxQuant 快照批次失败(%d 只, 第 %d 批): %s", len(codes), i // _SNAPSHOT_BATCH + 1, e)
                continue
            for code, row in rows.items():
                rec = _map_snapshot_row(code, row, fallback_ms)
                if rec is not None:
                    records.append(rec)
                else:
                    dropped += 1
        if failed_batches:
            logger.warning("TdxQuant 快照 %d/%d 批失败, 本轮仅返回 %d 条",
                           failed_batches, math.ceil(len(universe) / _SNAPSHOT_BATCH), len(records))
        if not records and universe:
            logger.warning("TdxQuant 实时行情本轮 0 条(请求 %d 只), 疑似连接或协议异常", len(universe))
        elif dropped:
            logger.info("TdxQuant 实时行情拉取完成: %d 条(丢弃 %d 行无价快照)", len(records), dropped)
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照(可选插件协议, quote_service 鸭子类型调用)。

        TDX 快照直接支持指数代码; 未映射的代码跳过(上层用本地日K兜底)。
        失败返回 None 保留上轮有效缓存, 成功但无数据返回 []。
        """
        wanted: list[str] = []
        for s in symbols:
            if not s:
                continue
            code = _INDEX_CODE_MAP.get(s, s)
            if code not in wanted:
                wanted.append(code)
        if not wanted:
            return []
        try:
            rows = self._get_client().snapshot_batch(wanted)
        except TdxQuantError as e:
            logger.warning("TdxQuant 指数行情拉取失败: %s", e)
            return None
        fallback_ms = int(time.time() * 1000)
        records = []
        for code, row in rows.items():
            rec = _map_snapshot_row(code, row, fallback_ms)
            if rec is not None:
                records.append(rec)
        logger.info("TdxQuant 指数行情拉取完成: %d 条(请求 %d 只)", len(records), len(wanted))
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        chunks = [
            df
            for df in self.iter_daily(
                symbols, start_time=start_time, end_time=end_time,
                asset_type=asset_type, on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame(schema=_DAILY_SCHEMA)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """逐标的 count 模式拉日K → 内部契约(原始价, volume 手, amount 元)。

        ETF/指数日K未实测覆盖, asset_type 仅接受 stock, 其余空手而归(上层回退)。
        """
        if not symbols or asset_type != "stock":
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        client = self._get_client()
        start_d = start_time.date() if start_time else None
        end_d = end_time.date() if end_time else date.today()
        span = (end_d - start_d).days if start_d else _HIST_MAX_COUNT * 1.4
        count = _bars_for_window(int(span))
        for i, symbol in enumerate(symbols):
            try:
                raw = client.his_dats(symbol, end_d, count)
            except TdxQuantError as e:
                logger.warning("TdxQuant 日K %s 拉取失败: %s", symbol, e)
                raw = {}
            rows = _kline_rows(symbol, raw, start_d, end_d)
            if rows and rows[0]["date"] is not None and start_d is not None and rows[0]["date"] > start_d:
                logger.info("TdxQuant 日K %s: count=%d 仅覆盖到 %s(窗口起点 %s 之前的数据不在返回内)",
                            symbol, count, rows[0]["date"], start_d)
            yield pl.DataFrame(rows, schema=_DAILY_SCHEMA)
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
            time.sleep(_HIST_INTERVAL_S)

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """除权因子 → 内部契约(symbol/trade_date/ex_factor, 单事件比值非累积)
        + 事件明细列(dividend/bonus/allot/allot_price/prev_close, 每股口径)。

        数据链: divid_factors 事件接口(每 10 股口径 → ÷10 转每股) → 同一标的
        一次 count 模式日K取齐窗口内收盘价 → 交易所公式推导 → 涨跌停自检剔除。
        未来已公告事件无前收盘, 自然跳过, 滚动增量窗口补上。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        client = self._get_client()
        start_d = start_time.date() if start_time else None
        end_d = end_time.date() if end_time else date.today()

        out_rows: list[dict] = []
        for i, symbol in enumerate(symbols):
            try:
                events = client.divid_factors(symbol, start_d, end_d + timedelta(days=_ADJ_FUTURE_DAYS))
            except TdxQuantError as e:
                logger.warning("TdxQuant 除权事件 %s 拉取失败: %s", symbol, e)
                events = []
            events = [ev for ev in events if _compact_date(ev.get("Date")) is not None]
            if events:
                events.sort(key=lambda ev: str(ev.get("Date")))
                first_ex = _compact_date(events[0]["Date"])
                closes = self._close_map(client, symbol, first_ex - timedelta(days=_ADJ_BACKDAYS), end_d)
                out_rows.extend(self._factor_rows(symbol, events, closes, start_d, end_d))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
            time.sleep(_HIST_INTERVAL_S)
        if not out_rows:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        return (
            pl.DataFrame(out_rows, schema=_ADJ_SCHEMA)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    def _close_map(self, client: TdxQuantClient, symbol: str, start_d: date, end_d: date) -> dict[date, float]:
        """一次 count 模式日K → {交易日: 原始收盘价}(推导前收盘与自检共用)。"""
        try:
            raw = client.his_dats(symbol, end_d, _bars_for_window((end_d - start_d).days))
        except TdxQuantError as e:
            logger.warning("TdxQuant 除权配价 %s 日K失败: %s", symbol, e)
            return {}
        out: dict[date, float] = {}
        dates = raw.get("Date") or []
        closes = raw.get("Close") or []
        for i, compact in enumerate(dates):
            d = _compact_date(compact)
            close = _to_float(closes[i]) if i < len(closes) else None
            if d is not None and close is not None:
                out[d] = close
        return out

    def _factor_rows(
        self, symbol: str, events: list[dict], closes: dict[date, float],
        start_d: date | None, end_d: date,
    ) -> list[dict]:
        days = sorted(closes)
        rows: list[dict] = []
        for ev in events:
            exd = _compact_date(ev.get("Date"))
            if exd is None or exd > end_d + timedelta(days=_ADJ_FUTURE_DAYS):
                continue
            if start_d is not None and exd < start_d:
                continue
            # 每 10 股口径 → 每股 (Bonus=10派X元, ShareBonus=10送Y股, Allotment=10配Z股)
            dividend = (_to_float(ev.get("Bonus")) or 0.0) / 10.0
            bonus = (_to_float(ev.get("ShareBonus")) or 0.0) / 10.0
            allot = (_to_float(ev.get("Allotment")) or 0.0) / 10.0
            allot_price = _to_float(ev.get("AllotPrice"))
            prev_days = [d for d in days if d < exd]
            if not prev_days:
                continue  # 日K窗口未覆盖(远古事件或未来已公告事件)
            p = closes[prev_days[-1]]
            ref = _ref_price(p, dividend, bonus, allot, allot_price or 0.0)
            if ref is None or ref <= 0:
                logger.warning("TdxQuant 除权因子: %s %s 参考价无法计算(P=%s D=%s S=%s AR=%s AP=%s), 跳过",
                               symbol, exd, p, dividend, bonus, allot, allot_price)
                continue
            factor = p / ref
            ex_days = [d for d in days if d >= exd]
            if ex_days:  # 涨跌停自检: 错误因子会令复权后除权日涨跌幅超出板限
                ret = closes[ex_days[0]] / (p / factor) - 1.0
                if abs(ret) > _price_limit(symbol) + 0.02:
                    logger.warning("TdxQuant 除权因子自检剔除 %s %s: 复权后除权日涨跌幅 %.1f%% 超出涨跌停",
                                   symbol, exd, ret * 100)
                    continue
            rows.append({
                "symbol": symbol, "trade_date": exd, "ex_factor": factor,
                "dividend": dividend, "bonus": bonus, "allot": allot,
                "allot_price": allot_price, "prev_close": p,
            })
        return rows

    # ---- minute / full_minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """分钟K → 内部契约(symbol/datetime 北京墙钟 naive/ohlcv/volume 手/amount 元)。

        period=1m count 模式单标的拉取(实测 240 根/日, count≤9600 正常);
        freq 仅支持 1m(上游有 5m 等档位但项目契约按 1m 路由), 其余空手而归还上层回退;
        ETF/指数分钟未实测覆盖, asset_type 仅接受 stock。
        """
        chunks = list(self.iter_minute(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, freq=freq, on_chunk_done=on_chunk_done,
        ))
        parts = [df for df in chunks if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_MINUTE_SCHEMA)

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        if not symbols or asset_type != "stock" or freq != "1m":
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        client = self._get_client()
        end_dt = end_time or datetime.now()
        count = _minute_count_for_window(start_time, end_dt)
        for i, symbol in enumerate(symbols):
            try:
                raw = client.his_dats(symbol, end_dt.date(), count, period="1m")
            except TdxQuantError as e:
                logger.warning("TdxQuant 分钟K %s 拉取失败: %s", symbol, e)
                raw = {}
            rows = _minute_rows(symbol, raw, start_time, end_dt)
            yield pl.DataFrame(rows, schema=_MINUTE_SCHEMA)
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
            time.sleep(_MINUTE_INTERVAL_S)

    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 当日窗口逐标的拉取, 只保留今日 1m 行。

        count 模式尾部天然带前一日余量, 按 today_only 过滤; 单标的请求
        240+count 余量根。全市场一轮 5579 次调用(含节流约 10 分钟量级),
        由 minute_refresh 以修复轮节奏调度。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        client = self._get_client()
        today = datetime.now().date()
        fetch = max(_MINUTE_BARS_PER_DAY, min(count, 480)) + _MINUTE_BARS_PER_DAY
        out: list[pl.DataFrame] = []
        for symbol in symbols:
            try:
                raw = client.his_dats(symbol, today, fetch, period="1m")
            except TdxQuantError as e:
                logger.warning("TdxQuant 修复轮 %s 拉取失败: %s", symbol, e)
                continue
            rows = _minute_rows(symbol, raw, None, None, today_only=today)
            if rows:
                out.append(pl.DataFrame(rows, schema=_MINUTE_SCHEMA))
            time.sleep(_MINUTE_INTERVAL_S)
        return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame(schema=_MINUTE_SCHEMA)

    def get_intraday_latest(self, symbols: list[str] | None = None, count: int = 3) -> pl.DataFrame:
        """全量分钟稳态增量轮: 每只标的最新 count 根 1m(symbols 缺省 = 全市场股票池)。

        与修复轮同为逐标的 count 调用(无批量端点), 差别在根数少、负载轻;
        失败标的跳过, 空轮由调用方按空处理。
        """
        client = self._get_client()
        targets = list(symbols) if symbols else []
        if not targets:
            try:
                targets = client.stock_universe()
            except TdxQuantError as e:
                logger.warning("TdxQuant 增量轮股票池枚举失败: %s", e)
                return pl.DataFrame(schema=_MINUTE_SCHEMA)
        count = max(1, min(int(count), 30))
        today = datetime.now().date()
        out: list[pl.DataFrame] = []
        for j, symbol in enumerate(targets):
            try:
                raw = client.his_dats(symbol, today, count, period="1m")
            except TdxQuantError as e:
                logger.warning("TdxQuant 增量轮 %s 拉取失败: %s", symbol, e)
                continue
            rows = _minute_rows(symbol, raw, None, None)
            if rows:
                out.append(pl.DataFrame(rows, schema=_MINUTE_SCHEMA))
            if j % 1000 == 999:
                logger.info("TdxQuant 增量轮进度: %d/%d", j + 1, len(targets))
            time.sleep(_MINUTE_INTERVAL_S)
        return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame(schema=_MINUTE_SCHEMA)

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口 → {symbol: 标准盘口字典}。数量单位手(透传), 价格转 float。"""
        client = self._get_client()
        out: dict[str, dict] = {}
        fallback_ms = int(time.time() * 1000)
        for i in range(0, len(symbols), _SNAPSHOT_BATCH):
            batch = symbols[i : i + _SNAPSHOT_BATCH]
            try:
                rows = client.snapshot_batch(batch)
            except TdxQuantError as e:
                logger.warning("TdxQuant 五档批次失败(第 %d 批): %s", i // _SNAPSHOT_BATCH + 1, e)
                continue
            for code, row in rows.items():
                entry = self._depth_entry(row, fallback_ms)
                if entry is not None:
                    out[code] = entry
        return out

    def _depth_entry(self, row: dict, fallback_ms: int) -> dict | None:
        def _prices(name: str) -> list[float] | None:
            values = row.get(name)
            if not values:
                return None
            return [_to_float(v) for v in values]

        def _volumes(name: str) -> list[int] | None:
            values = row.get(name)
            if not values:
                return None
            out: list[int] = []
            for v in values:
                f = _to_float(v)
                out.append(math.floor(f) if f is not None else 0)
            return out

        bid_prices = _prices("Buyp")
        ask_prices = _prices("Sellp")
        if not bid_prices or not ask_prices:
            return None
        return {
            "bid_prices": bid_prices,
            "bid_volumes": _volumes("Buyv") or [],
            "ask_prices": ask_prices,
            "ask_volumes": _volumes("Sellv") or [],
            "timestamp": _snapshot_ms(row.get("RefreshTime"), fallback_ms),
        }

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset in ("daily", "adj_factor"):
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                if dataset == "daily":
                    df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
                else:
                    df = self.get_adj_factors(syms, datetime.now() - timedelta(days=365), datetime.now())
            except TdxQuantError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {"provider": self.name, "dataset": dataset, "rows": df.height,
                    "columns": df.columns, "preview": head}
        if dataset in ("minute", "full_minute"):
            syms = [s for s in (symbols or [])][:3] or ["600519.SH"]
            try:
                end = datetime.now()
                df = (self.get_intraday_batch(syms) if dataset == "full_minute"
                      else self.get_minute(syms, end - timedelta(days=3), end))
            except TdxQuantError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {"provider": self.name, "dataset": dataset, "rows": df.height,
                    "columns": df.columns, "preview": head}
        if dataset in ("realtime", "depth5"):
            syms = [s for s in (symbols or [])][:5] or ["600519.SH", "000001.SZ"]
            try:
                rows = self._get_client().snapshot_batch(syms)
            except TdxQuantError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            fallback_ms = int(time.time() * 1000)
            if dataset == "realtime":
                head = [r for r in (_map_snapshot_row(c, row, fallback_ms) for c, row in rows.items()) if r]
                return {"provider": self.name, "dataset": dataset, "rows": len(rows),
                        "columns": list(head[0].keys()) if head else list(SNAPSHOT_FIELDS),
                        "preview": head[:5]}
            depths = {c: d for c, d in ((c, self._depth_entry(row, fallback_ms)) for c, row in rows.items()) if d}
            return {"provider": self.name, "dataset": dataset, "rows": len(depths),
                    "columns": ["symbol", "bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "timestamp"],
                    "preview": [{"symbol": c, **d} for c, d in list(depths.items())[:5]]}
        return {"provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"tdx 插件未接入 {dataset} 数据集(自动回退 TickFlow)"}


__all__ = [
    "API_KEY_ENV",
    "TdxQuantProvider",
    "availability",
    "get_api_key",
    "probe_api_key",
]
