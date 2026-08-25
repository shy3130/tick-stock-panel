"""eltdx 内置数据源 provider。

基于通达信 7709 协议(eltdx SDK)拉 A 股行情, 归一化到项目内部 schema。
方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
因此注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

数据口径:
- daily / minute 用不复权 K 线(volume 手 / amount 元, 与 tickflow canonical 对齐)。
- adj_factor 由 eltdx 逐日前复权因子换算成"事件日比值"(见 _factor_rows)。
- realtime 用全市场代码表 + 批量快照(volume 手); name/涨跌幅/换手率由上游 pipeline 回算。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import polars as pl

from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.plugins.eltdx import bridge

logger = logging.getLogger(__name__)

# eltdx 支持的数据集(financial 口径不足 → 不声明, 自动回退 tickflow)
_DATASETS = ("daily", "adj_factor", "minute", "realtime", "instruments")

# 批量拉取并发度: TdxClient 默认连接池 2 主站 x 4 TCP = 8 连接, 逐 symbol 串行
# 会浪费连接池吞吐; 并发到 8 对齐池大小, 全市场同步从 N 次往返降到 N/8 轮。
_IO_WORKERS = 8

_MINUTE_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

# 前复权因子比值相对 1 的阈值: 超过该阈值视为发生除权事件。
_RATIO_EPS = 1e-9

# 北京时间时区(eltdx 返回 +08:00, 统一归一化到该时区后再去 tz)。
_CN_TZ = timezone(timedelta(hours=8))

# 各资产类型的代码前缀白名单(排除指数等协议不支持的标的)。
# 指数 K 线实测报 ProtocolError; ETF 日K/分钟可用但历史窗口短, 仅在显式请求时提供。
_SYMBOL_PREFIXES = {
    "stock": {
        "SH": ("600", "601", "603", "605", "688", "689"),
        "SZ": ("000", "001", "002", "003", "300", "301"),
        "BJ": None,  # 北交所只有股票, 全量接受
    },
    "etf": {
        "SH": ("51", "52", "56", "58"),
        "SZ": ("15", "16", "18"),
    },
}


@dataclass
class _EltdxConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "eltdx"
    display_name: str = "eltdx (通达信协议)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def to_tdx(symbol: str) -> str:
    """内部代码 -> eltdx 代码: 000001.SZ -> sz000001"""
    code, exchange = symbol.rsplit(".", 1)
    return f"{exchange.lower()}{code}"


def from_tdx(code: str) -> str:
    """eltdx 代码 -> 内部代码: sz000001 -> 000001.SZ"""
    return f"{code[2:]}.{code[:2].upper()}"


def _is_supported_symbol(symbol: str, asset_type: str) -> bool:
    """按资产类型与代码前缀判断 eltdx 是否支持该标的。

    - index: 协议不支持(实测 bars 报 ProtocolError) → 一律 False;
    - stock: A 股股票前缀白名单, 挡掉 ETF/指数/B股等;
    - etf: ETF 前缀白名单(历史窗口仅数年, 见 plugin.yaml 说明)。
    """
    if asset_type == "index":
        return False
    try:
        code, exchange = symbol.rsplit(".", 1)
    except (ValueError, AttributeError):
        return False
    exchange = exchange.upper()
    prefixes = _SYMBOL_PREFIXES.get(asset_type, {}).get(exchange)
    if prefixes is None:
        return asset_type == "stock"  # 北交所: 只接受股票
    return code.startswith(prefixes)


def _parse_naive_time(value: str | None) -> datetime | None:
    """解析 eltdx 的 ISO 时间, 统一转北京时间后返回 naive 墙钟。

    eltdx 实测返回 +08:00; 若遇 UTC 等其他时区, 先转换到北京时间避免 8h 偏差。
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_CN_TZ)
    return parsed.replace(tzinfo=None)


