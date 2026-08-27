"""FQuantProvider v2 — 直连本地 DuckDB 数据源。

严格按 ``backend/docs/FQUANT_PROVIDER_DESIGN.md`` §4 模块设计 / §5 数据映射 /
§6 配置 / §7 错误降级 / §8 测试方案 实现。

架构（§4.1）::

    backend/app/data_providers/
    ├── fquant/
    │   ├── __init__.py          符号归一重导出
    │   ├── symbols.py           符号归一（split_symbol 等）
    │   ├── fstore_duckdb_client.py fstore.duckdb 只读客户端
    │   ├── tdx_duckdb_client.py tdx.duckdb / minutes / trans 只读客户端
    │   ├── sina_tencent_client.py realtime fallback 客户端
    │   ├── mapping.py           上游字段 → 内部 schema
    │   ├── adj_factor.py        xdxr → 单次事件 ex_factor
    │   ├── raw_reconstruct.py   TDX 前复权序列 → raw OHLC 修复
    │   └── fallback.py          降级策略表
    └── fquant_provider.py       本文件（聚合 Provider）

能力声明（§3.5 / §4.2）::

    instruments=True, daily=True, adj_factor=True,
    minute=True, realtime=True, financial=True, depth=False, universes=True

错误降级（§7）：任一源不可达 → 返回空 DF + warning，不抛异常。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.fquant.adj_factor import (
    build_ex_factor_df,
    compute_ex_factor_from_xdxr,
)
from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient
from app.data_providers.fquant import generation
from app.data_providers.fquant.ordered_trans import PublishedOrderedTransMinuteReader
from app.data_providers.fquant.mapping import (
    base_infos_rows_to_instruments,
    chengfen_gu_rows_to_universes,
    chuquan_rows_to_events,
    financial_rows_to_df,
    klines_rows_to_daily,
    minutes_rows_to_minute_df,
    moneyflow_daily_to_df,
    trans_rows_to_df,
    wide_rows_to_daily,
    xdxr_rows_to_events,
)
from app.data_providers.fquant.raw_reconstruct import reconstruct_raw_rows
from app.data_providers.fquant.symbols import (
    asset_type_str_to_nums,
    code_to_symbol,
    is_etf_symbol,
    split_symbol,
    symbol_to_code,
)
from app.data_providers.fquant.tdx_duckdb_client import TdxDuckDBClient
from app.data_providers.normalizer import (
    normalize_adj_factors,
    normalize_daily,
    normalize_instruments,
    normalize_realtime,
)

logger = logging.getLogger(__name__)

# fstore financial_report_* 表名映射（§5.6 / §4.8）
_FINANCIAL_TABLE_MAP: dict[str, str] = {
    "income":        "financial_report_income_statement",
    "balance_sheet": "financial_report_balance_sheet",
    "cash_flow":     "financial_report_cash_flow",
    "annual":        "financial_report_annual",
    "quick":         "financial_report_quick",
    "forecast":      "financial_report_forecast",
}


def _minute_freq_step(freq: str) -> int:
    s = (freq or "1m").strip().lower()
    if not s.endswith("m"):
        return 1
    try:
        return max(1, int(s[:-1]))
    except ValueError:
        return 1


def _sum_present(values: list[float | int | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    return sum(present) if present else None


def _aggregate_minute_df(df: pl.DataFrame, freq: str) -> pl.DataFrame:
    step = _minute_freq_step(freq)
    if step <= 1 or df.is_empty():
        return df

    rows: list[dict] = []
    current_key: tuple[str, str] | None = None
    bucket: list[dict] = []

    def flush() -> None:
        if not bucket:
            return
        highs = [r.get("high") for r in bucket if r.get("high") is not None]
        lows = [r.get("low") for r in bucket if r.get("low") is not None]
        rows.append({
            "symbol": bucket[0].get("symbol"),
            "asset_type": bucket[0].get("asset_type"),
            "source": bucket[0].get("source"),
            "datetime": bucket[-1].get("datetime"),
            "open": bucket[0].get("open"),
            "high": max(highs) if highs else None,
            "low": min(lows) if lows else None,
            "close": bucket[-1].get("close"),
            "volume": _sum_present([r.get("volume") for r in bucket]),
            "amount": _sum_present([r.get("amount") for r in bucket]),
            "freq": freq,
        })

    for row in df.sort(["symbol", "datetime"]).to_dicts():
        key = (str(row.get("symbol")), str(row.get("datetime"))[:10])
        if current_key is not None and (key != current_key or len(bucket) >= step):
            flush()
            bucket = []
        current_key = key
        bucket.append(row)
    flush()

    return pl.DataFrame(rows) if rows else pl.DataFrame()

# daily_markets realtime 投影：universe 与 symbols 两条查询路径共用，
# 保证输出形状完全一致（字段抽取由 _fstore_quote_to_row 消费）。
_DAILY_MARKETS_REALTIME_COLS = """\
    code,
    COALESCE(payload_json->>'Name', '') AS name,
    trade_date AS tdate,
    price,
    CAST(NULLIF(payload_json->>'Zdfd', '') AS DOUBLE) AS zdfd,
    CAST(NULLIF(payload_json->>'Zded', '') AS DOUBLE) AS zded,
    CAST(NULLIF(payload_json->>'Cjl', '') AS BIGINT) AS cjl,
    CAST(NULLIF(payload_json->>'Cje', '') AS DOUBLE) AS cje,
    CAST(NULLIF(payload_json->>'Jrkpj', '') AS DOUBLE) AS jrkpj,
    CAST(NULLIF(payload_json->>'Zgj', '') AS DOUBLE) AS zgj,
    CAST(NULLIF(payload_json->>'Zdj', '') AS DOUBLE) AS zdj,
    CAST(NULLIF(payload_json->>'Zrspj', '') AS DOUBLE) AS zrspj,
    CAST(NULLIF(payload_json->>'Hslv', '') AS DOUBLE) AS hslv,
    CAST(NULLIF(payload_json->>'Zhfu', '') AS DOUBLE) AS zhfu
