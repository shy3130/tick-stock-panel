"""9:25 全市场竞价扫描扩展 (基于 ExtCollector + ext_data 引擎)。

本扩展不新增私有调度器、存储格式或定制业务路由，完全通过标准
``ExtCollector`` 契约注册到 ``ext_data`` + ``PullScheduler``：

- 定时策略：交易日 09:25 竞价终态后执行，``time_window_end="09:30"`` 防止
  过窗补录或伪造历史；默认关闭 (``enabled=False``)，由用户在前端显式开启。
- 筛选口径：高开幅度 ``open_gap_pct >= 0.05`` (小数制, 相对昨收) 且
  竞价量比 ``auction_vol_ratio >= 10.0`` (``auction_volume / (vol_ma5_prev / 240)``,
  成交量统一为「手」口径，与全仓库 kline_daily 及 realtime provider 对齐)。
- 容错与状态：停牌/无竞价/异常快照单条跳过；部分批次失败时保留已成功命中
  并通过 ``partial_errors`` 写回 ``partial`` 状态，不伪装为干净空成功。
"""
from __future__ import annotations

import contextlib
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.extensions.contracts import (
    BACKEND_EXTENSION_API_VERSION,
    ExtCollector,
    ExtCollectorContext,
    ExtCollectorResult,
    ExtensionContext,
)
from app.extensions.registry import BackendExtensionRegistrar
from app.market_time import CN_TZ, cn_now
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig

logger = logging.getLogger(__name__)

EXTENSION_ID = "auction_scan"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

COLLECTOR_ID = "auction_scan.market_925"
EXT_CONFIG_ID = "ext_auction_scan"

MIN_OPEN_GAP_PCT = 0.05       # 高开幅度 ≥ 5% (小数制，与全仓库 change_pct 同口径)
MIN_AUCTION_VOL_RATIO = 10.0  # 竞价量比 ≥ 10
TRADING_MINUTES_PER_DAY = 240.0
BASELINE_LOOKBACK_DAYS = 5
BOUNDARY_EPS = 1e-12          # 避免 IEEE 754 精度误差导致恰好 5% / 10 漏判


def _to_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _is_a_share_stock_symbol(symbol: str) -> bool:
    """判断代码是否为 A 股正股 (排除常见指数与 ETF 代码段)。"""
    if "." not in symbol:
        return False
    code, market = symbol.upper().split(".", 1)
    if len(code) != 6 or not code.isdigit():
        return False
    if market == "SH":
        return code.startswith(("60", "68"))
    if market == "SZ":
        return code.startswith(("00", "30"))
    if market == "BJ":
        return code.startswith(("43", "83", "87", "92"))
    return False


def _timestamp_date_cn(ts_val: Any) -> date | None:
    """将 epoch 毫秒/秒时间戳解析为北京日期；无法解析返回 None。"""
    num = _to_finite_float(ts_val)
    if num is None or num <= 0:
        return None
    sec = num / 1000.0 if num >= 1e11 else num
    try:
        return datetime.fromtimestamp(sec, tz=CN_TZ).date()
    except (OSError, OverflowError, ValueError):
        return None


def _normalize_tickflow_quote(item: Any) -> dict[str, Any] | None:
    """将 TickFlow quotes 原始 dict 归一为与自定义 realtime provider 同口径的记录。"""
    if not isinstance(item, dict):
        return None
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    return {
        "symbol": symbol,
        "name": item.get("name"),
        "last_price": _to_finite_float(item.get("last_price")),
        "prev_close": _to_finite_float(item.get("prev_close")),
        "open": _to_finite_float(item.get("open")),
        "volume": _to_finite_float(item.get("volume")),
        "timestamp": item.get("timestamp"),
    }