def _parse_iso_date(value: str | None) -> date | None:
    """解析 bridge 返回的 ISO 日期字符串(如 '1991-04-03')为 date, 解析失败返回 None。"""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _bars_to_rows(bars: list[dict], symbol: str) -> list[dict]:
    """eltdx bar dict -> 内部日K/分钟K 行。

    volume 取手数: volume_lots 本身即手(与 wire/100 等价), 对齐项目 canonical volume
    单位(见 pipeline.py 换手率公式 volume(手) * 10000 / float_shares(股))。
    """
    rows: list[dict] = []
    for b in bars:
        t = _parse_naive_time(b.get("time"))
        if t is None:
            continue
        v = b.get("volume_lots")
        rows.append(
            {
                "symbol": symbol,
                "time": t,
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
                "volume": v
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                else None,
                "amount": b.get("amount"),
            }
        )
    return rows


def _factor_rows(items: list[dict], symbol: str) -> list[dict]:
    """eltdx 逐日前复权因子 -> 项目事件式 ex_factor 行。

    项目 ex_factor 语义(indicators/pipeline._apply_adj_factor):
      - ex_factor 是"每次除权事件的 pre/post 比值"(非累积, 仅在事件日有行);
      - adjusted = raw * cumprod_at_D / total_cumprod。

    eltdx 的 qfq_factor 是逐日累积前复权因子(最新≈1, 历史更小), 满足
    adjusted = raw * qfq_factor(D)。因此事件日比值
    ex_factor[D] = qfq_factor[D] / qfq_factor[D-1], 与 cumprod 语义自洽:
    项目重算的 adjusted = raw * (∏事件比 到 D) / (∏全部) = raw * qfq(D)/qfq(最新) = raw * qfq(D)。
    """
    rows: list[dict] = []
    prev: float | None = None
    for item in items:
        t = _parse_naive_time(item.get("time"))
        factor = item.get("qfq_factor")
        if (
            t is None
            or isinstance(factor, bool)
            or not isinstance(factor, (int, float))
            or factor <= 0
        ):
            continue
        if prev is not None and abs(factor / prev - 1.0) > _RATIO_EPS:
            rows.append({"symbol": symbol, "trade_date": t.date(), "ex_factor": factor / prev})
        prev = factor
    return rows


def _snapshot_rows(snaps: list[dict]) -> list[dict]:
    """eltdx 批量快照 -> 内部 realtime 行。total_hand 即为手数(与 tickflow quotes.volume 同口径)。"""
    rows: list[dict] = []
    for s in snaps:
        code = f"{s.get('exchange') or ''}{s.get('code') or ''}"
        if not code or len(code) <= 2:
            continue
        hand = s.get("total_hand")
        rows.append(
            {
                "symbol": from_tdx(code),
                "last_price": s.get("last_price"),
                "prev_close": s.get("pre_close_price"),
                "open": s.get("open_price"),
                "high": s.get("high_price"),
                "low": s.get("low_price"),
                "volume": hand
                if isinstance(hand, (int, float)) and not isinstance(hand, bool)
                else None,
                "amount": s.get("amount"),
            }
        )
    return rows