"""


# =========================================================================== #
# FQuantProvider（对外接口，本地源聚合）
# =========================================================================== #
class FQuantProvider:
    """FQuant 数据源 Provider — 直连底层本地源。

    实现 ``MarketDataProvider`` 接口（见 ``base.py``）。各本地源独立工作，
    任一故障不影响其余（§7）。

    能力声明（§3.5 / §4.2）：
    - instruments / daily / adj_factor / minute / realtime / financial / universes → True
    - depth → False；本地 DuckDB 当前无 5 档盘口
    """

    name = "fquant"
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        adj_factor=True,
        minute=True,
        realtime=True,
        financial=True,
        depth=False,
        universes=True,   # 阶段 3 #3.2：fstore chengfen_gu 提供指数/板块/行业
        minute_month_extension=True,
        ordered_trans_research=True,
    )

    def __init__(
        self,
        name: str = "fquant",
        *,
        snapshot_paths: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        pinned = dict(snapshot_paths or {})
        fstore_kwargs = {
            "path": pinned.get("fstore"),
            "markets_path": pinned.get("markets"),
            "klines_path": pinned.get("klines"),
            "extended_path": pinned.get("extended"),
        }
        self._fstore = FStoreDuckDBClient(**fstore_kwargs)
        # 独立 markets 客户端：realtime/daily_markets 查询走自己的连接与锁，
        # 不与 _fstore 上的财务/K线/管道查询共享客户端锁而互相阻塞。
        self._fstore_markets = FStoreDuckDBClient(**fstore_kwargs)
        self._engine = TdxDuckDBClient(
            tdx_path=pinned.get("tdx"),
            pin_paths=bool(snapshot_paths),
        )
        # instruments 缓存（§4.3 24h TTL）
        self._instruments_cache: dict[str, pl.DataFrame] = {}
        self._instruments_cache_ts: dict[str, datetime] = {}
        self._instruments_cache_ttl = 86400  # 秒

        # 标的参考标记缓存（AH/沪深股通/上市日期, 24h TTL, 与 instruments 缓存同生命周期）
        self._reference_flags_cache: pl.DataFrame | None = None
        self._reference_flags_cache_ts: datetime | None = None

    def close(self) -> None:
        """关闭底层 FStore 与 TDX 连接（幂等）。供 lifespan 关闭链调用。"""
        try:
            self._fstore.close()
        except Exception:  # noqa: BLE001
            logger.warning("FQuantProvider: 关闭 fstore 连接失败", exc_info=True)
        try:
            self._fstore_markets.close()
        except Exception:  # noqa: BLE001
            logger.warning("FQuantProvider: 关闭 fstore markets 连接失败", exc_info=True)
        try:
            self._engine.close()
        except Exception:  # noqa: BLE001
            logger.warning("FQuantProvider: 关闭 TDX 连接失败", exc_info=True)

    def open_ordered_trans_reader(self) -> PublishedOrderedTransMinuteReader | None:
        """Open the current immutable ordered-trans research generation."""
        root = generation.root_for("tdx_ordered_trans")
        if not root:
            return None
        try:
            return PublishedOrderedTransMinuteReader(root)
        except Exception:  # noqa: BLE001
            logger.warning(
                "FQuantProvider: ordered-trans generation unavailable at %s",
                root,
                exc_info=True,
            )
            return None

    def refresh_fstore_clients(self) -> None:
        """刷新所有 fstore 客户端连接，下次查询重新解析 generation 快照。

        同时刷新 ``_fstore``（财务/K线/元数据）与 ``_fstore_markets``（realtime/daily_markets），
        避免 markets generation 切换后 realtime 仍读旧快照。两客户端可能是同一实例，
        用 id() 去重避免重复关闭。盘后/盘前管道开始前调用本方法，强制重建连接到当前 generation。

        同时清空 instruments 内存缓存（24h TTL），确保 get_instruments 从新 generation 重读。
        """
        seen: set[int] = set()
        for attr in ("_fstore", "_fstore_markets"):
            client = getattr(self, attr, None)
            if client is None:
                continue
            cid = id(client)
            if cid in seen:
                continue
            seen.add(cid)
            refresh = getattr(client, "refresh", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    logger.warning("FQuantProvider: 刷新 %s 连接失败", attr, exc_info=True)

        # 客户端重建到新 generation 后, 旧 generation 的 instruments 内存缓存
        # （24h TTL）也必须失效, 否则 run_now 先 refresh 再 instrument_sync
        # 时 get_instruments 仍返回旧 generation 的标的列表。
        self._instruments_cache.clear()
        self._instruments_cache_ts.clear()
        self._reference_flags_cache = None
        self._reference_flags_cache_ts = None

    # ------------------------------------------------------------------ #
    # get_instruments — §4.3 主源 fstore.base_infos
    # ------------------------------------------------------------------ #
    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """拉取股票列表并归一（§4.3）。

        数据流：fstore ``base_infos``（按 ``asset_type`` 数字过滤）
        缓存：24h TTL（§4.3）
        降级：DB 连接失败 → 空 df（§7.1）

        支持的 asset_type（契约 Literal["stock","index","etf"]）：
        - ``stock`` → fstore asset_type=1（A 股）
        - ``index`` → fstore asset_type=10
        - ``etf``   → fstore asset_type=20
        """
        # 缓存检查
        cached = self._get_cached_instruments(asset_type)
        if cached is not None:
            return cached

        from app.data_providers.fquant.symbols import asset_type_str_to_nums

        nums = asset_type_str_to_nums(asset_type)
        if not nums:
            return pl.DataFrame()

        # 动态构造 IN 子句（asset_type 可能有多个数字）
        placeholders = ",".join(["%s"] * len(nums))
        rows = self._fstore.query(
            f"SELECT code, name, asset_type, ssdate, symbol, zgb, ltgb "
            f"FROM base_infos "
            f"WHERE asset_type IN ({placeholders}) "
            f"ORDER BY code LIMIT 10000",
            tuple(nums),
        )
        if not rows:
            logger.debug("FStoreDB base_infos 无数据（asset_type=%s）", asset_type)
            return pl.DataFrame()

        instruments = base_infos_rows_to_instruments(rows, asset_type=asset_type, source=self.name)
        df = normalize_instruments(instruments, asset_type=asset_type, source=self.name)

        # 写缓存
        self._set_cached_instruments(asset_type, df)
        return df

    def _get_cached_instruments(self, asset_type: str) -> pl.DataFrame | None:
        """读 instruments 缓存，过期返回 None。"""
        ts = self._instruments_cache_ts.get(asset_type)
        if ts is None:
            return None
        if (datetime.now() - ts).total_seconds() > self._instruments_cache_ttl:
            return None
        return self._instruments_cache.get(asset_type)

    def _set_cached_instruments(self, asset_type: str, df: pl.DataFrame) -> None:
        self._instruments_cache[asset_type] = df
        self._instruments_cache_ts[asset_type] = datetime.now()

    # ------------------------------------------------------------------ #
    # get_daily — §4.4 双源融合（engine-data wide 主 + fstore day_klines 备）
    def get_daily_freshness(self) -> date | None:
        """返回 ``get_daily`` 主源与 fallback 合并后的最新可用交易日。"""
        candidates: list[date] = []
        engine_date = self._engine.freshness()
        if isinstance(engine_date, date):
            candidates.append(engine_date)
        rows = self._fstore.query(
            """
            SELECT CAST(max(tdate) AS VARCHAR) AS latest_date
            FROM t_1_day_klines
            WHERE ktype = 101 AND fq = 0
            """
        )
        if rows and rows[0].get("latest_date"):
            try:
                candidates.append(date.fromisoformat(str(rows[0]["latest_date"])[:10]))
            except ValueError:
                logger.warning(
                    "FQuantProvider: 无法解析 fstore day_klines 水位 %r",
                    rows[0]["latest_date"],
                )
        return max(candidates) if candidates else None

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,  # noqa: ARG002
    ) -> pl.DataFrame:
        """拉取日 K 线并归一（§4.4）。

        数据流（§7.1 降级链）：
        1. 主源 engine-data ``wide``（字段最全，含 last_close/change_rate）
        2. 备份 fstore ``day_klines``（fq=0 不复权；实测 600519 最后 2025-10-31）
        3. 空 df

        输出列（经 ``normalize_daily``）：symbol/date/open/high/low/close/volume/amount
        """
        if not symbols:
            return pl.DataFrame()

        frames: list[pl.DataFrame] = []
        for sym in symbols:
            code = symbol_to_code(sym)
            rows = self._get_daily_from_engine_wide(
                sym, code, start_time, end_time, asset_type
            )
            if asset_type == "index":
                # engine wide 可能停在较早交易日；asset_type=10 的 daily_markets
                # 与指数 code 无歧义，可补齐缺失日期，同日仍以 engine 为准。
                market_rows = self._get_index_daily_from_markets(
                    sym, code, start_time, end_time
                )
                by_date = {
                    str(row.get("date")): row
                    for row in market_rows
                    if row.get("date") is not None
                }
                by_date.update(
                    {
                        str(row.get("date")): row
                        for row in rows
                        if row.get("date") is not None
                    }
                )
                rows = [by_date[value] for value in sorted(by_date)]
            else:
                # 有明确区间时，两源按日期合并：engine 覆盖同日，fstore
                # 补首尾缺口。只在无区间且 engine 已有数据时跳过额外查询。
                fallback_rows = (
                    self._get_daily_from_fstore_klines(
                        sym, code, start_time, end_time, asset_type
                    )
                    if not rows or start_time is not None or end_time is not None
                    else []
                )
                by_date = {
                    str(row.get("date")): row
                    for row in fallback_rows
                    if row.get("date") is not None
                }
                by_date.update(
                    {
                        str(row.get("date")): row
                        for row in rows
                        if row.get("date") is not None
                    }
                )
                rows = [by_date[value] for value in sorted(by_date)]
            if not rows:
                logger.debug("get_daily %s: 本地日K链均无数据", sym)
                continue
            normalized = normalize_daily(rows, default_symbol=sym, source=self.name)
            if not normalized.is_empty():
                frames.append(normalized)

        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def _get_daily_from_engine_wide(
        self, symbol: str, code: str,
        start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> list[dict]:
        """主源 engine-data ``wide``（§4.4 / §5.2）。"""
        if start_time and end_time:
            limit = max(250, (end_time - start_time).days + 10)
        else:
            limit = 250
        rows = self._engine.get_wide(code, limit=limit, asset_type=asset_type)
        if rows:
            # engine 返回最新在前，反转成时间正序
            rows = list(reversed(rows))
            if asset_type == "stock":
                oracle_rows = self._get_raw_oracle_rows(code, rows)
                events = self._engine.get_xdxr(code, asset_type=asset_type)
                rows = reconstruct_raw_rows(rows, events, oracle_rows)
            logger.debug("tdx wide %s: %d 行", code, len(rows))
        # 映射到 normalizer 期望的字段名
        return self._filter_daily_rows(wide_rows_to_daily(rows, symbol, source=self.name), start_time, end_time)

    def _filter_daily_rows(
        self,
        rows: list[dict],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list[dict]:
        if not rows or not (start_time or end_time):
            return rows
        start = start_time.date().isoformat() if start_time else None
        end = end_time.date().isoformat() if end_time else None
        out = []
        for row in rows:
            date_str = str(row.get("date") or "")
            if start and date_str < start:
                continue
            if end and date_str > end:
                continue
            out.append(row)
        return out

    def _get_index_daily_from_markets(
        self,
        symbol: str,
        code: str,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list[dict]:
        """从 daily_markets 的指数口径（asset_type=10）补齐日 K 缺口。"""
        conditions = ["asset_type = 10", "code = %s"]
        params: list[object] = [code]
        if start_time is not None:
            conditions.append("trade_date >= %s")
            params.append(start_time.date())
        if end_time is not None:
            conditions.append("trade_date <= %s")
            params.append(end_time.date())
        order_limit = "ORDER BY trade_date ASC"
        if start_time is None and end_time is None:
            order_limit = "ORDER BY trade_date DESC LIMIT 250"
        rows = self._fstore_markets.query(
            f"""
            SELECT
                trade_date::text AS date,
                CAST(NULLIF(payload_json->>'Jrkpj', '') AS DOUBLE) AS open,
                CAST(NULLIF(payload_json->>'Zgj', '') AS DOUBLE) AS high,
                CAST(NULLIF(payload_json->>'Zdj', '') AS DOUBLE) AS low,
                price::float8 AS close,
                CAST(NULLIF(payload_json->>'Cjl', '') AS DOUBLE) AS volume,
                CAST(NULLIF(payload_json->>'Cje', '') AS DOUBLE) AS amount
            FROM daily_markets
            WHERE {' AND '.join(conditions)}
            {order_limit}
            """,
            params,
        )
        if start_time is None and end_time is None:
            rows.reverse()
        return [
            {"symbol": symbol, **row}
            for row in rows
            if row.get("date") is not None
        ]

    def _get_raw_oracle_rows(self, code: str, rows: list[dict]) -> list[dict]:
        """Fetch fstore raw OHLCV oracle for the engine-row date span.

        ``t_1_day_klines`` 是完整、单位已校准的首选日 K oracle；仅当它缺少
        engine 请求日期时，才查询 ``daily_markets`` 补洞。这样既避免后者的
        陈旧 ``Cjl=0`` 覆盖正确成交量，也避免每次日 K 请求重复扫描 markets。
        """
        dates = sorted(str(r.get("date")) for r in rows if r.get("date"))
        if not dates:
            return []
        day_rows = self._fstore.query(
            """
            SELECT
                tdate::text AS date,
                open::float8 AS oracle_open,
                high::float8 AS oracle_high,
                low::float8 AS oracle_low,
                close::float8 AS oracle_close,
                cjl::float8 * 100 AS oracle_volume,
                cje::float8 AS oracle_amount
            FROM t_1_day_klines
            WHERE code = %s AND ktype = 101 AND fq = 0 AND tdate BETWEEN %s AND %s
            ORDER BY tdate ASC
            """,
            (code, dates[0], dates[-1]),
        )
        by_date = {str(r.get("date")): r for r in day_rows}
        missing_dates = [value for value in dates if value not in by_date]
        if missing_dates:
            market_rows = self._fstore.query(
                """
                SELECT
                    trade_date::text AS date,
                    CAST(NULLIF(payload_json->>'Jrkpj', '') AS DOUBLE) AS oracle_open,
                    CAST(NULLIF(payload_json->>'Zgj', '') AS DOUBLE) AS oracle_high,
                    CAST(NULLIF(payload_json->>'Zdj', '') AS DOUBLE) AS oracle_low,
                    price AS oracle_close,
                    CAST(NULLIF(payload_json->>'Cjl', '') AS DOUBLE) * 100 AS oracle_volume,
                    CAST(NULLIF(payload_json->>'Cje', '') AS DOUBLE) AS oracle_amount
                FROM daily_markets
                WHERE asset_type = 1 AND code = %s AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date ASC
                """,
                (code, missing_dates[0], missing_dates[-1]),
            )
            # t_1_day_klines 是专用日K oracle，优先级高于 daily_markets；
            # daily_markets 只补 day_klines 缺失的日期，不覆盖同日行。
            for row in market_rows:
                row_date = str(row.get("date"))
                if row_date not in by_date:
                    by_date[row_date] = row
        return [by_date[k] for k in sorted(by_date)]

    def _get_daily_from_fstore_klines(
        self, symbol: str, code: str,
        start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> list[dict]:
        """备份 fstore 日线（fq=0 不复权, ktype=101）。

        实测 600519 该表最后数据 2025-10-31（§2.1 / §7.3 场景 A），
        仅作历史回填，不依赖。

        表选择按 asset_type 映射到 fstore 对应的 ``t_{num}_day_klines``
        (如 hk -> t_3_day_klines)，而不是把非 etf 一律当 A 股查
        t_1_day_klines —— 之前这样写会导致港股在这张表上永远查不到数据，
        直接落到下面 unbounded 的 day_klines 兜底查询。
        """
        type_num = asset_type_str_to_nums(asset_type)[0]
        table = f"t_{type_num}_day_klines"
        if start_time and end_time:
            sql = (
                "SELECT tdate, open, close, high, low, cjl, cje, zf "
                f"FROM {table} "
                "WHERE code = %s AND ktype = 101 AND fq = 0 AND tdate BETWEEN %s AND %s "
                "ORDER BY tdate ASC"
            )
            params: tuple = (code, start_time.date(), end_time.date())
        else:
            sql = (
                "SELECT tdate, open, close, high, low, cjl, cje, zf "
                f"FROM {table} "
                "WHERE code = %s AND ktype = 101 AND fq = 0 "
                "ORDER BY tdate DESC LIMIT 250"
            )
            params = (code,)
        rows = self._fstore.query(sql, params)
        if not rows and asset_type != "etf":
            # day_klines 是全市场统一表；显式按 asset_type 过滤，避免不同市场
            # 的 code 恰好相同时把别的市场的行当成这个 symbol 的数据返回
            # （下游 klines_rows_to_daily 的 volume 换算倍数按 asset_type 区分，
            # 混进错误市场的行会产出错误的成交量）。
            if start_time and end_time:
                sql = (
                    "SELECT tdate, open, close, high, low, cjl, cje, zf "
                    "FROM day_klines "
                    "WHERE code = %s AND asset_type = %s AND ktype = 101 AND fq = 0 "
                    "AND tdate BETWEEN %s AND %s "
                    "ORDER BY tdate ASC"
                )
                params = (code, type_num, start_time.date(), end_time.date())
            else:
                sql = (
                    "SELECT tdate, open, close, high, low, cjl, cje, zf "
                    "FROM day_klines "
                    "WHERE code = %s AND asset_type = %s AND ktype = 101 AND fq = 0 "
                    "ORDER BY tdate DESC LIMIT 250"
                )
                params = (code, type_num)
            rows = self._fstore.query(sql, params)
        if rows:
            logger.debug("FStoreDB %s %s: %d 行", table, code, len(rows))
        # 映射到 normalizer 期望的字段名
        return klines_rows_to_daily(rows, symbol, source=self.name, asset_type=asset_type) if rows else []

    # ------------------------------------------------------------------ #
    # get_adj_factors — §4.5 主源 engine-data xdxr + 备份 fstore chuquan_chuxi
    # ------------------------------------------------------------------ #
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """复权除息因子（§4.5）。

        数据流（§7.1 降级链）：
        1. 主源 engine-data ``xdxr``（fenhong/fenshu → 单次事件 ex_factor）
        2. 备份 fstore ``chuquan_chuxi``（pxbl → 单次事件 ex_factor）
        3. 空 df

        输出列（经 ``normalize_adj_factors``）：symbol/trade_date/ex_factor
        """
        if not symbols:
            return pl.DataFrame()
        if asset_type == "hk":
            # No published HK corporate-action/adjustment dataset exists.
            return pl.DataFrame()

        frames: list[pl.DataFrame] = []
        for sym in symbols:
            code = symbol_to_code(sym)

            # 先取 daily close 序列（fenhong 除权除息计算需要 pre_close）
            daily_close = self._build_daily_close_map(sym, code, start_time, end_time, asset_type)

            # 主源 xdxr
            events = self._get_adj_events_from_engine(sym, code)
            if not events:
                # L2 降级：fstore chuquan_chuxi
                events = self._get_adj_events_from_fstore(sym, code, start_time, end_time)
            if not events:
                continue

            results = compute_ex_factor_from_xdxr(events, daily_close)
            if not results:
                continue

            df = build_ex_factor_df(results)
            if df.is_empty():
                continue

            # 日期截断
            df = self._filter_by_date_range(df, "trade_date", start_time, end_time)
            if df.is_empty():
                continue

            normalized = normalize_adj_factors(df.to_dicts(), source=self.name)
            if not normalized.is_empty():
                frames.append(normalized)

        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def _get_adj_events_from_engine(self, symbol: str, code: str) -> list[dict]:
        """主源 engine-data ``xdxr`` → 归一事件行（§5.3）。"""
        rows = self._engine.get_xdxr(code)
        if rows:
            logger.debug("tdx xdxr %s: %d 行", code, len(rows))
        return xdxr_rows_to_events(rows, symbol) if rows else []

    def _get_adj_events_from_fstore(
        self, symbol: str, code: str,
        start_time: datetime | None, end_time: datetime | None,
    ) -> list[dict]:
        """备份 fstore ``chuquan_chuxi`` → 归一事件行（§5.3）。

        把 SELECT 里的 ``t_date`` cast 成 ``DATE``，绕开 TIMESTAMPTZ 物化。
        """
        if start_time and end_time:
            sql = (
                "SELECT CAST(t_date AS DATE) AS t_date, pgbl, pgjg, pxbl, sgbl, cqcxtype "
                "FROM chuquan_chuxi WHERE code = %s AND t_date BETWEEN %s AND %s "
                "ORDER BY t_date ASC"
            )
            params: tuple = (code, start_time, end_time)
        else:
            sql = (
                "SELECT CAST(t_date AS DATE) AS t_date, pgbl, pgjg, pxbl, sgbl, cqcxtype "
                "FROM chuquan_chuxi WHERE code = %s "
                "ORDER BY t_date DESC LIMIT 100"
            )
            params = (code,)
        rows = self._fstore.query(sql, params)
        if rows:
            logger.debug("FStoreDB chuquan_chuxi %s: %d 行", code, len(rows))
        return chuquan_rows_to_events(rows, symbol) if rows else []


    def _build_daily_close_map(
        self, symbol: str, code: str,
        start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> dict[str, float]:
        """构建 ``{date_iso: close_price}`` 字典，供 adj_factor fenhong 计算用。

        复用 get_daily 的 engine wide + fstore klines 降级链。
        """
        try:
            close_start = start_time - timedelta(days=10) if start_time else None
            rows = self._get_daily_from_engine_wide(symbol, code, close_start, end_time, asset_type)
            if not rows:
                rows = self._get_daily_from_fstore_klines(symbol, code, close_start, end_time, asset_type)
            if not rows:
                return {}
            out: dict[str, float] = {}
            for r in rows:
                date_str = r.get("date")
                close = r.get("close")
                if date_str and close is not None:
                    out[str(date_str)] = float(close)
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug("_build_daily_close_map %s 失败: %s", symbol, e)
            return {}

    # ------------------------------------------------------------------ #
    # get_minute — §4.6 主源 engine-data minutes
    def get_minute_coverage(self) -> dict | None:
        """返回 A 股分钟 catalog 最新可安全读取的发布水位。"""
        from app.data_providers.fquant.catalog_resolver import latest_route_coverage

        return latest_route_coverage("tdx_minutes", "a")

    # ------------------------------------------------------------------ #
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
        freq: str = "1m",
    ) -> pl.DataFrame:
        """拉取分钟级数据并归一（§4.6）。

        A股 (asset_type="stock") 多日 start/end：按交易日期逐日调用 catalog route (engine.get_minutes per date) 并合并。
        缺任一中间日 route/stale now 原样上抛 (client updated + sync_batch no-swallow)。
        ETF/HK 维持原有 DuckDB 路径逻辑。
        所有调用显式 asset_type。
        """
        if not symbols:
            return pl.DataFrame()

        if (
            asset_type == "stock"
            and start_time is not None
            and end_time is not None
            and (end_time.date() - start_time.date()).days > 0
        ):
            # 多日 A股：逐日 catalog route 合并
            frames: list[pl.DataFrame] = []
            current = start_time.date()
            while current <= end_time.date():
                date_str = current.strftime("%Y%m%d")
                day_frames: list[pl.DataFrame] = []
                for sym in symbols:
                    code = symbol_to_code(sym)
                    ticks = self._engine.get_minutes(
                        code, date_str, asset_type=asset_type  # explicit
                    )
                    if ticks:
                        minute_df = minutes_rows_to_minute_df(
                            ticks, sym, asset_type, date_str,
                            source=self.name, freq="1m",
                        )
                        if not minute_df.is_empty():
                            day_frames.append(minute_df)
                if day_frames:
                    frames.append(pl.concat(day_frames, how="diagonal_relaxed"))
                current += timedelta(days=1)
            if not frames:
                return pl.DataFrame()
            df = pl.concat(frames, how="diagonal_relaxed")
            return _aggregate_minute_df(df, freq)

        # ETF/HK or single day: original DuckDB path
        ref_dt = end_time or start_time
        if ref_dt is None:
            return pl.DataFrame()
        date_str = ref_dt.strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        for sym in symbols:
            code = symbol_to_code(sym)
            ticks = self._engine.get_minutes(code, date_str, asset_type=asset_type)  # explicit
            if not ticks:
                logger.debug("tdx minutes %s %s: 无数据", code, date_str)
                continue
            minute_df = minutes_rows_to_minute_df(
                ticks, sym, asset_type, date_str,
                source=self.name, freq="1m",
            )
            if not minute_df.is_empty():
                frames.append(minute_df)

        df = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return _aggregate_minute_df(df, freq)

    # ------------------------------------------------------------------ #
    # get_realtime — fstore daily_markets DuckDB snapshot
    # ------------------------------------------------------------------ #
    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """从本地 DuckDB ``daily_markets`` 最新快照返回 realtime 形状。

        先取 ``daily_markets`` 全局最新 trade_date（单次 MAX），再按该日期 +
        asset_type（+ 可选 code IN）查询当前交易日全体行；不做历史全表窗口函数
        （QUALIFY/ROW_NUMBER/DISTINCT ON），避免扫历史取每 code 最新导致的 stale。
        所有查询走独立 ``_fstore_markets`` 客户端（独立连接/锁），不与财务/K线/管道
        查询共享 ``_fstore`` 的客户端锁。
        两条路径都经 ``_fstore_quote_to_row`` / ``normalize_realtime``，输出形状与
        source 标记一致（``fquant:fstore:daily_markets``）。
        """
        if universes and symbols:
            raise ValueError("FQuant realtime accepts either universes or symbols, not both")

        if symbols:
            target_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
            if not target_symbols:
                return pl.DataFrame()
            rows = self._get_fstore_realtime(list(dict.fromkeys(target_symbols)))
        elif universes:
            asset_types = self._realtime_universe_asset_types(universes)
            if not asset_types:
                return pl.DataFrame()
            rows = self._get_fstore_realtime_by_asset_types(asset_types)
        else:
            return pl.DataFrame()

        return normalize_realtime(rows, source=self.name) if rows else pl.DataFrame()

    def get_depth(self, symbols: list[str]) -> dict:
        """本地 DuckDB 当前无五档盘口。"""
        return {}

    def get_latest_market_supplements(self, symbols: list[str]) -> pl.DataFrame:
        """Latest fstore daily_markets fields that realtime sources may omit."""
        targets = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
        if len(targets) >= 500:
            target_set = set(targets)
            asset_types = sorted({
                asset_type
                for symbol in targets
                if (asset_type := self._asset_type_num_for_symbol(symbol)) is not None
            })
            rows = [
                row
                for row in self._get_fstore_realtime_by_asset_types(asset_types)
                if row.get("symbol") in target_set
            ]
        else:
            rows = self._get_fstore_realtime(targets)
        if not rows:
            return pl.DataFrame()
        out = []
        for row in rows:
            ext = row.get("ext") or {}
            out.append({
                "symbol": row.get("symbol"),
                "date": row.get("timestamp"),
                "change_pct": self._pct_points_to_ratio(ext.get("change_pct")),
                "change_amount": ext.get("change_amount"),
                "turnover_rate": ext.get("turnover_rate"),
                "amplitude": self._pct_points_to_ratio(ext.get("amplitude")),
            })
        return pl.DataFrame(out).drop_nulls(subset=["symbol"])

    # 港股资产类型编码（fstore base_infos/daily_markets 的 asset_type：1=A股 3=港股）
    HK_ASSET_TYPE = 3

    def get_hk_market_panel(self, start: date, end: date) -> pl.DataFrame:
        """港股全市场日行情横截面（[start, end] 闭区间），供盘后复盘按日聚合。

        源：fstore ``daily_markets``(asset_type=3) 左连 ``base_infos`` 取名称/板块。
        两张表在同一 DuckDB 连接内（markets 库已 ATTACH），可直接 join。

        ⚠️ 港股行**只有** price/change_percent/volume/amount 四个行情列有值；
        daily_markets 的 hslv(换手)/zgj(最高)/zdj(最低)/jrkpj(开盘)/zrspj(昨收)/
        资金流等列对港股**全是 NULL**（已实测）。所以复盘的港股分区只能做
        涨跌家数/成交额/涨跌分布/涨跌幅榜，做不了换手榜与冲高回落。

        change_percent 是百分数（-1.88 = -1.88%），此处**不做**单位换算，
        由调用方按各自口径处理；A 股 enriched 那条链路的 change_pct 是小数，别混。

        返回列：date/symbol/code/name/board/close/change_pct/volume/amount
        provider 不可用或无数据时返回空 DataFrame。
        """
        rows = self._fstore.query(
            """
            SELECT dm.trade_date AS date,
                   dm.code       AS code,
                   bi.name       AS name,
                   bi.bk         AS board,
                   dm.price      AS close,
                   dm.change_percent AS change_pct,
                   dm.volume     AS volume,
                   dm.amount     AS amount
            FROM daily_markets dm
            LEFT JOIN base_infos bi
                   ON bi.code = dm.code AND bi.asset_type = ?
            WHERE dm.asset_type = ?
              AND dm.trade_date >= ? AND dm.trade_date <= ?
            """,
            [self.HK_ASSET_TYPE, self.HK_ASSET_TYPE, start, end],
        )
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        # code(00700) → 对外符号(00700.HK)，与本项目 .HK 后缀约定一致
        return df.with_columns(
            (pl.col("code").cast(pl.Utf8) + pl.lit(".HK")).alias("symbol")
        )

    def get_stock_reference_flags(self) -> pl.DataFrame:
        """A 股标的参考标记 — AH 股 / AH 溢价率 / 沪深股通标的 / 上市日期。

        源：fstore ``base_infos``(asset_type=1, hsgt/ssdate/symbol) 左连
        ``ah_stock_compares`` 最新交易日的 acode → 溢价率。条件选股等
        业务层经 registry 取本方法（provider 特有, 不在 base 契约）。

        返回列：symbol / is_ah(bool) / ah_premium(float, %) /
        hk_connect(bool, hsgt>0) / listing_date(date | null)。
        缓存 24h TTL；fstore 不可用或查询失败 fail-soft 返回空 df。
        """
        if (
            self._reference_flags_cache is not None
            and self._reference_flags_cache_ts is not None
            and (datetime.now() - self._reference_flags_cache_ts).total_seconds()
            <= self._instruments_cache_ttl
        ):
            return self._reference_flags_cache

        try:
            rows = self._fstore.query(
                """
                WITH ah AS (
                    SELECT acode, premium_rate FROM (
                        SELECT acode, premium_rate,
                               ROW_NUMBER() OVER (PARTITION BY acode ORDER BY trade_date DESC) AS rn
                        FROM ah_stock_compares
                    ) ranked WHERE rn = 1
                )
                SELECT b.symbol, b.code,
                       (ah.acode IS NOT NULL) AS is_ah,
                       ah.premium_rate AS ah_premium,
                       (b.hsgt IS NOT NULL AND b.hsgt > 0) AS hk_connect,
                       b.ssdate AS listing_date
                FROM base_infos b
                LEFT JOIN ah ON ah.acode = b.code
                WHERE b.asset_type = 1 AND b.symbol IS NOT NULL AND b.symbol != ''
                """
            )
        except Exception:  # noqa: BLE001
            logger.warning("fstore 标的参考标记查询失败", exc_info=True)
            return pl.DataFrame()
        if not rows:
            return pl.DataFrame()

        df = pl.DataFrame(
            rows,
            schema_overrides={
                "symbol": pl.Utf8,
                "is_ah": pl.Boolean,
                "ah_premium": pl.Float64,
                "hk_connect": pl.Boolean,
                "listing_date": pl.Date,
            },
        )
        # base_infos.symbol 是 fstore 小写前缀格式(sh603501) → 规范化为对外符号(603501.SH)
        from app.data_providers.fquant.symbols import code_to_symbol

        df = df.with_columns(
            pl.col("code").cast(pl.Utf8).map_elements(
                lambda c: code_to_symbol(str(c), 1), return_dtype=pl.Utf8
            ).alias("symbol")
        ).drop("code")
        # 同一 code 多行时保留 updated_at 最新一条已在 SQL 侧由 ah rn=1 保证;
        # base_infos 理论唯一, 防御性去重保留首个非空溢价行。
        df = df.unique(subset=["symbol"], keep="first")
        self._reference_flags_cache = df
        self._reference_flags_cache_ts = datetime.now()
        return df

    def get_lhb_records(self, start: date, end: date) -> pl.DataFrame:
        """龙虎榜上榜记录 — ``[start, end]`` 区间内 (symbol, trade_date) 去重对。

        源：fstore ``longhb_detail``(2013 年起, 每标的每日一行多榜单原因,
        DISTINCT 按日去重)。业务层(条件选股等)经 registry 取本方法
        (provider 特有, 不在 base 契约), 按 as_of 窗口自行聚合。
        查询失败 fail-soft 返回空 df。
        """
        try:
            rows = self._fstore.query(
                """
                SELECT DISTINCT code, CAST(t_date AS DATE) AS trade_date
                FROM longhb_detail
                WHERE t_date IS NOT NULL
                  AND CAST(t_date AS DATE) BETWEEN ? AND ?
                """,
                [start.isoformat(), end.isoformat()],
            )
        except Exception:  # noqa: BLE001
            logger.warning("fstore 龙虎榜上榜记录查询失败", exc_info=True)
            return pl.DataFrame()
        if not rows:
            return pl.DataFrame()

        from app.data_providers.fquant.symbols import code_to_symbol

        df = pl.DataFrame(rows, schema_overrides={"code": pl.Utf8, "trade_date": pl.Date})
        return df.with_columns(
            pl.col("code").map_elements(
                lambda c: code_to_symbol(str(c), 1), return_dtype=pl.Utf8
            ).alias("symbol")
        ).drop("code")

    def get_lhb_institution_records(self, start: date, end: date) -> pl.DataFrame:
        """龙虎榜机构席位日记录，按标的/日期汇总净买入额。

        该扩展方法仅供按 ``as_of`` 聚合的业务入口调用，不属于通用
        ``MarketDataProvider`` 契约。fstore 不可读时 fail-soft 返回空帧。
        """
        try:
            rows = self._fstore.query(
                """
                SELECT code, CAST(t_date AS DATE) AS trade_date,
                       SUM(net_buy_amount) AS net_buy_amount
                FROM longhb_jigou
                WHERE t_date IS NOT NULL
                  AND CAST(t_date AS DATE) BETWEEN ? AND ?
                GROUP BY code, CAST(t_date AS DATE)
                """,
                [start.isoformat(), end.isoformat()],
            )
        except Exception:  # noqa: BLE001
            logger.warning("fstore 龙虎榜机构席位记录查询失败", exc_info=True)
            return pl.DataFrame()
        if not rows:
            return pl.DataFrame()

        df = pl.DataFrame(
            rows,
            schema_overrides={
                "code": pl.Utf8,
                "trade_date": pl.Date,
                "net_buy_amount": pl.Float64,
            },
        )
        return df.with_columns(
            pl.col("code").map_elements(
                lambda c: code_to_symbol(str(c), 1), return_dtype=pl.Utf8
            ).alias("symbol")
        ).drop("code")

    def get_margin_records(self, start: date, end: date) -> pl.DataFrame:
        """融资余额及融资净买入记录（金额口径：万元）。

        ``buy_balance`` 是融资余额，``buy_net_amount`` 是当日融资买入额减
        偿还额。业务层负责在 ``as_of`` 水位下选取最近交易日并计算窗口值。
        """
        try:
            rows = self._fstore.query(
                """
                SELECT code, CAST(t_date AS DATE) AS trade_date,
                       buy_balance AS financing_balance,
                       buy_net_amount AS financing_net_buy
                FROM stock_rzrj
                WHERE t_date IS NOT NULL
                  AND CAST(t_date AS DATE) BETWEEN ? AND ?
                """,
                [start.isoformat(), end.isoformat()],
            )
        except Exception:  # noqa: BLE001
            logger.warning("fstore 融资融券记录查询失败", exc_info=True)
            return pl.DataFrame()
        if not rows:
            return pl.DataFrame()

        df = pl.DataFrame(
            rows,
            schema_overrides={
                "code": pl.Utf8,
                "trade_date": pl.Date,
                "financing_balance": pl.Float64,
                "financing_net_buy": pl.Float64,
            },
        )
        return df.with_columns(
            pl.col("code").map_elements(
                lambda c: code_to_symbol(str(c), 1), return_dtype=pl.Utf8
            ).alias("symbol")
        ).drop("code")

    @staticmethod
    def _pct_points_to_ratio(value) -> float | None:
        number = FQuantProvider._float_or_none(value)
        return number / 100 if number is not None else None

    @staticmethod
    def _realtime_universe_asset_types(universes: list[str]) -> list[int]:
        """universe 前缀 → fstore daily_markets asset_type 数字（去重保序）。

        - ``CN_EQUITY*`` → 1（A 股）
        - ``CN_ETF*``    → 20
        - ``CN_INDEX*``  → 10
        """
        upper = {u.upper() for u in universes}
        nums: list[int] = []
        if any(u.startswith("CN_EQUITY") for u in upper):
            nums.append(1)
        if any(u.startswith("CN_ETF") for u in upper):
            nums.append(20)
        if any(u.startswith("CN_INDEX") for u in upper):
            nums.append(10)
        return nums

    def _fstore_latest_trade_date(self) -> str | None:
        """daily_markets 全局最新 trade_date（单次 MAX，替代历史全表窗口函数）。

        所有 CN 资产类型共用同一交易日历，全局 MAX(trade_date) 即当前交易日；
        realtime 视图只关心当前交易日，停牌/退市等旧标的不应混入。
        """
        rows = self._fstore_markets.query(
            "SELECT MAX(trade_date) AS latest FROM daily_markets"
        )
        if not rows:
            return None
        value = rows[0].get("latest")
        return str(value) if value is not None else None

    def _get_fstore_realtime_by_asset_types(self, asset_types: list[int]) -> list[dict]:
        """全 universe 快路径：先取全局最新 trade_date，再按 asset_type 查该日全表。

        不构造 code IN 列表，不做历史全表窗口函数（QUALIFY/ROW_NUMBER）；
        只查当前交易日的全体行。输出经 ``_fstore_quote_to_row``，与精确 symbols
        路径形状一致。
        """
        latest = self._fstore_latest_trade_date()
        if latest is None:
            return []
        out: list[dict] = []
        for asset_type in asset_types:
            rows = self._query_fstore_realtime_universe_rows(asset_type, latest)
            out.extend(self._fstore_quote_to_row(r, asset_type) for r in rows)
        return [r for r in out if r]

    def _query_fstore_realtime_universe_rows(
        self, asset_type: int, latest_trade_date: str
    ) -> list[dict]:
        """按 asset_type 取 daily_markets 指定 trade_date 的全体行（无 IN 列表）。

        SQL 仅参数化 asset_type + trade_date；字段抽取与
        ``_query_fstore_realtime_rows`` 共用同一投影，保证两条路径输出形状相同。
        """
        sql = f"""
            SELECT {_DAILY_MARKETS_REALTIME_COLS}
            FROM daily_markets
            WHERE asset_type = %s AND trade_date = %s
        """
        return self._fstore_markets.query(sql, (asset_type, latest_trade_date))

    def _get_fstore_realtime(self, symbols: list[str]) -> list[dict]:
        latest = self._fstore_latest_trade_date()
        if latest is None:
            return []
        grouped: dict[int, list[str]] = {}
        for symbol in symbols:
            asset_type = self._asset_type_num_for_symbol(symbol)
            if asset_type is None:
                continue
            grouped.setdefault(asset_type, []).append(symbol_to_code(symbol))

        out: list[dict] = []
        for asset_type, codes in grouped.items():
            rows = self._query_fstore_realtime_rows(asset_type, codes, latest)
            out.extend(self._fstore_quote_to_row(r, asset_type) for r in rows)
        return [r for r in out if r]

    def _query_fstore_realtime_rows(
        self, asset_type: int, codes: list[str], latest_trade_date: str
    ) -> list[dict]:
        """从 daily_markets 指定 trade_date 查询，其余字段打包在 payload_json
        （驼峰 key）里，用 ``->>`` 抽取后转型别名。
        """
        placeholders = ",".join(["%s"] * len(codes))
        sql = f"""
            SELECT {_DAILY_MARKETS_REALTIME_COLS}
            FROM daily_markets
            WHERE asset_type = %s AND trade_date = %s AND code IN ({placeholders})
        """
        return self._fstore_markets.query(sql, (asset_type, latest_trade_date, *codes))

    @staticmethod
    def _asset_type_num_for_symbol(symbol: str) -> int | None:
        _, suffix = split_symbol(symbol)
        if is_etf_symbol(symbol):
            return 20
        if suffix == "HK":
            return 3
        if suffix == "INDEX":
            return 10
        if suffix in {"SH", "SZ", "BJ"}:
            code = symbol_to_code(symbol)
            # SH exchange: 000xxx 是指数（上证指数/上证50/沪深300/科创综指等）
            if suffix == "SH" and code.startswith("000"):
                return 10
            # SZ exchange: 399xxx 是指数（深证成指/创业板指等）
            if suffix == "SZ" and code.startswith("399"):
                return 10
            return 1
        return None

    def _fstore_quote_to_row(self, item: dict, asset_type: int) -> dict | None:
        symbol = code_to_symbol(str(item.get("code") or ""), asset_type)
        last_price = self._float_or_none(item.get("price"))
        if not symbol or last_price is None:
            return None
        return self._quote_row(
            symbol=symbol,
            name=item.get("name"),
            source=f"{self.name}:fstore:daily_markets",
            last_price=last_price,
            prev_close=self._float_or_none(item.get("zrspj")),
            open_=self._float_or_none(item.get("jrkpj")),
            high=self._float_or_none(item.get("zgj")),
            low=self._float_or_none(item.get("zdj")),
            volume=self._hands_to_shares(item.get("cjl")),
            amount=self._float_or_none(item.get("cje")),
            timestamp=str(item.get("tdate") or ""),
            change_pct=self._float_or_none(item.get("zdfd")),
            change_amount=self._float_or_none(item.get("zded")),
            amplitude=self._float_or_none(item.get("zhfu")),
            turnover_rate=self._float_or_none(item.get("hslv")),
        )

    def _quote_row(
        self,
        *,
        symbol: str,
        name: str | None,
        source: str,
        last_price: float,
        prev_close: float | None,
        open_: float | None,
        high: float | None,
        low: float | None,
        volume: float | None,
        amount: float | None,
        timestamp: str,
        change_pct: float | None = None,
        change_amount: float | None = None,
        amplitude: float | None = None,
        turnover_rate: float | None = None,
    ) -> dict:
        if change_amount is None and prev_close not in (None, 0):
            change_amount = last_price - float(prev_close)
        if change_pct is None and prev_close not in (None, 0) and change_amount is not None:
            change_pct = change_amount / float(prev_close) * 100
        return {
            "symbol": symbol,
            "name": name,
            "last_price": last_price,
            "prev_close": prev_close,
            "open": open_,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "timestamp": timestamp,
            "source": source,
            "ext": {
                "name": name,
                "source": source,
                "change_pct": change_pct,
                "change_amount": change_amount,
                "amplitude": amplitude,
                "turnover_rate": turnover_rate,
            },
        }

    @staticmethod
    def _hands_to_shares(value) -> float | None:
        number = FQuantProvider._float_or_none(value)
        return number * 100 if number is not None else None

    @staticmethod
    def _float_or_none(value) -> float | None:
        try:
            if value is None:
                return None
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number

    # ------------------------------------------------------------------ #
    # get_by_universes — 阶段 3 #3.2 指数/板块/行业 universes
    # ------------------------------------------------------------------ #
    def get_by_universes(
        self,
        universes: list[str],
        asset_type: AssetType = "index",
    ) -> pl.DataFrame:
        """按 universe 名取标的清单（阶段 3 #3.2）。

        数据流：fstore ``chengfen_gu``（主源）+ ``base_infos``（指数/ETF 兜底）
        输出列（INSTRUMENT_COLS）：symbol / name / code / exchange / asset_type / source

        支持的 universe（按 name 匹配，§5.5b）：
        - ``"CN_Index"`` / ``"CN_Index_*"`` → ``chengfen_gu`` 中 6 位数字 code
          （覆盖中证/同花顺等行业指数；同时在 ``base_infos.asset_type=10``
          中也保留记录以补全标准交易所指数）
        - ``"CN_ETF"`` / ``"CN_ETF_*"`` → ``base_infos.asset_type=20``（ETF 清单）
        - ``"CN_Sector"`` / ``"CN_Sector_*"`` → ``chengfen_gu`` 中 BK/801 开头
          的板块/行业（asset_type=15/37/38/39/40/41/42 整体返回）

        其它 universe 名：返回空 df（§7.1 优雅降级）。

        降级：DB 不可达 → 返回空 df，warning（§7.1）。
        """
        if not universes:
            return pl.DataFrame()

        frames: list[pl.DataFrame] = []

        # 1) 分类 universe → 数据源
        want_index = any(u.upper().startswith("CN_INDEX") for u in universes)
        want_etf = any(u.upper().startswith("CN_ETF") for u in universes)
        want_sector = any(u.upper().startswith("CN_SECTOR") for u in universes)

        # 2) chengfen_gu 主源（取最新 t_date 的快照，去重 code）
        if want_index or want_sector:
            sector_label = "sector" if want_sector else asset_type
            frames.append(
                self._get_universe_codes_from_chengfen_gu(
                    want_index=want_index,
                    want_sector=want_sector,
                    sector_label=sector_label,
                )
            )

        # 3) base_infos 兜底：标准交易所指数 / ETF
        if want_index:
            try:
                df = self.get_instruments("index")
                if not df.is_empty():
                    frames.append(df)
            except Exception as e:  # noqa: BLE001
                logger.debug("get_by_universes: base_infos index 兜底失败: %s", e)
        if want_etf:
            try:
                df = self.get_instruments("etf")
                if not df.is_empty():
                    frames.append(df)
            except Exception as e:  # noqa: BLE001
                logger.debug("get_by_universes: base_infos etf 兜底失败: %s", e)

        if not frames:
            return pl.DataFrame()

        from app.data_providers.normalizer import INSTRUMENT_COLS
        try:
            merged = pl.concat(frames, how="diagonal_relaxed")
        except Exception as e:  # noqa: BLE001
            logger.warning("get_by_universes: concat 失败: %s", e)
            return pl.DataFrame()

        # 去重 + 排序（symbol 唯一；asset_type 保留首个）
        if "symbol" in merged.columns:
            merged = merged.unique(subset=["symbol"], keep="first").sort("symbol")

        # 只保留 INSTRUMENT_COLS
        keep = [c for c in INSTRUMENT_COLS if c in merged.columns]
        return merged.select(keep) if keep else pl.DataFrame()

    def _get_universe_codes_from_chengfen_gu(
        self,
        *,
        want_index: bool,
        want_sector: bool,
        sector_label: str = "sector",
    ) -> pl.DataFrame:
        """fstore ``chengfen_gu`` → 指数/板块 instrument df。

        实现：取每 code 最新 t_date 的 cfg 非空行（说明有有效成分股数据），
        拼接 base 记录。SQL 思路：取每 code 的 ``max(t_date)`` 子查询，再
        过滤 ``cfg`` 非空（``jsonb_array_length(cfg::jsonb) > 0``）。

        asset_type 数字 → 内部 asset_type：
        - 6 位数字 code → ``"index"``
        - BK/801/...  → ``sector_label``（默认 ``"sector"``）
        """
        if not (want_index or want_sector):
            return pl.DataFrame()

        # fstore DuckDB: 取每 code 最新非空 cfg 行
        # 使用 DISTINCT ON (code) 拿 max(t_date) 的行
        sql = """
            SELECT DISTINCT ON (code)
                code, name, t_date, cfg, asset_type
            FROM chengfen_gu
            WHERE cfg IS NOT NULL
              AND cfg::text != '[]'
              AND cfg::text != 'null'
            ORDER BY code, t_date DESC
        """
        rows = self._fstore.query(sql, None)
        if not rows:
            logger.debug("FStoreDB chengfen_gu: 无 universe 数据")
            return pl.DataFrame()

        # 客户端按 code 前缀过滤（6 位数字 → index；其他 → sector）
        out: list[dict] = []
        for r in rows:
            code = str(r.get("code", ""))
            if not code:
                continue
            is_index_like = code.isdigit() and len(code) == 6
            if is_index_like and not want_index:
                continue
            if (not is_index_like) and not want_sector:
                continue
            out.append({
                "code": code,
                "name": r.get("name") or code,
                "asset_type": sector_label,  # chengfen_gu_rows_to_universes 会重新归一
            })
        if not out:
            return pl.DataFrame()
        return pl.DataFrame(
            chengfen_gu_rows_to_universes(out, asset_type=sector_label, source=self.name)
        )

    # ================================================================== #
    # 扩展方法（§3.4，不在 base.py 契约内，仅 FQuantProvider 实例可用）
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # get_financial — §4.8 fstore financial_report_*
    # ------------------------------------------------------------------ #
    def get_financial(self, symbol: str, table: str) -> pl.DataFrame:
        """拉取财务报表（§4.8 / §5.6 扩展方法）。

        :param symbol: 带后缀符号（如 ``"600519.SH"``）或纯 code
        :param table: 表名简称 ∈ {income, balance_sheet, cash_flow, annual, quick, forecast}
        :return: 归一 df，列含 ``symbol/t_date/...<source cols>.../notice_date``
        降级（§7.1）：DB 不可达 → 空 df，warning
        """
        if symbol.upper().endswith(".HK"):
            # fstore financial snapshots contain no HK symbols; fail closed
            # rather than allowing a same-code row from another asset class.
            return pl.DataFrame()
        fstore_table = _FINANCIAL_TABLE_MAP.get(table)
        if not fstore_table:
            logger.warning("get_financial: 不支持的 table=%s（支持: %s）",
                           table, list(_FINANCIAL_TABLE_MAP))
            return pl.DataFrame()

        code = symbol_to_code(symbol) if "." in symbol else symbol
        sql = f"SELECT * FROM {fstore_table} WHERE code = %s ORDER BY t_date DESC LIMIT 50"
        rows = self._fstore.query(sql, (code,))
        if not rows:
            logger.debug("FStoreDB %s %s: 无数据", fstore_table, code)
            return pl.DataFrame()

        return financial_rows_to_df(rows, symbol=symbol, table=table, source_tag=f"{self.name}:fstore")

    # ------------------------------------------------------------------ #
    # get_moneyflow_daily — §4.9 DuckDB market_fund_flow
    # ------------------------------------------------------------------ #
    def get_moneyflow_daily(
        self, symbols: list[str], date: datetime | None = None,
    ) -> pl.DataFrame:
        """日级资金流（§4.9 / §5.7 扩展方法）。

        :param symbols: 带后缀符号列表
        :param date: 查询日期；None 取当天
        :return: df 列含 symbol/date/source/main_net/total_net/...
        """
        if not symbols:
            return pl.DataFrame()

        date_iso = (date or datetime.now()).strftime("%Y-%m-%d")
        codes = [symbol_to_code(s) for s in symbols]
        code_to_sym = {symbol_to_code(s): s for s in symbols}

        disk_data: dict[str, dict] = {}
        for code in codes:
            total = self._engine.get_fund_daily(code, date_iso)
            if total:
                disk_data[code] = total

        if not disk_data:
            return pl.DataFrame()

        return moneyflow_daily_to_df(disk_data, code_to_sym, date_iso, source=self.name)

    def get_moneyflow_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pl.DataFrame:
        """Return the existing daily money-flow range contract used by K-line APIs."""
        return self._engine.get_fund_range(
            symbol_to_code(symbol),
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )

    def get_moneyflow_stock(self, symbol: str, start: datetime, end: datetime,
                            freq: str = "daily") -> pl.DataFrame:
        """查询已发布快照中的个股日/分钟资金流。"""
        return self._engine.get_moneyflow_stock(
            symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), freq=freq,
        )

    def get_moneyflow_blocks(self, trade_date: datetime, freq: str = "daily",
                             block_type: str | None = None, limit: int = 100) -> pl.DataFrame:
        """查询已发布快照中的板块单日资金流排名。"""
        return self._engine.get_moneyflow_blocks(
            trade_date.strftime("%Y-%m-%d"), freq=freq, block_type=block_type, limit=limit,
        )

    def get_moneyflow_status(self) -> dict[str, dict]:
        """四项资金流快照能力事实（独立于聚合 status，避免并发覆盖）。"""
        return self._engine.get_moneyflow_status()

    def get_moneyflow_snapshot(self, trade_date: date) -> pl.DataFrame:
        """读取一个交易日的全市场日级资金流横截面。

        仅使用 ``tdx-moneyflow`` 已发布 generation；返回空帧代表快照不可用或
        该交易日无数据，调用方不得以旧 raw 数据回退。
        """
        rows = self._engine.get_moneyflow_daily_snapshot(trade_date.isoformat())
        if not rows:
            return pl.DataFrame()

        out: list[dict] = []
        for row in rows:
            symbol = self._tdx_a_share_symbol(row.get("code"))
            if symbol is None:
                continue
            out.append({
                "symbol": symbol,
                "trade_date": row.get("trade_date"),
                "moneyflow_total_amount": self._float_or_none(row.get("total_amount")),
                "main_net_inflow": self._float_or_none(row.get("main_traditional_net")),
                "super_large_net_inflow": self._float_or_none(row.get("super_large_net")),
                "valid_count": row.get("valid_count"),
                "invalid_count": row.get("invalid_count"),
            })
        return pl.DataFrame(out) if out else pl.DataFrame()


    # ------------------------------------------------------------------ #
    # get_chip_distribution — 筹码分布（stock_chip_peaks，strict snapshot）
    # ------------------------------------------------------------------ #
    _A_SHARE_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
    @staticmethod
    def _tdx_a_share_symbol(raw_code: object) -> str | None:
        """Convert a prefixed TDX A-share code to the public symbol contract."""
        code = str(raw_code or "").strip()
        if re.fullmatch(r"(?:sh|sz|bj)\d{6}", code, flags=re.IGNORECASE):
            code = code[2:]
        if not re.fullmatch(r"\d{6}", code):
            return None
        return code_to_symbol(code, 1)


    def get_chip_distribution(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> pl.DataFrame:
        """查询已发布快照中的个股筹码分布（stock_chip_peaks）。

        :param symbol: canonical A 股符号（``\\d{6}.(SH|SZ|BJ)``），仅接受单个 A 股
        :param start: 起始日期（含）；None 不限下界
        :param end: 结束日期（含）；None 不限上界
        :param limit: 最大返回行数（默认 500）
        :return: polars DataFrame，列含 ``symbol / trade_date / peak_price /
                 peak_volume / peak_ratio / profit_ratio / avg_cost /
                 concentration_90 / range_90_low / range_90_high /
                 concentration_70 / range_70_low / range_70_high /
                 cr10 / cr30 / gini /
                 main_peak_price / main_peak_volume / main_peak_ratio /
                 main_concentration /
                 retail_peak_price / retail_peak_volume / retail_peak_ratio /
                 retail_concentration / has_retail_peak /
                 peak_count / window_days / price_step / asset_type /
                 source``

        不直接透传 ``result_json``——该列是上游筹码引擎的原始 JSON，体积大且
        语义已被结构化字段覆盖，暴露它只会让 API 响应臃肿且引入版本耦合。

        数据源 ``tdx_chip`` 走 strict snapshot：已发布 generation 不可达时返回空
        DataFrame（``available`` 可由 :meth:`get_chip_status` 查询），绝不回退 raw。
        """
        if not self._A_SHARE_SYMBOL_RE.match(symbol or ""):
            return pl.DataFrame()
        if limit < 1:
            return pl.DataFrame()

        code = symbol_to_code(symbol)
        from app.data_providers.fquant.tdx_duckdb_client import _prefixed_code

        prefixed = _prefixed_code(code)
        rows = self._engine.get_chip(
            prefixed,
            start.strftime("%Y-%m-%d") if start else None,
            end.strftime("%Y-%m-%d") if end else None,
            limit=limit,
        )
        if not rows:
            return pl.DataFrame()

        source_tag = f"{self.name}:tdx_chip"
        out: list[dict] = []
        for r in rows:
            td = r.get("trade_date")
            td_str = td.isoformat() if hasattr(td, "isoformat") else str(td) if td else None
            out.append({
                "symbol": symbol,
                "trade_date": td_str,
                "peak_price": r.get("peak_price"),
                "peak_volume": r.get("peak_volume"),
                "peak_ratio": r.get("peak_ratio"),
                "profit_ratio": r.get("profit_ratio"),
                "avg_cost": r.get("avg_cost"),
                "concentration_90": r.get("concentration_90"),
                "range_90_low": r.get("range_90_low"),
                "range_90_high": r.get("range_90_high"),
                "concentration_70": r.get("concentration_70"),
                "range_70_low": r.get("range_70_low"),
                "range_70_high": r.get("range_70_high"),
                "cr10": r.get("cr10"),
                "cr30": r.get("cr30"),
                "gini": r.get("gini"),
                "main_peak_price": r.get("main_peak_price"),
                "main_peak_volume": r.get("main_peak_volume"),
                "main_peak_ratio": r.get("main_peak_ratio"),
                "main_concentration": r.get("main_concentration"),
                "retail_peak_price": r.get("retail_peak_price"),
                "retail_peak_volume": r.get("retail_peak_volume"),
                "retail_peak_ratio": r.get("retail_peak_ratio"),
                "retail_concentration": r.get("retail_concentration"),
                "has_retail_peak": r.get("has_retail_peak"),
                "peak_count": r.get("peak_count"),
                "window_days": r.get("window_days"),
                "price_step": r.get("price_step"),
                "asset_type": r.get("asset_type"),
                "source": source_tag,
            })
        return pl.DataFrame(out)

    def get_chip_snapshot(self, trade_date: date) -> pl.DataFrame:
        """读取一个交易日的全市场筹码统计横截面。

        ``profit_ratio`` 的上游比例口径在这里归一为百分点，避免消费方各自
        乘以 100。数据只来自 ``tdx-chip`` 已发布 generation。
        """
        rows = self._engine.get_chip_snapshot(trade_date.isoformat())
        if not rows:
            return pl.DataFrame()

        out: list[dict] = []
        for row in rows:
            symbol = self._tdx_a_share_symbol(row.get("code"))
            if symbol is None:
                continue
            profit_ratio = self._float_or_none(row.get("profit_ratio"))
            out.append({
                "symbol": symbol,
                "trade_date": row.get("trade_date"),
                "chip_profit_ratio": profit_ratio * 100 if profit_ratio is not None else None,
                "chip_avg_cost": self._float_or_none(row.get("avg_cost")),
                "chip_concentration_90": self._float_or_none(row.get("concentration_90")),
                "chip_peak_count": row.get("peak_count"),
                "chip_main_peak_price": self._float_or_none(row.get("main_peak_price")),
            })
        return pl.DataFrame(out) if out else pl.DataFrame()

    def get_chip_status(self) -> dict[str, dict]:
        """筹码快照能力事实（独立 key，供聚合 status 用 dict.update 合并）。

        返回 ``{"chip": {available, source, earliest_date, latest_date, rows,
        symbols, reason}}``。不可达与空表用 reason 区分。
        """
        fact = self._engine.get_chip_coverage()
        return {"chip": fact}

    def get_moneyflow_minute(
        self, symbols: list[str], date: datetime | None = None,
    ) -> pl.DataFrame:
        """分钟级资金流，兼容旧批量入口并切换到已发布快照。"""
        if not symbols:
            return pl.DataFrame()
        day = date or datetime.now()
        frames = [
            self.get_moneyflow_stock(sym, day, day, freq="minute")
            for sym in symbols
        ]
        frames = [frame for frame in frames if not frame.is_empty()]
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


    def get_transactions(self, symbol: str, date: datetime, limit: int = 5000) -> pl.DataFrame:
        code = symbol_to_code(symbol)
        date_str = date.strftime("%Y%m%d")
        _, suffix = split_symbol(symbol)
        rows = self._engine.get_trans(code, date_str, limit=limit, asset_type="hk" if suffix == "HK" else None)
        return trans_rows_to_df(rows, symbol, date_str, source=self.name) if rows else pl.DataFrame()

    def get_call_auction(self, symbol: str, trade_date: datetime, session: str | None = None, limit: int = 5000) -> pl.DataFrame:
        rows = self._engine.get_call_auction(symbol_to_code(symbol), trade_date.strftime("%Y%m%d"), session=session, limit=limit)
        return pl.DataFrame([{**row, "symbol": symbol} for row in rows]) if rows else pl.DataFrame()

    def get_microstructure_status(self) -> dict[str, dict]:
        return self._engine.get_microstructure_status()

    def get_market_data_status(self) -> dict[str, dict]:
        """Expose explicit unsupported HK boundaries alongside local capabilities."""
        return {
            "hk_adjustment": {
                "available": False,
                "source": "engine-hk",
                "earliest_date": None,
                "latest_date": None,
                "rows": 0,
                "symbols": 0,
                "reason": (
                    "published engine-hk snapshot has no HK corporate-action "
                    "or adjustment dataset"
                ),
            },
            "hk_financial": {
                "available": False,
                "source": "fstore-extended",
                "earliest_date": None,
                "latest_date": None,
                "rows": 0,
                "symbols": 0,
                "reason": "published fstore financial snapshots contain no HK symbols",
            },
        }

    # ------------------------------------------------------------------ #
    # get_corp_action — §3.4 扩展方法 fstore chuquan_chuxi + engine xdxr
    # ------------------------------------------------------------------ #
    def get_corp_action(
        self, symbol: str,
        start: datetime | None = None, end: datetime | None = None,
    ) -> pl.DataFrame:
        """公司行动一览（§3.4 扩展方法 / §5.3）。

        主源 fstore ``chuquan_chuxi``，备份 engine-data ``xdxr``。
        与 ``get_adj_factors`` 互补（本方法输出原始事件，非累积 ex_factor）。

        :param symbol: 带后缀符号
        :param start: 起始日期
        :param end: 结束日期
        :return: df 列含 symbol/trade_date/category/fenhong/fenshu/peigu/...
        降级（§7.1）：两源均不可达 → 空 df
        """
        if symbol.upper().endswith(".HK"):
            return pl.DataFrame()
        code = symbol_to_code(symbol)

        # 主源 fstore chuquan_chuxi
        events = self._get_adj_events_from_fstore(symbol, code, start, end)
        if not events:
            # 备份 engine-data xdxr
            events = self._get_adj_events_from_engine(symbol, code)
        if not events:
            return pl.DataFrame()

        return pl.DataFrame(events) if events else pl.DataFrame()

    # ------------------------------------------------------------------ #
    # get_universe_constituents — 阶段 3 #3.2 扩展方法
    # ------------------------------------------------------------------ #
    def get_universe_constituents(
        self,
        index_code: str,
        as_of_date: datetime | None = None,
    ) -> pl.DataFrame:
        """拉取指数/板块/行业的成分股清单（阶段 3 #3.2 扩展方法）。

        数据源：fstore ``chengfen_gu_items``（明细表，权重/入选日期齐全）。
        ``chengfen_gu.cfg`` JSON 内嵌的成分股是同一份数据的另一份冗余存储；
        优先用 ``chengfen_gu_items``（结构化字段 + 索引覆盖更全）。

        :param index_code: 指数/板块 code（6 位数字 / BK / 801 等）
        :param as_of_date: 截止日期；None 取最新 t_date
        :return: df 列含 ``index_code / index_name / stock_code / stock_name /
                              weight / join_date / t_date / asset_type / symbol``
        降级（§7.1）：DB 不可达 → 空 df，warning。
        """
        if not index_code:
            return pl.DataFrame()

        # as_of_date 过滤：取 <= as_of_date 的最新 t_date
        params: list = [str(index_code)]
        date_filter = ""
        if as_of_date is not None:
            date_filter = " AND t_date <= %s"
            params.append(as_of_date.date())

        sql = f"""
            SELECT DISTINCT ON (stock_code, t_date)
                index_code, index_name, stock_code, stock_name,
                weight, join_date, t_date, asset_type
            FROM chengfen_gu_items
            WHERE index_code = %s {date_filter}
            ORDER BY stock_code, t_date DESC
        """
        rows = self._fstore.query(sql, tuple(params))
        if not rows:
            logger.debug("FStoreDB chengfen_gu_items %s: 无成分数据", index_code)
            return pl.DataFrame()

        # 客户端构造 symbol（沪深 A 股按 code 前缀归一）
        out: list[dict] = []
        for r in rows:
            stock_code = str(r.get("stock_code", ""))
            if not stock_code:
                continue
            symbol = code_to_symbol(stock_code, 1)  # 成分股总是 A 股
            weight = r.get("weight")
            try:
                weight = float(weight) if weight is not None else None
            except (TypeError, ValueError):
                weight = None
            out.append({
                "index_code": str(r.get("index_code", "")),
                "index_name": r.get("index_name", ""),
                "stock_code": stock_code,
                "stock_name": r.get("stock_name", ""),
                "weight": weight,
                "join_date": str(r.get("join_date", "")) if r.get("join_date") else None,
                "t_date": str(r.get("t_date", "")) if r.get("t_date") else None,
                "asset_type": "stock",
                "symbol": symbol,
                "source": f"{self.name}:fstore:chengfen_gu_items",
            })
        return pl.DataFrame(out) if out else pl.DataFrame()

    # ================================================================== #
    # 内部辅助
    # ================================================================== #
    def _filter_by_date_range(
        self, df: pl.DataFrame, date_col: str,
        start: datetime | None, end: datetime | None,
    ) -> pl.DataFrame:
        """按日期范围过滤 DataFrame（用于 adj_factor 截断）。"""
        if df.is_empty() or date_col not in df.columns:
            return df
        try:
            # 确保日期列为 Date 类型
            if df.schema[date_col] != pl.Date:
                df = df.with_columns(pl.col(date_col).cast(pl.Date, strict=False))
            if start:
                df = df.filter(pl.col(date_col) >= start.date())
            if end:
                df = df.filter(pl.col(date_col) <= end.date())
        except Exception as e:  # noqa: BLE001
            logger.debug("_filter_by_date_range 失败: %s", e)
        return df
