"""Repository 层(§7.4)。

数据分层:
  - DuckDB 视图: 冷查询(统计、元数据、用户自定义SQL)
  - Polars 缓存: 热路径(enriched 最新日 ~5500行 + instruments ~5500行)
  - Polars scan_parquet: 分钟K/历史日K (predicate pushdown)

缓存生命周期:
  - startup 时不加载(数据可能为空)
  - pipeline 完成后调用 refresh_cache()
  - 服务层通过 get_enriched_latest() / get_instruments() 获取缓存
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from app.config import settings
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    bump_enriched_generation,
    get_enriched_generation,
)
from app.market_time import cn_today
from app.parquet import scan_enriched_parquet
from app.polars_guard import guarded_collect
from app.services.minute_adjust import apply_minute_adjustment, minute_basis_is_raw

logger = logging.getLogger(__name__)


def replace_with_retry(src: Path, dst: Path, *, attempts: int = 10, delay_s: float = 0.5) -> None:
    """os.replace 的 Windows 读锁重试版。

    分区 parquet 的读端 (polars scan_parquet / DuckDB read_parquet 视图) 在扫描进行
    期间持有句柄; Windows 不允许替换"仍被读端打开"的目标文件 (PermissionError,
    WinError 5), Linux 的 inode 交换语义则无此限制。读端扫描通常亚秒级完成,
    短退避重试即可穿过并发读窗口; attempts 次仍被占用则原样抛出, 由上层记录失败。
    """
    last: PermissionError | None = None
    for i in range(attempts):
        try:
            src.replace(dst)
            if i:
                logger.info("parquet replace succeeded after %d blocked attempt(s): %s", i, dst)
            return
        except PermissionError as e:
            last = e
            if i == 0:
                logger.warning(
                    "parquet replace blocked by concurrent reader, retrying (total <= %.1fs): %s",
                    attempts * delay_s, dst,
                )
            if i < attempts - 1:
                time.sleep(delay_s)
    raise last  # type: ignore[misc]  # attempts >= 1 时 last 必已赋值


def enriched_dirname(asset_type: str) -> str:
    """asset_type → enriched parquet 目录名。ETF 走独立目录, 其余(stock)用日K enriched。"""
    return "kline_etf_enriched" if asset_type == "etf" else "kline_daily_enriched"


# 盘中递推状态的最长窗口 (交易日): MA200 部分和 tail(199)、60 日动量 tail(60)
_LIVE_AGG_WINDOW_BARS = 200
_LIVE_AGG_HISTORY_DAYS = 320


def _live_agg_window_start(dates: pl.Series, latest: date, calendar_start: date) -> date:
    """盘中递推历史窗口起点: 自然日起点与「最近 200 个交易日」起点取较早者。

    自然日窗口可能不足 200 个交易日, tail(199) 会取到残缺窗口
    (与 get_enriched_history 按交易日计数同理)。
    """
    trading = dates.filter(dates <= latest).unique().sort()
    if trading.len() >= _LIVE_AGG_WINDOW_BARS:
        return min(calendar_start, trading[-_LIVE_AGG_WINDOW_BARS])
    return calendar_start


def _last_available_rows(df: pl.DataFrame, cutoff: date) -> pl.DataFrame:
    """从已按 symbol/date 排序的数据中取每只标的最后一条有效状态。"""
    if df.is_empty():
        return df
    return (
        df.filter(pl.col("date") <= cutoff)
        .group_by("symbol", maintain_order=True)
        .last()
    )


class DataStore:
    """唯一的存储入口 — 进程启动时创建。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir or settings.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 一次性数据迁移: 旧桌面版把数据放在 exe 同级的兄弟目录 TickFlowStockPanel_Data/,
        # 新版改为 {app}/data/。老用户首次启动时自动把旧数据搬过来, 无感升级。
        self._migrate_legacy_data_dir()

        # 关键子目录(§7.2)
        for sub in (
            "kline_daily",
            "kline_daily_enriched",
            "kline_index_daily",
            "kline_index_enriched",
            "kline_etf_daily",
            "kline_etf_enriched",
            "kline_etf_minute",
            "kline_minute",
            "adj_factor",
            "adj_factor_etf",
            "financials",
            "instruments",
            "instruments_index",
            "instruments_etf",
            "instruments_ext",
            "kline_ext",
            "pools",
            "backtest_results",
            "screener_results",
            "ai_cache",
            "user_data",
            "depth5",
        ):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

        # 财务数据子目录
        for sub in ("metrics", "income", "balance_sheet", "cash_flow", "shares"):
            (self.data_dir / "financials" / sub).mkdir(parents=True, exist_ok=True)

        # DuckDB 内存模式 — 不建 .db 文件(§7.1)
        self.db = duckdb.connect(database=":memory:")
        self._register_views()

    def _migrate_legacy_data_dir(self) -> None:
        """把旧桌面版数据目录 (<安装目录>/../TickFlowStockPanel_Data/) 迁移到新位置 (<安装目录>/data/)。

        背景: 旧版 data_dir = exe_dir.parent / "TickFlowStockPanel_Data" (兄弟目录),
        新版改为 exe_dir / "data" (子目录)。老用户首次升级时旧数据在兄弟目录,
        若不迁移会导致历史行情/策略/回测/监控全部"丢失"(实际还在旧位置)。

        策略 (仅打包桌面版触发, 开发/Docker 不受影响):
          1. 旧目录存在且新 data/ 还基本为空 → 整目录搬迁 (shutil.move, 跨盘符安全)。
          2. 新旧目录都已有数据 (用户在两套路径都跑过) → 不自动搬, 仅记日志, 避免覆盖。
          3. 旧目录不存在 → 新装用户, 无需迁移。
        所有异常都吞掉只记警告 —— 数据迁移失败绝不能阻塞应用启动。
        """
        # 仅打包桌面版需要迁移; 开发/Docker 模式 _PROJECT_ROOT/data 本就是唯一路径
        if not getattr(sys, "frozen", False):
            return

        import shutil

        try:
            legacy_dir = self.data_dir.parent / "TickFlowStockPanel_Data"
            if not legacy_dir.exists():
                return  # 新装用户, 无旧数据

            # 新 data/ 目录里已有实质性内容 → 用户已在新路径跑过, 不覆盖
            # (用 .parquet 作为"有真实数据"的判据, 避免空子目录误判)
            has_new_data = any(self.data_dir.rglob("*.parquet")) or any(
                self.data_dir.rglob("*.jsonl")
            )
            if has_new_data:
                logger.info(
                    "legacy data dir %s exists but new %s already has data, skip migration",
                    legacy_dir, self.data_dir,
                )
                return

            logger.info("migrating legacy data %s -> %s", legacy_dir, self.data_dir)
            # 逐项 move 而非整目录 move: data/ 可能已被 __init__ 创建了空子目录,
            # 直接 shutil.move(legacy, data) 会因目标已存在失败。
            for item in legacy_dir.iterdir():
                dest = self.data_dir / item.name
                if dest.exists():
                    # 同名子目录 (如 kline_daily): 合并内容
                    if dest.is_dir():
                        shutil.move(str(item), str(dest / item.name))
                    else:
                        item.unlink()  # 同名文件, 以新路径为准, 删旧
                else:
                    shutil.move(str(item), str(dest))
            # 搬完后清理空的旧目录
            try:
                shutil.rmtree(legacy_dir)
            except OSError:
                logger.warning("legacy dir %s not empty, kept", legacy_dir)
            logger.info("legacy data migration done")
        except Exception as e:  # noqa: BLE001
            logger.warning("legacy data migration failed (startup continues): %s", e)

    def _register_views(self) -> None:
        """把 Parquet 目录挂载为 DuckDB 视图(§7.3)。"""
        d = self.data_dir.as_posix()
        statements = [
            f"""CREATE OR REPLACE VIEW kline_daily AS
                SELECT * FROM read_parquet('{d}/kline_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_enriched AS
                SELECT * FROM read_parquet('{d}/kline_daily_enriched/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_index_daily AS
                SELECT * FROM read_parquet('{d}/kline_index_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_index_enriched AS
                SELECT * FROM read_parquet('{d}/kline_index_enriched/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_etf_daily AS
                SELECT * FROM read_parquet('{d}/kline_etf_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_etf_enriched AS
                SELECT * FROM read_parquet('{d}/kline_etf_enriched/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_etf_minute AS
                SELECT * FROM read_parquet('{d}/kline_etf_minute/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_minute AS
                SELECT * FROM read_parquet('{d}/kline_minute/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW adj_factor AS
                SELECT * FROM read_parquet('{d}/adj_factor/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW adj_factor_etf AS
                SELECT * FROM read_parquet('{d}/adj_factor_etf/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments AS
                SELECT * FROM read_parquet('{d}/instruments/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_index AS
                SELECT * FROM read_parquet('{d}/instruments_index/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_etf AS
                SELECT * FROM read_parquet('{d}/instruments_etf/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_ext AS
                SELECT * FROM read_parquet('{d}/instruments_ext/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_ext AS
                SELECT * FROM read_parquet('{d}/kline_ext/**/*.parquet', union_by_name=true)""",
            # 财务数据视图
            f"""CREATE OR REPLACE VIEW financials_metrics AS
                SELECT * FROM read_parquet('{d}/financials/metrics/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW financials_income AS
                SELECT * FROM read_parquet('{d}/financials/income/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW financials_balance_sheet AS
                SELECT * FROM read_parquet('{d}/financials/balance_sheet/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW financials_cash_flow AS
                SELECT * FROM read_parquet('{d}/financials/cash_flow/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW financials_shares AS
                SELECT * FROM read_parquet('{d}/financials/shares/*.parquet', union_by_name=true)""",
            # 五档盘口 sealed 真假涨停(独立旁路存储,不进 enriched)
            f"""CREATE OR REPLACE VIEW depth5 AS
                SELECT * FROM read_parquet('{d}/depth5/**/*.parquet', union_by_name=true)""",
        ]
        for sql in statements:
            try:
                self.db.execute(sql)
            except Exception as e:  # noqa: BLE001
                # 空数据目录(首次启动)或权限问题时 DuckDB 会抛 IOException;
                # 跨版本/平台也可能抛 CatalogException 等。空目录缺视图不影响启动
                # (后续同步写入数据后会重新刷新视图),这里一律降级为 debug 日志。
                logger.debug("view registration skipped (no parquet yet): %s", sql[:60])
        self._register_unified_views()

    def _has_parquet(self, subdir: str) -> bool:
        return any((self.data_dir / subdir).rglob("*.parquet"))

    def _register_unified_views(self) -> None:
        """Register optional all-asset views when their backing parquet exists.

        Physical storage remains split for performance and compatibility. These
        views are convenience read models for new APIs/features.
        """
        daily_parts: list[str] = []
        enriched_parts: list[str] = []
        minute_parts: list[str] = []
        inst_parts: list[str] = []

        if self._has_parquet("kline_daily"):
            daily_parts.append("""
                SELECT symbol, date, open, high, low, close, volume, amount,
                       'stock' AS asset_type, 'tickflow' AS source
                FROM kline_daily
            """)
        if self._has_parquet("kline_index_daily"):
            daily_parts.append("""
                SELECT symbol, date, open, high, low, close, volume, amount,
                       'index' AS asset_type, 'tickflow' AS source
                FROM kline_index_daily
            """)
        if self._has_parquet("kline_etf_daily"):
            daily_parts.append("""
                SELECT symbol, date, open, high, low, close, volume, amount,
                       'etf' AS asset_type, 'tickflow' AS source
                FROM kline_etf_daily
            """)

        if self._has_parquet("kline_daily_enriched"):
            enriched_parts.append("SELECT *, 'stock' AS asset_type, 'tickflow' AS source FROM kline_enriched")
        if self._has_parquet("kline_index_enriched"):
            enriched_parts.append("SELECT *, 'index' AS asset_type, 'tickflow' AS source FROM kline_index_enriched")
        if self._has_parquet("kline_etf_enriched"):
            enriched_parts.append("SELECT *, 'etf' AS asset_type, 'tickflow' AS source FROM kline_etf_enriched")

        if self._has_parquet("kline_minute"):
            minute_parts.append("""
                SELECT symbol, datetime, open, high, low, close, volume, amount,
                       'stock' AS asset_type, 'tickflow' AS source
                FROM kline_minute
            """)
        if self._has_parquet("kline_etf_minute"):
            minute_parts.append("""
                SELECT symbol, datetime, open, high, low, close, volume, amount,
                       'etf' AS asset_type, 'tickflow' AS source
                FROM kline_etf_minute
            """)

        if self._has_parquet("instruments"):
            inst_parts.append("""
                SELECT symbol, name, code, exchange, 'stock' AS asset_type, 'tickflow' AS source
                FROM instruments
            """)
        if self._has_parquet("instruments_index"):
            inst_parts.append("""
                SELECT symbol, name, code, NULL AS exchange, 'index' AS asset_type, 'tickflow' AS source
                FROM instruments_index
                WHERE coalesce(asset_type, 'index') != 'etf'
            """)
        if self._has_parquet("instruments_etf"):
            inst_parts.append("""
                SELECT symbol, name, code, NULL AS exchange, 'etf' AS asset_type, 'tickflow' AS source
                FROM instruments_etf
            """)

        unions = {
            "kline_daily_all": daily_parts,
            "kline_enriched_all": enriched_parts,
            "kline_minute_all": minute_parts,
            "instruments_all": inst_parts,
        }
        for name, parts in unions.items():
            if not parts:
                continue
            try:
                self.db.execute(f"CREATE OR REPLACE VIEW {name} AS " + " UNION ALL BY NAME ".join(parts))
            except Exception as e:  # noqa: BLE001
                logger.debug("unified view %s skipped: %s", name, e)