def _fetch_concurrent(
    symbols: list[str],
    worker: Callable[[str], pl.DataFrame],
    label: str,
    on_chunk_done: Callable[[int, int], None] | None,
) -> tuple[list[pl.DataFrame], list[str]]:
    """并发执行 worker(sym) 拉取单标的数据, 返回 (frames, failed)。

    每个 symbol 独立拉取, 异常时记入 failed(由上层聚合一条 WARNING),
    单个标的失败不会拖垮整批。进度回调在完成时以 (completed, total) 触发。
    """
    frames: list[pl.DataFrame] = []
    failed: list[str] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=_IO_WORKERS) as ex:
        futures = {ex.submit(worker, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                df = fut.result()
                if not df.is_empty():
                    frames.append(df)
            except Exception as e:
                logger.warning("eltdx %s 拉取失败(%s): %s", label, sym, e)
                failed.append(sym)
            completed += 1
            if on_chunk_done:
                on_chunk_done(completed, len(symbols))
    return frames, failed


def _daily_symbol_df(
    sym: str, asset_type: str, start_time: datetime | None, end_time: datetime | None
) -> pl.DataFrame:
    """拉取单只日K并归一化+裁剪, 返回 DataFrame(不支持/空数据返回空表)。"""
    if not _is_supported_symbol(sym, asset_type):
        logger.debug("eltdx 不支持标的(%s, asset_type=%s), 跳过", sym, asset_type)
        return pl.DataFrame()
    rows = _bars_to_rows(bridge.bars_all(to_tdx(sym), "day", since=start_time), sym)
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows).with_columns(pl.col("time").dt.date().alias("date")).drop("time")
    df = normalize_daily(df, default_symbol=sym, source="eltdx")
    return _clip(df, "date", start_time, end_time)


def _adj_symbol_df(
    sym: str, asset_type: str, start_time: datetime | None, end_time: datetime | None
) -> pl.DataFrame:
    """拉取单只除权因子并归一化+裁剪, 返回 DataFrame(不支持/空数据返回空表)。"""
    if not _is_supported_symbol(sym, asset_type):
        logger.debug("eltdx 不支持标的(%s, asset_type=%s), 跳过", sym, asset_type)
        return pl.DataFrame()
    rows = _factor_rows(bridge.factors(to_tdx(sym)), sym)
    if not rows:
        return pl.DataFrame()
    df = normalize_adj_factors(pl.DataFrame(rows), source="eltdx")
    return _clip(df, "trade_date", start_time, end_time)


def _minute_symbol_df(
    sym: str,
    asset_type: str,
    freq: str,
    start_time: datetime | None,
    end_time: datetime | None,
) -> pl.DataFrame:
    """拉取单只分钟K并归一化+裁剪, 返回 DataFrame(不支持/空数据返回空表)。"""
    if not _is_supported_symbol(sym, asset_type):
        logger.debug("eltdx 不支持标的(%s, asset_type=%s), 跳过", sym, asset_type)
        return pl.DataFrame()
    rows = _bars_to_rows(bridge.bars_all(to_tdx(sym), str(freq), since=start_time), sym)
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows).rename({"time": "datetime"}).select(_MINUTE_COLS)
    df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    return _clip(df, "datetime", start_time, end_time)


