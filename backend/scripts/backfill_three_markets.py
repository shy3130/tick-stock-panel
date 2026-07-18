"""将 ClickHouse 港股/美股日线回填到 TickFlow 本地分析数据层。

建议在应用容器停止写入后执行。脚本会同步标的维表、分批写入日线 Parquet，
并仅重算指定市场的 enriched 数据，不覆盖其他市场已有行。
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from app.config import settings
from app.indicators.pipeline import run_pipeline
from app.services import instrument_sync, kline_sync, preferences
from app.services.market_scope import filter_frame_by_market, normalize_market
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

logger = logging.getLogger(__name__)


def select_market_symbols(instruments: pl.DataFrame, markets: set[str]) -> list[str]:
    selected: set[str] = set()
    for raw_market in markets:
        market = normalize_market(raw_market)
        scoped = filter_frame_by_market(instruments, market)
        if not scoped.is_empty():
            selected.update(scoped.get_column("symbol").drop_nulls().cast(pl.Utf8).to_list())
    return sorted(selected)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 ClickHouse 三市场历史日线与 enriched")
    parser.add_argument("--markets", nargs="+", choices=("cn", "hk", "us"), default=("hk", "us"))
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=370),
        help="起始日期 YYYY-MM-DD，默认回填最近 370 天",
    )
    parser.add_argument("--end", type=date.fromisoformat, default=date.today(), help="结束日期 YYYY-MM-DD")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provider_name = preferences.get_daily_data_provider()
    if provider_name != "clickhouse":
        raise RuntimeError(f"当前日线数据源是 {provider_name!r}，请先切换为 clickhouse")

    store = DataStore(Path(settings.data_dir))
    repo = KlineRepository(store)
    instrument_rows = instrument_sync.sync_instruments(store.data_dir)
    logger.info("标的维表已同步：%d 行", instrument_rows)

    instrument_path = store.data_dir / "instruments" / "instruments.parquet"
    instruments = pl.read_parquet(instrument_path, columns=["symbol"])
    symbols = select_market_symbols(instruments, set(args.markets))
    if not symbols:
        raise RuntimeError(f"标的维表中没有市场 {args.markets} 的证券")

    logger.info("开始回填市场=%s，标的=%d，区间=%s~%s", args.markets, len(symbols), args.start, args.end)

    def daily_progress(current: int, total: int) -> None:
        logger.info("ClickHouse 日线批次 %d/%d", current, total)

    daily_rows = kline_sync.sync_and_persist_daily_batch(
        symbols,
        repo,
        CapabilitySet(),
        start_date=datetime.combine(args.start, time.min),
        end_date=datetime.combine(args.end, time.max),
        on_chunk_done=daily_progress,
    )
    if daily_rows == 0:
        raise RuntimeError("ClickHouse 未返回日线数据，已停止 enriched 重算")

    def enriched_progress(current: int, total: int) -> None:
        logger.info("enriched 指标批次 %d/%d", current, total)

    enriched_rows = run_pipeline(
        data_dir=store.data_dir,
        symbols=symbols,
        on_batch_done=enriched_progress,
    )
    repo.rebuild_views()
    repo.clear_cache()
    repo.refresh_cache()
    logger.info("回填完成：日线 %d 行，enriched 写入 %d 行", daily_rows, enriched_rows)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