class KlineRepository:
    """日 K / 分钟 K 的读写入口。"""

    def __init__(self, store: DataStore) -> None:
        self.store = store
        self.db = store.db
        self._lock = threading.Lock()
        # 序列化 parquet 分区的读-改-写: 实时轮询线程、手动 refresh、盘后管道
        # 可能并发 merge/flush 同一分区文件, 无锁会互相覆盖丢数据
        self._write_lock = threading.Lock()

        # ---- Polars 缓存 ----
        self._enriched_cache: pl.DataFrame | None = None       # 最新一天 (~5500行)
        self._enriched_cache_date: date | None = None
        self._live_agg_cache: pl.DataFrame | None = None       # 预计算聚合表 (~5500行)
        self._live_agg_cache_date: date | None = None
        self._live_agg_check_date: date | None = None          # 上次跨日校验时的 today (快路径节流)
        self._instruments_cache: pl.DataFrame | None = None
        self._historical_shares_cache: pl.DataFrame | None = None
        self._historical_shares_mtime_ns: int | None = None
        # 完整 enriched 历史 (含所有指标, 供 filter_history 策略使用)
        self._enriched_history_cache: pl.DataFrame | None = None  # ~100万行
        self._enriched_history_start: date | None = None
        self._enriched_history_generation: str | None = None
        self._index_instruments_cache: pl.DataFrame | None = None
        self._etf_enriched_cache: pl.DataFrame | None = None
        self._etf_enriched_cache_date: date | None = None
        self._etf_live_agg_cache: pl.DataFrame | None = None
        self._etf_live_agg_cache_date: date | None = None
        self._etf_instruments_cache: pl.DataFrame | None = None
        # symbol 集合 memo (随对应 instruments 缓存失效): 供每请求资产分流用
        self._index_symbol_set_cache: set[str] | None = None
        self._etf_symbol_set_cache: set[str] | None = None
        self._index_enriched_cache: pl.DataFrame | None = None
        self._index_enriched_cache_date: date | None = None

        # ---- enriched 后台预热 ----
        # 启动时 compute_indicators (107万行, 低配机 50s+) 移出 lifespan 关键路径,
        # 推到 daemon 线程异步完成。预热期间 get_enriched_latest / get_live_agg
        # 返回空表 (上层优雅降级), 不触发同步重算 (否则会抵消异步化收益)。
        self._enriched_warming: bool = False
        self._warmup_thread: threading.Thread | None = None
        self._warmup_lock = threading.Lock()
        # 预热完成后的回调 (lifespan 注入, 用于设置 app.state.indicators_ready)
        self._on_warmup_done: Callable[[], None] | None = None
        # parquet/instruments 同步刷新完成后的轻量回调；用于调度派生缓存预热。
        self._on_refresh_done: Callable[[], None] | None = None

        # parquet glob 路径
        self._enriched_glob = str(store.data_dir / "kline_daily_enriched" / "**" / "*.parquet")
        self._index_enriched_glob = str(store.data_dir / "kline_index_enriched" / "**" / "*.parquet")
        self._etf_enriched_glob = str(store.data_dir / "kline_etf_enriched" / "**" / "*.parquet")
        self._minute_glob = str(store.data_dir / "kline_minute" / "**" / "*.parquet")
        self._etf_minute_glob = str(store.data_dir / "kline_etf_minute" / "**" / "*.parquet")
        self._inst_glob = str(store.data_dir / "instruments" / "**" / "*.parquet")
        self._index_inst_glob = str(store.data_dir / "instruments_index" / "**" / "*.parquet")
        self._etf_inst_glob = str(store.data_dir / "instruments_etf" / "**" / "*.parquet")

    def execute_all(self, sql: str, params: list | None = None) -> list[tuple]:
        """线程安全的 SELECT → fetchall。DuckDB 单 connection 非线程安全，所有读路径须走此方法。"""
        with self._lock:
            cursor = self.db.cursor()
            try:
                return cursor.execute(sql, params or []).fetchall()
            finally:
                cursor.close()

    def execute_one(self, sql: str, params: list | None = None) -> tuple | None:
        """线程安全的 SELECT → fetchone。"""
        with self._lock:
            cursor = self.db.cursor()
            try:
                return cursor.execute(sql, params or []).fetchone()
            finally:
                cursor.close()

    # ================================================================
    # Polars 缓存管理
    # ================================================================

    def refresh_cache(self, background: bool = False) -> None:
        """刷新 Polars 缓存。在 pipeline 完成后、服务启动时调用。

        background=True (启动时): instruments/index/ETF 同步刷新 (毫秒级),
        enriched 的重计算 (compute_indicators, 107万行) 推到 daemon 线程,
        不阻塞 FastAPI lifespan。预热期间上层走空表降级。
        background=False (盘后管道/手动刷新): 全部同步, 保证数据即时一致。
        """
        started = time.perf_counter()
        logger.info("cache refresh start (background=%s)", background)

        step = time.perf_counter()
        logger.info("cache refresh step start: instruments")
        self._refresh_instruments()
        logger.info("cache refresh step done: instruments (%.2fs)", time.perf_counter() - step)

        step = time.perf_counter()
        logger.info("cache refresh step start: index instruments")
        self._refresh_index_instruments()
        logger.info("cache refresh step done: index instruments (%.2fs)", time.perf_counter() - step)

        step = time.perf_counter()
        logger.info("cache refresh step start: ETF instruments")
        self._refresh_etf_instruments()
        logger.info("cache refresh step done: ETF instruments (%.2fs)", time.perf_counter() - step)

        # ETF enriched 只失效不重建: 下次访问时按新数据懒加载,
        # 避免自选无 ETF 的用户在管道后白付全量重算成本
        self._etf_enriched_cache = None
        self._etf_enriched_cache_date = None
        # 指数 enriched 同样只失效不重建 (懒加载)
        self._index_enriched_cache = None
        self._index_enriched_cache_date = None

        if background:
            logger.info("cache refresh: enriched 推后台线程预热")
            self._start_enriched_warmup()
        else:
            step = time.perf_counter()
            logger.info("cache refresh step start: enriched")
            self._refresh_enriched()
            logger.info("cache refresh step done: enriched (%.2fs)", time.perf_counter() - step)
            self._notify_refresh_done()

        logger.info("cache refresh done (%.2fs)", time.perf_counter() - started)

    def _start_enriched_warmup(self) -> None:
        """启动后台 daemon 线程预热 enriched 缓存 (compute_indicators)。

        仿 QuoteService 的线程模式: 设 warming 标志 → 起 daemon → 完成后清标志 +
        触发回调。重复调用时若已有线程在跑则跳过 (避免重复预热)。
        """
        with self._warmup_lock:
            if self._enriched_warming:
                logger.info("enriched warmup already in progress, skip")
                return
            self._enriched_warming = True

        def _warmup() -> None:
            t0 = time.perf_counter()
            try:
                logger.info("enriched warmup thread started")
                self._refresh_enriched()
                logger.info("enriched warmup thread done (%.1fs)", time.perf_counter() - t0)
                self._notify_refresh_done()
            except Exception:  # noqa: BLE001
                logger.exception("enriched warmup thread failed")
            finally:
                with self._warmup_lock:
                    self._enriched_warming = False
                cb = self._on_warmup_done
                if cb is not None:
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        logger.warning("enriched warmup callback failed", exc_info=True)

        self._warmup_thread = threading.Thread(
            target=_warmup, name="enriched-warmup", daemon=True,
        )
        self._warmup_thread.start()

    def _notify_refresh_done(self) -> None:
        callback = self._on_refresh_done
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            logger.warning("repository refresh callback failed", exc_info=True)

    @property
    def enriched_ready(self) -> bool:
        """enriched 缓存是否已就绪 (非 None 且不在后台预热中)。"""
        return (
            not self._enriched_warming
            and self._enriched_cache is not None
            and self._live_agg_cache is not None
        )

    def clear_cache(self) -> None:
        """清空所有 Polars 内存缓存。

        与 refresh_cache 的区别: refresh_cache 在磁盘无数据时会提前 return,
        导致内存里的旧缓存残留 (clear 数据后看板仍显示旧数据的根因)。
        本方法无条件清空, 供清除数据/重置场景调用。
        """
        self._enriched_cache = None
        self._enriched_cache_date = None
        self._enriched_history_cache = None
        self._enriched_history_start = None
        self._enriched_history_generation = None
        self._live_agg_cache = None
        self._live_agg_cache_date = None
        self._live_agg_check_date = None
        self._instruments_cache = None
        self._index_instruments_cache = None
        self._etf_enriched_cache = None
        self._etf_enriched_cache_date = None
        self._etf_live_agg_cache = None
        self._etf_live_agg_cache_date = None
        self._etf_instruments_cache = None
        self._index_symbol_set_cache = None
        self._etf_symbol_set_cache = None
        self._name_map_cache = None
        self._index_enriched_cache = None
        self._index_enriched_cache_date = None

    def _refresh_enriched(self) -> None:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        with shared_heavy_job_limiter.slot("exclusive"):
            self._refresh_enriched_impl()

    def _refresh_enriched_impl(self) -> None:
        """从 parquet 加载 enriched 最新日到内存 + 构建聚合表。

        enriched parquet 仅存 14 列基础数据。启动时读入历史数据并即时计算完整指标，
        将结果缓存在内存中供各服务使用。

        优化: 扩大历史读取范围, 同时缓存完整历史 (含指标), 供 filter_history 策略直接复用。
        """
        try:
            started = time.perf_counter()
            refresh_generation = self.get_matrix_data_generation("stock")
            logger.info("enriched refresh start")

            step = time.perf_counter()
            logger.info("enriched refresh step start: latest date")
            latest = self._latest_enriched_date_duckdb()
            logger.info("enriched refresh step done: latest date=%s (%.2fs)", latest, time.perf_counter() - step)
            if not latest:
                # 磁盘已无数据: 必须清空内存缓存, 否则旧数据会残留
                # (清数据后看板仍显示旧数据的根因)
                self.clear_cache()
                logger.info("enriched refresh skipped: no latest date (%.2fs)", time.perf_counter() - started)
                return

            # Step 1: 直接读最新日期的分区文件 (仅 14 列)
            enriched_dir = self.store.data_dir / "kline_daily_enriched"
            ds = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)
            target_parquet = enriched_dir / f"date={ds}" / "part.parquet"

            if not target_parquet.exists():
                logger.info("enriched refresh skipped: %s not found (%.2fs)", target_parquet, time.perf_counter() - started)
                return

            step = time.perf_counter()
            logger.info("enriched refresh step start: read latest parquet %s", target_parquet)
            df_latest = pl.read_parquet(target_parquet)
            logger.info("enriched refresh step done: read latest parquet rows=%d (%.2fs)", len(df_latest), time.perf_counter() - step)
            if df_latest.is_empty():
                logger.info("enriched refresh skipped: latest parquet empty (%.2fs)", time.perf_counter() - started)
                return

            # Step 2: 读近 300 天 14 列数据 → compute → filter(latest) → 缓存
            # 300 日历天 ≈ 210 交易日, 覆盖 filter_history 最大 lookback(90) + warmup(200)
            try:
                from datetime import timedelta
                from app.indicators.pipeline import compute_enriched_history_window
                start_full = latest - timedelta(days=300)
                read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                         "volume", "amount", "raw_close", "raw_high", "raw_low"]
                             if c in df_latest.columns]
                lf = (
                    scan_enriched_parquet(self._enriched_glob)
                    .filter(pl.col("date") >= start_full)
                    .sort(["symbol", "date"])
                )

                step = time.perf_counter()
                logger.info("enriched refresh step start: collect history from %s", start_full)
                df_hist = guarded_collect(lf.select(read_cols), priority="background")
                logger.info("enriched refresh step done: collect history rows=%d (%.2fs)", len(df_hist), time.perf_counter() - step)
                if not df_hist.is_empty():
                    instruments = self._instruments_cache if self._instruments_cache is not None else pl.DataFrame()

                    # 分批计算并关联元数据, 保留完整历史, 限制宽表临时副本。
                    step = time.perf_counter()
                    logger.info("enriched refresh step start: compute window (batched)")
                    df_full = compute_enriched_history_window(
                        df_hist,
                        self.store.data_dir,
                        instruments=instruments,
                        historical_shares=(
                            self.get_historical_shares()
                            if instruments is not None and not instruments.is_empty()
                            else None
                        ),
                        include_instrument_metadata=True,
                    )
                    del df_hist
                    logger.info("enriched refresh step done: compute window rows=%d (%.2fs)",
                                len(df_full), time.perf_counter() - step)

                    # 缓存完整历史 (含指标+必要基础信息) 供 filter_history/backtest 直接复用
                    if self.get_matrix_data_generation("stock") != refresh_generation:
                        raise EnrichedGenerationUnavailableError(
                            "enriched data changed while refreshing its history cache"
                        )
                    self._enriched_history_cache = df_full
                    self._enriched_history_start = df_full["date"].min()
                    self._enriched_history_generation = refresh_generation
                    logger.info("enriched 历史缓存: %d rows, %s ~ %s",
                                len(df_full), self._enriched_history_start, latest)

                    # 只取最新一天作为 enriched_cache
                    df_today = df_full.filter(pl.col("date") == latest)
                    if not df_today.is_empty():
                        self._enriched_cache = df_today
                        self._enriched_cache_date = latest
                        # 构建盘中递推基准: 若最新分区是今天的实时盘中数据,
                        # 递推状态必须停在上一交易日, 不能把今天作为“昨日”。
                        step = time.perf_counter()
                        logger.info("enriched refresh step start: build live agg")
                        self._build_live_agg(self._live_agg_baseline_date(latest))
                        logger.info("enriched refresh step done: build live agg (%.2fs)", time.perf_counter() - step)
                        repaired_today = self._restore_missing_latest_rows(
                            latest, df_today, df_full,
                        )
                        if len(repaired_today) > len(df_today):
                            df_full = pl.concat(
                                [df_full.filter(pl.col("date") != latest), repaired_today],
                                how="diagonal_relaxed",
                            ).sort(["symbol", "date"])
                            self._enriched_history_cache = df_full
                            self._enriched_cache = repaired_today
                            df_today = repaired_today
                        logger.info("enriched 缓存已计算: %d 只, 日期 %s (即时计算)", len(df_today), latest)
                        logger.info("enriched refresh done (%.2fs)", time.perf_counter() - started)
                        return
            except EnrichedGenerationUnavailableError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("enriched 即时计算失败, 使用原始 14 列缓存: %s", e)

            # 降级: 直接使用 14 列数据 + 构建 live_agg
            self._enriched_cache = df_latest
            self._enriched_cache_date = latest
            step = time.perf_counter()
            logger.info("enriched refresh fallback step start: build live agg")
            self._build_live_agg(self._live_agg_baseline_date(latest))
            logger.info("enriched refresh fallback step done: build live agg (%.2fs)", time.perf_counter() - step)

            logger.info("enriched 缓存已加载: %d 只, 日期 %s", len(df_latest), latest)
            logger.info("enriched refresh done fallback (%.2fs)", time.perf_counter() - started)
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched 缓存刷新失败: %s", e)

    def _restore_missing_latest_rows(
        self,
        latest: date,
        df_today: pl.DataFrame,
        history: pl.DataFrame,
    ) -> pl.DataFrame:
        """用同日原始日K补齐旧实时快照漏写的正常成交股票。"""
        if self._live_agg_cache is None or self._live_agg_cache.is_empty():
            return df_today
        daily_path = (
            self.store.data_dir
            / "kline_daily"
            / f"date={latest.isoformat()}"
            / "part.parquet"
        )
        if not daily_path.exists():
            return df_today

        try:
            from app.indicators.pipeline import compute_enriched_today, filter_halt_days

            daily = filter_halt_days(pl.read_parquet(daily_path))
            missing = daily.join(
                df_today.select("symbol").unique(),
                on="symbol",
                how="anti",
            )
            if missing.is_empty():
                return df_today

            missing_symbols = missing.select("symbol").unique()
            previous = history.filter(pl.col("date") < latest).join(
                missing_symbols,
                on="symbol",
                how="semi",
            )
            if not previous.is_empty():
                previous = _last_available_rows(previous, latest)
            recovered = compute_enriched_today(
                self._live_agg_cache,
                previous,
                missing,
                self.get_instruments(),
            )
            if recovered.is_empty():
                return df_today
            recovered = self._with_instrument_metadata("stock", recovered)
            result = pl.concat(
                [df_today, recovered],
                how="diagonal_relaxed",
            ).unique(subset=["symbol", "date"], keep="last").sort("symbol")
            logger.info(
                "enriched latest cache restored from daily: date=%s, rows=%d",
                latest,
                len(result) - len(df_today),
            )
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched latest cache restore skipped: %s", e)
            return df_today

    def _build_live_agg(self, latest: date) -> None:
        """从 OHLCV 即时计算递推状态 + 窗口聚合, 构建盘中实时聚合表。

        优化: 优先使用 _enriched_history_cache (启动时已计算), 避免重复 compute_indicators。
        """
        from datetime import timedelta
        from app.indicators.pipeline import _ema_alpha

        started = time.perf_counter()
        logger.info("live agg build start: latest=%s", latest)
        start_history = latest - timedelta(days=_LIVE_AGG_HISTORY_DAYS)
        # EMA12/26、RSI 递推状态的暖机来源; None = 与窗口切片 df_hist 同源 (降级路径)
        ewm_history: pl.DataFrame | None = None

        # 优先使用已有的历史缓存 (避免重复 scan_parquet + compute_indicators)
        if self._enriched_history_cache is not None and not self._enriched_history_cache.is_empty():
            hist_all = self._enriched_history_cache
            if "date" in hist_all.columns and hist_all["date"].n_unique() > 0:
                # 从历史缓存中提取所需列 (历史缓存已有指标列)
                base_cols = [
                    "symbol", "date", "open", "high", "low", "close", "volume",
                    "raw_close", "raw_high", "raw_low",
                    "consecutive_limit_ups", "consecutive_limit_downs",
                ]
                needed = [c for c in base_cols if c in hist_all.columns]
                step = time.perf_counter()
                logger.info("live agg step start: slice history cache")
                window_start = _live_agg_window_start(hist_all["date"], latest, start_history)
                df_hist = hist_all.select(needed).filter(
                    (pl.col("date") >= window_start) & (pl.col("date") <= latest)
                ).sort(["symbol", "date"])
                logger.info("live agg step done: slice history cache rows=%d (%.2fs)", len(df_hist), time.perf_counter() - step)

                state_cols = [
                    "symbol",
                    "ema5", "ema10", "ema20", "ema30", "ema60",
                    "macd_dea",
                    "kdj_k", "kdj_d",
                    "atr_14",
                    "close", "high", "low",
                    "annual_vol_20d",
                ]
                existing_state = [c for c in state_cols if c in hist_all.columns]
                state_source = _last_available_rows(
                    hist_all.select("date", *existing_state), latest,
                )
                agg_a = state_source.select(existing_state)
                # ema5~ema60 / macd_dea 等状态取自完整历史缓存; _ema12/_ema26 与 RSI 也必须
                # 同源暖机, 只用 90 天窗口递推会与 macd_dea、盘后全量口径不一致
                ewm_history = hist_all.select("symbol", "date", "close")
            else:
                df_hist = pl.DataFrame()
                agg_a = pl.DataFrame()
        else:
            # 降级: 读 parquet + compute_indicators
            df_hist, agg_a = self._build_live_agg_from_parquet(latest, start_history)

        if df_hist.is_empty():
            self._live_agg_cache = pl.DataFrame()
            self._live_agg_cache_date = None
            logger.info("live agg build skipped: empty history (%.2fs)", time.perf_counter() - started)
            return

        if agg_a.is_empty():
            self._live_agg_cache = pl.DataFrame()
            self._live_agg_cache_date = None
            logger.info("live agg build skipped: empty state (%.2fs)", time.perf_counter() - started)
            return

        ewm_source = df_hist if ewm_history is None else ewm_history

        # 单独计算 _ema12 / _ema26 (compute_indicators 内部会 drop 掉)
        step = time.perf_counter()
        logger.info("live agg step start: ema state")
        df_ema = _last_available_rows(
            ewm_source.sort(["symbol", "date"]).with_columns([
                pl.col("close").ewm_mean(alpha=_ema_alpha(12), adjust=False).over("symbol").alias("_ema12"),
                pl.col("close").ewm_mean(alpha=_ema_alpha(26), adjust=False).over("symbol").alias("_ema26"),
            ]).select("symbol", "date", "_ema12", "_ema26"),
            latest,
        ).select("symbol", "_ema12", "_ema26")

        agg_a = agg_a.join(df_ema, on="symbol", how="inner")
        logger.info("live agg step done: ema state (%.2fs)", time.perf_counter() - step)

        # 单独计算 RSI 状态列 (compute_indicators 内部会 drop 掉)
        step = time.perf_counter()
        logger.info("live agg step start: rsi state")
        df_rsi_base = ewm_source.sort(["symbol", "date"]).with_columns(
            pl.col("close").diff().over("symbol").alias("_daily_delta")
        )
        gain = pl.when(pl.col("_daily_delta") > 0).then(pl.col("_daily_delta")).otherwise(0.0)
        loss = pl.when(pl.col("_daily_delta") < 0).then(-pl.col("_daily_delta")).otherwise(0.0)
        rsi_exprs = []
        for n in (6, 14, 24):
            a = 1.0 / n
            rsi_exprs.append(gain.ewm_mean(alpha=a, adjust=False).over("symbol").alias(f"_rsi_avg_gain_{n}"))
            rsi_exprs.append(loss.ewm_mean(alpha=a, adjust=False).over("symbol").alias(f"_rsi_avg_loss_{n}"))
        df_rsi = (
            _last_available_rows(
                df_rsi_base.with_columns(rsi_exprs), latest,
            )
            .select("symbol", *[f"_rsi_avg_gain_{n}" for n in (6, 14, 24)],
                              *[f"_rsi_avg_loss_{n}" for n in (6, 14, 24)])
        )
        agg_a = agg_a.join(df_rsi, on="symbol", how="inner")
        logger.info("live agg step done: rsi state (%.2fs)", time.perf_counter() - step)

        # 前复权因子: adj_factor = close(复权) / raw_close(原始)
        if "raw_close" in df_hist.columns:
            step = time.perf_counter()
            logger.info("live agg step start: adj factor state")
            adj_factor_df = (
                _last_available_rows(
                    df_hist.select("symbol", "date", "close", "raw_close"), latest,
                )
                .select("symbol", (pl.col("close") / pl.col("raw_close")).alias("_adj_factor"))
            )
            agg_a = agg_a.join(adj_factor_df, on="symbol", how="left")
            if "_adj_factor" in agg_a.columns:
                agg_a = agg_a.with_columns(pl.col("_adj_factor").fill_null(1.0))
            logger.info("live agg step done: adj factor state (%.2fs)", time.perf_counter() - step)

        # annual_vol_20d 递推状态: 最近 19 天日收益率的部分和 / 平方和
        step = time.perf_counter()
        logger.info("live agg step start: annual vol state")
        df_daily_pct = (
            df_hist.sort(["symbol", "date"])
            .with_columns(
                pl.col("close").pct_change().over("symbol").alias("_daily_pct")
            )
        )
        df_vol = df_daily_pct.group_by("symbol").agg([
            pl.col("_daily_pct").tail(19).sum().alias("_vol_19d_pct_sum"),
            (pl.col("_daily_pct") ** 2).tail(19).sum().alias("_vol_19d_pct_sq_sum"),
        ])
        agg_a = agg_a.join(df_vol, on="symbol", how="left")
        logger.info("live agg step done: annual vol state (%.2fs)", time.perf_counter() - step)

        # 昨日连板数: 使用每只股票最后一个有效交易日状态 (用于增量计算同向 +1)
        step = time.perf_counter()
        logger.info("live agg step start: consecutive state")
        consec_cols = [c for c in ["symbol", "consecutive_limit_ups", "consecutive_limit_downs"]
                       if c in df_hist.columns]
        consec_source = df_hist
        if len(consec_cols) != 3:
            lf = (
                scan_enriched_parquet(self._enriched_glob)
                .filter((pl.col("date") >= start_history) & (pl.col("date") <= latest))
                .sort(["symbol", "date"])
            )
            consec_cols = [
                c for c in ["symbol", "consecutive_limit_ups", "consecutive_limit_downs"]
                if c in lf.collect_schema().names()
            ]
            consec_source = guarded_collect(lf.select("date", *consec_cols), priority="background")
        if len(consec_cols) == 3:
            consec_df = _last_available_rows(
                consec_source.select("date", *consec_cols), latest,
            )
            if not consec_df.is_empty():
                consec = consec_df.select(
                    "symbol",
                    pl.col("consecutive_limit_ups").alias("_prev_consec_up"),
                    pl.col("consecutive_limit_downs").alias("_prev_consec_down"),
                )
                agg_a = agg_a.join(consec, on="symbol", how="left")
        logger.info("live agg step done: consecutive state (%.2fs)", time.perf_counter() - step)

        # B类: 按 symbol 分组聚合 — 窗口统计
        step = time.perf_counter()
        logger.info("live agg step start: rolling windows")
        agg_b = (
            df_hist.sort(["symbol", "date"])
            .group_by("symbol")
            .agg([
                pl.col("close").tail(4).sum().alias("_ma5_partial_sum"),
                pl.col("close").tail(9).sum().alias("_ma10_partial_sum"),
                pl.col("close").tail(19).sum().alias("_ma20_partial_sum"),
                pl.col("close").tail(29).sum().alias("_ma30_partial_sum"),
                pl.col("close").tail(59).sum().alias("_ma60_partial_sum"),
                pl.col("close").tail(119).sum().alias("_ma120_partial_sum"),
                pl.col("close").tail(199).sum().alias("_ma200_partial_sum"),

                pl.col("close").tail(19).sum().alias("_boll_partial_sum"),
                (pl.col("close").tail(19) ** 2).sum().alias("_boll_partial_sq_sum"),

                # 60 日极值为收盘价口径 (与 compute_indicators / 回测矩阵 high_60d 一致)
                pl.col("close").tail(59).max().alias("_high_59d"),
                pl.col("close").tail(59).min().alias("_low_59d"),

                # 异动偏离 deviate_3d 用 (与 5d/10d/30d 同语义: 尾部第 N 个收盘)
                pl.col("close").tail(3).first().alias("_close_3d_ago"),
                pl.col("close").tail(5).first().alias("_close_5d_ago"),
                pl.col("close").tail(10).first().alias("_close_10d_ago"),
                pl.col("close").tail(20).first().alias("_close_20d_ago"),
                pl.col("close").tail(30).first().alias("_close_30d_ago"),
                pl.col("close").tail(60).first().alias("_close_60d_ago"),

                pl.col("volume").tail(4).sum().alias("_vol_ma5_partial_sum"),
                pl.col("volume").tail(9).sum().alias("_vol_ma10_partial_sum"),
                # 标准量比分母: 前5日成交量之和(不含当天), 用于 vol_ratio_5d
                pl.col("volume").tail(5).sum().alias("_vol_ma5_prev_sum"),

                pl.col("low").tail(8).min().alias("_kdj_8d_low"),
                pl.col("high").tail(8).max().alias("_kdj_8d_high"),

                # 窗口内实际 K 线根数 (≤ 窗口天数): compute_enriched_today 据此把
                # 历史不足的窗口指标置空, 与全量 rolling(窗口满才出值) 同口径
                pl.len().alias("_window_len"),
            ])
        )

        self._live_agg_cache = agg_a.join(agg_b, on="symbol", how="inner")
        self._live_agg_cache_date = latest
        logger.info("live agg step done: rolling windows (%.2fs)", time.perf_counter() - step)
        logger.info("live agg build done: rows=%d (%.2fs)", len(self._live_agg_cache), time.perf_counter() - started)

    def _live_agg_baseline_date(self, latest: date) -> date:
        """盘中递推基准日期。当天实时分区存在时使用上一可用交易日。"""
        if latest != cn_today():
            return latest
        try:
            row = self.execute_one(
                "SELECT max(date) FROM kline_enriched WHERE date < ?",
                [latest],
            )
            if row and row[0]:
                d = row[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:  # noqa: BLE001
            pass
        return latest

    def _build_live_agg_from_parquet(self, latest: date, start_history: date) -> tuple[pl.DataFrame, pl.DataFrame]:
        """降级路径: 从 parquet 读取数据并计算指标 (当 _enriched_history_cache 不可用时)。"""
        from app.indicators.pipeline import compute_indicators

        lf = (
            scan_enriched_parquet(self._enriched_glob)
            .filter(pl.col("date") >= start_history)
            .filter(pl.col("date") <= latest)
            .sort(["symbol", "date"])
        )

        read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close", "volume",
                                 "raw_close", "raw_high", "raw_low",
                                 "consecutive_limit_ups", "consecutive_limit_downs"]
                     if c in lf.collect_schema().names()]
        df_hist = guarded_collect(lf.select(read_cols), priority="background")

        if df_hist.is_empty():
            return df_hist, pl.DataFrame()
        window_start = _live_agg_window_start(df_hist["date"], latest, start_history)
        df_hist = df_hist.filter(pl.col("date") >= window_start)

        df_with_indicators = compute_indicators(df_hist)

        state_cols = [
            "symbol",
            "ema5", "ema10", "ema20", "ema30", "ema60",
            "macd_dea",
            "kdj_k", "kdj_d",
            "atr_14",
            "close", "high", "low",
            "annual_vol_20d",
        ]
        existing_state = [c for c in state_cols if c in df_with_indicators.columns]
        agg_a = _last_available_rows(
            df_with_indicators.select("date", *existing_state), latest,
        ).select(existing_state)

        return df_hist, agg_a

    def _refresh_etf_enriched(self) -> None:
        """从 ETF enriched parquet 加载最新日到内存缓存。"""
        try:
            enriched_dir = self.store.data_dir / "kline_etf_enriched"
            dates = sorted(
                p.name[5:] for p in enriched_dir.glob("date=*")
                if p.is_dir() and p.name.startswith("date=")
            ) if enriched_dir.exists() else []
            if not dates:
                self._etf_enriched_cache = None
                self._etf_enriched_cache_date = None
                return
            latest = date.fromisoformat(dates[-1])
            target_parquet = enriched_dir / f"date={dates[-1]}" / "part.parquet"
            df_latest = pl.read_parquet(target_parquet)
            if df_latest.is_empty():
                return

            from datetime import timedelta
            from app.indicators.pipeline import compute_indicators, compute_signals
            start_full = latest - timedelta(days=300)
            read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                     "volume", "amount", "raw_close", "raw_high", "raw_low"]
                         if c in df_latest.columns]
            df_hist = guarded_collect(
                scan_enriched_parquet(self._etf_enriched_glob,
                                cast_options=pl.ScanCastOptions(integer_cast="allow-float"))
                .filter(pl.col("date") >= start_full)
                .select(read_cols)
                .sort(["symbol", "date"]),
                priority="background",
            )
            if df_hist.is_empty():
                self._etf_enriched_cache = df_latest.sort(["symbol"])
            else:
                df_full = compute_signals(compute_indicators(df_hist))
                self._etf_enriched_cache = df_full.filter(pl.col("date") == latest).sort(["symbol"])
            self._etf_enriched_cache_date = latest
        except Exception as e:  # noqa: BLE001
            logger.debug("ETF enriched 缓存刷新跳过: %s", e)

    def _refresh_index_enriched(self) -> None:
        """从指数 enriched parquet 加载最新日到内存缓存 (300天重算通用指标)。

        磁盘窄表无指标列, 必须 scan 近 300 天重算, 否则监控信号规则无列可评估。
        指数无复权需求, 不读 raw_close/raw_high/raw_low。
        """
        try:
            enriched_dir = self.store.data_dir / "kline_index_enriched"
            dates = sorted(
                p.name[5:] for p in enriched_dir.glob("date=*")
                if p.is_dir() and p.name.startswith("date=")
            ) if enriched_dir.exists() else []
            if not dates:
                self._index_enriched_cache = None
                self._index_enriched_cache_date = None
                return
            latest = date.fromisoformat(dates[-1])
            target_parquet = enriched_dir / f"date={dates[-1]}" / "part.parquet"
            df_latest = pl.read_parquet(target_parquet)
            if df_latest.is_empty():
                return

            from datetime import timedelta
            start_full = latest - timedelta(days=300)
            read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                     "volume", "amount"]
                         if c in df_latest.columns]
            df_hist = guarded_collect(
                scan_enriched_parquet(self._index_enriched_glob,
                                cast_options=pl.ScanCastOptions(integer_cast="allow-float"))
                .filter(pl.col("date") >= start_full)
                .select(read_cols)
                .sort(["symbol", "date"]),
                priority="background",
            )
            if df_hist.is_empty():
                self._index_enriched_cache = df_latest.sort(["symbol"])
            else:
                df_full = self._compute_index_enriched_range(df_hist)
                self._index_enriched_cache = df_full.filter(pl.col("date") == latest).sort(["symbol"])
            self._index_enriched_cache_date = latest
        except Exception as e:  # noqa: BLE001
            logger.debug("指数 enriched 缓存刷新跳过: %s", e)

    def _refresh_instruments(self) -> None:
        """加载 instruments 到内存。"""
        try:
            df = guarded_collect(pl.scan_parquet(self._inst_glob), priority="background")
            if not df.is_empty():
                self._instruments_cache = df
                self._name_map_cache = None
                logger.info("instruments 缓存已加载: %d 只", len(df))
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments 缓存刷新失败: %s", e)

    def _refresh_index_instruments(self) -> None:
        """加载指数 instruments 到内存。"""
        try:
            df = guarded_collect(pl.scan_parquet(self._index_inst_glob), priority="background")
            if not df.is_empty():
                self._index_instruments_cache = df
                self._index_symbol_set_cache = None
                self._name_map_cache = None
                logger.info("index instruments 缓存已加载: %d 只", len(df))
        except Exception as e:  # noqa: BLE001
            logger.debug("index instruments 缓存刷新跳过: %s", e)

    def _refresh_etf_instruments(self) -> None:
        """加载 ETF instruments 到内存；兼容旧版 instruments_index 中的 ETF。"""
        parts: list[pl.DataFrame] = []
        try:
            df = guarded_collect(pl.scan_parquet(self._etf_inst_glob), priority="background")
            if not df.is_empty():
                parts.append(df)
        except Exception as e:  # noqa: BLE001
            logger.debug("etf instruments 缓存刷新跳过(new): %s", e)
        try:
            legacy = self.get_index_instruments()
            if not legacy.is_empty() and "asset_type" in legacy.columns:
                legacy = legacy.filter(pl.col("asset_type") == "etf")
                if not legacy.is_empty():
                    parts.append(legacy)
        except Exception as e:  # noqa: BLE001
            logger.debug("etf instruments legacy fallback skipped: %s", e)
        if parts:
            df_all = pl.concat(parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
            self._etf_instruments_cache = df_all
            self._etf_symbol_set_cache = None
            self._name_map_cache = None
            logger.info("ETF instruments 缓存已加载: %d 只", len(df_all))

    def get_enriched_latest(self) -> tuple[pl.DataFrame, date | None]:
        """返回缓存的 enriched 最新日 DataFrame + 日期。如无缓存则懒加载。

        后台预热期间 (_enriched_warming=True) 返回空表, 不触发同步重算 ——
        否则首次访问会把异步化想避免的 50s+ 计算拉回同步路径。
        """
        if self._enriched_cache is None:
            if self._enriched_warming:
                return pl.DataFrame(), None
            self._refresh_enriched()
        if self._enriched_cache is None:
            return pl.DataFrame(), self._enriched_cache_date
        return self._enriched_cache, self._enriched_cache_date

    def get_enriched_latest_asset(self, asset_type: str, refresh: bool = True) -> tuple[pl.DataFrame, date | None]:
        """按资产类型返回最新 enriched 缓存。stock 保持旧缓存语义。

        refresh=False: 缓存冷时不触发同步 _refresh_etf_enriched(300 天 scan+compute)。
        供行情轮询线程使用 —— 避免在热路径上做重活阻塞股票行情/告警;缓存由 ETF 实时
        flush 焐热, 未焐热(无 ETF 实时数据)时返回空表, 本轮跳过 ETF 评估即可。
        """
        if asset_type == "stock":
            return self.get_enriched_latest()
        if asset_type == "etf":
            if self._etf_enriched_cache is None and refresh:
                self._refresh_etf_enriched()
            if self._etf_enriched_cache is None:
                return pl.DataFrame(), self._etf_enriched_cache_date
            return self._etf_enriched_cache, self._etf_enriched_cache_date
        if asset_type == "index":
            if self._index_enriched_cache is None and refresh:
                self._refresh_index_enriched()
            if self._index_enriched_cache is None:
                return pl.DataFrame(), self._index_enriched_cache_date
            return self._index_enriched_cache, self._index_enriched_cache_date
        return pl.DataFrame(), None

    def get_enriched_history(self, target_date: date, lookback_days: int) -> pl.DataFrame | None:
        """返回预计算的 enriched 历史数据 (仅 lookback 范围, 不含 warmup)。

        warmup 部分在 _refresh_enriched 计算指标时已使用, 策略只需要最终的 lookback 窗口。
        返回 ~33万行 (90日历天) 而非 ~107万行, filter_history 策略的 group_by 快 20x+。
        """
        cache = self._enriched_history_cache
        if cache is None or cache.is_empty():
            return None
        if "date" not in cache.columns:
            return None
        cache_max = cache["date"].max()
        cache_min = cache["date"].min()
        from datetime import timedelta
        # 验证缓存覆盖完整范围 (含 warmup)。lookback_days 是交易日语义, 用 ×2 日历日
        # 放宽确保覆盖 (节假日/周末), 与 warmup 60 一起留足余量。
        warmup_start = target_date - timedelta(days=(lookback_days + 60) * 2)
        if cache_min > warmup_start or cache_max < target_date:
            return None
        # 按交易日计数裁剪: 从数据里实际存在的交易日序列取最后 lookback_days 个交易日。
        # 不能用 timedelta(days=N) (自然日), 否则周末/节假日会让窗口只有 ~N×5/7 个交易日,
        # 导致 filter_history 策略的滚动窗口/行号差(_gap)漏算, 与回测结果不一致。
        # 只数目标日及之前的交易日: 历史日期选股时缓存里还有更晚的交易日
        trading_dates = cache["date"].filter(cache["date"] <= target_date).unique().sort()
        if len(trading_dates) > lookback_days:
            lookback_start = trading_dates[-(lookback_days + 1)]
        else:
            lookback_start = trading_dates[0]
        return cache.filter((pl.col("date") >= lookback_start) & (pl.col("date") <= target_date))

    def get_enriched_range(
        self,
        start: date,
        end: date,
        symbols: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> pl.DataFrame | None:
        """从预计算 enriched 历史缓存返回完整区间；缓存不覆盖时返回 None。"""
        if self._enriched_history_cache is None:
            if self._enriched_warming:
                # 后台预热中: 返回 None (缓存不覆盖), 调用方各自走慢路径;
                # 否则请求线程会与预热线程并发重复 300 天全量重算
                # (同 get_enriched_latest 的守卫语义)。
                return None
            self._refresh_enriched()
        cache = self._enriched_history_cache
        data_dir = getattr(getattr(self, "store", None), "data_dir", None)
        if data_dir is not None:
            try:
                current_generation = self.get_matrix_data_generation("stock")
            except EnrichedGenerationUnavailableError:
                return None
            if self._enriched_history_generation != current_generation:
                return None
        if cache is None or cache.is_empty() or "date" not in cache.columns:
            return None

        cache_min = cache["date"].min()
        cache_max = cache["date"].max()
        if cache_min > start or cache_max < end:
            return None

        df = cache
        if columns:
            existing = [c for c in columns if c in df.columns]
            if "symbol" not in existing and "symbol" in df.columns:
                existing.insert(0, "symbol")
            if "date" not in existing and "date" in df.columns:
                existing.insert(1, "date")
            df = df.select(list(dict.fromkeys(existing)))
        df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if symbols is not None:
            df = df.filter(pl.col("symbol").is_in(symbols))
        if columns:
            # 保持旧接口空结果的完整 schema, 非空时沿用请求列校验。
            df = cache.clear() if df.is_empty() else df.select(existing)
        return df.sort(["symbol", "date"])

    def get_enriched_history_span(self) -> tuple[date, date] | None:
        """内存 enriched 历史缓存的可用日期区间 (含端点); 不可用/预热中返回 None。

        纯读 O(1), 不触发刷新: 调用方 (随行情 tick 反复调用的自选 enriched 端点) 需要
        区分「预热中」与「日期超窗」, 而 get_enriched_range 对两者都返回 None。
        """
        cache = self._enriched_history_cache
        start = self._enriched_history_start
        if cache is None or cache.is_empty() or start is None:
            return None
        # 历史缓存与最新日缓存同批写入/清空, end 通常即 _enriched_cache_date;
        # 仅当首次刷新在写 latest 之前中断时缺失, 回退取日期列最大值
        return start, self._enriched_cache_date or cache["date"].max()

    def get_live_agg(self) -> pl.DataFrame:
        """返回盘中实时指标预计算聚合表。如无缓存则懒加载。

        live_agg 的核心列 _prev_consec_up/down (昨日连板数) 取自基准日 enriched。
        基准日由 _live_agg_baseline_date 决定: 盘中(today 有实时分区) 取上一交易日,
        非盘中(磁盘最新日 < today) 取该最新日本身。一旦跨日, 期望基准日会前移,
        旧缓存会把连板数整体少算一档, 故这里除首次懒加载外还要校验基准日是否仍
        符合当前预期, 不符则重建 (无需等盘后管道刷缓存)。

        性能: get_live_agg 被每轮实时行情调用 (expert 档 1s 一次)。跨日只在
        date.today() 翻天时发生, 故先用 today 做廉价的 fast-path (μs 级),
        仅当 today 变化时才查磁盘确认 (DuckDB 扫 132 万行约 100ms+) 并按需重建。
        """
        if self._live_agg_cache is None:
            if self._enriched_warming:
                # 后台预热中: 返回空表, 不触发同步重算 (同 get_enriched_latest 守卫)
                return pl.DataFrame()
            self._refresh_enriched()
            self._live_agg_check_date = cn_today()  # 刚建过, 当天不必再查磁盘
        else:
            today = cn_today()
            if self._live_agg_check_date != today:
                # today 翻天了 (次日开盘首次轮询): 校验基准日是否需要前移重建。
                # 同一天内多次调用直接跳过, 避免每轮都扫 parquet。
                self._live_agg_check_date = today
                disk_latest = self._latest_enriched_date_duckdb()
                if disk_latest is not None:
                    expected = self._live_agg_baseline_date(disk_latest)
                    if self._live_agg_cache_date != expected:
                        logger.info(
                            "live_agg 跨日失效, 重建: 缓存基准=%s, 期望基准=%s",
                            self._live_agg_cache_date, expected,
                        )
                        self._refresh_enriched()
        if self._live_agg_cache is None:
            return pl.DataFrame()
        return self._live_agg_cache

    def get_instruments(self) -> pl.DataFrame:
        """返回缓存的 instruments DataFrame。如无缓存则懒加载。"""
        if self._instruments_cache is None:
            self._refresh_instruments()
        if self._instruments_cache is None:
            return pl.DataFrame()
        return self._instruments_cache

    def get_historical_shares(self) -> pl.DataFrame:
        """读取财务股本历史，并在文件更新后自动刷新缓存。"""
        path = self.store.data_dir / "financials" / "shares" / "part.parquet"
        mtime_ns = path.stat().st_mtime_ns if path.exists() else None
        if self._historical_shares_cache is None or mtime_ns != self._historical_shares_mtime_ns:
            from app.share_capital import load_share_history
            self._historical_shares_cache = load_share_history(self.store.data_dir)
            self._historical_shares_mtime_ns = mtime_ns
        return self._historical_shares_cache

    def get_index_instruments(self) -> pl.DataFrame:
        """返回缓存的指数 instruments DataFrame。如无缓存则懒加载。"""
        if self._index_instruments_cache is None:
            self._refresh_index_instruments()
        if self._index_instruments_cache is None:
            return pl.DataFrame()
        return self._index_instruments_cache

    def get_etf_instruments(self) -> pl.DataFrame:
        """返回缓存的 ETF instruments DataFrame；兼容旧版 instruments_index 中的 ETF。"""
        if self._etf_instruments_cache is None:
            self._refresh_etf_instruments()
        if self._etf_instruments_cache is None:
            return pl.DataFrame()
        return self._etf_instruments_cache

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        """按资产类型返回 instruments；老 stock 路径保持原样。"""
        if asset_type == "stock":
            return self.get_instruments()
        if asset_type == "index":
            df = self.get_index_instruments()
            if not df.is_empty() and "asset_type" in df.columns:
                return df.filter(pl.col("asset_type") != "etf")
            return df
        if asset_type == "etf":
            return self.get_etf_instruments()
        return pl.DataFrame()

    def get_index_symbol_set(self) -> set[str]:
        """返回已缓存指数 symbol 集合 (memo, 随 instruments 缓存失效)。"""
        if self._index_symbol_set_cache is None:
            df = self.get_index_instruments()
            if df.is_empty() or "symbol" not in df.columns:
                return set()
            self._index_symbol_set_cache = set(df["symbol"].cast(pl.Utf8).to_list())
        return self._index_symbol_set_cache

    def get_etf_symbol_set(self) -> set[str]:
        """返回已缓存 ETF symbol 集合 (memo, 随 instruments 缓存失效)。"""
        if self._etf_symbol_set_cache is None:
            df = self.get_etf_instruments()
            if df.is_empty() or "symbol" not in df.columns:
                return set()
            self._etf_symbol_set_cache = set(df["symbol"].cast(pl.Utf8).to_list())
        return self._etf_symbol_set_cache

    def resolve_asset_type(self, symbol: str) -> str:
        """按 symbol 判定资产类型: etf / index / stock(默认)。

        供 API 层对单标的查询做资产分流 (get_daily_asset 等)。
        ETF/指数集合为 memo, 每请求查询成本可忽略。
        """
        if symbol in self.get_etf_symbol_set():
            return "etf"
        if symbol in self.get_index_symbol_set():
            return "index"
        return "stock"

    def get_name_map(self, symbols: list[str] | None = None) -> dict[str, str]:
        """返回 {symbol: name} 映射, 合并股票 + ETF + 指数 instruments (股票优先去重)。

        自选列表/名称批查等场景的统一名称解析入口, 避免各调用方自行合并两份缓存。
        symbols 非 None 时只返回命中的条目。
        全量结果缓存在 _name_map_cache (随三份 instruments 维表刷新失效),
        避免每请求对 ~7000 行维表做 iter_rows 重建。
        """
        if self._name_map_cache is not None:
            if symbols is None:
                return dict(self._name_map_cache)
            wanted = set(symbols)
            return {s: n for s, n in self._name_map_cache.items() if s in wanted}
        # 只构建并缓存全量映射; symbols 过滤只作用于返回值。
        # 若把过滤后的结果写入缓存, 后续不同 symbols 的查询会命中残缺缓存,
        # 导致新加入自选的标的查不到名称。
        name_map: dict[str, str] = {}
        for df in (self.get_instruments(), self.get_etf_instruments(), self.get_instruments_asset("index")):
            if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
                continue
            for symbol, name in df.select(["symbol", "name"]).iter_rows():
                name_map.setdefault(symbol, name)
        self._name_map_cache = name_map
        if symbols is None:
            return dict(name_map)
        wanted = set(symbols)
        return {s: n for s, n in name_map.items() if s in wanted}

    def enriched_latest_date(self) -> date | None:
        """返回缓存中的 enriched 最新日期。"""
        return self._enriched_cache_date

    # ================================================================
    # 热路径: Polars 查询 (Chart / Screener / Signals / Intraday)
    # ================================================================

    def get_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """单股日K查询 — 从14列parquet读取后即时计算指标。"""
        from datetime import timedelta

        # 快路径: 请求的列全是 parquet 直接存储的列 (如迷你蜡烛图只要 OHLCV) →
        # scan + 列下推直接返回, 跳过 warmup(320天) 与 _compute_enriched_range 全套指标计算。
        # 仍用 enriched_latest 缓存覆盖最新日 (盘中更准), 只保留请求列。
        # 试探 scan 仅读请求的列; 缺列时回退到下方完整计算路径 (代价仅一次轻量 scan)。
        if columns:
            df = self._scan_daily_symbol(symbol, start, end, columns)
            if not df.is_empty() and all(c in df.columns for c in columns):
                cached, cache_date = self.get_enriched_latest()
                if cached is not None and not cached.is_empty() and cache_date:
                    if start <= cache_date <= end:
                        cached_part = self._filter_cached(cached, symbol, columns)
                        if not cached_part.is_empty():
                            df = df.filter(pl.col("date") != cache_date)
                            common_cols = [c for c in df.columns if c in cached_part.columns]
                            df = pl.concat([df.select(common_cols), cached_part.select(common_cols)])
                return df

        # 扩展范围用于指标预热 (MA200 需要 200 个交易日)
        warmup_start = start - timedelta(days=_LIVE_AGG_HISTORY_DAYS)

        # 优先复用预计算 enriched 历史缓存 (300 天全指标, 与回测引擎同源):
        # 个股对话框打开时本接口每个行情 tick 被调一次, 逐请求 320 天扫描 + 全套
        # 指标重算是热路径上最大的重复计算。缓存最新日可能不含当日实时行,
        # 由下方 get_enriched_latest 覆盖逻辑补齐; 覆盖不足时回退单股计算路径。
        df = pl.DataFrame()
        hist = self._enriched_history_cache
        if hist is not None and not hist.is_empty() and "date" in hist.columns:
            hist_min = self._enriched_history_start
            hist_max = hist["date"].max()
            if hist_min is not None and hist_min <= start and hist_max >= start:
                df = hist.filter(
                    (pl.col("symbol") == symbol)
                    & (pl.col("date") >= start)
                    & (pl.col("date") <= end)
                )
        if df.is_empty():
            # 扫描14列 parquet
            df = self._scan_daily_symbol(symbol, warmup_start, end, None)
            if not df.is_empty():
                df = self._compute_enriched_range(df)

        # 尝试用缓存数据覆盖最新日 (盘中更准确)
        cached, cache_date = self.get_enriched_latest()
        if not df.is_empty() and cached is not None and not cached.is_empty() and cache_date:
            if start <= cache_date <= end:
                cached_part = self._filter_cached(cached, symbol, None)
                if not cached_part.is_empty():
                    df = df.filter(pl.col("date") != cache_date)
                    common_cols = [c for c in df.columns if c in cached_part.columns]
                    df = pl.concat([df.select(common_cols), cached_part.select(common_cols)])

        # 裁剪到请求范围
        if not df.is_empty():
            df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))

        if columns and not df.is_empty():
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing)

        return df

    def get_daily_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """批量日K查询。"""
        cached, cache_date = self.get_enriched_latest()
        if cached is not None and not cached.is_empty() and cache_date:
            if start >= cache_date:
                return self._filter_cached_batch(cached, symbols, columns)

        # 回退 scan_parquet
        return self._scan_daily_batch(symbols, start, end, columns)

    def get_index_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """指数日K查询 — 从独立指数 enriched parquet 读取后即时计算通用指标。"""
        from datetime import timedelta

        # 快路径: 若请求的列全部是 parquet 直接存储的列 (如迷你蜡烛图只要 OHLCV),
        # 直接 scan + 列下推返回, 跳过 warmup(320天) 与 _compute_index_enriched_range 全套指标计算。
        # 试探 scan 仅读请求的列, 缺列时回退到下方完整计算路径 (代价仅一次轻量 scan)。
        if columns:
            df = self._scan_index_daily_symbol(symbol, start, end, columns)
            if not df.is_empty() and all(c in df.columns for c in columns):
                return df

        warmup_start = start - timedelta(days=_LIVE_AGG_HISTORY_DAYS)
        df = self._scan_index_daily_symbol(symbol, warmup_start, end, None)
        if not df.is_empty():
            df = self._compute_index_enriched_range(df)
            df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if columns and not df.is_empty():
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing)
        return df

    def get_etf_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """ETF 日K查询 — 优先读独立 ETF enriched，兼容旧版 index enriched 中的 ETF。"""
        from datetime import timedelta

        # 快路径: 独立 ETF 数据 + 请求列全是 parquet 存储列 → 直接 scan 列下推, 跳过 warmup + compute。
        # 无独立 ETF 数据 (旧版存入 index enriched) 时 df 为空, 自然回退到下方完整路径。
        if columns:
            df = self._scan_etf_daily_symbol(symbol, start, end, columns)
            if not df.is_empty() and all(c in df.columns for c in columns):
                return df

        warmup_start = start - timedelta(days=_LIVE_AGG_HISTORY_DAYS)
        df = self._scan_etf_daily_symbol(symbol, warmup_start, end, None)
        if df.is_empty():
            # 旧版 ETF 曾存入 kline_index_enriched；没有独立数据时回退读取。
            df = self._scan_index_daily_symbol(symbol, warmup_start, end, None)
        if not df.is_empty():
            df = self._compute_index_enriched_range(df)
            df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if columns and not df.is_empty():
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing)
        return df

    def get_daily_asset(
        self,
        asset_type: str,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        if asset_type == "stock":
            return self.get_daily(symbol, start, end, columns)
        if asset_type == "index":
            return self.get_index_daily(symbol, start, end, columns)
        if asset_type == "etf":
            return self.get_etf_daily(symbol, start, end, columns)
        return pl.DataFrame()

    def _minute_glob_for(self, asset_type: str) -> str:
        """按资产类型选择分钟K parquet glob。ETF 分钟数据独立存储于 kline_etf_minute。"""
        return self._etf_minute_glob if asset_type == "etf" else self._minute_glob

    def _maybe_adjust_minute(self, df: pl.DataFrame, asset_type: str) -> pl.DataFrame:
        """原始基准标记开启时应用读取时复权投影 (services/minute_adjust 三层架构)。

        标记未开启 (存量未迁移) 原样返回, 行为与旧版逐字节一致; 投影异常也按原样
        返回 (fail-open, 与日K缺因子语义一致), 不让复权层破坏数据可用性。
        """
        if df.is_empty() or not minute_basis_is_raw(self.store.data_dir):
            return df
        try:
            return apply_minute_adjustment(df, self.store.data_dir, asset_type)
        except Exception as e:
            logger.warning("分钟复权投影失败, 按原始数据返回: %s", e)
            return df

    def get_minute(
        self,
        symbol: str,
        trade_date: date,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """分钟K查询 — Polars scan_parquet + predicate pushdown。"""
        try:
            df = guarded_collect(
                pl.scan_parquet(self._minute_glob_for(asset_type)).filter(
                    (pl.col("symbol") == symbol)
                    & (pl.col("datetime").dt.date() == trade_date)
                ).sort("datetime")
            )
            return self._maybe_adjust_minute(df, asset_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("分钟K查询失败: %s", e)
            return pl.DataFrame()

    def get_minute_batch(
        self,
        symbols: list[str],
        trade_date: date,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """批量分钟K查询 — 多 symbol 一次 scan_parquet。

        用于自选列表分时图: 一次 predicate pushdown 读多只股票当日分钟K,
        避免逐只查询的 N 次 I/O。
        """
        if not symbols:
            return pl.DataFrame()
        try:
            df = guarded_collect(
                pl.scan_parquet(self._minute_glob_for(asset_type)).filter(
                    pl.col("symbol").is_in(symbols)
                    & (pl.col("datetime").dt.date() == trade_date)
                ).sort(["symbol", "datetime"])
            )
            return self._maybe_adjust_minute(df, asset_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("批量分钟K查询失败: %s", e)
            return pl.DataFrame()

    def get_minute_range(
        self,
        symbols: list[str],
        start: date,
        end: date,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """多 symbol × 日期范围的分钟K查询 (分钟K精确回测用)。

        一次 scan_parquet + predicate pushdown 读多只股票在 [start, end] 内的所有分钟K。
        返回列: symbol, datetime, open, high, low, close, volume, amount。
        """
        if not symbols:
            return pl.DataFrame()
        try:
            lf = pl.scan_parquet(self._minute_glob_for(asset_type))
            available = set(lf.collect_schema().names())
            select_cols = [c for c in ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"] if c in available]
            df = guarded_collect(
                lf.select(select_cols)
                .filter(
                    pl.col("symbol").is_in(symbols)
                    & (pl.col("datetime").dt.date() >= start)
                    & (pl.col("datetime").dt.date() <= end)
                )
                .sort(["symbol", "datetime"]),
                streaming=True,
            )
            return self._maybe_adjust_minute(df, asset_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("分钟K范围查询失败: %s", e)
            return pl.DataFrame()

    def get_minute_by_dates(
        self,
        symbols: list[str],
        dates: list[date],
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """按日期列表精确读取分钟K分区文件 (分钟K精确回测用)。

        与 get_minute_range 的区别: 后者扫描 [start, end] 区间全部日期的 parquet
        (触发日稀疏时会读大量无关日期 → 爆内存); 本方法只读 dates 里列出的日期
        对应的分区文件 (date=YYYY-MM-DD/part.parquet), 内存与回测区间长度解耦,
        只随触发日数量增长。

        缺失的日期分区直接跳过 (该日无分钟K数据)。
        返回列: symbol, datetime, open, high, low, close, volume, amount。
        """
        if not symbols or not dates:
            return pl.DataFrame()
        base = self._etf_minute_glob.rsplit("/", 2)[0] if asset_type == "etf" else self._minute_glob.rsplit("/", 2)[0]
        # 收集存在的分区文件路径, 避免对不存在的文件 scan 报错
        parts: list[str] = []
        for d in dates:
            p = f"{base}/date={d.isoformat()}/part.parquet"
            if Path(p).exists():
                parts.append(p)
        if not parts:
            return pl.DataFrame()
        try:
            lf = pl.scan_parquet(parts)
            available = set(lf.collect_schema().names())
            select_cols = [c for c in ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"] if c in available]
            df = guarded_collect(
                lf.select(select_cols)
                .filter(pl.col("symbol").is_in(symbols))
                .sort(["symbol", "datetime"]),
                streaming=True,
            )
            return self._maybe_adjust_minute(df, asset_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("分钟K按日期查询失败: %s", e)
            return pl.DataFrame()

    # ================================================================
    # Polars 查询内部方法
    # ================================================================

    def _compute_enriched_range(self, df: pl.DataFrame) -> pl.DataFrame:
        """对14列enriched数据即时计算完整指标+信号。输入应含足够预热行数。"""
        from app.indicators.pipeline import compute_indicators, compute_signals, compute_limit_signals, filter_halt_days
        if df.is_empty() or df.height < 2:
            return df
        # 兜底过滤历史脏数据中的停牌日 (close 可能被填充为前收盘价)
        df = filter_halt_days(df)
        if df.is_empty() or df.height < 2:
            return df
        try:
            df = compute_indicators(df)
            df = compute_signals(df)
            instruments = self.get_instruments()
            df = compute_limit_signals(
                df,
                instruments,
                historical_shares=self.get_historical_shares(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("on-demand compute failed: %s", e)
        return df

    def _compute_index_enriched_range(self, df: pl.DataFrame) -> pl.DataFrame:
        """指数只计算通用技术指标和通用信号，跳过涨跌停/股本/市值逻辑。"""
        from app.indicators.pipeline import compute_indicators, compute_signals
        if df.is_empty() or df.height < 2:
            return df
        try:
            df = compute_indicators(df)
            df = compute_signals(df)
        except Exception as e:  # noqa: BLE001
            logger.warning("index on-demand compute failed: %s", e)
        return df

    def _filter_cached(self, cached: pl.DataFrame, symbol: str, columns: list[str] | None) -> pl.DataFrame:
        df = cached.filter(pl.col("symbol") == symbol)
        if columns and not df.is_empty():
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing)
        return df

    def _filter_cached_batch(self, cached: pl.DataFrame, symbols: list[str], columns: list[str] | None) -> pl.DataFrame:
        df = cached.filter(pl.col("symbol").is_in(symbols))
        if columns and not df.is_empty():
            existing = [c for c in columns if c in df.columns]
            df = df.select(existing)
        return df.sort(["symbol", "date"])

    def _scan_daily_symbol(self, symbol: str, start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = scan_enriched_parquet(self._enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return guarded_collect(lf)
        except Exception as e:  # noqa: BLE001
            logger.warning("日K查询失败: %s", e)
            return pl.DataFrame()

    def _scan_daily_batch(self, symbols: list[str], start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = scan_enriched_parquet(self._enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol").is_in(symbols))
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort(["symbol", "date"])
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return guarded_collect(lf)
        except Exception as e:  # noqa: BLE001
            logger.warning("日K批量查询失败: %s", e)
            return pl.DataFrame()

    def _scan_index_daily_symbol(self, symbol: str, start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = scan_enriched_parquet(self._index_enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return guarded_collect(lf)
        except Exception as e:  # noqa: BLE001
            logger.warning("指数日K查询失败: %s", e)
            return pl.DataFrame()

    def _scan_etf_daily_symbol(self, symbol: str, start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = scan_enriched_parquet(self._etf_enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return guarded_collect(lf)
        except Exception as e:  # noqa: BLE001
            logger.debug("ETF 日K查询跳过: %s", e)
            return pl.DataFrame()

    def _merge_cached_and_scan(
        self,
        cached: pl.DataFrame,
        cache_date: date,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None,
    ) -> pl.DataFrame:
        """合并缓存部分 + scan 历史部分。

        历史部分用 strict < cache_date, 避免与缓存重复。
        两部分 schema 可能不一致 (增量 vs 全量), concat 前对齐列。
        """
        hist = self._scan_daily_symbol(symbol, start, cache_date, columns)
        cached_part = self._filter_cached(cached, symbol, columns)
        if hist.is_empty():
            return cached_part
        if cached_part.is_empty():
            return hist
        # 去重: 历史部分可能包含 cache_date, 去掉后再合并
        hist = hist.filter(pl.col("date") < cache_date)
        # 对齐列: 取交集, 统一类型
        common_cols = [c for c in hist.columns if c in cached_part.columns]
        hist = hist.select(common_cols)
        cached_part = cached_part.select(common_cols)
        # 统一类型: 历史可能是 Float64, 缓存可能是 Int64, 统一为 cast
        for c in common_cols:
            if hist[c].dtype != cached_part[c].dtype:
                # 统一到更宽的类型
                target = hist[c].dtype if hist.height > cached_part.height else cached_part[c].dtype
                hist = hist.with_columns(pl.col(c).cast(target))
                cached_part = cached_part.with_columns(pl.col(c).cast(target))
        return pl.concat([hist, cached_part])

    # ================================================================
    # DuckDB 查询 (冷路径: 统计/元数据/自定义SQL)
    # ================================================================

    def latest_minute_date(self, symbol: str, asset_type: str = "stock") -> date | None:
        # 注意: 必须走 execute_one (cursor+close)。直连 self.db.execute(...).fetchone()
        # 的未消费结果集会把首个分区 parquet 的句柄钉在共享连接上, Windows 下阻塞
        # 同步写入的 os.replace → 个股分时"补齐数据"500。
        table = "kline_etf_minute" if asset_type == "etf" else "kline_minute"
        try:
            row = self.execute_one(
                f"SELECT max(CAST(datetime AS DATE)) FROM {table} WHERE symbol = ?",
                [symbol],
            )
            if row and row[0]:
                return row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
        except duckdb.CatalogException:
            pass
        return None

    def latest_minute_date_global(self) -> date | None:
        """全市场最近分钟K日期 (不分 symbol)。用于非交易日回退到上一交易日。"""
        try:
            row = self.execute_one(
                "SELECT max(CAST(datetime AS DATE)) FROM kline_minute",
            )
            if row and row[0]:
                return row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
        except Exception:  # noqa: BLE001
            return None

    def earliest_daily_date(self) -> date | None:
        """本地日K数据的最早日期。"""
        try:
            res = self.execute_one(
                "SELECT min(date) FROM kline_daily",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:
            return None
        return None

    def earliest_minute_date(self) -> date | None:
        """本地分钟K数据的最早日期。"""
        try:
            res = self.execute_one(
                "SELECT min(CAST(datetime AS DATE)) FROM kline_minute",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:
            return None
        return None

    def list_minute_dates(self, start: date, end: date, asset_type: str = "stock") -> list[date]:
        """枚举 [start, end] 内存在的分钟K分区日 (目录名直读, 零 parquet 扫描)。

        分钟回测按交易日精确对日: 缺分区的日子由调用方显式跳过,
        不做"回退最近分区" (那是实盘选股的语义, 回放会串日)。
        """
        dirname = "kline_minute" if asset_type == "stock" else f"kline_{asset_type}_minute"
        minute_dir = self.store.data_dir / dirname
        if not minute_dir.exists():
            return []
        out: list[date] = []
        for entry in minute_dir.iterdir():
            if not (entry.is_dir() and entry.name.startswith("date=")):
                continue
            try:
                day = date.fromisoformat(entry.name[5:])
            except ValueError:
                continue
            if start <= day <= end:
                out.append(day)
        out.sort()
        return out

    def latest_daily_date(self) -> date | None:
        """本地日K数据的最新日期。"""
        try:
            res = self.execute_one(
                "SELECT max(date) FROM kline_daily",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:
            return None
        return None

    def latest_enriched_date(self, asset_type: str = "stock") -> date | None:
        """Return the newest partition available to matrix-native consumers."""
        dirname = enriched_dirname(asset_type)
        root = self.store.data_dir / dirname
        latest: date | None = None
        if not root.exists():
            return None
        for partition in root.glob("date=*"):
            try:
                value = date.fromisoformat(partition.name.removeprefix("date="))
            except ValueError:
                continue
            if latest is None or value > latest:
                latest = value
        return latest

    def get_matrix_data_generation(self, asset_type: str = "stock") -> str:
        """Return the stable generation for managed enriched readers."""
        return get_enriched_generation(self.store.data_dir, asset_type)

    def _bump_matrix_data_generation(self, asset_type: str) -> str:
        return bump_enriched_generation(self.store.data_dir, asset_type)

    def symbols_lagging(self, reference_date: date, min_gap_days: int = 3) -> list[str]:
        """返回日K覆盖落后的标的: 其最新 bar 早于 reference_date - min_gap_days。

        全局 max(date) 只要有一只票有今日数据就成立, 会掩盖停牌/复牌/一直拉失败而
        掉队的个股缺口。此方法按 symbol 聚合最新日期, 找出掉队者。只读, 不改数据。
        """
        from datetime import timedelta
        try:
            cutoff = reference_date - timedelta(days=min_gap_days)
            with self._lock:
                rows = self.db.execute(
                    "SELECT symbol, max(date) AS mx FROM kline_daily "
                    "GROUP BY symbol HAVING max(date) < ? ORDER BY mx",
                    [cutoff],
                ).fetchall()
            return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []

    def _latest_enriched_date_duckdb(self) -> date | None:
        try:
            res = self.execute_one(
                "SELECT max(date) FROM kline_enriched",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:  # noqa: BLE001
            return None
        return None

    # ================================================================
    # 写入 (Pipeline / Sync)
    # ================================================================

    def append_daily(self, df: pl.DataFrame) -> None:
        """按日分区写入日K数据 (merge-upsert)。"""
        if df.is_empty():
            return
        self._write_daily_partition(df, "kline_daily")

    def append_enriched(self, df: pl.DataFrame) -> None:
        """按日分区写入 enriched 数据 (merge-upsert)。磁盘仅写入 14 列存储列。"""
        if df.is_empty():
            return
        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        df_storage = df.select(storage_cols)
        self._write_daily_partition(df_storage, "kline_daily_enriched")

    def append_index_daily(self, df: pl.DataFrame) -> None:
        """按日分区写入指数日K数据 (merge-upsert)。"""
        if df.is_empty():
            return
        self._write_daily_partition(df, "kline_index_daily")

    def append_index_enriched(self, df: pl.DataFrame) -> None:
        """按日分区写入指数 enriched 数据。磁盘仅写入通用基础行情窄表。"""
        if df.is_empty():
            return
        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        df_storage = df.select(storage_cols)
        self._write_daily_partition(df_storage, "kline_index_enriched")

    def append_etf_daily(self, df: pl.DataFrame) -> None:
        """按日分区写入 ETF 日K数据 (merge-upsert)。"""
        if df.is_empty():
            return
        self._write_daily_partition(df, "kline_etf_daily")

    def append_etf_enriched(self, df: pl.DataFrame) -> None:
        """按日分区写入 ETF enriched 数据。磁盘仅写入基础行情窄表。"""
        if df.is_empty():
            return
        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        df_storage = df.select(storage_cols)
        self._write_daily_partition(df_storage, "kline_etf_enriched")

    def append_daily_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按资产类型写入日K；stock/index 保持旧目录兼容。"""
        if asset_type == "stock":
            self.append_daily(df)
        elif asset_type == "index":
            self.append_index_daily(df)
        elif asset_type == "etf":
            self.append_etf_daily(df)

    def append_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按资产类型写入 enriched；stock/index 保持旧目录兼容。"""
        if asset_type == "stock":
            self.append_enriched(df)
        elif asset_type == "index":
            self.append_index_enriched(df)
        elif asset_type == "etf":
            self.append_etf_enriched(df)

    def save_index_instruments(self, df: pl.DataFrame) -> None:
        """保存指数标的维表。"""
        if df.is_empty() or "symbol" not in df.columns:
            return
        out = self.store.data_dir / "instruments_index" / "instruments_index.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_parquet(df.unique(subset=["symbol"], keep="last").sort("symbol"), out)
        self._index_instruments_cache = None
        self._etf_instruments_cache = None
        self._name_map_cache = None
        self._refresh_index_instruments()

    def save_etf_instruments(self, df: pl.DataFrame) -> None:
        """保存 ETF 标的维表到独立目录。"""
        if df.is_empty() or "symbol" not in df.columns:
            return
        if "asset_type" not in df.columns:
            df = df.with_columns(pl.lit("etf").alias("asset_type"))
        out = self.store.data_dir / "instruments_etf" / "instruments_etf.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_parquet(df.unique(subset=["symbol"], keep="last").sort("symbol"), out)
        self._etf_instruments_cache = None
        self._name_map_cache = None
        self._refresh_etf_instruments()

    def refresh_index_views(self) -> None:
        """刷新指数相关 DuckDB 视图。"""
        d = self.store.data_dir.as_posix()
        statements = [
            f"""CREATE OR REPLACE VIEW kline_index_daily AS
                SELECT * FROM read_parquet('{d}/kline_index_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_index_enriched AS
                SELECT * FROM read_parquet('{d}/kline_index_enriched/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_etf_daily AS
                SELECT * FROM read_parquet('{d}/kline_etf_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_etf_enriched AS
                SELECT * FROM read_parquet('{d}/kline_etf_enriched/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_index AS
                SELECT * FROM read_parquet('{d}/instruments_index/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_etf AS
                SELECT * FROM read_parquet('{d}/instruments_etf/**/*.parquet', union_by_name=true)""",
        ]
        for sql in statements:
            try:
                with self._lock:
                    self.db.execute(sql)
            except Exception as e:  # noqa: BLE001
                logger.debug("index/etf view refresh skipped: %s", e)
        with self._lock:
            self.store._register_unified_views()

    def rebuild_views(self) -> None:
        """重建全部 13 张 parquet 视图并重挂 unified 视图 —— 唯一权威实现。

        原先 daily_pipeline._refresh_views(盘后管道) 与 /api/data/clear(清库) 各自
        内联了同一份视图重建 SQL, 清库那份还漏了几张视图导致漂移。此处收敛为单一入口:
        覆盖全部 13 张视图 (二者的超集), 空目录 (清库后) 也能安全重挂。
        """
        d = self.store.data_dir.as_posix()
        views = {
            "kline_daily": f"{d}/kline_daily/**/*.parquet",
            "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
            "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
            "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
            "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
            "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
            "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
            "kline_minute": f"{d}/kline_minute/**/*.parquet",
            "adj_factor": f"{d}/adj_factor/**/*.parquet",
            "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
            "instruments": f"{d}/instruments/**/*.parquet",
            "instruments_index": f"{d}/instruments_index/**/*.parquet",
            "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
        }
        for name, path in views.items():
            try:
                with self._lock:
                    self.db.execute(
                        f"CREATE OR REPLACE VIEW {name} AS "
                        f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("rebuild view %s failed: %s", name, e)
        with self._lock:
            self.store._register_unified_views()

    @staticmethod
    def _atomic_write_parquet(df: pl.DataFrame, out: Path) -> None:
        """先写临时文件再原子替换, 避免进程中断留下损坏的 parquet。

        直接 write_parquet(out) 在进程被 kill (dev.sh 清端口用 kill -9)
        或断电时会留下半截文件, 之后 scan_parquet glob 整条链路报错。
        临时文件后缀 .tmp 不匹配 *.parquet glob, 不会被扫描误读。
        Windows 下目标正被并发读取时由 replace_with_retry 短退避穿过。
        """
        tmp = out.with_name(out.name + ".tmp")
        df.write_parquet(tmp)
        replace_with_retry(tmp, out)

    def _write_daily_partition(self, df: pl.DataFrame, table: str) -> None:
        """按 date 分区写入 parquet，每个日期一个文件，支持 merge-upsert。"""
        base = self.store.data_dir / table
        generation_asset = {
            "kline_daily_enriched": "stock",
            "kline_etf_enriched": "etf",
        }.get(table)
        # recover=True: 外部进程残留的僵死 publishing 标记不应阻塞实时/管道
        # enriched 落盘, 首次写入即接管自愈; 活进程的发布仍会抛错保护竞态。
        publication = (
            EnrichedPublication(self.store.data_dir, generation_asset, recover=True)
            if generation_asset is not None
            else None
        )
        for date_df in df.partition_by("date"):
            dt = date_df["date"][0]
            ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
            out = base / f"date={ds}" / "part.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            self._optimistic_upsert_partition(out, date_df, publication)

    @staticmethod
    def _partition_fingerprint(path: Path) -> tuple[int, int] | None:
        """分区文件的修改指纹 (mtime_ns, size); 不存在返回 None。"""
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _optimistic_upsert_partition(
        self,
        out: Path,
        incoming: pl.DataFrame,
        publication: EnrichedPublication | None,
        *,
        retries: int = 3,
    ) -> None:
        """单分区 merge-upsert: polars 读/合并/排序在 _write_lock 外, 锁内只做
        指纹校验 + 原子替换 + commit。

        背景: polars 并发执行存在死锁风险 (见 app.polars_guard), 重活若在
        _write_lock 内悬死, 全局写锁被永久持有, 所有写路径排队冻结。乐观模式
        把读/算移出锁外; 锁内用指纹确认基底未被其他写入者改动, 失配则重试,
        重试耗尽退回锁内直读直写 (正确性优先, 牺牲隔离性)。
        """
        def _merge(existing: pl.DataFrame) -> pl.DataFrame:
            if existing.is_empty():
                return incoming.sort(["symbol", "date"])
            return pl.concat([existing, incoming], how="diagonal_relaxed").unique(
                subset=["symbol", "date"], keep="last"
            ).sort(["symbol", "date"])

        for _ in range(retries):
            existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()
            base_fp = self._partition_fingerprint(out)
            merged = _merge(existing)
            with self._write_lock:
                if self._partition_fingerprint(out) != base_fp:
                    continue  # 基底被并发写入者改过, 出锁重读重算
                self._write_partition_locked(out, merged, existing, publication)
            return
        # 乐观重试耗尽 (罕见: 高频并发写同一分区): 退回锁内全量模式保证正确性
        with self._write_lock:
            existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()
            self._write_partition_locked(out, _merge(existing), existing, publication)

    def _write_partition_locked(
        self,
        out: Path,
        merged: pl.DataFrame,
        existing: pl.DataFrame,
        publication: EnrichedPublication | None,
    ) -> None:
        """锁内的纯文件阶段: 无变化跳过; 否则原子替换 + 提交 generation。"""
        if not existing.is_empty() and existing.equals(merged):
            if publication is not None:
                publication.commit()  # 未写入时为无害空提交
            return
        if publication is None:
            self._atomic_write_parquet(merged, out)
        else:
            publication.write_parquet(merged, out)
            publication.commit()

    def merge_live_daily_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按 symbol 合并当天指定资产日K分区。用于少量自选实时，不覆盖全市场。"""
        if df.is_empty() or "date" not in df.columns:
            return
        table = {
            "stock": "kline_daily",
            "index": "kline_index_daily",
            "etf": "kline_etf_daily",
        }.get(asset_type)
        if not table:
            return
        base = self.store.data_dir / table
        dt = df["date"][0]
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        self._optimistic_upsert_partition(out, df, None)

    def _with_instrument_metadata(self, asset_type: str, df: pl.DataFrame) -> pl.DataFrame:
        """补齐实时内存缓存所需的维表字段；这些字段不会写入 enriched 分区。"""
        if asset_type not in {"stock", "etf"} or df.is_empty():
            return df
        instruments = self.get_instruments_asset(asset_type)
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return df
        metadata_cols = [
            c for c in ("name", "total_shares", "float_shares")
            if c in instruments.columns and c not in df.columns
        ]
        if not metadata_cols:
            return df
        metadata = instruments.select(["symbol", *metadata_cols]).unique(
            subset=["symbol"], keep="last",
        )
        return df.join(metadata, on="symbol", how="left")

    def merge_live_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按 symbol 合并当天 enriched 分区和内存缓存。用于少量自选实时。"""
        if df.is_empty() or "date" not in df.columns:
            return
        dt = df["date"][0]
        if asset_type == "stock":
            table = "kline_daily_enriched"
            existing_cache = self._enriched_cache if self._enriched_cache_date == dt else pl.DataFrame()
        elif asset_type == "etf":
            table = "kline_etf_enriched"
            existing_cache = self._etf_enriched_cache if self._etf_enriched_cache_date == dt else pl.DataFrame()
        elif asset_type == "index":
            table = "kline_index_enriched"
            existing_cache = self._index_enriched_cache if self._index_enriched_cache_date == dt else pl.DataFrame()
        else:
            return

        cache_df = self._with_instrument_metadata(asset_type, df)
        merged_cache = cache_df
        if existing_cache is not None and not existing_cache.is_empty():
            merged_cache = pl.concat([existing_cache, cache_df], how="diagonal_relaxed").unique(
                subset=["symbol", "date"], keep="last"
            )
        merged_cache = merged_cache.sort(["symbol"])

        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        df_storage = df.select(storage_cols).sort(["symbol"])
        base = self.store.data_dir / table
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        publication = (
            EnrichedPublication(self.store.data_dir, asset_type, recover=True)
            if asset_type in {"stock", "etf"}
            else None
        )
        self._optimistic_upsert_partition(out, df_storage, publication)

        if asset_type == "stock":
            self._enriched_cache = merged_cache
            self._enriched_cache_date = dt
        elif asset_type == "etf":
            self._etf_enriched_cache = merged_cache
            self._etf_enriched_cache_date = dt
        elif asset_type == "index":
            self._index_enriched_cache = merged_cache
            self._index_enriched_cache_date = dt

    def flush_live_daily(self, df: pl.DataFrame) -> None:
        """覆写当天 kline_daily 分区 (实时行情落盘, 非merge)。"""
        if df.is_empty() or "date" not in df.columns:
            return
        self.flush_live_daily_asset("stock", df)

    def flush_live_daily_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """覆写当天指定资产日K分区 (实时行情落盘, 非merge)。"""
        if df.is_empty() or "date" not in df.columns:
            return
        table = {
            "stock": "kline_daily",
            "index": "kline_index_daily",
            "etf": "kline_etf_daily",
        }.get(asset_type)
        if not table:
            return
        base = self.store.data_dir / table
        dt = df["date"][0]
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        # 覆写语义: 排序在锁外, 锁内只做原子替换。
        df_sorted = df.sort(["symbol", "date"])
        with self._write_lock:
            self._atomic_write_parquet(df_sorted, out)

    def flush_live_enriched(self, df: pl.DataFrame) -> None:
        """覆写当天 kline_daily_enriched 分区 (实时 enriched 落盘, 非merge)。

        内存缓存保留完整指标列供各服务使用，磁盘仅写入 14 列存储列。
        """
        self.flush_live_enriched_asset("stock", df)

    def flush_live_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """覆写当天指定资产 enriched 分区 (实时 enriched 落盘, 非merge)。"""
        if df.is_empty() or "date" not in df.columns:
            return
        dt = df["date"][0]
        cache_df = self._with_instrument_metadata(asset_type, df).sort(["symbol"])
        if asset_type == "stock":
            table = "kline_daily_enriched"
        elif asset_type == "etf":
            table = "kline_etf_enriched"
        elif asset_type == "index":
            table = "kline_index_enriched"
        else:
            return

        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        df_storage = df.select(storage_cols).sort(["symbol"])
        base = self.store.data_dir / table
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        publication = (
            EnrichedPublication(self.store.data_dir, asset_type, recover=True)
            if asset_type in {"stock", "etf"}
            else None
        )
        # 覆写语义: 读旧内容只为跳过无变化的写, 读在锁外 (误判最多造成一次
        # 冗余覆写, 不影响正确性); 锁内只做替换 + commit。
        existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()
        with self._write_lock:
            self._write_partition_locked(out, df_storage, existing, publication)

        if asset_type == "stock":
            self._enriched_cache = cache_df
            self._enriched_cache_date = dt
        elif asset_type == "etf":
            self._etf_enriched_cache = cache_df
            self._etf_enriched_cache_date = dt
        elif asset_type == "index":
            self._index_enriched_cache = cache_df
            self._index_enriched_cache_date = dt