class EltdxProvider:
    """内置 eltdx 数据源。"""

    name = "eltdx"
    builtin = True

    def __init__(self) -> None:
        self.config = _EltdxConfig()

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        bridge.close_client()

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols or asset_type not in ("stock", "etf"):
            return pl.DataFrame()
        logger.info("eltdx daily 拉取开始(%d symbols)", len(symbols))
        frames, failed = _fetch_concurrent(
            symbols,
            lambda sym: _daily_symbol_df(sym, asset_type, start_time, end_time),
            "daily",
            on_chunk_done,
        )
        if failed:
            logger.warning("eltdx daily 部分失败: %d/%d 标的未获取, 将保持旧数据 (样例: %s)",
                           len(failed), len(symbols), failed[:10])
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        # eltdx 的 qfq 因子仅对 A 股股票有明确口径, ETF/指数不提供(回退 tickflow)。
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        frames, failed = _fetch_concurrent(
            symbols,
            lambda sym: _adj_symbol_df(sym, asset_type, start_time, end_time),
            "adj_factor",
            on_chunk_done,
        )
        if failed:
            logger.warning("eltdx adj_factor 部分失败: %d/%d 标的未获取, 将保持旧复权价 (样例: %s)",
                           len(failed), len(symbols), failed[:10])
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        # 指数分钟线协议不支持(实测报 ProtocolError), 拒绝后由上层感知缺失。
        if not symbols or asset_type not in ("stock", "etf"):
            return pl.DataFrame()
        logger.info("eltdx minute 拉取开始(%d symbols, freq=%s)", len(symbols), freq)
        frames, failed = _fetch_concurrent(
            symbols,
            lambda sym: _minute_symbol_df(sym, asset_type, freq, start_time, end_time),
            "minute",
            on_chunk_done,
        )
        if failed:
            logger.warning("eltdx minute 部分失败: %d/%d 标的未获取, 将保持旧数据 (样例: %s)",
                           len(failed), len(symbols), failed[:10])
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- realtime (全市场快照) ----
    def get_realtime(self) -> list[dict]:
        logger.info("eltdx realtime 拉取开始(全市场快照)")
        try:
            codes = bridge.a_share_codes()
        except Exception as e:
            logger.warning("eltdx A 股代码表拉取失败: %s", e)
            return []
        rows: list[dict] = []
        for i in range(0, len(codes), bridge.SNAPSHOT_BATCH):
            chunk = codes[i : i + bridge.SNAPSHOT_BATCH]
            try:
                rows.extend(_snapshot_rows(bridge.snapshots(chunk)))
            except Exception as e:
                logger.warning("eltdx realtime 快照失败(%d codes): %s", len(chunk), e)
        return rows

    # ---- instruments (标的维表) ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """返回 tickflow Instrument 形状的行(symbol/name/code/exchange/region/type + ext),

        供 instrument_sync._flatten_instruments 复用同一 flatten 路径。
        当前覆盖 A 股股票; etf/index 未覆盖(返回空, 上层回退 tickflow)。

        ext.float_shares / ext.total_shares 由财务快照换算(万股→股), 供 pipeline 计算
        换手率; 缺失时置 None, pipeline 会把换手率降级为空, 不会静默套用错误口径。
        ext.listing_date 由 ipo_date 转换(ISO 字符串→date), 保持维表 Date 类型一致。
        """
        if asset_type != "stock":
            return []
        try:
            codes = bridge.a_share_codes()
            names = bridge.code_names()
            caps = bridge.share_capitals()
        except Exception as e:
            logger.warning("eltdx instruments 拉取失败: %s", e)
            return []
        rows: list[dict] = []
        for tdx_code in codes:
            symbol = from_tdx(tdx_code)
            cap = caps.get(tdx_code) or {}
            rows.append(
                {
                    "symbol": symbol,
                    "name": names.get(tdx_code) or symbol,
                    "code": symbol.split(".")[0],
                    "exchange": symbol.split(".")[1],
                    "region": "CN",
                    "type": "stock",
                    "ext": {
                        "float_shares": cap.get("float_shares"),
                        "total_shares": cap.get("total_shares"),
                        "listing_date": _parse_iso_date(cap.get("ipo_date")),
                    },
                }
            )
        return rows

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        symbols = symbols or ["000001.SZ"]
        if dataset == "daily":
            return _preview("daily", self.get_daily(symbols, None, None))
        if dataset == "adj_factor":
            return _preview("adj_factor", self.get_adj_factors(symbols, None, None))
        if dataset == "minute":
            return _preview("minute", self.get_minute(symbols, None, None))
        if dataset == "realtime":
            rows = self.get_realtime()
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        if dataset == "instruments":
            rows = self.get_instruments("stock")
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "instruments",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        raise ValueError(f"eltdx 不支持数据集: {dataset}")


def _clip(
    df: pl.DataFrame, col: str, start_time: datetime | None, end_time: datetime | None
) -> pl.DataFrame:
    """本地时间范围裁剪(eltdx 接口只按 count 返回, 不支持区间参数)。

    start_time/end_time 可能为 datetime 或 date(上游从 Parquet Date 列取出的就是 date)。
    date 列比较时统一取 .date(); datetime 列用 naive 墙钟比较。
    """
    if df.is_empty() or col not in df.columns:
        return df
    if start_time is not None:
        if col in {"date", "trade_date"}:
            start_v = start_time.date() if isinstance(start_time, datetime) else start_time
        else:
            start_v = (
                start_time.replace(tzinfo=None)
                if isinstance(start_time, datetime)
                else datetime.combine(start_time, time.min)
            )
        df = df.filter(pl.col(col) >= start_v)
    if end_time is not None:
        if col in {"date", "trade_date"}:
            end_v = end_time.date() if isinstance(end_time, datetime) else end_time
        else:
            end_v = (
                end_time.replace(tzinfo=None)
                if isinstance(end_time, datetime)
                else datetime.combine(end_time, time.max)
            )
        df = df.filter(pl.col(col) <= end_v)
    return df


def _preview(dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": "eltdx",
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