def _fetch_market_snapshots() -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """按当前能力路由拉取全市场实时快照，不写死单一数据源。

    Returns:
        (records, partial_errors)
    Raises:
        RuntimeError: 无可用实时源或整批拉取完全失败。
    """
    from app.services import preferences

    provider_name = preferences.get_realtime_data_provider()
    configured = str(preferences.load().get("realtime_data_provider") or "").strip().lower()
    if configured and configured != "tickflow" and provider_name == "tickflow":
        provider_name = configured

    if provider_name != "tickflow":
        from app.data_providers import custom as custom_sources

        if not custom_sources.provider_has_dataset(provider_name, "realtime"):
            raise RuntimeError(
                f"当前实时数据源 '{provider_name}' 不可用或未声明 realtime 能力"
            )
        provider = custom_sources.get_provider(provider_name)
        records = provider.get_realtime() or []
        meta = getattr(provider, "last_realtime_meta", None)
        partial_errors: tuple[str, ...] = ()
        if isinstance(meta, dict) and meta.get("failed_batches"):
            raw_errors = [str(e) for e in (meta.get("errors") or []) if e]
            if not raw_errors:
                raw_errors = [
                    f"{meta['failed_batches']}/{meta.get('total_batches', '?')} 批快照拉取失败"
                ]
            partial_errors = tuple(raw_errors)
            if not records:
                raise RuntimeError(
                    f"全市场竞价快照拉取失败: {'; '.join(partial_errors[:3])}"
                )
        if not records:
            raise RuntimeError(f"实时数据源 '{provider_name}' 未返回任何行情快照")
        return records, partial_errors

    from app.tickflow.client import get_paid_realtime_client

    tf = get_paid_realtime_client()
    if tf is None:
        raise RuntimeError("当前未配置可用的实时行情数据源")
    resp = tf.quotes.get_by_universes(universes=["CN_Equity_A"]) or []
    records = [
        rec for item in resp
        if (rec := _normalize_tickflow_quote(item)) is not None
    ]
    if not records:
        raise RuntimeError("TickFlow 未返回任何 A 股实时行情快照")
    return records, ()


