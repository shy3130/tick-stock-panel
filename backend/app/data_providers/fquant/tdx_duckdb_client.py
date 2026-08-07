"""TDX 行情 DuckDB 只读客户端 —— 覆盖 get_day/get_wide/get_minutes/get_trans/get_xdxr。

## 命名（重要，别被 "engine-data" 误导）

这个上游**早就不是 HTTP 服务了**：它读的是本地磁盘上的 tdx*.duckdb 文件，没有任何
网络调用。历史上它叫 `engine-data`（一个 HTTP 日 K 主源），迁到 DuckDB 后类名/文件名
已改成 `TdxDuckDBClient` / `tdx_duckdb_client.py`。

但 `engine-data` 这个名字在两个地方**故意保留**，因为它们是数据值而不是代码符号：
  1. `fallback.py` 的降级链标识（`"engine-data:wide"` 等）——链路契约；
  2. 归一化输出的 `source` 字段，**会落进 parquet**。改名会让同一列历史/新数据
     出现两种取值，读侧要额外做等价映射,不值当。
方法 docstring 里"沿用 xxx 数据集契约"指的就是这套历史字段形状。

## 数据文件

分别打开 A 股与港股拆分后的独立文件（不做跨库 ATTACH，因为没有跨表 join 需求）：
- /Volumes/WD1/duckdb/tdx.duckdb          -> market_day_kline / market_wide_kline / market_xdxr
- engine catalog tdx_minutes/a     -> market_minutes（按日期路由快照）
- engine catalog tdx_trans/a       -> market_transactions（历史归档按年、活跃年按月的日期路由快照）
- /Volumes/WD1/duckdb/tdx-hk.duckdb        -> market_day_kline(dataset='hkday')
- /Volumes/WD1/duckdb/tdx-hkminutes.duckdb -> market_minutes(dataset='hkminutes')
- /Volumes/WD1/duckdb/tdx-hktrans.duckdb   -> market_transactions(dataset='hktrans')
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime

from app.data_providers.fquant import catalog_resolver, generation
from app.data_providers.fquant.lease import ConnectionSet

logger = logging.getLogger(__name__)

TDX_PATH = os.getenv("FQUANT_TDX_DUCKDB_PATH", "/Volumes/WD1/duckdb/tdx.duckdb")
# HK 三库默认指向 raw：_LeasedSource._resolve 先按 logical（tdx_hk/tdx_hk_minutes/
# tdx_hk_trans）解析 engine-hk generation 快照（最新，immutable 只读）；只有快照未
# 发布时才回退到 raw 本身。旧的 ``*-web.duckdb`` 已停更，不作默认 raw_path。
TDX_HK_PATH = os.getenv("FQUANT_TDX_HK_DUCKDB_PATH", "/Volumes/WD1/duckdb/tdx-hk.duckdb")
TDX_HK_MINUTES_PATH = os.getenv("FQUANT_TDX_HK_MINUTES_DUCKDB_PATH", "/Volumes/WD1/duckdb/tdx-hkminutes.duckdb")
TDX_HK_TRANS_PATH = os.getenv("FQUANT_TDX_HK_TRANS_DUCKDB_PATH", "/Volumes/WD1/duckdb/tdx-hktrans.duckdb")
# A 股日级资金流派生库（moneyflow_daily_stock / moneyflow_daily_block）。
# _LeasedSource 按 logical=tdx_moneyflow 解析 engine-a generation 快照。
TDX_MONEYFLOW_PATH = os.getenv("FQUANT_TDX_MONEYFLOW_DUCKDB_PATH", "/Volumes/WD1/duckdb/tdx-moneyflow.duckdb")

# side 直接就是 HTTP 契约的 direction 编码（已实测核实，取值 {0,1,2,5,8}，另外
# 实测还发现了极少量的 3，规模量级 <1万行/总量 9亿+行，同样直接透传不做映射），
# 不需要映射表，get_trans 里直接透传。

# A 股/ETF/指数 代码段 -> 交易所前缀。market_day_kline/market_wide_kline/market_xdxr/
# market_minutes/market_transactions 的 code 列都带这个前缀（如 sh600519），
# 而 FQuantProvider 传进来的 code 是裸代码（如 600519，来自 symbol_to_code）。
_PREFIX_BY_HEAD = {
    # 沪市: 主板/科创板/B股
    "60": "sh", "68": "sh", "90": "sh",
    # 沪市 ETF (51x/50x/52x/56x/58x)
    "51": "sh", "50": "sh", "52": "sh", "56": "sh", "58": "sh",
    # 深市: 主板/创业板/B股
    "00": "sz", "30": "sz", "20": "sz",
    # 深市 ETF (15x/16x/17x/18x)
    "15": "sz", "16": "sz", "17": "sz", "18": "sz",
    # 深证指数 (39x)
    "39": "sz",
    # 北交所
    "43": "bj", "83": "bj", "87": "bj", "92": "bj", "88": "bj", "89": "bj",
}

# 00 开头的 code 有歧义: 股票(000001.SZ=平安银行) vs 指数(000001.SH=上证指数)。
# 仅凭 code 前两位无法区分，需要 asset_type 消歧。
_INDEX_HEAD_OVERRIDES = {"00": "sh", "39": "sz"}


def _prefixed_code(code: str, asset_type: str | None = None) -> str:
    """裸 6 位 code -> 带交易所前缀的 TDX code。

    asset_type 用于消歧: 00 开头的 code 在 index 语境下是沪市指数(上证系列),
    在 stock 语境下是深市股票(000xxx)。不传 asset_type 时按股票处理(向后兼容)。
    """
    code = code.strip()
    if len(code) != 6:
        return code
    head = code[:2]
    if asset_type and asset_type.strip().lower() == "index":
        override = _INDEX_HEAD_OVERRIDES.get(head)
        if override:
            return override + code
    return _PREFIX_BY_HEAD.get(head, "") + code if head in _PREFIX_BY_HEAD else code


def _hk_code(code: str) -> str:
    code = code.strip().lower()
    if code.startswith("hk"):
        return code
    return f"hk{code.zfill(5)}"


def _is_hk(asset_type: str | None) -> bool:
    return (asset_type or "").strip().lower() == "hk"


def _a_share_wide_volume(wide_volume, day_volume):
    """A 股 get_wide 对外 volume 的单位归一：统一以「股」为准。

    market_wide_kline.volume 多数交易日就是正确的股数，但实测存在「部分导入」异常日：
    成交额(amount)正确，volume（及 inner/outer_volume 同比例）只有真实值的若干成
    （sh600519 2026-07-14 = 31%、2026-07-15 = 61%）——engine 侧 wide 表导入流水线的
    上游数据质量问题，本客户端无法修源头。market_day_kline.volume（dataset='day'，
    官方日线成交量）始终是完整股数（全历史仅 1 例 amount/[high,low] 违例，且为
    high==low 涨停一字板的浮点边界），因此作为权威值。day_volume 缺失（LEFT JOIN 未
    命中）时回退 wide_volume——仍是「股」，仅个别异常日不准。港股走 _get_hk_day 的
    ×10000 路径，不经过这里。
    """
    if day_volume is not None:
        return day_volume
    return wide_volume


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
                from app.storage.duckdb_runtime import connect_duckdb
            except ImportError:
                self._duckdb_missing = True
                return None
            self._set = ConnectionSet(lambda p: connect_duckdb(p, read_only=True))
        return self._set

    @contextmanager
    def lease(self):
        cs = self._ensure_set()
        if cs is None:
            yield None
            return
        path = self._resolve()
        if path is None:
            logger.warning("TdxDuckDBClient: 文件不存在 logical=%s raw=%s", self._logical, self._raw_path)
            yield None
            return
        try:
            cm = cs.lease(path)
            conn = cm.__enter__()
        except Exception as e:  # noqa: BLE001
            logger.warning("TdxDuckDBClient: 打开失败 %s — %s", path, e)
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
                logger.warning("TdxDuckDBClient: %s 查询失败 — %s", label, e)
                return []

    def close(self) -> None:
        if self._set is not None:
            self._set.close()


class _CatalogSource:
    """Date-routed source backed by path-keyed read-only connections."""

    def __init__(self, route_key: str, market: str) -> None:
        self._route_key = route_key
        self._market = market
        self._set: ConnectionSet | None = None
        self._duckdb_missing = False

    def _ensure_set(self) -> ConnectionSet | None:
        if self._duckdb_missing:
            return None
        if self._set is None:
            try:
                from app.storage.duckdb_runtime import connect_duckdb
            except ImportError:
                self._duckdb_missing = True
                return None
            self._set = ConnectionSet(lambda path: connect_duckdb(path, read_only=True))
        return self._set

    def query(self, sql: str, params: list, date_yyyymmdd: str) -> list:
        """Resolve the date on every query and never fall back to a raw file."""
        try:
            trade_date = datetime.strptime(date_yyyymmdd, "%Y%m%d").date()
        except ValueError as exc:
            logger.warning(
                "TdxDuckDBClient: invalid trade date %r — %s", date_yyyymmdd, exc
            )
            return []
        connection_set = self._ensure_set()
        if connection_set is None:
            raise catalog_resolver.CatalogError("duckdb module is unavailable")
        try:
            path = catalog_resolver.resolve_route(
                self._route_key, self._market, trade_date
            )
        except catalog_resolver.CatalogError:
            # RouteNotFoundError 和 StaleCatalogError 现在都原样上抛 (fail-closed)。
            # 这满足分钟核心契约：多日 A股 中间日缺失 route 或 stale 必须可见。
            # sync_minute_batch 不再吞, 会传播给 API 映射 503。
            raise
        try:
            with connection_set.lease(path) as connection:
                return connection.cursor().execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise catalog_resolver.CatalogError(
                f"catalog query failed {path}: {exc}"
            ) from exc

    def close(self) -> None:
        if self._set is not None:
            self._set.close()


class TdxDuckDBClient:
    """只读打开整库快照，并按交易日从 catalog 解析 A 股 minutes/trans。"""

    def __init__(
        self,
        tdx_path: str | None = None,
        hk_path: str | None = None,
        hk_minutes_path: str | None = None,
        hk_trans_path: str | None = None,
        moneyflow_path: str | None = None,
    ) -> None:
        self._tdx = _LeasedSource("tdx", tdx_path or TDX_PATH)
        self._a_catalog_minutes = _CatalogSource("tdx_minutes", "a")
        self._a_catalog_trans = _CatalogSource("tdx_trans", "a")
        self._moneyflow = _LeasedSource("tdx_moneyflow", moneyflow_path or TDX_MONEYFLOW_PATH)
        self._hk = _LeasedSource("tdx_hk", hk_path or TDX_HK_PATH)
        self._hk_minutes = _LeasedSource("tdx_hk_minutes", hk_minutes_path or TDX_HK_MINUTES_PATH)
        self._hk_trans = _LeasedSource("tdx_hk_trans", hk_trans_path or TDX_HK_TRANS_PATH)

    def close(self) -> None:
        for src in (
            self._a_catalog_minutes,
            self._a_catalog_trans,
            self._tdx,
            self._moneyflow,
            self._hk,
            self._hk_minutes,
            self._hk_trans,
        ):
            src.close()

    def _a_minutes_source(self, date_yyyymmdd: str) -> _CatalogSource:
        return self._a_catalog_minutes

    def _a_trans_source(self, date_yyyymmdd: str) -> _CatalogSource:
        return self._a_catalog_trans

    def get_day(self, code: str, limit: int = 250) -> list[dict]:
        """读 market_day_kline（dataset='day'），字段沿用 day 数据集契约（命名见模块头）。"""
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
        """读 market_wide_kline，字段沿用 wide 数据集契约（命名见模块头）。

        market_wide_kline 没有 datetime/adjustment_count 两列（market_day_kline 有），
        这里固定填 None/0——调用方的字段归一函数需要能容忍这两个字段缺失。

        volume 口径：market_wide_kline.volume 多数交易日是正确的「股」，但实测存在
        「部分导入」异常日——amount 正确、volume（及 inner/outer_volume）只有真实值
        的若干成（sh600519 2026-07-14 = 31%、2026-07-15 = 61%）。这是 engine 侧 wide
        表导入流水线的上游数据质量问题。market_day_kline.volume（dataset='day'，官方
        日线成交量，始终为完整股数）是权威值，因此 get_wide LEFT JOIN 它并以
        _a_share_wide_volume 归一：优先取 day_kline 的股数，未命中才回退 wide.volume。
        港股走 _get_hk_day 的 ×10000 路径，不经过这里——A/HK 对外 volume 统一为「股」。

        已确认 market_wide_kline 相对 market_day_kline 和 HTTP 路径存在约 2 个交易日
        的稳定滞后（表级导入延迟，非单个代码的问题），因此 get_wide 的结果可能缺少
        近期交易日的数据，即使这些数据在 get_day 或 HTTP 路径中已存在——这是 engine
        仓库数据导入流水线的上游问题，本客户端无法修复。
        """
        if _is_hk(asset_type):
            return self._get_hk_day(code, limit)
        rows = self._tdx.query(
            """
            SELECT w.trade_date, w.open, w.close, w.high, w.low, w.volume, w.amount,
                   w.up_count, w.down_count, w.last_close, w.change_rate,
                   w.open_volume, w.open_turnz, w.open_unmatched,
                   w.close_volume, w.close_turnz, w.close_unmatched,
                   w.inner_volume, w.outer_volume, w.inner_amount, w.outer_amount,
                   d.volume AS day_volume
            FROM market_wide_kline w
            LEFT JOIN (
                SELECT code, trade_date, volume
                FROM market_day_kline
                WHERE dataset = 'day' AND code = ?
            ) d ON d.trade_date = w.trade_date
            WHERE w.code = ?
            ORDER BY w.trade_date DESC
            LIMIT ?
            """,
            [_prefixed_code(code, asset_type), _prefixed_code(code, asset_type), limit],
            "get_wide",
        )
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None, "datetime": None,
                "open": r[1], "close": r[2], "high": r[3], "low": r[4],
                "volume": _a_share_wide_volume(r[5], r[21]), "amount": r[6],
                "up": r[7], "down": r[8], "adjustment_count": 0,
                "last_close": r[9], "change_rate": r[10],
                "open_volume": r[11], "open_turnz": r[12], "open_unmatched": r[13],
                "close_volume": r[14], "close_turnz": r[15], "close_unmatched": r[16],
                "inner_volume": r[17], "outer_volume": r[18], "inner_amount": r[19], "outer_amount": r[20],
            }
            for r in rows
        ]

    # tdx-hk.duckdb 的 market_day_kline.volume 存的是「手」，不是「股」，与 A 股对外
    # 口径（股，见 get_wide → _a_share_wide_volume 的归一）不一致。实测 hk00700
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
        """读 market_minutes，字段沿用 minutes 数据集契约（命名见模块头）（price/volume）。

        market_minutes 的 time/amount 两列全表 34 亿+行全是 NULL（已实测确认），
        只有 price/volume/minute_index 有真实数据，这也是为什么只选 price/volume
        两列、靠 minute_index 排序——不要改成查 time 列，查了也是 None。
        """
        hk = _is_hk(asset_type)
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        sql = """
            SELECT price, volume
            FROM market_minutes
            WHERE code = ? AND trade_date = ? AND dataset = ?
            ORDER BY minute_index
            LIMIT ?
            """
        params = [
            _hk_code(code) if hk else _prefixed_code(code),
            trade_date,
            "hkminutes" if hk else "minutes",
            limit,
        ]
        rows = (
            self._hk_minutes.query(sql, params, "get_minutes")
            if hk
            else self._a_minutes_source(date_yyyymmdd).query(
                sql, params, date_yyyymmdd
            )
        )
        return [{"price": r[0], "volume": r[1]} for r in rows]

    def get_trans(self, code: str, date_yyyymmdd: str, limit: int = 5000, asset_type: str | None = None) -> list[dict]:
        """读 market_transactions，字段沿用 trans 数据集契约（命名见模块头）。

        market_transactions 没有 order_count 列，这里固定填 None——
        调用方 trans_rows_to_df 需要能容忍这一列缺失/为空。
        """
        hk = _is_hk(asset_type)
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        sql = """
            SELECT time, price, volume, amount, side
            FROM market_transactions
            WHERE code = ? AND trade_date = ? AND dataset = ?
            ORDER BY time
            LIMIT ?
            """
        params = [
            _hk_code(code) if hk else _prefixed_code(code),
            trade_date,
            "hktrans" if hk else "trans",
            limit,
        ]
        rows = (
            self._hk_trans.query(sql, params, "get_trans")
            if hk
            else self._a_trans_source(date_yyyymmdd).query(
                sql, params, date_yyyymmdd
            )
        )
        return [
            {
                "time": r[0], "price": r[1], "volume": r[2], "amount": r[3],
                "order_count": None, "direction": r[4],
            }
            for r in rows
        ]

    def get_xdxr(self, code: str, limit: int = 100, asset_type: str | None = None) -> list[dict]:
        """读 market_xdxr，字段沿用 xdxr 数据集契约（命名见模块头）。

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
        """读 tdx-moneyflow.duckdb.moneyflow_daily_stock 单日资金流。

        :param code: 裸代码（如 ``600519``），内部会加交易所前缀
        :param date_iso: ``YYYY-MM-DD`` 格式日期字符串
        :return: 含 main_net/total_net/super_large_net/... 的 dict；
                 文件不可达或无数据时返回 {}（与历史 engine-data 契约一致）

        源表从旧的 ``tdx.duckdb.market_fund_flow``（残废，停在 2026-07-02 / 33 行）
        切换到独立派生库 ``tdx-moneyflow.duckdb.moneyflow_daily_stock``（日级完整，
        main_traditional_net = 传统主力 = 超大单+大单净额）。
        """
        rows = self._moneyflow.query(
            """
            SELECT main_traditional_net, main_broad_net, net_amount,
                   super_large_net, large_net, medium_net, small_net,
                   main_traditional_inflow, main_traditional_outflow
            FROM moneyflow_daily_stock
            WHERE code = ? AND trade_date = ?
            """,
            [_prefixed_code(code), date_iso],
            "get_fund_daily",
        )
        if not rows:
            return {}
        r = rows[0]
        main = float(r[0] or 0)
        return {
            "main_net": main,
            "total_net": float(r[2] or 0),
            "super_large_net": float(r[3] or 0),
            "large_net": float(r[4] or 0),
            "medium_net": float(r[5] or 0),
            "small_net": float(r[6] or 0),
            "main_inflow": float(r[7] or 0),
            "main_outflow": float(r[8] or 0),
            # 旧表有 ratio 列，新表无；下游 _to_float 容忍缺失
            "main_ratio": None,
            "super_large_ratio": None,
            "large_ratio": None,
            "medium_ratio": None,
            "small_ratio": None,
        }

    def get_fund_range(self, code: str, start_iso: str, end_iso: str):
        """读 tdx-moneyflow.duckdb.moneyflow_daily_stock 区间资金流，返回 polars DataFrame。

        契约沿用 fund_range：只返回 ["date", "main_net_inflow"] 两列。
        main_net_inflow 取 main_traditional_net（传统主力净额 = 超大单+大单）。

        :param code: 裸代码（如 ``600519``），内部会加交易所前缀
        :param start_iso: ``YYYY-MM-DD`` 格式起始日期（含）
        :param end_iso: ``YYYY-MM-DD`` 格式结束日期（含）
        :return: polars DataFrame，列为 date/main_net_inflow；
                 文件不可达或无数据时返回空 DataFrame
        """
        import polars as pl

        rows = self._moneyflow.query(
            """
            SELECT trade_date::TEXT AS date, main_traditional_net AS main_net_inflow
            FROM moneyflow_daily_stock
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [_prefixed_code(code), start_iso, end_iso],
            "get_fund_range",
        )
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame({"date": [r[0] for r in rows], "main_net_inflow": [r[1] for r in rows]})

    def freshness(self):
        """最新已发布交易日的探测值，供 local enriched bootstrap 判定新鲜度。

        返回 market_day_kline(dataset='day') 的 max(trade_date)。文件不可达时
        返回 None（调用方据此跳过 bootstrap，不会误判）。
        """
        from datetime import date as _date
        rows = self._tdx.query(
            "SELECT max(trade_date) FROM market_day_kline WHERE dataset = 'day'",
            [],
            "freshness",
        )
        if rows and rows[0][0]:
            d = rows[0][0]
            return d.date() if hasattr(d, "date") else d
        return None
