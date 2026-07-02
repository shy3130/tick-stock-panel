"""fstore PostgreSQL 直连客户端（§4.1 / §4.10）。

依赖：``psycopg[binary]`` v3.x（已装）；装不上则回退 ``psycopg2-binary``（N5 约束）。
不引入 sqlalchemy / asyncpg。

错误降级（§7.2）：
- L2：连接失败 → ``available=False``，后续查询直接返回空，不影响其它子客户端
- L5：import 失败 → 标记不可用，warning 日志

配置（§6.1）：
- ``FQUANT_DB_HOST`` / ``FSTORE_DATABASE_HOST``（默认 ``pve.wf``）
- ``FQUANT_DB_PORT`` / ``FSTORE_DATABASE_PORT``（默认 ``5432``）
- ``FQUANT_DB_USER`` / ``FSTORE_DATABASE_USERNAME``（默认 ``postgresql``）
- ``FQUANT_DB_PASSWORD`` / ``FSTORE_DATABASE_PASSWORD``（必填，从 ``fquant/.env`` 读）
- ``FQUANT_DB_NAME`` / ``FSTORE_DATABASE_DATABASE``（默认 ``fstore``）
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 配置（纯环境变量读取，不硬编码密码 — §6.3 约定）
# --------------------------------------------------------------------------- #
FSTORE_HOST = os.getenv("FQUANT_DB_HOST") or os.getenv("FSTORE_DATABASE_HOST", "pve.wf")
FSTORE_PORT = int(os.getenv("FQUANT_DB_PORT") or os.getenv("FSTORE_DATABASE_PORT", "5432"))
FSTORE_USER = os.getenv("FQUANT_DB_USER") or os.getenv("FSTORE_DATABASE_USERNAME", "postgresql")
FSTORE_PASSWORD = os.getenv("FQUANT_DB_PASSWORD") or os.getenv("FSTORE_DATABASE_PASSWORD", "")
FSTORE_DBNAME = os.getenv("FQUANT_DB_NAME") or os.getenv("FSTORE_DATABASE_DATABASE", "fstore")


class FStoreClient:
    """fstore PostgreSQL 直连客户端。

    懒加载连接，首次查询时建立。连接失败不影响其他子客户端。
    任一查询出错 → 重置连接以便下次重连（L2 行为）。
    """

    def __init__(self) -> None:
        self._conn: Any = None
        self._available: bool | None = None  # None=未检测, True/False=已检测

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def _get_conn(self):
        """获取（懒加载）连接。失败返回 None 并标记不可用。

        优先使用 psycopg v3；装不上回退 psycopg2-binary（N5 fallback）。
        """
        if self._available is False:
            return None
        if self._conn is not None:
            # psycopg v3 / psycopg2 都有 ``closed`` 属性
            try:
                if not self._conn.closed:
                    return self._conn
            except Exception:  # noqa: BLE001
                self._conn = None

        # 优先 psycopg v3
        conn = self._connect_psycopg3()
        if conn is None:
            conn = self._connect_psycopg2()
        if conn is None:
            self._available = False
            return None

        self._conn = conn
        self._available = True
        logger.info(
            "FStoreClient: 连接成功 %s@%s:%s/%s",
            FSTORE_USER, FSTORE_HOST, FSTORE_PORT, FSTORE_DBNAME,
        )
        return conn

    def _connect_psycopg3(self):
        """尝试用 psycopg v3 连接。失败返回 None。"""
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            conn = psycopg.connect(
                host=FSTORE_HOST,
                port=FSTORE_PORT,
                user=FSTORE_USER,
                password=FSTORE_PASSWORD,
                dbname=FSTORE_DBNAME,
                connect_timeout=5,
            )
            return conn
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreClient: psycopg3 连接失败 %s:%s — %s", FSTORE_HOST, FSTORE_PORT, e)
            return None

    def _connect_psycopg2(self):
        """尝试用 psycopg2-binary 连接（psycopg3 装不上时回退，N5）。"""
        try:
            import psycopg2  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "FStoreClient: psycopg/psycopg2 均未安装，DB 数据源不可用。"
                "请 pip install 'psycopg[binary]' 或 psycopg2-binary",
            )
            return None
        try:
            conn = psycopg2.connect(
                host=FSTORE_HOST,
                port=FSTORE_PORT,
                user=FSTORE_USER,
                password=FSTORE_PASSWORD,
                dbname=FSTORE_DBNAME,
                connect_timeout=5,
            )
            return conn
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreClient: psycopg2 连接失败 %s:%s — %s", FSTORE_HOST, FSTORE_PORT, e)
            return None

    @property
    def available(self) -> bool:
        """是否可用（已成功连接过）。"""
        return self._get_conn() is not None

    # ------------------------------------------------------------------ #
    # 查询接口
    # ------------------------------------------------------------------ #
    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        """执行 SELECT，返回 ``[{col: val, ...}, ...]``。失败返回空列表（L2）。

        Decimal/date 等特殊类型由调用方（mapping.py）负责转换；
        本方法只做 RealDictCursor 取数。
        """
        conn = self._get_conn()
        if conn is None:
            return []

        # psycopg v3 与 psycopg2 的 dict cursor 接口不同，分别处理
        try:
            if self._is_psycopg3(conn):
                return self._query_psycopg3(conn, sql, params)
            return self._query_psycopg2(conn, sql, params)
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreClient: 查询失败 — %s | SQL: %s", e, sql[:200])
            # 连接可能已坏，重置以便下次重连
            self._reset_conn()
            return []

    def _is_psycopg3(self, conn) -> bool:
        """判断当前连接是 psycopg v3 还是 psycopg2。"""
        try:
            import psycopg  # type: ignore[import-not-found]
            return isinstance(conn, psycopg.Connection)
        except ImportError:
            return False

    def _query_psycopg3(self, conn, sql: str, params) -> list[dict]:
        """psycopg v3 查询。"""
        import psycopg  # type: ignore[import-not-found]
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]

    def _query_psycopg2(self, conn, sql: str, params) -> list[dict]:
        """psycopg2 查询（回退路径）。"""
        import psycopg2.extras  # type: ignore[import-not-found]
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]

    def _reset_conn(self) -> None:
        """关闭并清空当前连接（L2 重连）。"""
        try:
            if self._conn:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
        # 注意：不重置 _available=False，允许下次自动重连