def _load_vol_ma5_baseline(
    data_dir: Path,
    target_date: date,
    repository: Any = None,
    *,
    lookback_days: int = BASELINE_LOOKBACK_DAYS,
) -> dict[str, float]:
    """加载每只股票在 target_date 之前最近 N 个有效交易日的日均成交量 (单位: 手)。"""
    if repository is not None and hasattr(repository, "execute_all"):
        try:
            rows = repository.execute_all(
                """
                WITH ranked AS (
                    SELECT symbol, volume,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                    FROM kline_daily
                    WHERE date < ? AND volume IS NOT NULL AND volume > 0
                )
                SELECT symbol, AVG(volume) AS vol_ma5
                FROM ranked
                WHERE rn <= ?
                GROUP BY symbol
                """,
                [target_date, lookback_days],
            )
            return {
                str(sym).upper(): float(avg_v)
                for sym, avg_v in rows
                if sym and avg_v is not None and math.isfinite(float(avg_v)) and float(avg_v) > 0
            }
        except Exception as exc:  # noqa: BLE001 — 回退直读 parquet
            logger.debug("auction_scan DuckDB vol_ma5 查询回退 Polars: %s", exc)

    daily_dir = Path(data_dir) / "kline_daily"
    if not daily_dir.exists():
        return {}
    parquet_files = sorted(daily_dir.rglob("*.parquet"))
    if not parquet_files:
        return {}
    try:
        lf = pl.scan_parquet([str(p) for p in parquet_files])
        df = (
            lf.select(["symbol", "date", "volume"])
            .with_columns(
                pl.col("symbol").cast(pl.Utf8).str.to_uppercase(),
                pl.col("date").cast(pl.Date),
                pl.col("volume").cast(pl.Float64),
            )
            .filter(
                (pl.col("date") < pl.lit(target_date))
                & pl.col("volume").is_not_null()
                & (pl.col("volume") > 0)
            )
            .sort(["symbol", "date"])
            .group_by("symbol")
            .agg(pl.col("volume").tail(lookback_days).mean().alias("vol_ma5"))
            .collect()
        )
        return {
            row["symbol"]: float(row["vol_ma5"])
            for row in df.to_dicts()
            if row["symbol"] and row["vol_ma5"] is not None
            and math.isfinite(float(row["vol_ma5"])) and float(row["vol_ma5"]) > 0
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("auction_scan 读取本地日K基准量失败: %s", exc)
        return {}


def _load_excluded_symbols(repository: Any) -> set[str]:
    excluded: set[str] = set()
    if repository is None:
        return excluded
    if hasattr(repository, "get_index_symbol_set"):
        with contextlib.suppress(Exception):
            excluded.update(str(s).upper() for s in repository.get_index_symbol_set() if s)
    if hasattr(repository, "get_etf_instruments"):
        with contextlib.suppress(Exception):
            etf_df = repository.get_etf_instruments()
            if etf_df is not None and not etf_df.is_empty() and "symbol" in etf_df.columns:
                excluded.update(
                    str(s).upper() for s in etf_df["symbol"].cast(pl.Utf8).to_list() if s
                )
    return excluded


@dataclass(frozen=True)
class ScanStats:
    total_snapshots: int
    evaluated: int
    skipped_invalid: int
    skipped_missing_baseline: int
    matched: int


def scan_auction_snapshots(
    snapshots: list[dict[str, Any]],
    vol_ma5_map: dict[str, float],
    name_map: dict[str, str],
    *,
    target_date: date,
    collected_at: str,
    excluded_symbols: set[str] | None = None,
    min_open_gap_pct: float = MIN_OPEN_GAP_PCT,
    min_auction_vol_ratio: float = MIN_AUCTION_VOL_RATIO,
) -> tuple[list[dict[str, Any]], ScanStats]:
    """纯函数：对全市场竞价快照执行容错清洗、指标计算与边界筛选。

    - ``open_gap_pct = (auction_price - prev_close) / prev_close`` (小数制)
    - ``auction_vol_ratio = (auction_volume * 240.0) / vol_ma5_prev``
    - 边界包含：恰好等于 5% 且量比恰好等于 10 命中；低于任一阈值不命中。
    - 停牌、无竞价量、缺失昨收或基准量、异常/畸形行自动跳过，不中断整批任务。
    """
    excluded = excluded_symbols or set()
    deduped: dict[str, dict[str, Any]] = {}
    skipped_invalid = 0

    for item in snapshots:
        if not isinstance(item, dict):
            skipped_invalid += 1
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in excluded or not _is_a_share_stock_symbol(symbol):
            continue
        deduped[symbol] = item

    evaluated = 0
    skipped_missing_baseline = 0
    matched: list[dict[str, Any]] = []
    trade_date_str = target_date.isoformat()

    for symbol, row in deduped.items():
        prev_close = _to_finite_float(row.get("prev_close"))
        open_price = _to_finite_float(row.get("open"))
        last_price = _to_finite_float(row.get("last_price"))
        auction_price = open_price if (open_price is not None and open_price > 0) else last_price
        auction_volume = _to_finite_float(row.get("volume"))

        if (
            prev_close is None or prev_close <= 0
            or auction_price is None or auction_price <= 0
            or auction_volume is None or auction_volume <= 0
        ):
            skipped_invalid += 1
            continue

        vol_ma5 = _to_finite_float(vol_ma5_map.get(symbol))
        if vol_ma5 is None or vol_ma5 <= 0:
            skipped_missing_baseline += 1
            continue

        evaluated += 1
        open_gap_pct = (auction_price - prev_close) / prev_close
        auction_vol_ratio = (auction_volume * TRADING_MINUTES_PER_DAY) / vol_ma5

        if (
            open_gap_pct + BOUNDARY_EPS >= min_open_gap_pct
            and auction_vol_ratio + BOUNDARY_EPS >= min_auction_vol_ratio
        ):
            name = name_map.get(symbol) or str(row.get("name") or "").strip()
            matched.append({
                "symbol": symbol,
                "trade_date": trade_date_str,
                "name": name,
                "open_gap_pct": round(open_gap_pct, 6),
                "auction_vol_ratio": round(auction_vol_ratio, 4),
                "auction_price": round(auction_price, 4),
                "prev_close": round(prev_close, 4),
                "auction_volume": round(auction_volume, 2),
                "collected_at": collected_at,
            })

    matched.sort(
        key=lambda r: (-r["open_gap_pct"], -r["auction_vol_ratio"], r["symbol"])
    )
    return matched, ScanStats(
        total_snapshots=len(snapshots),
        evaluated=evaluated,
        skipped_invalid=skipped_invalid,
        skipped_missing_baseline=skipped_missing_baseline,
        matched=len(matched),
    )


def _snapshots_all_stale(snapshots: list[dict[str, Any]], target_date: date) -> bool:
    """若快照携带了有效时间戳但全部落在非目标日 (例如休市日返回上一交易日收盘快照)，判为陈旧。"""
    dates = [
        d for item in snapshots
        if isinstance(item, dict) and (d := _timestamp_date_cn(item.get("timestamp"))) is not None
    ]
    return bool(dates) and all(d != target_date for d in dates)


class AuctionScanCollector(ExtCollector):
    """9:25 全市场集合竞价扫描采集器。"""

    label = "9:25 全市场竞价扫描"
    description = "交易日 09:25 竞价终态后扫描全市场 A 股，筛出高开 ≥5% 且竞价量比 ≥10 的标的并按日落盘"

    def collect(self, context: ExtCollectorContext) -> ExtCollectorResult:
        snapshots, partial_errors = _fetch_market_snapshots()
        if _snapshots_all_stale(snapshots, context.target_date):
            return ExtCollectorResult(
                rows=(),
                skipped_reason=(
                    f"{context.target_date.isoformat()} 快照时间戳非当日，跳过落盘（可能为非交易日）"
                ),
            )

        excluded = _load_excluded_symbols(context.repository)
        vol_ma5_map = _load_vol_ma5_baseline(
            context.data_dir, context.target_date, context.repository,
        )
        name_map: dict[str, str] = {}
        if context.repository is not None and hasattr(context.repository, "get_name_map"):
            try:
                name_map = context.repository.get_name_map() or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("auction_scan 获取名称映射失败: %s", exc)

        collected_at = cn_now().replace(microsecond=0).isoformat()
        rows, stats = scan_auction_snapshots(
            snapshots,
            vol_ma5_map,
            name_map,
            target_date=context.target_date,
            collected_at=collected_at,
            excluded_symbols=excluded,
        )
        if stats.evaluated == 0 and stats.skipped_missing_baseline > 0 and not vol_ma5_map:
            raise RuntimeError(
                "本地缺少历史日K成交量基准（前5日均量），无法计算竞价量比，请先同步日K数据"
            )
        msg = (
            f"扫描 {stats.evaluated} 只 (总快照 {stats.total_snapshots}), "
            f"命中 {stats.matched} 只 @ {context.target_date.isoformat()}"
        )
        return ExtCollectorResult(
            rows=tuple(rows),
            message=msg,
            allow_empty=True,
            partial_errors=partial_errors,
            replace_partition=True,
        )


def build_default_auction_scan_config() -> ExtConfig:
    """构造默认关闭的 9:25 全市场竞价扫描扩展表配置。"""
    return ExtConfig(
        id=EXT_CONFIG_ID,
        label="9:25 全市场竞价扫描",
        mode="timeseries",
        description=(
            "交易日 09:25 竞价终态后扫描全市场 A 股，自动筛出高开 ≥5% 且竞价量比 ≥10 "
            "的标的并按日落盘积累历史（仅从启用后首次成功扫描开始积累，不补录历史）"
        ),
        fields=[
            ExtField("trade_date", "string", "交易日"),
            ExtField("name", "string", "名称"),
            ExtField("open_gap_pct", "float", "竞价高开幅度"),
            ExtField("auction_vol_ratio", "float", "竞价量比"),
            ExtField("auction_price", "float", "竞价价"),
            ExtField("prev_close", "float", "昨收价"),
            ExtField("auction_volume", "float", "竞价量(手)"),
            ExtField("collected_at", "string", "采集时间"),
        ],
        pull=PullConfig(
            url="",
            collector=COLLECTOR_ID,
            enabled=False,
            schedule_time="09:25",
            schedule_period="daily",
            schedule_trading_only=True,
            time_window_start="09:25",
            time_window_end="09:30",
            timeout_seconds=60,
        ),
    )


def ensure_auction_scan_config(data_dir: Path) -> ExtConfig:
    """首次启动时写入默认关闭的扩展表配置；已存在时绝不覆盖用户配置。"""
    store = ExtConfigStore(data_dir)
    existing = store.get(EXT_CONFIG_ID)
    if existing is not None:
        return existing
    config = build_default_auction_scan_config()
    try:
        store.create(config)
    except FileExistsError:
        return store.get(EXT_CONFIG_ID) or config
    return config


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.register_ext_collector(COLLECTOR_ID, AuctionScanCollector())


def startup(context: ExtensionContext) -> None:
    ensure_auction_scan_config(context.data_dir)
