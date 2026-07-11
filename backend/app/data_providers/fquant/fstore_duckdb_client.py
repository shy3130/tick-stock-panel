"""fstore DuckDB 直连客户端 —— FStoreClient 的只读替代实现。

背景：fstore 已经把分析结果表迁移到本机
``/Volumes/WD1/fstore-web.duckdb``，并为每张迁移过的 PostgreSQL 表提供了
同名无前缀兼容 view（物理镜像表带 pg_/fd_ 前缀，兼容 view 不带）。

本客户端对外暴露和 ``FStoreClient`` 完全相同的
``query(sql, params) -> list[dict]`` 接口 —— 调用方（fquant_provider.py）
不需要感知底层是 PostgreSQL 还是 DuckDB。唯一的差异是 SQL 占位符风格：
调用方写的是 psycopg 风格 ``%s``，这里在执行前统一替换成 DuckDB 的
``?``。**因此调用方写的 SQL 字符串里不能出现字面意义的 "%s"**
（比如 LIKE 模式里的 ``%`` 后面刚好跟一个 ``s`` 的情况），当前已核实
fquant_provider.py 里所有直连 fstore 的 SQL 都不满足这个反例。

配置：
- ``FQUANT_FSTORE_DUCKDB_PATH``（默认 ``/Volumes/WD1/fstore-web.duckdb``）
- ``FQUANT_FSTORE_MARKETS_DUCKDB_PATH``（默认 ``/Volumes/WD1/fstore-markets-web.duckdb``）
- ``FQUANT_FSTORE_KLINES_DUCKDB_PATH``（默认 ``/Volumes/WD1/fstore-klines-web.duckdb``）
- ``FQUANT_FSTORE_MINUTES_DUCKDB_PATH``（默认 ``/Volumes/WD1/fstore-minutes-web.duckdb``）
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from app.data_providers.fquant.snapshot_resolver import snapshot_or_raw

logger = logging.getLogger(__name__)

FSTORE_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_DUCKDB_PATH", "/Volumes/WD1/fstore-web.duckdb")
FSTORE_MARKETS_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_MARKETS_DUCKDB_PATH", "/Volumes/WD1/fstore-markets-web.duckdb")
FSTORE_KLINES_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_KLINES_DUCKDB_PATH", "/Volumes/WD1/fstore-klines-web.duckdb")
FSTORE_MINUTES_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_MINUTES_DUCKDB_PATH", "/Volumes/WD1/fstore-minutes-web.duckdb")


class FStoreDuckDBClient:
    """fstore DuckDB 只读客户端，接口对齐 FStoreClient。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or FSTORE_DUCKDB_PATH
        self._conn: Any = None
        self._available: bool | None = None
        self._lock = threading.Lock()

    def _get_conn(self):
        if self._available is False:
            return None
        if self._conn is not None:
            return self._conn
        try:
            import duckdb
        except ImportError:
            logger.warning("FStoreDuckDBClient: duckdb 未安装")
            self._available = False
            return None
        # Prefer the published snapshot; ``-web``/unknown paths and
        # not-yet-published roots fall back to the raw path unchanged.
        main_path = snapshot_or_raw(self._path)
        if not os.path.exists(main_path):
            logger.warning("FStoreDuckDBClient: 文件不存在 %s", main_path)
            self._available = False
            return None
        try:
            conn = duckdb.connect(main_path, read_only=True)
            self._attach(conn, "fstore_markets", FSTORE_MARKETS_DUCKDB_PATH, main_path)
            self._attach(conn, "fstore_klines", FSTORE_KLINES_DUCKDB_PATH, main_path)
            self._attach(conn, "fstore_minutes", FSTORE_MINUTES_DUCKDB_PATH, main_path)
            self._create_temp_views(conn)
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreDuckDBClient: 打开失败 %s — %s", main_path, e)
            self._available = False
            return None
        self._conn = conn
        self._available = True
        logger.info("FStoreDuckDBClient: 已打开 %s（只读）", main_path)
        return conn

    def _attach(self, conn: Any, alias: str, path: str, main_path: str) -> None:
        path = snapshot_or_raw(path)
        if not path or os.path.abspath(path) == os.path.abspath(main_path) or not os.path.exists(path):
            return
        escaped = path.replace("'", "''")
        conn.execute(f"ATTACH '{escaped}' AS {alias} (READ_ONLY)")

    def _create_temp_views(self, conn: Any) -> None:
        # ponytail: connection-local aliases keep old SQL working after split files.
        self._create_split_alias(conn, "daily_markets", "fstore_markets.daily_markets")
        self._create_split_alias(conn, "day_klines", "fstore_klines.day_klines")
        self._create_split_alias(conn, "minute_kline", "fstore_minutes.minute_kline")
        for asset_type in (1, 2, 3, 4, 5, 6, 7, 10, 15, 20, 30, 37, 38, 39, 40, 41, 42, 47, 48, 49):
            conn.execute(
                f"CREATE TEMP VIEW IF NOT EXISTS t_{asset_type}_day_klines "
                f"AS SELECT * FROM day_klines WHERE asset_type = {asset_type}"
            )

    @staticmethod
    def _create_split_alias(conn: Any, view_name: str, source: str) -> None:
        try:
            conn.execute(f"CREATE OR REPLACE TEMP VIEW {view_name} AS SELECT * FROM {source}")
        except Exception as e:  # noqa: BLE001
            logger.debug("FStoreDuckDBClient: 跳过拆分库别名 %s -> %s: %s", view_name, source, e)

    @property
    def available(self) -> bool:
        return self._get_conn() is not None

    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        """执行 SELECT，返回 ``[{col: val, ...}, ...]``。失败返回空列表。

        对 Python date/datetime 对象自动转换为 ISO 格式字符串，以兼容 DuckDB VARCHAR 列。
        """
        conn = self._get_conn()
        if conn is None:
            return []
        duck_sql = sql.replace("%s", "?")
        # 转换 date/datetime 对象为 ISO 格式字符串（DuckDB 的 VARCHAR 列需要）
        converted_params = self._convert_params(params)
        try:
            with self._lock:
                cursor = conn.execute(duck_sql, converted_params)
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreDuckDBClient: 查询失败 — %s | SQL: %s", e, duck_sql[:200])
            return []
        return [dict(zip(columns, row)) for row in rows]

    @staticmethod
    def _convert_params(params: tuple | list | None) -> list:
        """将 Python date/datetime 对象转换为 ISO 格式字符串。"""
        if not params:
            return []
        from datetime import date, datetime
        converted = []
        for param in params:
            if isinstance(param, datetime):
                converted.append(param.isoformat())
            elif isinstance(param, date):
                converted.append(param.isoformat())
            else:
                converted.append(param)
        return converted
