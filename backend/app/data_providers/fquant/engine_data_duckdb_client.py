"""engine 侧 TDX 数据只读客户端 —— 完整覆盖 get_day/get_wide/get_minutes/get_trans/get_xdxr。

分别打开 A 股与港股拆分后的独立文件（不做跨库 ATTACH，因为没有跨表 join 需求）：
- /Volumes/WD1/tdx.duckdb          -> market_day_kline / market_wide_kline / market_xdxr
- /Volumes/WD1/tdx-minutes.duckdb  -> market_minutes
- /Volumes/WD1/tdx-trans.duckdb    -> market_transactions
- /Volumes/WD1/tdx-hk-web.duckdb        -> market_day_kline(dataset='hkday')
- /Volumes/WD1/tdx-hkminutes-web.duckdb -> market_minutes(dataset='hkminutes')
- /Volumes/WD1/tdx-hktrans-web.duckdb   -> market_transactions(dataset='hktrans')
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

from app.data_providers.fquant import generation
from app.data_providers.fquant.lease import ConnectionSet

logger = logging.getLogger(__name__)

TDX_PATH = os.getenv("FQUANT_TDX_DUCKDB_PATH", "/Volumes/WD1/tdx.duckdb")
TDX_MINUTES_PATH = os.getenv("FQUANT_TDX_MINUTES_DUCKDB_PATH", "/Volumes/WD1/tdx-minutes.duckdb")
TDX_TRANS_PATH = os.getenv("FQUANT_TDX_TRANS_DUCKDB_PATH", "/Volumes/WD1/tdx-trans.duckdb")
TDX_HK_PATH = os.getenv("FQUANT_TDX_HK_DUCKDB_PATH", "/Volumes/WD1/tdx-hk-web.duckdb")
TDX_HK_MINUTES_PATH = os.getenv("FQUANT_TDX_HK_MINUTES_DUCKDB_PATH", "/Volumes/WD1/tdx-hkminutes-web.duckdb")
TDX_HK_TRANS_PATH = os.getenv("FQUANT_TDX_HK_TRANS_DUCKDB_PATH", "/Volumes/WD1/tdx-hktrans-web.duckdb")

# side 直接就是 HTTP 契约的 direction 编码（已实测核实，取值 {0,1,2,5,8}，另外
# 实测还发现了极少量的 3，规模量级 <1万行/总量 9亿+行，同样直接透传不做映射），
# 不需要映射表，get_trans 里直接透传。

# A 股代码段 -> 交易所前缀。market_day_kline/market_wide_kline/market_xdxr/
# market_minutes/market_transactions 的 code 列都带这个前缀（如 sh600519），
# 而 FQuantProvider 传进来的 code 是裸代码（如 600519，来自 symbol_to_code）。
_PREFIX_BY_HEAD = {
    "60": "sh", "68": "sh", "90": "sh",
    "00": "sz", "30": "sz", "20": "sz",
    "43": "bj", "83": "bj", "87": "bj", "92": "bj",
}


def _prefixed_code(code: str) -> str:
    code = code.strip()
    if len(code) != 6:
        return code
    return _PREFIX_BY_HEAD.get(code[:2], "") + code if code[:2] in _PREFIX_BY_HEAD else code


def _hk_code(code: str) -> str:
    code = code.strip().lower()
    if code.startswith("hk"):
        return code
    return f"hk{code.zfill(5)}"


def _is_hk(asset_type: str | None) -> bool:
    return (asset_type or "").strip().lower() == "hk"


class _LeasedSource:
    """Generation-aware read-only source for one logical DuckDB database.

    Every query re-resolves the current snapshot generation (falling back to the
    raw path when no snapshot is published) and runs under a refcounted lease, so
    a generation swap mid-query never closes the connection in use.
    """

    def __init__(self, logical: str, raw_path: str) -> None:
        self._logical = logical
        self._raw_path = raw_path
        self._set: ConnectionSet | None = None
        self._duckdb_missing = False

    def _resolve(self) -> str | None:
        path = generation.current_path(self._logical)
        if path and os.path.exists(path):
            return path
        if os.path.exists(self._raw_path):
            return self._raw_path
        return None

    def _ensure_set(self) -> ConnectionSet | None:
        if self._duckdb_missing:
            return None
        if self._set is None:
            try:
                import duckdb
            except ImportError:
                self._duckdb_missing = True
                return None
            self._set = ConnectionSet(lambda p: duckdb.connect(p, read_only=True))
        return self._set

    @contextmanager
    def lease(self):
        cs = self._ensure_set()
        if cs is None:
            yield None
            return
        path = self._resolve()
        if path is None:
            logger.warning("EngineDataDuckDBClient: 文件不存在 logical=%s raw=%s", self._logical, self._raw_path)
            yield None
            return
        try:
            cm = cs.lease(path)
            conn = cm.__enter__()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: 打开失败 %s — %s", path, e)
            yield None
            return
        try:
            yield conn
        finally:
            cm.__exit__(None, None, None)

    def query(self, sql: str, params: list, label: str = "") -> list:
        """Run a read query under a lease; returns rows, or [] if unavailable.

        Uses ``conn.cursor()`` rather than executing directly on the leased
        connection — DuckDB's documented pattern for letting concurrent
        callers share one underlying database without contending on a single
        connection object (measured ~12% faster than sharing the raw
        connection under concurrent load, no added locking needed).
        """
        with self.lease() as conn:
            if conn is None:
                return []
            try:
                return conn.cursor().execute(sql, params).fetchall()
            except Exception as e:  # noqa: BLE001
                logger.warning("EngineDataDuckDBClient: %s 查询失败 — %s", label, e)
                return []

    def close(self) -> None:
        if self._set is not None:
            self._set.close()


class EngineDataDuckDBClient:
    """只读打开 tdx.duckdb/tdx-minutes.duckdb/tdx-trans.duckdb，完整实现五个数据集。"""

    def __init__(
        self,
        tdx_path: str | None = None,
        minutes_path: str | None = None,
        trans_path: str | None = None,
        hk_path: str | None = None,
        hk_minutes_path: str | None = None,
        hk_trans_path: str | None = None,
    ) -> None:
        self._tdx = _LeasedSource("tdx", tdx_path or TDX_PATH)
        self._minutes = _LeasedSource("tdx_minutes", minutes_path or TDX_MINUTES_PATH)
        self._trans = _LeasedSource("tdx_trans", trans_path or TDX_TRANS_PATH)
        self._hk = _LeasedSource("tdx_hk", hk_path or TDX_HK_PATH)
        self._hk_minutes = _LeasedSource("tdx_hk_minutes", hk_minutes_path or TDX_HK_MINUTES_PATH)
        self._hk_trans = _LeasedSource("tdx_hk_trans", hk_trans_path or TDX_HK_TRANS_PATH)

    def close(self) -> None:
        for src in (self._tdx, self._minutes, self._trans, self._hk, self._hk_minutes, self._hk_trans):
            src.close()

    def get_day(self, code: str, limit: int = 250) -> list[dict]:
        """读 market_day_kline（dataset='day'），字段对齐 EngineDataClient 的 day 数据集。"""
        rows = self._tdx.query(
            """
            SELECT trade_date, datetime, open, close, high, low, volume, amount,
                   up_count, down_count, adjustment_count
            FROM market_day_kline
            WHERE code = ? AND dataset = 'day'
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [_prefixed_code(code), limit],
            "get_day",
        )
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None,
                "datetime": r[1], "open": r[2], "close": r[3], "high": r[4], "low": r[5],
                "volume": r[6], "amount": r[7], "up": r[8], "down": r[9], "adjustment_count": r[10],
            }
            for r in rows
        ]

    def get_wide(self, code: str, limit: int = 250, asset_type: str | None = None) -> list[dict]:
        """读 market_wide_kline，字段对齐 EngineDataClient 的 wide 数据集。

        market_wide_kline 没有 datetime/adjustment_count 两列（market_day_kline 有），
        这里固定填 None/0——调用方的字段归一函数需要能容忍这两个字段缺失。

        已确认 market_wide_kline 相对 market_day_kline 和 HTTP 路径存在约 2 个交易日
        的稳定滞后（表级导入延迟，非单个代码的问题），因此 get_wide 的结果可能缺少
        近期交易日的数据，即使这些数据在 get_day 或 HTTP 路径中已存在——这是 engine
        仓库数据导入流水线的上游问题，本客户端无法修复。
        """
        if _is_hk(asset_type):
            return self._get_hk_day(code, limit)
        rows = self._tdx.query(
            """
            SELECT trade_date, open, close, high, low, volume, amount, up_count, down_count,
                   last_close, change_rate, open_volume, open_turnz, open_unmatched,
                   close_volume, close_turnz, close_unmatched, inner_volume, outer_volume,
                   inner_amount, outer_amount
            FROM market_wide_kline
            WHERE code = ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [_prefixed_code(code), limit],
            "get_wide",
        )
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None, "datetime": None,
                "open": r[1], "close": r[2], "high": r[3], "low": r[4], "volume": r[5], "amount": r[6],
                "up": r[7], "down": r[8], "adjustment_count": 0,
                "last_close": r[9], "change_rate": r[10],
                "open_volume": r[11], "open_turnz": r[12], "open_unmatched": r[13],
                "close_volume": r[14], "close_turnz": r[15], "close_unmatched": r[16],
                "inner_volume": r[17], "outer_volume": r[18], "inner_amount": r[19], "outer_amount": r[20],
            }
            for r in rows
        ]

    # tdx-hk.duckdb 的 market_day_kline.volume 存的是「手」，不是「股」——和 A股
    # 同名列（market_wide_kline.volume，存股数）单位不一致。实测 hk00700
    # 2025-10-20：该列 = 1,496，而 amount/close 推出的真实股数 = 14,947,219，
    # 比值 9,991.5。本项目对外的 volume 口径统一是股数（A股路径拿到的就是股数），
    # 所以港股这一列必须 ×10000 补回去，否则港股日线成交量会比真实值小 1 万倍，
    # 且与 A股口径不一致（下游 enriched 指标里的量比、换手率等会全部算错）。
    #
    # 判据用 amount/价格（成交额÷价格＝股数），不依赖任何 volume 列——这一列本身
    # 就是不可信的那个。详见 fm-cli/engine tdx-kline 设计文档同一坑的记录。
    _HK_DAY_VOLUME_TO_SHARES = 10000

    def _get_hk_day(self, code: str, limit: int) -> list[dict]:
        rows = self._hk.query(
            """
            SELECT trade_date, datetime, open, close, high, low, volume, amount,
                   up_count, down_count, adjustment_count
            FROM market_day_kline
            WHERE code = ? AND dataset = 'hkday'
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [_hk_code(code), limit],
            "get_hk_day",
        )
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None,
                "datetime": r[1], "open": r[2], "close": r[3], "high": r[4], "low": r[5],
                "volume": (r[6] * self._HK_DAY_VOLUME_TO_SHARES) if r[6] is not None else None,
                "amount": r[7], "up": r[8], "down": r[9], "adjustment_count": r[10],
            }
            for r in rows
        ]

    def get_minutes(self, code: str, date_yyyymmdd: str, limit: int = 5000, asset_type: str | None = None) -> list[dict]:
        """读 market_minutes，字段对齐 EngineDataClient 的 minutes 数据集（price/volume）。

        market_minutes 的 time/amount 两列全表 34 亿+行全是 NULL（已实测确认），
        只有 price/volume/minute_index 有真实数据，这也是为什么只选 price/volume
        两列、靠 minute_index 排序——不要改成查 time 列，查了也是 None。
        """
        hk = _is_hk(asset_type)
        src = self._hk_minutes if hk else self._minutes
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        rows = src.query(
            """
            SELECT price, volume
            FROM market_minutes
            WHERE code = ? AND trade_date = ? AND dataset = ?
            ORDER BY minute_index
            LIMIT ?
            """,
            [_hk_code(code) if hk else _prefixed_code(code), trade_date, "hkminutes" if hk else "minutes", limit],
            "get_minutes",
        )
        return [{"price": r[0], "volume": r[1]} for r in rows]

    def get_trans(self, code: str, date_yyyymmdd: str, limit: int = 5000, asset_type: str | None = None) -> list[dict]:
        """读 market_transactions，字段对齐 EngineDataClient 的 trans 数据集。

        market_transactions 没有 order_count 列，这里固定填 None——
        调用方 trans_rows_to_df 需要能容忍这一列缺失/为空。
        """
        hk = _is_hk(asset_type)
        src = self._hk_trans if hk else self._trans
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        rows = src.query(
            """
            SELECT time, price, volume, amount, side
            FROM market_transactions
            WHERE code = ? AND trade_date = ? AND dataset = ?
            ORDER BY time
            LIMIT ?
            """,
            [_hk_code(code) if hk else _prefixed_code(code), trade_date, "hktrans" if hk else "trans", limit],
            "get_trans",
        )
        return [
            {
                "time": r[0], "price": r[1], "volume": r[2], "amount": r[3],
                "order_count": None, "direction": r[4],
            }
            for r in rows
        ]

    def get_xdxr(self, code: str, limit: int = 100, asset_type: str | None = None) -> list[dict]:
        """读 market_xdxr，字段对齐 EngineDataClient 的 xdxr 数据集。

        表里的列名是 xingquanjiya（比 HTTP 契约的 xingquanjia 多一个 ya），这里
        用 AS xingquanjia 对齐字段名——但这只是对齐命名，不是修复数据：这一列
        当前全表都是 NULL（已实测确认，engine 侧写入/导入用错了列名把真实数据
        写丢了），所以这个方法返回的 xingquanjia 字段现阶段恒为 None，等 engine
        仓库修好表结构/回填存量数据之后才会有真实值，这里不做任何掩盖或伪造。
        """
        _ = asset_type
        rows = self._tdx.query(
            """
            SELECT event_date, category, name, fenhong, peigujia, songzhuangu, peigu, suogu,
                   qianliutong, houliutong, qianzongguben, houzongguben, fenshu, xingquanjiya
            FROM market_xdxr
            WHERE code = ?
            ORDER BY event_date DESC
            LIMIT ?
            """,
            [_prefixed_code(code), limit],
            "get_xdxr",
        )
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None,
                "category": r[1], "name": r[2], "fenhong": r[3], "peigujia": r[4],
                "songzhuangu": r[5], "peigu": r[6], "suogu": r[7], "qianliutong": r[8],
                "houliutong": r[9], "qianzongguben": r[10], "houzongguben": r[11],
                "fenshu": r[12], "xingquanjia": r[13],
            }
            for r in rows
        ]

    def get_fund_daily(self, code: str, date_iso: str) -> dict:
        """读 market_fund_flow 单日资金流数据，字段契约对齐 EngineDataDiskClient.get_fund_daily。

        :param code: 裸代码（如 ``600519``），内部会加交易所前缀
        :param date_iso: ``YYYY-MM-DD`` 格式日期字符串
        :return: 含 main_net/total_net/super_large_net/... 的 dict；
                 文件不可达或无数据时返回 {}（与 EngineDataDiskClient 一致）
        """
        rows = self._tdx.query(
            """
            SELECT main, super_large, large, medium, small,
                   main_ratio, super_large_ratio, large_ratio, medium_ratio, small_ratio
            FROM market_fund_flow
            WHERE code = ? AND trade_date = ?
            """,
            [_prefixed_code(code), date_iso],
            "get_fund_daily",
        )
        if not rows:
            return {}
        row = rows[0]
        main = float(row[0] or 0)
        super_large = float(row[1] or 0)
        large = float(row[2] or 0)
        medium = float(row[3] or 0)
        small = float(row[4] or 0)
        return {
            "main_net": main,
            "total_net": main + medium + small,
            "super_large_net": super_large,
            "large_net": large,
            "medium_net": medium,
            "small_net": small,
            "main_ratio": float(row[5] or 0),
            "super_large_ratio": float(row[6] or 0),
            "large_ratio": float(row[7] or 0),
            "medium_ratio": float(row[8] or 0),
            "small_ratio": float(row[9] or 0),
        }

    def get_fund_range(self, code: str, start_iso: str, end_iso: str):
        """读 market_fund_flow 区间资金流，返回 polars DataFrame。

        契约对齐 EngineDataDiskClient.get_fund_range：只返回 ["date", "main_net_inflow"] 两列。

        :param code: 裸代码（如 ``600519``），内部会加交易所前缀
        :param start_iso: ``YYYY-MM-DD`` 格式起始日期（含）
        :param end_iso: ``YYYY-MM-DD`` 格式结束日期（含）
        :return: polars DataFrame，列为 date/main_net_inflow；
                 文件不可达或无数据时返回空 DataFrame
        """
        import polars as pl

        rows = self._tdx.query(
            """
            SELECT trade_date::TEXT AS date, main AS main_net_inflow
            FROM market_fund_flow
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [_prefixed_code(code), start_iso, end_iso],
            "get_fund_range",
        )
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame({"date": [r[0] for r in rows], "main_net_inflow": [r[1] for r in rows]})
