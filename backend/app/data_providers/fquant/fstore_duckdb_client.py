"""fstore DuckDB 直连客户端 —— FStoreClient 的只读替代实现。

背景：fstore 已经把分析结果表迁移到本机
``/Volumes/WD1/fstore.duckdb``，并为每张迁移过的 PostgreSQL 表提供了
同名无前缀兼容 view（物理镜像表带 pg_/fd_ 前缀，兼容 view 不带）。

本客户端对外暴露和 ``FStoreClient`` 完全相同的
``query(sql, params) -> list[dict]`` 接口 —— 调用方（fquant_provider.py）
不需要感知底层是 PostgreSQL 还是 DuckDB。唯一的差异是 SQL 占位符风格：
调用方写的是 psycopg 风格 ``%s``，这里在执行前统一替换成 DuckDB 的
``?``。**因此调用方写的 SQL 字符串里不能出现字面意义的 "%s"**
（比如 LIKE 模式里的 ``%`` 后面刚好跟一个 ``s`` 的情况），当前已核实
fquant_provider.py 里所有直连 fstore 的 SQL 都不满足这个反例。

配置：
- ``FQUANT_FSTORE_DUCKDB_PATH``（默认 ``/Volumes/WD1/fstore.duckdb``）
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

FSTORE_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_DUCKDB_PATH", "/Volumes/WD1/fstore.duckdb")


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
        if not os.path.exists(self._path):
            logger.warning("FStoreDuckDBClient: 文件不存在 %s", self._path)
            self._available = False
            return None
        try:
            conn = duckdb.connect(self._path, read_only=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreDuckDBClient: 打开失败 %s — %s", self._path, e)
            self._available = False
            return None
        self._conn = conn
        self._available = True
        logger.info("FStoreDuckDBClient: 已打开 %s（只读）", self._path)
        return conn

    @property
    def available(self) -> bool:
        return self._get_conn() is not None

    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        """执行 SELECT，返回 ``[{col: val, ...}, ...]``。失败返回空列表。"""
        conn = self._get_conn()
        if conn is None:
            return []
        duck_sql = sql.replace("%s", "?")
        try:
            with self._lock:
                cursor = conn.execute(duck_sql, list(params) if params else [])
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreDuckDBClient: 查询失败 — %s | SQL: %s", e, duck_sql[:200])
            return []
        return [dict(zip(columns, row)) for row in rows]
