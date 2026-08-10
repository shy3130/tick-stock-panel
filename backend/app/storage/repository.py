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

import logging
import sys
import threading
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from app.config import settings
from app.storage.atomic_write import atomic_write_parquet as _atomic_write_parquet
from app.storage.duckdb_runtime import connect_duckdb
from app.indicators.engine_compat import ENGINE_COMPAT_WARMUP_CALENDAR_DAYS, build_engine_compat_live_state

logger = logging.getLogger(__name__)


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
            "kline_hk_daily",
            "kline_hk_enriched",
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
        for sub in ("metrics", "income", "balance_sheet", "cash_flow"):
            (self.data_dir / "financials" / sub).mkdir(parents=True, exist_ok=True)

        # DuckDB 内存模式 — 不建 .db 文件(§7.1)
        self.db = connect_duckdb()
        self._register_views()

    def close(self) -> None:
        """关闭主 DuckDB 实例；幂等。"""
        db = self.db
        if db is None:
            return
        self.db = None
        db.close()

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
            f"""CREATE OR REPLACE VIEW kline_hk_daily AS
                SELECT * FROM read_parquet('{d}/kline_hk_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_hk_enriched AS
                SELECT * FROM read_parquet('{d}/kline_hk_enriched/**/*.parquet', union_by_name=true)""",
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
            f"""CREATE OR REPLACE VIEW instruments_hk AS
                SELECT * FROM read_parquet('{d}/instruments_hk/**/*.parquet', union_by_name=true)""",
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
            # 五档盘口 sealed 真假涨停(独立旁路存储,不进 enriched)
            f"""CREATE OR REPLACE VIEW depth5 AS
                SELECT * FROM read_parquet('{d}/depth5/**/*.parquet', union_by_name=true)""",
        ]
        for sql in statements:
            try:
                self.db.execute(sql)
            except duckdb.Error as e:
                # Catches more than "no parquet yet" (duckdb.IOException): a
                # truncated/corrupt part.parquet (e.g. from a crash mid-write,
                # see _atomic_write_parquet) raises InvalidInputException, not
                # IOException. Narrowly catching IOException here meant any
                # single corrupt file would crash the whole app at startup
                # instead of just leaving that one view unavailable.
                logger.warning("view registration skipped for %s: %s", sql[:60], e)
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
                       'stock' AS asset_type, 'legacy' AS source
                FROM kline_daily
            """)
        if self._has_parquet("kline_index_daily"):
            daily_parts.append("""
                SELECT symbol, date, open, high, low, close, volume, amount,
                       'index' AS asset_type, 'legacy' AS source
                FROM kline_index_daily
            """)
        if self._has_parquet("kline_etf_daily"):
            daily_parts.append("""
                SELECT symbol, date, open, high, low, close, volume, amount,
                       'etf' AS asset_type, 'legacy' AS source
                FROM kline_etf_daily
            """)
        if self._has_parquet("kline_hk_daily"):
            daily_parts.append("""
                SELECT symbol, date, open, high, low, close, volume, amount,
                       'hk' AS asset_type, 'legacy' AS source
                FROM kline_hk_daily
            """)

        if self._has_parquet("kline_daily_enriched"):
            enriched_parts.append("SELECT *, 'stock' AS asset_type, 'legacy' AS source FROM kline_enriched")
        if self._has_parquet("kline_index_enriched"):
            enriched_parts.append("SELECT *, 'index' AS asset_type, 'legacy' AS source FROM kline_index_enriched")
        if self._has_parquet("kline_etf_enriched"):
            enriched_parts.append("SELECT *, 'etf' AS asset_type, 'legacy' AS source FROM kline_etf_enriched")
        if self._has_parquet("kline_hk_enriched"):
            enriched_parts.append("SELECT *, 'hk' AS asset_type, 'legacy' AS source FROM kline_hk_enriched")

        if self._has_parquet("kline_minute"):
            minute_parts.append("""
                SELECT symbol, datetime, open, high, low, close, volume, amount,
                       'stock' AS asset_type, 'legacy' AS source
                FROM kline_minute
            """)
        if self._has_parquet("kline_etf_minute"):
            minute_parts.append("""
                SELECT symbol, datetime, open, high, low, close, volume, amount,
                       'etf' AS asset_type, 'legacy' AS source
                FROM kline_etf_minute
            """)

        if self._has_parquet("instruments"):
            inst_parts.append("""
                SELECT symbol, name, code, exchange, 'stock' AS asset_type, 'legacy' AS source
                FROM instruments
            """)
        if self._has_parquet("instruments_index"):
            inst_parts.append("""
                SELECT symbol, name, code, NULL AS exchange, 'index' AS asset_type, 'legacy' AS source
                FROM instruments_index
                WHERE coalesce(asset_type, 'index') != 'etf'
            """)
        if self._has_parquet("instruments_etf"):
            inst_parts.append("""
                SELECT symbol, name, code, NULL AS exchange, 'etf' AS asset_type, 'legacy' AS source
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

        # ---- Polars 缓存 ----
        self._enriched_cache: pl.DataFrame | None = None       # 最新一天 (~5500行)
        self._enriched_cache_date: date | None = None
        self._live_agg_cache: pl.DataFrame | None = None       # 预计算聚合表 (~5500行)
        self._live_agg_cache_date: date | None = None
        self._live_agg_check_date: date | None = None          # 上次跨日校验时的 today (快路径节流)
        self._instruments_cache: pl.DataFrame | None = None
        self._index_instruments_cache: pl.DataFrame | None = None
        self._etf_enriched_cache: pl.DataFrame | None = None
        self._etf_enriched_cache_date: date | None = None
        self._etf_live_agg_cache: pl.DataFrame | None = None
        self._etf_live_agg_cache_date: date | None = None
        self._etf_instruments_cache: pl.DataFrame | None = None
        self._hk_enriched_cache: pl.DataFrame | None = None
        self._hk_enriched_cache_date: date | None = None
        self._hk_instruments_cache: pl.DataFrame | None = None
        # 缓存失效代际: refresh_cache()/clear_cache() 改变可见缓存状态时自增,
        # 作为 service 层有界缓存的逻辑失效令牌 (同一日期更正不再陈旧 120s)。
        self._cache_generation: int = 0

        # parquet glob 路径
        self._enriched_glob = str(store.data_dir / "kline_daily_enriched" / "**" / "*.parquet")
        self._index_enriched_glob = str(store.data_dir / "kline_index_enriched" / "**" / "*.parquet")
        self._etf_enriched_glob = str(store.data_dir / "kline_etf_enriched" / "**" / "*.parquet")
        self._hk_enriched_glob = str(store.data_dir / "kline_hk_enriched" / "**" / "*.parquet")
        self._minute_glob = str(store.data_dir / "kline_minute" / "**" / "*.parquet")
        self._etf_minute_glob = str(store.data_dir / "kline_etf_minute" / "**" / "*.parquet")
        self._inst_glob = str(store.data_dir / "instruments" / "**" / "*.parquet")
        self._index_inst_glob = str(store.data_dir / "instruments_index" / "**" / "*.parquet")
        self._etf_inst_glob = str(store.data_dir / "instruments_etf" / "**" / "*.parquet")
        self._hk_inst_glob = str(store.data_dir / "instruments_hk" / "**" / "*.parquet")

    def execute_all(self, sql: str, params: list | None = None) -> list[tuple]:
        """线程安全的 SELECT → fetchall。DuckDB 单 connection 非线程安全，所有读路径须走此方法。"""
        with self._lock:
            return self.db.execute(sql, params or []).fetchall()

    def execute_one(self, sql: str, params: list | None = None) -> tuple | None:
        """线程安全的 SELECT → fetchone。"""
        with self._lock:
            return self.db.execute(sql, params or []).fetchone()

    # ================================================================
    # Polars 缓存管理
    # ================================================================

    def refresh_cache(self) -> None:
        """刷新 Polars 缓存。在 pipeline 完成后、服务启动时调用。"""
        self._refresh_instruments()
        self._refresh_index_instruments()
        self._refresh_etf_instruments()
        self._refresh_hk_instruments()
        self._refresh_enriched()
        self._cache_generation += 1

    def clear_cache(self) -> None:
        """清空所有 Polars 内存缓存。

        与 refresh_cache 的区别: refresh_cache 在磁盘无数据时会提前 return,
        导致内存里的旧缓存残留 (clear 数据后看板仍显示旧数据的根因)。
        仅在可见缓存状态实际改变时推进失效代际。
        """
        changed = any(
            value is not None
            for value in (
                self._enriched_cache,
                self._enriched_cache_date,
                self._live_agg_cache,
                self._live_agg_cache_date,
                self._live_agg_check_date,
                self._instruments_cache,
                self._index_instruments_cache,
                self._etf_enriched_cache,
                self._etf_enriched_cache_date,
                self._etf_live_agg_cache,
                self._etf_live_agg_cache_date,
                self._etf_instruments_cache,
                self._hk_enriched_cache,
                self._hk_enriched_cache_date,
                self._hk_instruments_cache,
            )
        )
        self._enriched_cache = None
        self._enriched_cache_date = None
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
        self._hk_enriched_cache = None
        self._hk_enriched_cache_date = None
        self._hk_instruments_cache = None
        if changed:
            self._cache_generation += 1

    @property
    def cache_generation(self) -> int:
        """返回 service 层有界缓存使用的只读失效代际。"""
        return self._cache_generation

    def _refresh_enriched(self) -> None:
        """从 parquet 加载 enriched 最新日到内存 + 构建聚合表。

        enriched parquet 仅存 14 列基础数据。启动时按需读入近 300 天数据即时计算
        最新日的完整指标, 仅把最新日缓存 (_enriched_cache) 与盘中递推聚合
        (_live_agg_cache) 留在内存; 完整历史不再常驻, 历史窗口查询走
        get_enriched_range() 的惰性扫描。
        """
        try:
            latest = self._latest_enriched_date_duckdb()
            if not latest:
                # 磁盘已无数据: 必须清空内存缓存, 否则旧数据会残留
                # (清数据后看板仍显示旧数据的根因)
                self.clear_cache()
                return

            # Step 1: 直接读最新日期的分区文件 (仅 14 列)
            enriched_dir = self.store.data_dir / "kline_daily_enriched"
            ds = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)
            target_parquet = enriched_dir / f"date={ds}" / "part.parquet"

            if not target_parquet.exists():
                return

            df_latest = pl.read_parquet(target_parquet)
            df_latest = df_latest.unique(
                subset=["symbol", "date"], keep="last", maintain_order=True,
            )
            if df_latest.is_empty():
                return

            # Step 2: 读近 300 天 14 列数据 → compute → filter(latest) → 仅缓存最新日
            # 300 日历天 ≈ 210 交易日, 覆盖 warmup(60) + engine_compat(120)
            try:
                from datetime import timedelta
                from app.indicators.pipeline import (
                    compute_indicators, compute_signals, compute_limit_signals, clean_nan_inf,
                )
                start_full = latest - timedelta(days=300)
                read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                         "volume", "amount", "raw_close", "raw_high", "raw_low",
                                         "turnover_rate"]
                             if c in df_latest.columns]
                df_hist = self._scan_unique_enriched(
                    self._enriched_glob, start=start_full, end=latest, columns=read_cols,
                )
                if not df_hist.is_empty():
                    instruments = self._instruments_cache if self._instruments_cache is not None else pl.DataFrame()
                    df_full = compute_indicators(df_hist)
                    df_full = compute_signals(df_full)
                    if instruments is not None and not instruments.is_empty():
                        df_full = compute_limit_signals(df_full, instruments)
                    df_full = clean_nan_inf(df_full)

                    # 只取最新一天作为 enriched_cache (历史不再常驻)
                    df_today = df_full.filter(pl.col("date") == latest)
                    if not df_today.is_empty():
                        self._enriched_cache = df_today
                        self._enriched_cache_date = latest
                        # 一次性构建 live state: 把已算好的 base/indicator 帧传进去,
                        # 避免 _build_live_agg 再扫一次 parquet + compute_indicators。
                        baseline = self._live_agg_baseline_date(latest)
                        self._build_live_agg(
                            baseline,
                            df_hist=df_hist,
                            indicator_history=df_full,
                        )
                        logger.info("enriched 缓存已计算: %d 只, 日期 %s (即时计算)", len(df_today), latest)
                        return
            except Exception as e:  # noqa: BLE001
                logger.warning("enriched 即时计算失败, 使用原始 14 列缓存: %s", e)

            # 降级: 直接使用 14 列数据 + 构建 live_agg
            self._enriched_cache = df_latest
            self._enriched_cache_date = latest
            self._build_live_agg(self._live_agg_baseline_date(latest))

            logger.info("enriched 缓存已加载: %d 只, 日期 %s", len(df_latest), latest)
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched 缓存刷新失败: %s", e)

    def _build_live_agg(
        self,
        latest: date,
        *,
        df_hist: pl.DataFrame | None = None,
        indicator_history: pl.DataFrame | None = None,
    ) -> None:
        """从 OHLCV 即时计算递推状态 + 窗口聚合, 构建盘中实时聚合表。

        参数:
          latest:            递推基准日 (最新日或上一交易日)。
          df_hist:           调用方已扫描好的窄列历史 (14 列); 为 None 时走
                             _build_live_agg_from_parquet() 自行扫描, 避免二次读盘。
          indicator_history: 调用方已算好的含指标历史帧 (_refresh_enriched 的产物,
                             同一份数据已含 engine compat 列); 为 None 时用 df_hist。
        """
        if df_hist is not None and not df_hist.is_empty():
            df_hist = df_hist.filter(pl.col("date") <= latest)
        if indicator_history is not None and not indicator_history.is_empty():
            indicator_history = indicator_history.filter(pl.col("date") <= latest)
        from datetime import timedelta
        from app.indicators.pipeline import _ema_alpha, clean_nan_inf
        start_60d = latest - timedelta(days=300)

        if df_hist is not None and not df_hist.is_empty():
            # 调用方已提供窄列历史, 直接复用, 并从 indicator_history 取最新日状态
            state_source = (
                indicator_history.filter(pl.col("date") == latest)
                if indicator_history is not None and not indicator_history.is_empty()
                else pl.DataFrame()
            )
            state_cols = [
                "symbol",
                "ema5", "ema10", "ema20", "ema30", "ema60",
                "macd_dea",
                "kdj_k", "kdj_d",
                "atr_14",
                "close", "high", "low",
                "annual_vol_20d",
            ]
            existing_state = [c for c in state_cols if c in state_source.columns]
            agg_a = state_source.select(existing_state) if existing_state else pl.DataFrame()
        else:
            # 降级: 读 parquet + compute_indicators
            df_hist, agg_a = self._build_live_agg_from_parquet(latest, start_60d)

        if df_hist.is_empty():
            self._live_agg_cache = pl.DataFrame()
            self._live_agg_cache_date = None
            return

        if agg_a.is_empty():
            self._live_agg_cache = pl.DataFrame()
            self._live_agg_cache_date = None
            return

        # 单独计算 _ema12 / _ema26 (compute_indicators 内部会 drop 掉)
        df_ema = df_hist.sort(["symbol", "date"]).with_columns([
            pl.col("close").ewm_mean(alpha=_ema_alpha(12), adjust=False).over("symbol").alias("_ema12"),
            pl.col("close").ewm_mean(alpha=_ema_alpha(26), adjust=False).over("symbol").alias("_ema26"),
        ]).filter(pl.col("date") == latest).select("symbol", "_ema12", "_ema26")

        agg_a = agg_a.join(df_ema, on="symbol", how="inner")

        # 单独计算 RSI 状态列 (compute_indicators 内部会 drop 掉)
        df_rsi_base = df_hist.sort(["symbol", "date"]).with_columns(
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
            df_rsi_base
            .with_columns(rsi_exprs)
            .filter(pl.col("date") == latest)
            .select("symbol", *[f"_rsi_avg_gain_{n}" for n in (6, 14, 24)],
                              *[f"_rsi_avg_loss_{n}" for n in (6, 14, 24)])
        )
        agg_a = agg_a.join(df_rsi, on="symbol", how="inner")

        # 前复权因子: adj_factor = close(复权) / raw_close(原始)
        # raw_close 理论上不该是 0(filter_halt_days 只保证 open/high>0,不直接
        # 校验 close),数据源异常/脏数据仍可能让它真的是 0 → 除出 Inf,这里显式
        # 兜底成 1.0(不复权),而不是依赖末尾 clean_nan_inf 才发现。
        if "raw_close" in df_hist.columns:
            adj_factor_df = (
                df_hist.filter(pl.col("date") == latest)
                .select(
                    "symbol",
                    pl.when(pl.col("raw_close") != 0)
                      .then(pl.col("close") / pl.col("raw_close"))
                      .otherwise(1.0)
                      .alias("_adj_factor"),
                )
            )
            agg_a = agg_a.join(adj_factor_df, on="symbol", how="left")
            if "_adj_factor" in agg_a.columns:
                agg_a = agg_a.with_columns(pl.col("_adj_factor").fill_null(1.0))

        # annual_vol_20d 递推状态: 最近 19 天日收益率的部分和 / 平方和
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

        # 昨日连板数: 从 enriched parquet 取 (用于增量计算同向 +1)
        # 用去重扫描保证 (symbol,date) 唯一, 防止重复行翻倍连板数。
        consec = self._scan_unique_enriched(
            self._enriched_glob, start=latest, end=latest,
            columns=["symbol", "consecutive_limit_ups", "consecutive_limit_downs"],
        )
        if (
            not consec.is_empty()
            and "consecutive_limit_ups" in consec.columns
            and "consecutive_limit_downs" in consec.columns
        ):
            consec = consec.select(
                "symbol",
                pl.col("consecutive_limit_ups").alias("_prev_consec_up"),
                pl.col("consecutive_limit_downs").alias("_prev_consec_down"),
            )
            agg_a = agg_a.join(consec, on="symbol", how="left")

        # B类: 按 symbol 分组聚合 — 窗口统计
        agg_b = (
            df_hist.sort(["symbol", "date"])
            .group_by("symbol")
            .agg([
                pl.col("close").tail(4).sum().alias("_ma5_partial_sum"),
                pl.col("close").tail(9).sum().alias("_ma10_partial_sum"),
                pl.col("close").tail(19).sum().alias("_ma20_partial_sum"),
                pl.col("close").tail(29).sum().alias("_ma30_partial_sum"),
                pl.col("close").tail(59).sum().alias("_ma60_partial_sum"),

                pl.col("close").tail(19).sum().alias("_boll_partial_sum"),
                (pl.col("close").tail(19) ** 2).sum().alias("_boll_partial_sq_sum"),

                pl.col("high").tail(59).max().alias("_high_59d"),
                pl.col("low").tail(59).min().alias("_low_59d"),

                pl.col("close").tail(5).first().alias("_close_5d_ago"),
                pl.col("close").tail(10).first().alias("_close_10d_ago"),
                pl.col("close").tail(20).first().alias("_close_20d_ago"),
                pl.col("close").tail(30).first().alias("_close_30d_ago"),
                pl.col("close").tail(60).first().alias("_close_60d_ago"),

                pl.col("volume").tail(4).sum().alias("_vol_ma5_partial_sum"),
                pl.col("volume").tail(9).sum().alias("_vol_ma10_partial_sum"),

                pl.col("low").tail(8).min().alias("_kdj_8d_low"),
                pl.col("high").tail(8).max().alias("_kdj_8d_high"),

                pl.col("close").tail(59).len().alias("_window_len"),
            ])
        )

        # 优先传已含 engine compat 列的 indicator_history, 否则回退到 df_hist
        ec_input = (
            indicator_history
            if indicator_history is not None
            and not indicator_history.is_empty()
            and "expma_12" in indicator_history.columns
            else df_hist
        )
        ec_state = build_engine_compat_live_state(ec_input, latest)
        live = agg_a.join(agg_b, on="symbol", how="inner")
        if not ec_state.is_empty():
            live = live.join(ec_state, on="symbol", how="left")
        self._live_agg_cache = clean_nan_inf(live)
        self._live_agg_cache_date = latest

    def _live_agg_baseline_date(self, latest: date) -> date:
        """盘中递推基准日期。当天实时分区存在时使用上一可用交易日。"""
        if latest != date.today():
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

    def _build_live_agg_from_parquet(self, latest: date, start_60d: date) -> tuple[pl.DataFrame, pl.DataFrame]:
        """降级路径: 从 parquet 读取数据并计算指标 (df_hist 未由调用方提供时)。"""
        from app.indicators.pipeline import compute_indicators, clean_nan_inf

        read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close", "volume",
                                 "raw_close", "raw_high", "raw_low", "turnover_rate"]]
        df_hist = self._scan_unique_enriched(
            self._enriched_glob, start=start_60d, end=latest, columns=read_cols,
        )

        if df_hist.is_empty():
            return df_hist, pl.DataFrame()

        df_with_indicators = clean_nan_inf(compute_indicators(df_hist))

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
        agg_a = df_with_indicators.filter(pl.col("date") == latest).select(existing_state)

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
            df_latest = df_latest.unique(
                subset=["symbol", "date"], keep="last", maintain_order=True,
            )
            if df_latest.is_empty():
                return

            from datetime import timedelta
            from app.indicators.pipeline import compute_indicators, compute_signals, clean_nan_inf
            start_full = latest - timedelta(days=300)
            read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                     "volume", "amount", "raw_close", "raw_high", "raw_low",
                                     "turnover_rate"]
                         if c in df_latest.columns]
            df_hist = self._scan_unique_enriched(
                self._etf_enriched_glob, start=start_full, end=latest, columns=read_cols,
            )
            if df_hist.is_empty():
                self._etf_enriched_cache = df_latest.sort(["symbol"])
            else:
                df_full = clean_nan_inf(compute_signals(compute_indicators(df_hist)))
                self._etf_enriched_cache = df_full.filter(pl.col("date") == latest).sort(["symbol"])
            self._etf_enriched_cache_date = latest
        except Exception as e:  # noqa: BLE001
            logger.debug("ETF enriched 缓存刷新跳过: %s", e)

    def _refresh_hk_enriched(self) -> None:
        """从港股 enriched parquet 加载最新日到内存缓存 (历史窗口改为惰性扫描)。

        不算涨跌停信号 —— 港股无该制度,compute_signals 之外不再调 compute_limit_signals。
        换手率已在管道落盘时算好 (compute_all 的 asset_type != stock 分支),这里直接读。
        完整历史不再常驻 (与 A 股同), 历史窗口查询走 get_enriched_range() 的惰性扫描。
        """
        try:
            enriched_dir = self.store.data_dir / "kline_hk_enriched"
            dates = sorted(
                p.name[5:] for p in enriched_dir.glob("date=*")
                if p.is_dir() and p.name.startswith("date=")
            ) if enriched_dir.exists() else []
            if not dates:
                self._hk_enriched_cache = None
                self._hk_enriched_cache_date = None
                return
            latest = date.fromisoformat(dates[-1])
            target_parquet = enriched_dir / f"date={dates[-1]}" / "part.parquet"
            df_latest = pl.read_parquet(target_parquet)
            df_latest = df_latest.unique(
                subset=["symbol", "date"], keep="last", maintain_order=True,
            )
            if df_latest.is_empty():
                return

            from datetime import timedelta
            from app.indicators.pipeline import compute_indicators, compute_signals, clean_nan_inf
            start_full = latest - timedelta(days=300)
            read_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                     "volume", "amount", "raw_close", "raw_high", "raw_low",
                                     "turnover_rate"]
                         if c in df_latest.columns]
            df_hist = self._scan_unique_enriched(
                self._hk_enriched_glob, start=start_full, end=latest, columns=read_cols,
            )
            if df_hist.is_empty():
                self._hk_enriched_cache = df_latest.sort(["symbol"])
            else:
                df_full = clean_nan_inf(compute_signals(compute_indicators(df_hist)))
                # JOIN 名称等维表列 (港股 instruments 有 name/float_shares)
                inst = self.get_hk_instruments()
                if not inst.is_empty():
                    inst_cols = [c for c in ["name", "total_shares", "float_shares"]
                                 if c in inst.columns and c not in df_full.columns]
                    if inst_cols:
                        df_full = df_full.join(
                            inst.select(["symbol", *inst_cols]).unique(subset=["symbol"]),
                            on="symbol", how="left",
                        )
                self._hk_enriched_cache = df_full.filter(pl.col("date") == latest).sort(["symbol"])
            self._hk_enriched_cache_date = latest
            logger.info("港股 enriched 缓存已加载: %d 只, 日期 %s",
                        len(self._hk_enriched_cache), latest)
        except Exception as e:  # noqa: BLE001
            logger.debug("港股 enriched 缓存刷新跳过: %s", e)

    def _refresh_instruments(self) -> None:
        """加载 instruments 到内存。"""
        try:
            df = pl.scan_parquet(self._inst_glob).collect()
            if not df.is_empty():
                self._instruments_cache = df
                logger.info("instruments 缓存已加载: %d 只", len(df))
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments 缓存刷新失败: %s", e)

    def _refresh_index_instruments(self) -> None:
        """加载指数 instruments 到内存。"""
        try:
            df = pl.scan_parquet(self._index_inst_glob).collect()
            if not df.is_empty():
                self._index_instruments_cache = df
                logger.info("index instruments 缓存已加载: %d 只", len(df))
        except Exception as e:  # noqa: BLE001
            logger.debug("index instruments 缓存刷新跳过: %s", e)

    def _refresh_etf_instruments(self) -> None:
        """加载 ETF instruments 到内存；兼容旧版 instruments_index 中的 ETF。"""
        parts: list[pl.DataFrame] = []
        try:
            df = pl.scan_parquet(self._etf_inst_glob).collect()
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
            logger.info("ETF instruments 缓存已加载: %d 只", len(df_all))

    def _refresh_hk_instruments(self) -> None:
        """加载港股 instruments 到内存。

        与 ETF 不同,港股没有"旧版 instruments_index 里混着"的历史包袱,只读自己的目录。
        """
        try:
            df = pl.scan_parquet(self._hk_inst_glob).collect()
        except Exception as e:  # noqa: BLE001
            logger.debug("hk instruments 缓存刷新跳过: %s", e)
            return
        if df.is_empty():
            return
        self._hk_instruments_cache = df.unique(subset=["symbol"], keep="last").sort("symbol")
        logger.info("港股 instruments 缓存已加载: %d 只", len(self._hk_instruments_cache))

    def get_hk_instruments(self) -> pl.DataFrame:
        """返回缓存的港股 instruments。含 float_shares,供 enriched 计算换手率。"""
        if self._hk_instruments_cache is None:
            self._refresh_hk_instruments()
        if self._hk_instruments_cache is None:
            return pl.DataFrame()
        return self._hk_instruments_cache

    def get_enriched_latest(self) -> tuple[pl.DataFrame, date | None]:
        """返回缓存的 enriched 最新日 DataFrame + 日期。如无缓存则懒加载。"""
        if self._enriched_cache is None:
            self._refresh_enriched()
        if self._enriched_cache is None:
            return pl.DataFrame(), self._enriched_cache_date
        return self._enriched_cache, self._enriched_cache_date

    def get_enriched_latest_asset(self, asset_type: str) -> tuple[pl.DataFrame, date | None]:
        """按资产类型返回最新 enriched 缓存。stock 保持旧缓存语义。"""
        if asset_type == "stock":
            return self.get_enriched_latest()
        if asset_type == "etf":
            if self._etf_enriched_cache is None:
                self._refresh_etf_enriched()
            if self._etf_enriched_cache is None:
                return pl.DataFrame(), self._etf_enriched_cache_date
            return self._etf_enriched_cache, self._etf_enriched_cache_date
        if asset_type == "hk":
            if self._hk_enriched_cache is None:
                self._refresh_hk_enriched()
            if self._hk_enriched_cache is None:
                return pl.DataFrame(), self._hk_enriched_cache_date
            return self._hk_enriched_cache, self._hk_enriched_cache_date
        return pl.DataFrame(), None

    def get_enriched_range(
        self,
        start: date,
        end: date,
        symbols: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> pl.DataFrame | None:
        """惰性扫描 enriched parquet 返回 [start, end] 区间。唯一的公开历史入口。

        三条计算路径 (按 columns 选择, 避免无谓的全套指标计算):
          - 全部列属于 ENRICHED_STORAGE_COLS: 只扫 [start,end], 不算指标;
          - 全部列属于 STORAGE ∪ PRICE_CHANGE_COLUMNS: 扫 warmup 起点的原始 OHLC,
            用 compute_price_change_columns() 只算四列 price-change (RPS change_pct
            快路径), 裁剪后投影, 不调 compute_all;
          - columns is None 或含其他派生列: 扫 warmup 起点 → compute_all 全套指标 →
            JOIN instruments 的 name/total_shares/float_shares → 裁剪投影。

        返回约定 (与原缓存版一致):
          - start > end 或 symbols == [] → 空 DataFrame (不扫描);
          - parquet/schema/读取失败 → None;
          - 有效区间无匹配行 → 空 DataFrame。
        symbols 在 collect 前过滤; 投影只保留实际存在的请求列, 并强制 symbol/date。
        """
        from datetime import timedelta
        from app.indicators.pipeline import (
            ENRICHED_STORAGE_COLS,
            PRICE_CHANGE_COLUMNS,
            compute_price_change_columns,
            compute_all,
        )

        if start > end:
            return pl.DataFrame()
        if symbols is not None and len(symbols) == 0:
            return pl.DataFrame()

        storage_set = set(ENRICHED_STORAGE_COLS)
        fast_set = storage_set | PRICE_CHANGE_COLUMNS
        requested = set(columns) if columns is not None else None

        if requested is not None and requested.issubset(storage_set):
            # 路径 1: 仅存储列, 直接扫 [start,end]
            scan_start = start
            try:
                df = self._scan_unique_enriched(
                    self._enriched_glob, start=scan_start, end=end,
                    columns=list(requested), symbols=symbols,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("enriched range scan failed: %s", e)
                return None
        elif requested is not None and requested.issubset(fast_set):
            # 路径 2: storage ∪ price-change 快路径
            try:
                warmup_start = start - timedelta(days=ENGINE_COMPAT_WARMUP_CALENDAR_DAYS)
            except OverflowError:
                warmup_start = date.min
            raw_cols = [
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "raw_close", "raw_high", "raw_low",
            ]
            # 请求里属于存储列的 (如 turnover_rate/consecutive_limit_ups) 也要扫出来
            for c in sorted(requested & storage_set):
                if c not in raw_cols:
                    raw_cols.append(c)
            try:
                df = self._scan_unique_enriched(
                    self._enriched_glob, start=warmup_start, end=end,
                    columns=raw_cols, symbols=symbols,
                )
                if df.is_empty():
                    return pl.DataFrame()
                df = compute_price_change_columns(df.sort(["symbol", "date"]))
            except Exception as e:  # noqa: BLE001
                logger.warning("enriched price-change range failed: %s", e)
                return None
            df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        else:
            # 路径 3: 全套派生列 (columns is None 或含其他派生列)
            try:
                warmup_start = start - timedelta(days=ENGINE_COMPAT_WARMUP_CALENDAR_DAYS)
            except OverflowError:
                warmup_start = date.min
            raw_cols = [c for c in [
                "symbol", "date", "open", "high", "low", "close", "volume", "amount",
                "raw_close", "raw_high", "raw_low", "turnover_rate",
            ]]
            try:
                df = self._scan_unique_enriched(
                    self._enriched_glob, start=warmup_start, end=end,
                    columns=raw_cols, symbols=symbols,
                )
                if df.is_empty():
                    return pl.DataFrame()
                instruments = self.get_instruments()
                df = compute_all(df.sort(["symbol", "date"]), instruments=instruments)
            except Exception as e:  # noqa: BLE001
                logger.warning("enriched derived range failed: %s", e)
                return None
            # JOIN 缺失的 name/total_shares/float_shares
            if instruments is not None and not instruments.is_empty():
                inst_cols = [c for c in ["name", "total_shares", "float_shares"]
                             if c in instruments.columns and c not in df.columns]
                if inst_cols:
                    df = df.join(
                        instruments.select(["symbol", *inst_cols]).unique(subset=["symbol"]),
                        on="symbol", how="left",
                    )
            df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))

        if df.is_empty():
            return pl.DataFrame()

        # 投影: 只保留实际存在的请求列, 强制 symbol/date
        if columns:
            existing = [c for c in columns if c in df.columns]
            if "symbol" not in existing and "symbol" in df.columns:
                existing.insert(0, "symbol")
            if "date" not in existing and "date" in df.columns:
                existing.insert(1, "date")
            df = df.select(existing)
        return df.sort(["symbol", "date"])

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
            self._refresh_enriched()
            self._live_agg_check_date = date.today()  # 刚建过, 当天不必再查磁盘
        else:
            today = date.today()
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
        if asset_type == "hk":
            return self.get_hk_instruments()
        return pl.DataFrame()

    def get_index_symbol_set(self) -> set[str]:
        """返回已缓存指数 symbol 集合。"""
        df = self.get_index_instruments()
        if df.is_empty() or "symbol" not in df.columns:
            return set()
        return set(df["symbol"].cast(pl.Utf8).to_list())

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

        # 扩展范围用于指标预热 (MA60 需要 ~60 交易日 ≈ 120 日历日)
        warmup_start = start - timedelta(days=ENGINE_COMPAT_WARMUP_CALENDAR_DAYS)

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

        warmup_start = start - timedelta(days=ENGINE_COMPAT_WARMUP_CALENDAR_DAYS)
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

        warmup_start = start - timedelta(days=ENGINE_COMPAT_WARMUP_CALENDAR_DAYS)
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

    def get_hk_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """港股日K查询 — 读取独立 HK enriched 窄表。"""
        return self._scan_hk_daily_symbol(symbol, start, end, columns)

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
        if asset_type == "hk":
            return self.get_hk_daily(symbol, start, end, columns)
        return pl.DataFrame()

    def get_minute(
        self,
        symbol: str,
        trade_date: date,
    ) -> pl.DataFrame:
        """分钟K查询 — Polars scan_parquet + predicate pushdown。"""
        try:
            return pl.scan_parquet(self._minute_glob).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("datetime").dt.date() == trade_date)
            ).sort("datetime").collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("分钟K查询失败: %s", e)
            return pl.DataFrame()

    # ================================================================
    # Polars 查询内部方法
    # ================================================================

    def _scan_unique_enriched(
        self,
        parquet_glob: str,
        *,
        start: date,
        end: date,
        columns: list[str],
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """统一的去重惰性扫描: 日期/符号下推 + unique(symbol,date, keep='last')。

        保证冲突的遗留重复行按 repository 既有的「最后物理行胜出」策略确定性地
        去重。投影强制含 symbol/date; 用 ScanCastOptions(integer_cast="allow-float")
        兼容旧分区。调用方 (get_enriched_range 等) 负责 warning/空帧兜底; 这里不做
        第二次全量扫描来统计重复计数。
        """
        try:
            lf = pl.scan_parquet(
                parquet_glob, cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
            )
            schema_names = lf.collect_schema().names()
            proj = [c for c in columns if c in schema_names]
            if "symbol" not in proj and "symbol" in schema_names:
                proj.append("symbol")
            if "date" not in proj and "date" in schema_names:
                proj.append("date")
            lf = lf.select(proj).filter(
                (pl.col("date") >= start) & (pl.col("date") <= end)
            )
            if symbols is not None and "symbol" in proj:
                lf = lf.filter(pl.col("symbol").is_in(symbols))
            return (
                lf.collect()
                .unique(subset=["symbol", "date"], keep="last", maintain_order=True)
                .sort(["symbol", "date"])
            )
        except Exception:
            logger.debug("enriched 扫描失败 (%s)", parquet_glob, exc_info=True)
            raise

    def _compute_enriched_range(self, df: pl.DataFrame) -> pl.DataFrame:
        """对14列enriched数据即时计算完整指标+信号。输入应含足够预热行数。"""
        from app.indicators.pipeline import compute_indicators, compute_signals, compute_limit_signals, filter_halt_days, clean_nan_inf
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
            df = compute_limit_signals(df, instruments)
            df = clean_nan_inf(df)
        except Exception as e:  # noqa: BLE001
            logger.warning("on-demand compute failed: %s", e)
        return df

    def _compute_index_enriched_range(self, df: pl.DataFrame) -> pl.DataFrame:
        """指数只计算通用技术指标和通用信号，跳过涨跌停/股本/市值逻辑。"""
        from app.indicators.pipeline import compute_indicators, compute_signals, clean_nan_inf
        if df.is_empty() or df.height < 2:
            return df
        try:
            df = compute_indicators(df)
            df = compute_signals(df)
            df = clean_nan_inf(df)
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
            lf = pl.scan_parquet(self._enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return lf.collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("日K查询失败: %s", e)
            return pl.DataFrame()

    def _scan_daily_batch(self, symbols: list[str], start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = pl.scan_parquet(self._enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol").is_in(symbols))
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort(["symbol", "date"])
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return lf.collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("日K批量查询失败: %s", e)
            return pl.DataFrame()

    def _scan_index_daily_symbol(self, symbol: str, start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = pl.scan_parquet(self._index_enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return lf.collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("指数日K查询失败: %s", e)
            return pl.DataFrame()

    def _scan_etf_daily_symbol(self, symbol: str, start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = pl.scan_parquet(self._etf_enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return lf.collect()
        except Exception as e:  # noqa: BLE001
            logger.debug("ETF 日K查询跳过: %s", e)
            return pl.DataFrame()

    def _scan_hk_daily_symbol(self, symbol: str, start: date, end: date, columns: list[str] | None) -> pl.DataFrame:
        try:
            lf = pl.scan_parquet(self._hk_enriched_glob,
                                 cast_options=pl.ScanCastOptions(integer_cast="allow-float")).filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= start)
                & (pl.col("date") <= end)
            ).sort("date")
            if columns:
                schema_names = lf.collect_schema().names()
                existing = [c for c in columns if c in schema_names]
                lf = lf.select(existing)
            return lf.collect()
        except Exception as e:  # noqa: BLE001
            logger.debug("港股日K查询跳过: %s", e)
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

    def latest_minute_date(self, symbol: str) -> date | None:
        try:
            with self._lock:
                row = self.db.execute(
                    "SELECT max(CAST(datetime AS DATE)) FROM kline_minute WHERE symbol = ?",
                    [symbol],
                ).fetchone()
            if row and row[0]:
                return row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
        except duckdb.CatalogException:
            pass
        return None

    def earliest_daily_date(self) -> date | None:
        """本地日K数据的最早日期。"""
        try:
            with self._lock:
                res = self.db.execute(
                    "SELECT min(date) FROM kline_daily",
                ).fetchone()
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:
            return None
        return None

    def earliest_minute_date(self) -> date | None:
        """本地分钟K数据的最早日期。"""
        try:
            with self._lock:
                res = self.db.execute(
                    "SELECT min(CAST(datetime AS DATE)) FROM kline_minute",
                ).fetchone()
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:
            return None
        return None

    def latest_daily_date(self) -> date | None:
        """本地日K数据的最新日期。"""
        try:
            with self._lock:
                res = self.db.execute(
                    "SELECT max(date) FROM kline_daily",
                ).fetchone()
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:
            return None
        return None

    def _latest_enriched_date_duckdb(self) -> date | None:
        try:
            with self._lock:
                res = self.db.execute(
                    "SELECT max(date) FROM kline_enriched",
                ).fetchone()
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
        if self._skip_raw_daily_write("append_daily", df):
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
        if self._skip_raw_daily_write("append_index_daily", df):
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
        if self._skip_raw_daily_write("append_etf_daily", df):
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

    def append_hk_daily(self, df: pl.DataFrame) -> None:
        """按日分区写入港股日K数据 (merge-upsert)。"""
        if df.is_empty():
            return
        self._write_daily_partition(df, "kline_hk_daily")

    def append_hk_enriched(self, df: pl.DataFrame) -> None:
        """按日分区写入港股 enriched。存储列与 A 股/ETF 同为 ENRICHED_STORAGE_COLS。

        原先只存 symbol/date/close/change_pct 四列 —— 那样的面板算不出任何指标,
        筛选/复盘都用不了。现按同一套存储列落盘,港股天然没有的列
        (consecutive_limit_ups 等涨跌停派生列) 由 `in df.columns` 过滤自动跳过。
        """
        if df.is_empty():
            return
        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        if not storage_cols:
            return
        self._write_daily_partition(df.select(storage_cols), "kline_hk_enriched")

    def save_hk_instruments(self, df: pl.DataFrame) -> None:
        """保存港股标的维表到独立目录。含 float_shares,enriched 算换手率要用。"""
        if df.is_empty() or "symbol" not in df.columns:
            return
        if "asset_type" not in df.columns:
            df = df.with_columns(pl.lit("hk").alias("asset_type"))
        out = self.store.data_dir / "instruments_hk" / "instruments_hk.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(df.unique(subset=["symbol"], keep="last").sort("symbol"), out)
        self._hk_instruments_cache = None
        self._refresh_hk_instruments()

    def append_daily_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按资产类型写入日K；stock/index 保持旧目录兼容。"""
        if df.is_empty():
            return
        if self._skip_raw_daily_write(f"append_daily_asset:{asset_type}", df):
            return
        if asset_type == "stock":
            self.append_daily(df)
        elif asset_type == "index":
            self.append_index_daily(df)
        elif asset_type == "etf":
            self.append_etf_daily(df)
        elif asset_type == "hk":
            self.append_hk_daily(df)

    def append_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按资产类型写入 enriched；stock/index 保持旧目录兼容。"""
        if asset_type == "stock":
            self.append_enriched(df)
        elif asset_type == "index":
            self.append_index_enriched(df)
        elif asset_type == "etf":
            self.append_etf_enriched(df)
        elif asset_type == "hk":
            self.append_hk_enriched(df)

    def save_index_instruments(self, df: pl.DataFrame) -> None:
        """保存指数标的维表。"""
        if df.is_empty() or "symbol" not in df.columns:
            return
        out = self.store.data_dir / "instruments_index" / "instruments_index.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(df.unique(subset=["symbol"], keep="last").sort("symbol"), out)
        self._index_instruments_cache = None
        self._etf_instruments_cache = None
        self._refresh_index_instruments()

    def save_etf_instruments(self, df: pl.DataFrame) -> None:
        """保存 ETF 标的维表到独立目录。"""
        if df.is_empty() or "symbol" not in df.columns:
            return
        if "asset_type" not in df.columns:
            df = df.with_columns(pl.lit("etf").alias("asset_type"))
        out = self.store.data_dir / "instruments_etf" / "instruments_etf.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(df.unique(subset=["symbol"], keep="last").sort("symbol"), out)
        self._etf_instruments_cache = None
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
            f"""CREATE OR REPLACE VIEW kline_hk_daily AS
                SELECT * FROM read_parquet('{d}/kline_hk_daily/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW kline_hk_enriched AS
                SELECT * FROM read_parquet('{d}/kline_hk_enriched/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_index AS
                SELECT * FROM read_parquet('{d}/instruments_index/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_etf AS
                SELECT * FROM read_parquet('{d}/instruments_etf/**/*.parquet', union_by_name=true)""",
            f"""CREATE OR REPLACE VIEW instruments_hk AS
                SELECT * FROM read_parquet('{d}/instruments_hk/**/*.parquet', union_by_name=true)""",
        ]
        for sql in statements:
            try:
                with self._lock:
                    self.db.execute(sql)
            except Exception as e:  # noqa: BLE001
                logger.debug("index/etf/hk view refresh skipped: %s", e)
        with self._lock:
            self.store._register_unified_views()

    def _write_daily_partition(self, df: pl.DataFrame, table: str) -> None:
        """按 date 分区写入 parquet，每个日期一个文件，支持 merge-upsert。"""
        # 首次写入前无条件去重: 覆盖 append_enriched/append_index_enriched/
        # append_etf_enriched/append_hk_enriched; 不依赖 out.exists(), 因为确认过的
        # 指数级重复也能在全新分区里被创建。
        if "symbol" in df.columns and "date" in df.columns and not df.is_empty():
            df = df.unique(subset=["symbol", "date"], keep="last", maintain_order=True)
        base = self.store.data_dir / table
        for date_df in df.partition_by("date"):
            dt = date_df["date"][0]
            ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
            out = base / f"date={ds}" / "part.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists():
                existing = pl.read_parquet(out)
                date_df = pl.concat([existing, date_df], how="diagonal_relaxed").unique(
                    subset=["symbol", "date"], keep="last"
                )
            date_df = date_df.sort(["symbol", "date"])
            _atomic_write_parquet(date_df, out)

    def merge_live_daily_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按 symbol 合并当天指定资产日K分区。用于少量自选实时，不覆盖全市场。"""
        if df.is_empty() or "date" not in df.columns:
            return
        self._assert_sealed_write_source(df)
        if self._skip_raw_daily_write(f"merge_live_daily_asset:{asset_type}", df):
            return
        table = {
            "stock": "kline_daily",
            "index": "kline_index_daily",
            "etf": "kline_etf_daily",
            "hk": "kline_hk_daily",
        }.get(asset_type)
        if not table:
            return
        base = self.store.data_dir / table
        dt = df["date"][0]
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        date_df = df.sort(["symbol", "date"])
        if out.exists():
            existing = pl.read_parquet(out)
            date_df = pl.concat([existing, date_df], how="diagonal_relaxed").unique(
                subset=["symbol", "date"], keep="last"
            )
        _atomic_write_parquet(date_df.sort(["symbol", "date"]), out)

    def merge_live_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """按 symbol 合并当天 enriched 分区和内存缓存。用于少量自选实时。"""
        if df.is_empty() or "date" not in df.columns:
            return
        self._assert_sealed_write_source(df)
        dt = df["date"][0]
        if asset_type == "stock":
            table = "kline_daily_enriched"
            existing_cache = self._enriched_cache if self._enriched_cache_date == dt else pl.DataFrame()
        elif asset_type == "etf":
            table = "kline_etf_enriched"
            existing_cache = self._etf_enriched_cache if self._etf_enriched_cache_date == dt else pl.DataFrame()
        elif asset_type == "index":
            table = "kline_index_enriched"
            existing_cache = pl.DataFrame()
        elif asset_type == "hk":
            table = "kline_hk_enriched"
            existing_cache = pl.DataFrame()
        else:
            return

        # 内存帧去重: 即使 existing_cache 为空, 输入 df 本身也可能含重复 (symbol,date)
        merged_cache = df.unique(
            subset=["symbol", "date"], keep="last", maintain_order=True,
        ) if "symbol" in df.columns else df
        if existing_cache is not None and not existing_cache.is_empty():
            merged_cache = pl.concat([existing_cache, merged_cache], how="diagonal_relaxed").unique(
                subset=["symbol", "date"], keep="last"
            )
        merged_cache = merged_cache.sort(["symbol"])
        if asset_type == "stock":
            self._enriched_cache = merged_cache
            self._enriched_cache_date = dt
        elif asset_type == "etf":
            self._etf_enriched_cache = merged_cache
            self._etf_enriched_cache_date = dt

        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        # 存储帧同样先去重, 再按需与磁盘 merge
        df_storage = df.select(storage_cols).unique(
            subset=["symbol", "date"], keep="last", maintain_order=True,
        ).sort(["symbol"])
        base = self.store.data_dir / table
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            existing = pl.read_parquet(out)
            df_storage = pl.concat([existing, df_storage], how="diagonal_relaxed").unique(
                subset=["symbol", "date"], keep="last"
            )
        _atomic_write_parquet(df_storage.sort(["symbol"]), out)

    def flush_live_daily(self, df: pl.DataFrame) -> None:
        """覆写当天 kline_daily 分区 (实时行情落盘, 非merge)。"""
        if df.is_empty() or "date" not in df.columns:
            return
        if self._skip_raw_daily_write("flush_live_daily", df):
            return
        self.flush_live_daily_asset("stock", df)

    def flush_live_daily_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """覆写当天指定资产日K分区 (实时行情落盘, 非merge)。"""
        if df.is_empty() or "date" not in df.columns:
            return
        self._assert_sealed_write_source(df)
        if self._skip_raw_daily_write(f"flush_live_daily_asset:{asset_type}", df):
            return
        table = {
            "stock": "kline_daily",
            "index": "kline_index_daily",
            "etf": "kline_etf_daily",
            "hk": "kline_hk_daily",
        }.get(asset_type)
        if not table:
            return
        base = self.store.data_dir / table
        dt = df["date"][0]
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(df.sort(["symbol", "date"]), out)

    @staticmethod
    def _assert_sealed_write_source(df: pl.DataFrame) -> None:
        """拒绝外部或无法确认 provenance 的帧进入 sealed 分区。

        现有实时链路产出的帧无 source 列，零误伤；有 source 但无法规范化时
        fail-closed，不能借异常绕过 provenance 检查。
        """
        if "source" not in df.columns or df.is_empty():
            return
        try:
            bad = df.filter(
                pl.col("source").is_not_null()
                & ~pl.col("source").cast(pl.Utf8).str.starts_with("fquant")
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "sealed partition write rejected: unrecognized source provenance"
            ) from exc
        if not bad.is_empty():
            raise ValueError(
                "sealed partition write rejected: external fallback frame "
                f"(rows={bad.height})"
            )

    def _skip_raw_daily_write(self, op: str, df: pl.DataFrame) -> bool:
        from app.services.data_mode import is_local_daily_mode

        if not is_local_daily_mode():
            return False
        if not self._is_stock_raw_write_op(op):
            return False
        logger.debug("stock raw daily write skipped in fquant_local mode: op=%s rows=%d", op, df.height)
        return True

    @staticmethod
    def _is_stock_raw_write_op(op: str) -> bool:
        asset_ops = (
            "append_daily_asset:",
            "merge_live_daily_asset:",
            "flush_live_daily_asset:",
        )
        for prefix in asset_ops:
            if op.startswith(prefix):
                return op.split(":", 1)[1] == "stock"
        return op in {"append_daily", "flush_live_daily"}

    def flush_live_enriched(self, df: pl.DataFrame) -> None:
        """覆写当天 kline_daily_enriched 分区 (实时 enriched 落盘, 非merge)。

        内存缓存保留完整指标列供各服务使用，磁盘仅写入 14 列存储列。
        """
        self.flush_live_enriched_asset("stock", df)

    def flush_live_enriched_asset(self, asset_type: str, df: pl.DataFrame) -> None:
        """覆写当天指定资产 enriched 分区 (实时 enriched 落盘, 非merge)。"""
        if df.is_empty() or "date" not in df.columns:
            return
        self._assert_sealed_write_source(df)
        if "symbol" in df.columns:
            df = df.unique(
                subset=["symbol", "date"], keep="last", maintain_order=True,
            )
        dt = df["date"][0]
        if asset_type == "stock":
            self._enriched_cache = df.sort(["symbol"])
            self._enriched_cache_date = dt
            table = "kline_daily_enriched"
        elif asset_type == "etf":
            self._etf_enriched_cache = df.sort(["symbol"])
            self._etf_enriched_cache_date = dt
            table = "kline_etf_enriched"
        elif asset_type == "index":
            table = "kline_index_enriched"
        elif asset_type == "hk":
            table = "kline_hk_enriched"
        else:
            return

        from app.indicators.pipeline import ENRICHED_STORAGE_COLS
        storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
        # 覆写落盘前同样强制 (symbol,date) 唯一, 阻止重复行写入全新分区
        df_storage = df.select(storage_cols).unique(
            subset=["symbol", "date"], keep="last", maintain_order=True,
        ).sort(["symbol"])
        base = self.store.data_dir / table
        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        out = base / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(df_storage, out)
