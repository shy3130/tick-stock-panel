"""fstore DuckDB 直连客户端 —— FStoreClient 的只读替代实现。

背景：fstore 已经把分析结果表迁移到本机
``/Volumes/WD1/duckdb/fstore-web.duckdb``，并为每张迁移过的 PostgreSQL 表提供了
同名无前缀兼容 view（物理镜像表带 pg_/fd_ 前缀，兼容 view 不带）。

本客户端对外暴露和 ``FStoreClient`` 完全相同的
``query(sql, params) -> list[dict]`` 接口 —— 调用方（fquant_provider.py）
不需要感知底层是 PostgreSQL 还是 DuckDB。唯一的差异是 SQL 占位符风格：
调用方写的是 psycopg 风格 ``%s``，这里在执行前统一替换成 DuckDB 的
``?``。**因此调用方写的 SQL 字符串里不能出现字面意义的 "%s"**
（比如 LIKE 模式里的 ``%`` 后面刚好跟一个 ``s`` 的情况），当前已核实
fquant_provider.py 里所有直连 fstore 的 SQL 都不满足这个反例。

配置：
- ``FQUANT_FSTORE_DUCKDB_PATH``（默认 ``/Volumes/WD1/duckdb/fstore.duckdb``）
- ``FQUANT_FSTORE_MARKETS_DUCKDB_PATH``（默认 ``/Volumes/WD1/duckdb/fstore-markets.duckdb``）
- ``FQUANT_FSTORE_KLINES_DUCKDB_PATH``（默认 ``/Volumes/WD1/duckdb/fstore-klines.duckdb``）
- ``FQUANT_FSTORE_MINUTES_DUCKDB_PATH``（默认 ``/Volumes/WD1/duckdb/fstore-minutes.duckdb``）
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from app.data_providers.fquant.snapshot_resolver import snapshot_or_raw

logger = logging.getLogger(__name__)

# 默认指向 raw 写入库：snapshot_or_raw() 会把已知 raw 路径解析为已发布的
# generation 快照（snapshots/fstore/<gen>/，immutable 只读，不锁 raw），只有当该
# root 未发布快照时才回退到 raw 本身（只读 open）。旧的 ``*-web.duckdb`` 物理副本已
# 停更（数据停在历史日期），不再作为默认源——传 -web 路径不会被映射到快照。
FSTORE_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_DUCKDB_PATH", "/Volumes/WD1/duckdb/fstore.duckdb")
FSTORE_MARKETS_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_MARKETS_DUCKDB_PATH", "/Volumes/WD1/duckdb/fstore-markets.duckdb")
FSTORE_KLINES_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_KLINES_DUCKDB_PATH", "/Volumes/WD1/duckdb/fstore-klines.duckdb")
FSTORE_MINUTES_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_MINUTES_DUCKDB_PATH", "/Volumes/WD1/duckdb/fstore-minutes.duckdb")
FSTORE_EXTENDED_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_EXTENDED_DUCKDB_PATH", "/Volumes/WD1/duckdb/fstore-extended.duckdb")

# 初始连接失败后的固定退避窗口（秒）：窗口内不重试，避免紧密重连和日志风暴；
# 窗口过后下次查询自动重连。查询异常释放连接后立即允许重连（不施加退避）。
RECONNECT_BACKOFF_SECONDS = float(os.getenv("FQUANT_FSTORE_RECONNECT_BACKOFF_S", "30"))


class FStoreDuckDBClient:
    """fstore DuckDB 只读客户端，接口对齐 FStoreClient。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or FSTORE_DUCKDB_PATH
        self._conn: Any = None
        self._available: bool | None = None
        self._unavailable_until: float | None = None
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """关闭当前连接，下次查询重新解析 generation 快照。

        fstore 长连接只连一次、不跟随 generation 切换：进程跑很久后，engine 发布
        了新 generation 但连接还指向旧快照，instruments/财务等元数据查询会读到
        旧数据（甚至因旧 generation 被回收而返回空）。盘后管道开始前调用本方法，
        强制重建连接到当前 generation。
        """
        self.close()

    def close(self) -> None:
        """关闭当前连接；幂等。后续查询会重新解析 generation 快照。"""
        with self._lock:
            self._reset_conn_locked()

    def _reset_conn_locked(self) -> None:
        """锁内：关闭并清空当前连接，复位可用/退避状态（不施加退避）。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        self._available = None
        self._unavailable_until = None

    def _get_conn(self):
        # 在锁内完成"是否需要重连"的判断，避免并发查询同时触发紧密重连。
        with self._lock:
            return self._get_conn_locked()

    def _get_conn_locked(self):
        # 复用已建立的连接。
        if self._conn is not None and self._available is not False:
            return self._conn
        # 初始连接失败后的固定退避窗口：窗口内不重试（避免紧密重连和日志风暴）。
        if self._unavailable_until is not None:
            if time.monotonic() < self._unavailable_until:
                return None
            # 退避窗口已过：允许重连。
            self._unavailable_until = None
            self._available = None

        try:
            from app.storage.duckdb_runtime import connect_duckdb
        except ImportError:
            logger.warning("FStoreDuckDBClient: duckdb 未安装")
            self._mark_unavailable_locked()
            return None
        # Prefer the published generation snapshot for the raw path; a
        # not-yet-published root falls back to the raw path itself unchanged.
        main_path = snapshot_or_raw(self._path)
        if not os.path.exists(main_path):
            logger.warning("FStoreDuckDBClient: 文件不存在 %s", main_path)
            self._mark_unavailable_locked()
            return None
        conn: Any = None
        try:
            conn = connect_duckdb(main_path, read_only=True)
            self._attach(conn, "fstore_markets", FSTORE_MARKETS_DUCKDB_PATH, main_path)
            self._attach(conn, "fstore_klines", FSTORE_KLINES_DUCKDB_PATH, main_path)
            self._attach(conn, "fstore_minutes", FSTORE_MINUTES_DUCKDB_PATH, main_path)
            self._attach(conn, "fstore_extended", FSTORE_EXTENDED_DUCKDB_PATH, main_path)
            self._create_temp_views(conn)
        except Exception as e:  # noqa: BLE001
            # ATTACH/视图设置在 _conn 发布前失败时，必须关闭已打开的本地连接，
            # 避免泄漏 database-instance 级别的连接句柄。
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            logger.warning("FStoreDuckDBClient: 打开失败 %s — %s", main_path, e)
            self._mark_unavailable_locked()
            return None
        self._conn = conn
        self._available = True
        self._unavailable_until = None
        logger.info("FStoreDuckDBClient: 已打开 %s（只读）", main_path)
        return conn

    def _mark_unavailable_locked(self) -> None:
        """锁内：记录一次初始连接失败，进入固定退避窗口；窗口过后允许重连。"""
        self._available = False
        self._unavailable_until = time.monotonic() + RECONNECT_BACKOFF_SECONDS

    def _attach(self, conn: Any, alias: str, path: str, main_path: str) -> None:
        path = snapshot_or_raw(path)
        if not path or os.path.abspath(path) == os.path.abspath(main_path) or not os.path.exists(path):
            return
        escaped = path.replace("'", "''")
        try:
            conn.execute(f"ATTACH '{escaped}' AS {alias} (READ_ONLY)")
        except Exception as e:  # noqa: BLE001
            # DuckDB ATTACH 别名是 database-instance 级别而非 connection 级别:
            # 多个 FStoreDuckDBClient 连同一个 fstore.duckdb 会共享底层实例,
            # 第二次 ATTACH 同名别名会冲突。忽略 "already exists" —— 该别名
            # 已由同实例的第一个连接 ATTACH，当前连接可直接引用。
            if "already exists" not in str(e):
                raise

    def _create_temp_views(self, conn: Any) -> None:
        # ponytail: connection-local aliases keep old SQL working after split files.
        self._create_split_alias(conn, "daily_markets", "fstore_markets.daily_markets")
        self._create_split_alias(conn, "day_klines", "fstore_klines.day_klines")
        self._create_split_alias(conn, "minute_kline", "fstore_minutes.minute_kline")
        self._create_split_alias(conn, "chuquan_chuxi", "fstore_extended.chuquan_chuxi")
        # financial_report_* 物理表在 fstore-extended.duckdb（已从 fstore.duckdb 迁出），
        # 为 provider.get_financial 的裸表名查询建别名。
        for tbl in (
            "financial_report_income_statement",
            "financial_report_balance_sheet",
            "financial_report_cash_flow",
            "financial_report_annual",
            "financial_report_quick",
            "financial_report_forecast",
            "financial_report_dividend",
            "financial_report_schedule",
        ):
            self._create_split_alias(conn, tbl, f"fstore_extended.{tbl}")
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
            # 查询异常通常意味着连接已失效：释放它，使下次调用可重连（不施加退避）。
            with self._lock:
                self._reset_conn_locked()
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
