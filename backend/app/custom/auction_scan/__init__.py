"""9:25 全市场竞价扫描扩展 (基于 ExtCollector + MarketAccess + ext_data 引擎)。

本扩展完全依赖稳定公开扩展契约（``ExtCollector``、``MarketAccess``、``ext_data``），
不直接导入任何数据源插件或偏好路由内部模块：

- 定时策略：交易日 09:25 竞价终态后执行，``time_window_end="09:30"`` 防止
  过窗补录或伪造历史；默认关闭 (``enabled=False``)，由用户在前端显式开启。
- 可参数化复用：支持通过 ``PullConfig.collector_params`` 配置 ``min_open_gap_pct``
  （默认 ``0.05``）与 ``min_auction_vol_ratio``（默认 ``10.0``），一套采集器可在
  界面零代码派生多张不同阈值的扫描表。
- 容错与状态：停牌/无竞价/异常快照单条跳过；部分批次失败时保留已成功命中
  并通过 ``partial_errors`` 写回 ``partial`` 状态，不伪装为干净空成功。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtCollector,
    ExtCollectorContext,
    ExtCollectorResult,
    ExtensionContext,
)
from app.extensions.market_access import CoreMarketAccess
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

AUCTION_SCAN_FIELDS: tuple[dict[str, str], ...] = (
    {"name": "trade_date", "dtype": "string", "label": "交易日"},
    {"name": "name", "dtype": "string", "label": "名称"},
    {"name": "open_gap_pct", "dtype": "float", "label": "竞价高开幅度"},
    {"name": "auction_vol_ratio", "dtype": "float", "label": "竞价量比"},
    {"name": "auction_price", "dtype": "float", "label": "竞价价"},
    {"name": "prev_close", "dtype": "float", "label": "昨收价"},
    {"name": "auction_volume", "dtype": "float", "label": "竞价量(手)"},
    {"name": "collected_at", "dtype": "string", "label": "采集时间"},
)


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
    - 边界包含：恰好等于阈值时命中；低于任一阈值不命中。
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


def _resolve_param_float(params: Any, key: str, default: float, *, minimum: float) -> float:
    raw = params.get(key) if hasattr(params, "get") else None
    val = _to_finite_float(raw)
    if val is None or val < minimum:
        return default
    return val


class AuctionScanCollector(ExtCollector):
    """9:25 全市场集合竞价扫描采集器（支持参数化阈值）。"""

    label = "9:25 全市场竞价扫描"
    description = "交易日 09:25 竞价终态后扫描全市场 A 股，按高开幅度与竞价量比阈值筛选并按日落盘"
    default_mode = "timeseries"
    default_fields = AUCTION_SCAN_FIELDS
    params_schema: ClassVar[Mapping[str, Any]] = {
        "min_open_gap_pct": {
            "type": "float",
            "label": "最低高开幅度 (小数制，如 0.05=5%)",
            "default": MIN_OPEN_GAP_PCT,
            "min": 0.0,
            "max": 0.30,
        },
        "min_auction_vol_ratio": {
            "type": "float",
            "label": "最低竞价量比",
            "default": MIN_AUCTION_VOL_RATIO,
            "min": 0.1,
            "max": 1000.0,
        },
    }

    def collect(self, context: ExtCollectorContext) -> ExtCollectorResult:
        market = context.market or CoreMarketAccess(context.data_dir, context.repository)
        snapshots, partial_errors = market.get_realtime_stocks()
        if _snapshots_all_stale(snapshots, context.target_date):
            return ExtCollectorResult(
                rows=(),
                skipped_reason=(
                    f"{context.target_date.isoformat()} 快照时间戳非当日，跳过落盘（可能为非交易日）"
                ),
            )

        excluded = market.get_excluded_non_stock_symbols()
        vol_ma5_map = market.get_vol_ma5_baseline(
            context.target_date, lookback_days=BASELINE_LOOKBACK_DAYS,
        )
        name_map = market.get_name_map()

        min_gap = _resolve_param_float(
            context.params, "min_open_gap_pct", MIN_OPEN_GAP_PCT, minimum=0.0,
        )
        min_ratio = _resolve_param_float(
            context.params, "min_auction_vol_ratio", MIN_AUCTION_VOL_RATIO, minimum=0.0,
        )

        collected_at = cn_now().replace(microsecond=0).isoformat()
        rows, stats = scan_auction_snapshots(
            snapshots,
            vol_ma5_map,
            name_map,
            target_date=context.target_date,
            collected_at=collected_at,
            excluded_symbols=excluded,
            min_open_gap_pct=min_gap,
            min_auction_vol_ratio=min_ratio,
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
            ExtField(f["name"], f["dtype"], f["label"])
            for f in AUCTION_SCAN_FIELDS
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
