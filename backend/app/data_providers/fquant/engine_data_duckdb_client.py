"""engine 侧 TDX 数据只读客户端 —— 完整覆盖 get_day/get_wide/get_minutes/get_trans/get_xdxr。

分别打开三个独立文件（不做跨库 ATTACH，因为没有跨表 join 需求）：
- /Volumes/WD1/tdx.duckdb          -> market_day_kline / market_wide_kline / market_xdxr
- /Volumes/WD1/tdx-minutes.duckdb  -> market_minutes
- /Volumes/WD1/tdx-trans.duckdb    -> market_transactions
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

TDX_PATH = os.getenv("FQUANT_TDX_DUCKDB_PATH", "/Volumes/WD1/tdx.duckdb")
TDX_MINUTES_PATH = os.getenv("FQUANT_TDX_MINUTES_DUCKDB_PATH", "/Volumes/WD1/tdx-minutes.duckdb")
TDX_TRANS_PATH = os.getenv("FQUANT_TDX_TRANS_DUCKDB_PATH", "/Volumes/WD1/tdx-trans.duckdb")

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


class _SingleFileConn:
    """单个 DuckDB 文件的懒加载只读连接，四个数据集各自独立复用一份。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Any = None
        self._available: bool | None = None

    def get(self):
        if self._available is False:
            return None
        if self._conn is not None:
            return self._conn
        try:
            import duckdb
        except ImportError:
            self._available = False
            return None
        if not os.path.exists(self._path):
            logger.warning("EngineDataDuckDBClient: 文件不存在 %s", self._path)
            self._available = False
            return None
        try:
            self._conn = duckdb.connect(self._path, read_only=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: 打开失败 %s — %s", self._path, e)
            self._available = False
            return None
        self._available = True
        return self._conn


class EngineDataDuckDBClient:
    """只读打开 tdx.duckdb/tdx-minutes.duckdb/tdx-trans.duckdb，完整实现五个数据集。"""

    def __init__(
        self,
        tdx_path: str | None = None,
        minutes_path: str | None = None,
        trans_path: str | None = None,
    ) -> None:
        self._tdx = _SingleFileConn(tdx_path or TDX_PATH)
        self._minutes = _SingleFileConn(minutes_path or TDX_MINUTES_PATH)
        self._trans = _SingleFileConn(trans_path or TDX_TRANS_PATH)

    def get_day(self, code: str, limit: int = 250) -> list[dict]:
        """读 market_day_kline（dataset='day'），字段对齐 EngineDataClient 的 day 数据集。"""
        conn = self._tdx.get()
        if conn is None:
            return []
        try:
            cursor = conn.execute(
                """
                SELECT trade_date, datetime, open, close, high, low, volume, amount,
                       up_count, down_count, adjustment_count
                FROM market_day_kline
                WHERE code = ? AND dataset = 'day'
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                [_prefixed_code(code), limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_day 查询失败 — %s", e)
            return []
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
        _ = asset_type
        conn = self._tdx.get()
        if conn is None:
            return []
        try:
            cursor = conn.execute(
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
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_wide 查询失败 — %s", e)
            return []
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

    def get_minutes(self, code: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        """读 market_minutes，字段对齐 EngineDataClient 的 minutes 数据集（price/volume）。

        market_minutes 的 time/amount 两列全表 34 亿+行全是 NULL（已实测确认），
        只有 price/volume/minute_index 有真实数据，这也是为什么只选 price/volume
        两列、靠 minute_index 排序——不要改成查 time 列，查了也是 None。
        """
        conn = self._minutes.get()
        if conn is None:
            return []
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        try:
            cursor = conn.execute(
                """
                SELECT price, volume
                FROM market_minutes
                WHERE code = ? AND trade_date = ? AND dataset = 'minutes'
                ORDER BY minute_index
                LIMIT ?
                """,
                [_prefixed_code(code), trade_date, limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_minutes 查询失败 — %s", e)
            return []
        return [{"price": r[0], "volume": r[1]} for r in rows]

    def get_trans(self, code: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        """读 market_transactions，字段对齐 EngineDataClient 的 trans 数据集。

        market_transactions 没有 order_count 列，这里固定填 None——
        调用方 trans_rows_to_df 需要能容忍这一列缺失/为空。
        """
        conn = self._trans.get()
        if conn is None:
            return []
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        try:
            cursor = conn.execute(
                """
                SELECT time, price, volume, amount, side
                FROM market_transactions
                WHERE code = ? AND trade_date = ? AND dataset = 'trans'
                ORDER BY time
                LIMIT ?
                """,
                [_prefixed_code(code), trade_date, limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_trans 查询失败 — %s", e)
            return []
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
        conn = self._tdx.get()
        if conn is None:
            return []
        try:
            cursor = conn.execute(
                """
                SELECT event_date, category, name, fenhong, peigujia, songzhuangu, peigu, suogu,
                       qianliutong, houliutong, qianzongguben, houzongguben, fenshu, xingquanjiya
                FROM market_xdxr
                WHERE code = ?
                ORDER BY event_date DESC
                LIMIT ?
                """,
                [_prefixed_code(code), limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_xdxr 查询失败 — %s", e)
            return []
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
