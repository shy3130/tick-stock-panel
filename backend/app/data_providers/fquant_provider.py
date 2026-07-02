"""FQuantProvider v2 — 直连 fstore PG + engine-data + moneyflow 三上游源。

严格按 ``backend/docs/FQUANT_PROVIDER_DESIGN.md`` §4 模块设计 / §5 数据映射 /
§6 配置 / §7 错误降级 / §8 测试方案 实现。

架构（§4.1）::

    backend/app/data_providers/
    ├── fquant/
    │   ├── __init__.py          符号归一重导出
    │   ├── symbols.py           符号归一（split_symbol 等）
    │   ├── fstore_client.py     psycopg v3 PG 客户端
    │   ├── engine_data_client.py engine-data HTTP 客户端
    │   ├── moneyflow_client.py  moneyflow HTTP 客户端
    │   ├── mapping.py           上游字段 → 内部 schema
    │   ├── adj_factor.py        xdxr → 累积 ex_factor
    │   └── fallback.py          降级策略表
    └── fquant_provider.py       本文件（聚合 Provider）

能力声明（§3.5 / §4.2）::

    instruments=True, daily=True, adj_factor=True,
    minute=True, realtime=False, financial=True

错误降级（§7）：任一源不可达 → 返回空 DF + warning，不抛异常。
"""
from __future__ import annotations

