"""分钟K复权投影与原始基准迁移 (与日K复权架构同构, 见 CONTRIBUTING §3.2)。

三层架构:
- 事实层: kline_minute / kline_etf_minute 存原始价 + 原始 amount (拉取 adjust='none',
  落盘后不可变)。原始价与成交额天然同口径, 均价线在存储层即正确。
- 因子层: 共享日K的 adj_factor (股票) / adj_factor_etf (ETF), 只追加。
- 投影层: 读取时复权 ratio = 截至 D 日累积因子 / 全部累积因子, 价格与 amount 同乘,
  volume 不变。amount 同乘保证 VWAP 与复权价严格同口径。当日 ratio 恒为 1
  (今天不可能有未来事件), 盘中实时路径整体短路。

基准标记 data/kline_minute/.raw_basis:
- 存在 → 分区已是原始口径: 读取应用复权投影, TickFlow 拉取 adjust='none'。
- 不存在 → 旧基准 (拉取时前复权, 混合口径): 读取原样返回, 拉取维持 adjust='forward'。
- 迁移任务把全部分区换算为原始口径后创建标记; 失败不标记, 幂等可续跑。

性能 (2026-09 本机实测, 133 万行/日真实分区):
- 单股单日 240 根: 复权 +6~9ms; 当日全市场: ratio 全 1 短路, 开销为零。
- 实现为「去重 (symbol,date) 小表 asof → hash join 贴回 → ratio=1 短路」三层。
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

PRICE_COLS = ("open", "high", "low", "close")
ADJUSTED_COLS = ("open", "high", "low", "close", "amount")

_MARKER_NAME = ".raw_basis"

# 因子表内存缓存: (data_dir, 表名) -> (源文件最新 mtime, fs帧, total帧)。
# 因子表仅数千行 (全市场一年 ~5000 事件), 读盘 ~10ms, 按 mtime 失效。
_factor_cache: dict[tuple[str, str], tuple[float, pl.DataFrame, pl.DataFrame]] = {}
_factor_cache_lock = threading.Lock()


def minute_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / "kline_minute"


def marker_path(data_dir: Path | str) -> Path:
    return minute_dir(data_dir) / _MARKER_NAME


def minute_basis_is_raw(data_dir: Path | str) -> bool:
    """分区数据是否已迁移为原始口径 (标记文件存在)。"""
    return marker_path(data_dir).exists()


def mark_minute_basis_raw(data_dir: Path | str) -> None:
    """迁移完成后创建标记。目录不存在时一并创建 (空库直接切新基准)。"""
    p = marker_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def minute_fetch_adjust(data_dir: Path | str) -> str:
    """TickFlow 分钟拉取的 adjust 参数: 已迁移 → 'none' (原始), 否则 'forward' (旧行为)。"""
    return "none" if minute_basis_is_raw(data_dir) else "forward"


def _factor_table_dir(data_dir: Path, asset_type: str) -> Path:
    return data_dir / ("adj_factor_etf" if asset_type == "etf" else "adj_factor")


def _load_factor_frames(data_dir: Path, asset_type: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """读因子表并构建 (symbol, trade_date, cum) 与 (symbol, total) 两帧, 按 mtime 缓存。

    返回空帧表示该资产类型无因子数据 (调用方按恒等处理)。
    """
    table_dir = _factor_table_dir(data_dir, asset_type)
    files = sorted(table_dir.glob("*.parquet")) if table_dir.exists() else []
    if not files:
        return (pl.DataFrame(), pl.DataFrame())
    newest = max(f.stat().st_mtime for f in files)
    key = (str(data_dir), table_dir.name)
    with _factor_cache_lock:
        cached = _factor_cache.get(key)
        if cached is not None and cached[0] == newest:
            return cached[1], cached[2]

    factors = pl.read_parquet([str(f) for f in files])
    if factors.is_empty() or not {"symbol", "trade_date", "ex_factor"}.issubset(factors.columns):
        frames = (pl.DataFrame(), pl.DataFrame())
    else:
        fs = (
            factors.with_columns(
                pl.col("trade_date").cast(pl.Date, strict=False),
                pl.col("ex_factor").cast(pl.Float64, strict=False),
            )
            .select("symbol", "trade_date", "ex_factor")
            .drop_nulls()
            .unique(subset=["symbol", "trade_date"])
            .sort(["symbol", "trade_date"])
            .with_columns(pl.col("ex_factor").cum_prod().over("symbol").alias("cum"))
        )
        total = fs.group_by("symbol").agg(pl.col("cum").last().alias("total"))
        frames = (fs.select("symbol", "trade_date", "cum"), total)
    with _factor_cache_lock:
        _factor_cache[key] = (newest, frames[0], frames[1])
    return frames


def compute_ratio_pairs(
    df: pl.DataFrame,
    data_dir: Path | str,
    asset_type: str = "stock",
) -> pl.DataFrame:
    """为 df 中出现的去重 (symbol, 交易日) 对计算复权 ratio。

    返回 schema: symbol, _d (Date), _ratio。无因子/无事件时 _ratio 恒为 1。
    """
    if asset_type == "index" or df.is_empty() or "datetime" not in df.columns:
        return pl.DataFrame(schema={"symbol": pl.String, "_d": pl.Date, "_ratio": pl.Float64})
    fs, total = _load_factor_frames(Path(data_dir), asset_type)
    pairs = df.select(pl.col("symbol"), pl.col("datetime").dt.date().alias("_d")).unique()
    if fs.is_empty():
        return pairs.with_columns(pl.lit(1.0).alias("_ratio"))
    pairs = (
        pairs.sort(["symbol", "_d"])
        .join_asof(fs, left_on="_d", right_on="trade_date", by="symbol", strategy="backward", check_sortedness=False)
        .join(total, on="symbol", how="left")
        .with_columns(
            (pl.col("cum").fill_null(1.0) / pl.col("total").fill_null(1.0)).alias("_ratio")
        )
        .select("symbol", "_d", "_ratio")
    )
    return pairs


def apply_minute_adjustment(
    df: pl.DataFrame,
    data_dir: Path | str,
    asset_type: str = "stock",
) -> pl.DataFrame:
    """读取时复权投影: 价格与 amount 乘以 ratio (volume 不变), 纯函数无副作用。

    - 指数无复权概念, 原样返回;
    - 因子表为空 → ratio 恒 1, 原样返回 (fail-open, 与日K缺因子语义一致);
    - 当日/无事件数据 ratio 全 1, 短路返回原帧 (零拷贝)。
    """
    if df.is_empty() or asset_type == "index":
        return df
    cols = [c for c in ADJUSTED_COLS if c in df.columns]
    if not cols or "datetime" not in df.columns:
        return df
    pairs = compute_ratio_pairs(df, data_dir, asset_type)
    if pairs.is_empty() or pairs["_ratio"].min() >= 1.0 - 1e-12:
        return df
    out = (
        df.with_columns(pl.col("datetime").dt.date().alias("_d"))
        .join(pairs, on=["symbol", "_d"], how="left")
        .with_columns([pl.col(c) * pl.col("_ratio").fill_null(1.0) for c in cols])
        .drop(["_d", "_ratio"])
    )
    return out


# ================================================================
# 存量迁移: 拉取时前复权的旧分区 → 原始口径 (日K原始收盘价锚点换算)
# ================================================================

def _daily_close_lookup(daily_dir: Path, trade_day: date) -> dict[str, float] | None:
    """读某交易日日K分区的原始收盘价 {symbol: close}。分区缺失返回 None。"""
    part = daily_dir / f"date={trade_day.isoformat()}" / "part.parquet"
    if not part.exists():
        return None
    try:
        df = pl.read_parquet(part, columns=["symbol", "close"])
    except Exception as e:
        logger.warning("迁移读日K分区失败 %s: %s", part, e)
        return None
    if df.is_empty():
        return None
    return dict(zip(df["symbol"].to_list(), df["close"].to_list(), strict=True))


def _partition_anchor_ratio(minute_df: pl.DataFrame, daily_close: dict[str, float]) -> pl.DataFrame:
    """按 (symbol) 计算锚点 k = 当日最后一根分钟 close / 日K原始 close。

    该分区单日, (symbol, date) 即 symbol。返回 schema: symbol, _k。
    k≈1 → 已是原始价; k 偏离 1 → 该 symbol 为拉取时前复权数据, 价格需除以 k。
    """
    last_close = (
        minute_df.sort("datetime")
        .group_by("symbol")
        .agg(pl.col("close").last().alias("_last"))
    )
    anchors = (
        pl.DataFrame({"symbol": list(daily_close.keys()), "_raw": list(daily_close.values())})
        .join(last_close, on="symbol", how="inner")
        .with_columns((pl.col("_last") / pl.col("_raw")).alias("_k"))
        .filter(
            pl.col("_k").is_finite()
            & (pl.col("_k") > 0.2)
            & (pl.col("_k") < 5.0)   # 锚点换算的合理界: 远超个股除权幅度视为数据异常
        )
        .select("symbol", "_k")
    )
    return anchors


def migrate_minute_to_raw(
    data_dir: Path | str,
    write_lock: threading.Lock | None = None,
    progress_cb=None,
) -> dict:
    """把 kline_minute / kline_etf_minute 全部分区换算为原始口径, 成功后创建基准标记。

    换算法: 每 (symbol, 分区日) 的锚点 k = 最后一根分钟 close ÷ 日K原始 close;
    已是原始价的行 k≈1 不动, 前复权价除以 k 还原为原始价。amount 本就是原始值, 不动。
    幂等: 已换算分区二次运行 k≈1 全程 no-op; 全部成功才写标记, 失败可续跑。

    返回统计: partitions / converted_symbols / skipped_no_daily / failed / marked。
    """
    data_dir = Path(data_dir)
    stats = {"partitions": 0, "converted_symbols": 0, "skipped_no_daily": 0, "failed": 0, "marked": False}
    if minute_basis_is_raw(data_dir):
        stats["marked"] = True
        return stats

    jobs: list[tuple[Path, Path]] = [
        (data_dir / "kline_minute", data_dir / "kline_daily"),
        (data_dir / "kline_etf_minute", data_dir / "kline_etf_daily"),
    ]
    for minute_root, daily_root in jobs:
        if not minute_root.exists():
            continue
        partitions = sorted(p for p in minute_root.glob("date=*") if (p / "part.parquet").exists())
        for part_dir in partitions:
            trade_day = date.fromisoformat(part_dir.name[5:])
            stats["partitions"] += 1
            try:
                daily_close = _daily_close_lookup(daily_root, trade_day)
                if not daily_close:
                    # 无日K锚点的分区无法换算, 不算失败: 留待日K补齐后续跑
                    stats["skipped_no_daily"] += 1
                    continue
                part = part_dir / "part.parquet"
                df = pl.read_parquet(part)
                if df.is_empty() or "close" not in df.columns:
                    continue
                anchors = _partition_anchor_ratio(df, daily_close)
                conv = anchors.filter((pl.col("_k") - 1.0).abs() > 1e-9)
                if not conv.is_empty():
                    fixed = (
                        df.join(conv, on="symbol", how="left")
                        .with_columns(
                            [pl.col(c) / pl.col("_k").fill_null(1.0) for c in PRICE_COLS if c in df.columns]
                        )
                        .drop("_k")
                        .sort(["symbol", "datetime"])
                    )
                    tmp = part.with_suffix(".parquet.tmp")
                    fixed.write_parquet(tmp)
                    if write_lock is not None:
                        with write_lock:
                            tmp.replace(part)
                    else:
                        tmp.replace(part)
                    stats["converted_symbols"] += conv.height
            except Exception as e:
                stats["failed"] += 1
                logger.warning("分钟分区迁移失败 %s: %s", part_dir.name, e)
            if progress_cb is not None:
                progress_cb(stats)

    if stats["failed"] == 0:
        # 有分区但全部缺日K锚点时不下标记 (数据未真正收敛), 空库可直接切换
        total_actionable = stats["partitions"] - stats["skipped_no_daily"]
        if total_actionable == 0 and stats["partitions"] > 0:
            return stats
        mark_minute_basis_raw(data_dir)
        stats["marked"] = True
        logger.info(
            "分钟K已迁移为原始口径: %d 分区, 换算 %d symbol·日, 跳过 %d",
            stats["partitions"], stats["converted_symbols"], stats["skipped_no_daily"],
        )
    return stats
