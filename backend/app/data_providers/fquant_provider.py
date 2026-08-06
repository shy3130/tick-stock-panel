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
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.fquant.adj_factor import (
    build_ex_factor_df,
    compute_ex_factor_from_xdxr,
)
from app.data_providers.fquant.tdx_duckdb_client import TdxDuckDBClient
from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient
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
    )

    def __init__(self, name: str = "fquant") -> None:
        self.name = name
        self._fstore = FStoreDuckDBClient()
        self._engine = TdxDuckDBClient()
        # instruments 缓存（§4.3 24h TTL）
        self._instruments_cache: dict[str, pl.DataFrame] = {}
        self._instruments_cache_ts: dict[str, datetime] = {}
        self._instruments_cache_ttl = 86400  # 秒

    def close(self) -> None:
        """关闭底层 FStore 与 TDX 连接（幂等）。供 lifespan 关闭链调用。"""
        try:
            self._fstore.close()
        except Exception:  # noqa: BLE001
            logger.warning("FQuantProvider: 关闭 fstore 连接失败", exc_info=True)
        try:
            self._engine.close()
        except Exception:  # noqa: BLE001
            logger.warning("FQuantProvider: 关闭 TDX 连接失败", exc_info=True)

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
    # ------------------------------------------------------------------ #
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
            rows = self._get_daily_from_engine_wide(sym, code, start_time, end_time, asset_type)
            if not rows:
                # L2 降级：engine-data 不可用 → fstore day_klines
                rows = self._get_daily_from_fstore_klines(sym, code, start_time, end_time, asset_type)
            if not rows:
                logger.debug("get_daily %s: 两源均无数据", sym)
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

    def _get_raw_oracle_rows(self, code: str, rows: list[dict]) -> list[dict]:
        """Fetch fstore raw OHLC oracle for the date span of engine rows.

        t_1_daily_markets 不存在，改用无前缀的 daily_markets 长表，
        字段从 payload_json 抽取。
        t_1_day_klines 两种模式下均可用（DuckDB 原生支持 ::text/::float8 cast）。
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
            (code, dates[0], dates[-1]),
        )
        by_date = {str(r.get("date")): r for r in day_rows}
        by_date.update({str(r.get("date")): r for r in market_rows})
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
        asset_type: AssetType,  # noqa: ARG002
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
        """从本地 DuckDB ``daily_markets`` 最新快照返回 realtime 形状。"""
        target_symbols = self._resolve_realtime_symbols(universes, symbols)
        if not target_symbols:
            return pl.DataFrame()

        rows = self._get_fstore_realtime(list(dict.fromkeys(target_symbols)))
        return normalize_realtime(rows, source=self.name) if rows else pl.DataFrame()

    def get_depth(self, symbols: list[str]) -> dict:
        """本地 DuckDB 当前无五档盘口。"""
        return {}

    def get_latest_market_supplements(self, symbols: list[str]) -> pl.DataFrame:
        """Latest fstore daily_markets fields that realtime sources may omit."""
        rows = self._get_fstore_realtime(symbols)
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

    @staticmethod
    def _pct_points_to_ratio(value) -> float | None:
        number = FQuantProvider._float_or_none(value)
        return number / 100 if number is not None else None

    def _resolve_realtime_symbols(
        self,
        universes: list[str] | None,
        symbols: list[str] | None,
    ) -> list[str]:
        if universes and symbols:
            raise ValueError("FQuant realtime accepts either universes or symbols, not both")
        if symbols:
            return [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not universes:
            return []

        frames: list[pl.DataFrame] = []
        upper = {u.upper() for u in universes}
        if any(u.startswith("CN_EQUITY") for u in upper):
            frames.append(self.get_instruments("stock"))
        if any(u.startswith("CN_ETF") for u in upper):
            frames.append(self.get_instruments("etf"))
        if any(u.startswith("CN_INDEX") for u in upper):
            frames.append(self.get_instruments("index"))

        if not frames:
            return []
        non_empty = [f for f in frames if f is not None and not f.is_empty()]
        if not non_empty:
            return []
        df = pl.concat(non_empty, how="diagonal_relaxed")
        if df.is_empty() or "symbol" not in df.columns:
            return []
        return df["symbol"].cast(pl.Utf8).to_list()

    def _get_fstore_realtime(self, symbols: list[str]) -> list[dict]:
        grouped: dict[int, list[str]] = {}
        for symbol in symbols:
            asset_type = self._asset_type_num_for_symbol(symbol)
            if asset_type is None:
                continue
            grouped.setdefault(asset_type, []).append(symbol_to_code(symbol))

        out: list[dict] = []
        for asset_type, codes in grouped.items():
            rows = self._query_fstore_realtime_rows(asset_type, codes)
            out.extend(self._fstore_quote_to_row(r, asset_type) for r in rows)
        return [r for r in out if r]

    def _query_fstore_realtime_rows(self, asset_type: int, codes: list[str]) -> list[dict]:
        """从 daily_markets 长表中查询，其余字段打包在 payload_json（驼峰 key）里，用
        ``->>`` 抽取后转型别名。
        """
        placeholders = ",".join(["%s"] * len(codes))
        sql = f"""
            SELECT DISTINCT ON (code)
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
            FROM daily_markets
            WHERE asset_type = %s AND code IN ({placeholders})
            ORDER BY code, trade_date DESC
        """
        return self._fstore.query(sql, (asset_type, *codes))

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

    def get_moneyflow_range(self, symbol: str, start: datetime, end: datetime) -> pl.DataFrame:
        """区间资金流查询（三锁指标资金锁专用）。"""
        code = symbol_to_code(symbol)
        return self._engine.get_fund_range(
            code,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )

    def get_moneyflow_minute(
        self, symbols: list[str], date: datetime | None = None,
    ) -> pl.DataFrame:
        """分钟级资金流（已下线，恒返回空 DataFrame）。"""
        return pl.DataFrame()

    def get_transactions(self, symbol: str, date: datetime) -> pl.DataFrame:
        """逐笔成交（§3.4 扩展方法 / §2.2 trans）。

        :param symbol: 带后缀符号
        :param date: 查询日期
        :return: df 列含 symbol/datetime/price/volume/amount/order_count/direction/source
                 direction: 0=中性 / 1=买 / 2=卖（§2.2）
        降级（§7.1）：engine-data 不响应 → 空 df
        """
        code = symbol_to_code(symbol)
        date_str = date.strftime("%Y%m%d")
        _, suffix = split_symbol(symbol)
        rows = self._engine.get_trans(code, date_str, asset_type="hk" if suffix == "HK" else None)
        if not rows:
            return pl.DataFrame()
        return trans_rows_to_df(rows, symbol, date_str, source=self.name)

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