import logging
from datetime import datetime

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.fquant.adj_factor import (
    build_ex_factor_df,
    compute_ex_factor_from_chuquan,
    compute_ex_factor_from_xdxr,
)
from app.data_providers.fquant.engine_data_client import EngineDataClient
from app.data_providers.fquant.fstore_client import FStoreClient
from app.data_providers.fquant.mapping import (
    base_infos_rows_to_instruments,
    chuquan_rows_to_events,
    day_rows_to_daily,
    financial_rows_to_df,
    generated_minute_time,
    klines_rows_to_daily,
    minutes_rows_to_minute_df,
    moneyflow_daily_to_df,
    moneyflow_minute_to_df,
    trans_rows_to_df,
    wide_rows_to_daily,
    xdxr_rows_to_events,
)
from app.data_providers.fquant.moneyflow_client import MoneyflowClient
from app.data_providers.fquant.symbols import (
    code_and_market_to_symbol,
    code_to_symbol,
    split_symbol,
    symbol_to_code,
    symbol_to_market,
)
from app.data_providers.normalizer import (
    normalize_adj_factors,
    normalize_daily,
    normalize_instruments,
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


# =========================================================================== #
# FQuantProvider（对外接口，三子客户端聚合）
# =========================================================================== #
class FQuantProvider:
    """FQuant 数据源 Provider — 直连 fstore / engine-data / moneyflow。

    实现 ``MarketDataProvider`` 接口（见 ``base.py``）。三子客户端独立工作，
    任一故障不影响其余（§7）。

    能力声明（§3.5 / §4.2）：
    - instruments / daily / adj_factor / minute / financial → True
    - realtime → False（§4.7 本期不实现，留 §10 路线 1）
    """

    name = "fquant"
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        adj_factor=True,
        minute=True,
        realtime=False,
        financial=True,
    )

    def __init__(self) -> None:
        self._fstore = FStoreClient()
        self._engine = EngineDataClient()
        self._moneyflow = MoneyflowClient()
        # instruments 缓存（§4.3 24h TTL）
        self._instruments_cache: dict[str, pl.DataFrame] = {}
        self._instruments_cache_ts: dict[str, datetime] = {}
        self._instruments_cache_ttl = 86400  # 秒

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
            f"SELECT code, name, asset_type, ssdate, symbol "
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
            rows = self._get_daily_from_engine_wide(sym, code, start_time, end_time)
            if not rows:
                # L2 降级：engine-data 不可用 → fstore day_klines
                rows = self._get_daily_from_fstore_klines(sym, code, start_time, end_time)
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
    ) -> list[dict]:
        """主源 engine-data ``wide``（§4.4 / §5.2）。"""
        if start_time and end_time:
            limit = max(250, (end_time - start_time).days + 10)
        else:
            limit = 250
        rows = self._engine.get_wide(code, limit=limit)
        if rows:
            # engine 返回最新在前，反转成时间正序
            rows = list(reversed(rows))
            logger.debug("EngineData wide %s: %d 行", code, len(rows))
        # 映射到 normalizer 期望的字段名
        return wide_rows_to_daily(rows, symbol, source=self.name)

    def _get_daily_from_fstore_klines(
        self, symbol: str, code: str,
        start_time: datetime | None, end_time: datetime | None,
    ) -> list[dict]:
        """备份 fstore ``day_klines``（fq=0 不复权, ktype=101 日线）。

        实测 600519 该表最后数据 2025-10-31（§2.1 / §7.3 场景 A），
        仅作历史回填，不依赖。
        """
        if start_time and end_time:
            sql = (
                "SELECT tdate, open, close, high, low, cjl, cje, zf "
                "FROM day_klines "
                "WHERE code = %s AND ktype = 101 AND fq = 0 AND tdate BETWEEN %s AND %s "
                "ORDER BY tdate ASC"
            )
            params: tuple = (code, start_time.date(), end_time.date())
        else:
            sql = (
                "SELECT tdate, open, close, high, low, cjl, cje, zf "
                "FROM day_klines "
                "WHERE code = %s AND ktype = 101 AND fq = 0 "
                "ORDER BY tdate DESC LIMIT 250"
            )
            params = (code,)
        rows = self._fstore.query(sql, params)
        if rows:
            logger.debug("FStoreDB day_klines %s: %d 行", code, len(rows))
        # 映射到 normalizer 期望的字段名
        return klines_rows_to_daily(rows, symbol, source=self.name) if rows else []

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
        1. 主源 engine-data ``xdxr``（fenhong/fenshu → 累积 ex_factor）
        2. 备份 fstore ``chuquan_chuxi``（pxbl → 累积 ex_factor）
        3. 空 df

        输出列（经 ``normalize_adj_factors``）：symbol/trade_date/ex_factor
        """
        if not symbols:
            return pl.DataFrame()

        frames: list[pl.DataFrame] = []
        for sym in symbols:
            code = symbol_to_code(sym)

            # 先取 daily close 序列（fenhong 除权除息计算需要 pre_close）
            daily_close = self._build_daily_close_map(sym, code, start_time, end_time)

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
            logger.debug("EngineData xdxr %s: %d 行", code, len(rows))
        return xdxr_rows_to_events(rows, symbol) if rows else []

    def _get_adj_events_from_fstore(
        self, symbol: str, code: str,
        start_time: datetime | None, end_time: datetime | None,
    ) -> list[dict]:
        """备份 fstore ``chuquan_chuxi`` → 归一事件行（§5.3）。"""
        if start_time and end_time:
            sql = (
                "SELECT t_date, pgbl, pgjg, pxbl, sgbl, cqcxtype "
                "FROM chuquan_chuxi WHERE code = %s AND t_date BETWEEN %s AND %s "
                "ORDER BY t_date ASC"
            )
            params: tuple = (code, start_time, end_time)
        else:
            sql = (
                "SELECT t_date, pgbl, pgjg, pxbl, sgbl, cqcxtype "
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
    ) -> dict[str, float]:
        """构建 ``{date_iso: close_price}`` 字典，供 adj_factor fenhong 计算用。

        复用 get_daily 的 engine wide + fstore klines 降级链。
        """
        try:
            rows = self._get_daily_from_engine_wide(symbol, code, start_time, end_time)
            if not rows:
                rows = self._get_daily_from_fstore_klines(symbol, code, start_time, end_time)
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

        数据流：engine-data ``minutes``（price/volume，客户端重建时间戳）
        降级：API 不响应 → 空 df

        date 推断：优先 ``end_time``，其次 ``start_time``。
        freq 仅支持 ``1m``（其它 freq 仅日志警告并返回 1m 结果，§4.6）。

        输出列（MINUTE_COLUMNS）：symbol/asset_type/source/datetime/open/high/low/close/
                                 volume/amount/freq
        """
        if not symbols:
            return pl.DataFrame()

        if freq != "1m":
            logger.warning("get_minute: freq=%s 本期仅支持 1m，返回 1m 结果", freq)

        # 确定查询日期（§4.6 date 推断）
        ref_dt = end_time or start_time
        if ref_dt is None:
            return pl.DataFrame()
        date_str = ref_dt.strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        for sym in symbols:
            code = symbol_to_code(sym)
            ticks = self._engine.get_minutes(code, date_str)
            if not ticks:
                logger.debug("EngineData minutes %s %s: 无数据", code, date_str)
                continue
            minute_df = minutes_rows_to_minute_df(
                ticks, sym, asset_type, date_str,
                source=self.name, freq="1m",
            )
            if not minute_df.is_empty():
                frames.append(minute_df)

        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ------------------------------------------------------------------ #
    # get_realtime — §4.7 本期不实现
    # ------------------------------------------------------------------ #
    def get_realtime(
        self,
        universes: list[str] | None = None,  # noqa: ARG002
        symbols: list[str] | None = None,  # noqa: ARG002
    ) -> pl.DataFrame:
        """本期不实现 realtime（§4.7 / §10 路线 1）。

        engine-data ``trans`` 已能凑出主买主卖聚类作为 realtime 平替，
        但构建 Standard RealtimeQuote 还需 tencent/tdex 兜底——留 §10 R1。
        """
        return pl.DataFrame()

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

        return financial_rows_to_df(rows, symbol=symbol, table=table, source_tag="fquant:fstore")

    # ------------------------------------------------------------------ #
    # get_moneyflow_daily — §4.9 moneyflow /daily/stocks
    # ------------------------------------------------------------------ #
    def get_moneyflow_daily(
        self, symbols: list[str], date: datetime | None = None,
    ) -> pl.DataFrame:
        """日级资金流（§4.9 / §5.7 扩展方法）。

        :param symbols: 带后缀符号列表
        :param date: 查询日期；None 取当天
        :return: df 列含 symbol/date/source/main_net/total_net/...
        降级（§7.1）：API 不可达 → 空 df
        """
        if not symbols:
            return pl.DataFrame()

        date_iso = (date or datetime.now()).strftime("%Y-%m-%d")
        codes = [symbol_to_code(s) for s in symbols]
        code_to_sym = {symbol_to_code(s): s for s in symbols}

        data = self._moneyflow.get_daily(codes, date_iso)
        if not data:
            return pl.DataFrame()

        return moneyflow_daily_to_df(data, code_to_sym, date_iso, source=self.name)

    # ------------------------------------------------------------------ #
    # get_moneyflow_minute — §4.9 moneyflow /minute/stocks
    # ------------------------------------------------------------------ #
    def get_moneyflow_minute(
        self, symbols: list[str], date: datetime | None = None,
    ) -> pl.DataFrame:
        """分钟级资金流（§4.9 / §5.7 扩展方法）。

        :param symbols: 带后缀符号列表
        :param date: 查询日期；None 取当天
        :return: df 列含 symbol/trade_date/bucket_time/net_amount/main_traditional_net/...
        注意（§7.4）：09:25 桶 NetAmount=0 是集合竞价假阳性，上层需自行跳过。
        降级（§7.1）：API 不可达 → 空 df
        """
        if not symbols:
            return pl.DataFrame()

        date_iso = (date or datetime.now()).strftime("%Y-%m-%d")
        frames: list[pl.DataFrame] = []
        for sym in symbols:
            code = symbol_to_code(sym)
            records = self._moneyflow.get_minute(code, date_iso)
            if not records:
                continue
            df = moneyflow_minute_to_df(records, sym, date_iso, source=self.name)
            if not df.is_empty():
                frames.append(df)

        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ------------------------------------------------------------------ #
    # get_transactions — §3.4 扩展方法 engine-data trans
    # ------------------------------------------------------------------ #
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
        rows = self._engine.get_trans(code, date_str)
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
