"""财务数据独立同步服务。

解耦于 K-line 管道, 自有调度 + 自有存储。
能力门控: Cap.FINANCIAL

数据获取通过 data_providers 抽象层,支持 provider 切换。
- ``fquant``/``fquant_local``: 通过 FQuantProvider.get_financial() 直连 fstore financial_report_* 表
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.data_providers.registry import get_active_provider_name, get_provider
from app.capabilities import Cap, CapabilitySet

logger = logging.getLogger(__name__)

# 每个 API 请求最多 100 个标的
_BATCH_SIZE = 100

# 财务表(对外/调度器层面沿用旧名称,不改函数签名)
FINANCIAL_TABLES = (
    "metrics",
    "income",
    "balance_sheet",
    "cash_flow",
    "quick",
    "forecast",
)

# 业务表名 → provider.get_financial(table=...) 参数。
# FQuantProvider.get_financial() 接受: income / balance_sheet / cash_flow / annual / quick / forecast
# (见 backend/app/data_providers/fquant_provider.py 的 _FINANCIAL_TABLE_MAP)。
# - "metrics" 在 provider 侧没有同名表,映射到最接近的 "annual"(年度核心指标 EPS/BPS/ROE/净利)。
# - 三张报表、快报、预告直接同名透传。
_PROVIDER_TABLE_MAP: dict[str, str] = {
    "metrics": "annual",
    "income": "income",
    "balance_sheet": "balance_sheet",
    "cash_flow": "cash_flow",
    "quick": "quick",
    "forecast": "forecast",
}

_EASTMONEY_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# 数据源 provider 单例缓存
_provider_instance = None


def _get_data_provider():
    """获取当前配置的数据源 provider。

    通过 registry 解析当前 provider。
    与 ``app.services.kline_sync._get_data_provider`` 同模式。
    """
    global _provider_instance
    if _provider_instance is None:
        provider_name = get_active_provider_name("financial")
        _provider_instance = get_provider(provider_name)
        logger.info("financial data provider initialized: %s", provider_name)
    return _provider_instance


# ================================================================
# 同步函数
# ================================================================

def _get_symbols(data_dir: Path) -> list[str]:
    """从 instruments 表获取标的列表。"""
    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return []
    try:
        df = pl.read_parquet(inst_path, columns=["symbol"])
        return df["symbol"].to_list()
    except Exception as e:
        logger.warning("读取 instruments 失败: %s", e)
        return []


def _sync_table(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    latest_only: bool = True,
) -> int:
    """同步单张财务表。返回写入的行数。

    通过 ``data_providers`` 抽象层取数(与 ``kline_sync.py`` 同模式):
    - ``provider.get_financial(symbol, table=...)`` 接受**单个 symbol**,返回归一化 Polars DF,
      列含 ``symbol/t_date/...<source cols>.../notice_date``(见 fquant_provider.py)。
    - 本函数逐 symbol 调用并 concat,降级:provider 异常或返回空 → 跳过该 symbol。
    - ``latest_only`` 参数保留在签名中以保持向后兼容;provider 内部默认返回最近 N 条
      (FQuantProvider.get_financial 内部 ``LIMIT 50``),与旧 ``latest=True`` 语义一致。
    """
    if not capset.has(Cap.FINANCIAL):
        logger.info("sync_%s skipped: no FINANCIAL capability", table)
        return 0
    if not symbols:
        logger.warning("sync_%s skipped: no symbols", table)
        return 0

    provider = _get_data_provider()

    # 业务表名 → provider.get_financial() 的 table 参数
    provider_table = _PROVIDER_TABLE_MAP.get(table, table)

    # 用 getattr 兜底:不存在 → 直接返回 0 行,使本函数优雅降级,不抛异常。
    get_financial = getattr(provider, "get_financial", None)
    if get_financial is None:
        logger.warning(
            "sync_%s skipped: provider %s 未实现 get_financial",
            table, getattr(provider, "name", type(provider).__name__),
        )
        return 0

    all_frames: list[pl.DataFrame] = []

    for sym in symbols:
        try:
            df = get_financial(sym, provider_table)
        except Exception as e:  # noqa: BLE001
            logger.warning("sync_%s: provider.get_financial(%s) failed: %s", table, sym, e)
            continue

        if df is None or len(df) == 0:
            continue
        if not isinstance(df, pl.DataFrame):
            # provider 契约要求返回 polars DF;防御性兜底:pandas → polars
            try:
                df = pl.from_pandas(df)
            except Exception as e:  # noqa: BLE001
                logger.warning("sync_%s: provider returned non-DataFrame for %s: %s", table, sym, e)
                continue

        # 确保 symbol 列存在(provider 已带,但作为安全网)
        if "symbol" not in df.columns:
            df = df.with_columns(pl.lit(sym).alias("symbol"))
        all_frames.append(df)

    if not all_frames:
        return 0

    df = pl.concat(all_frames, how="diagonal_relaxed")
    if df.is_empty():
        return 0

    # 确保 symbol 列存在(concat 后再确认一次,防御性)
    if "symbol" not in df.columns:
        return 0

    # 写入 Parquet (全量覆盖,保留原有 repository 写入逻辑)
    out_dir = data_dir / "financials" / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "part.parquet"
    df.write_parquet(out_file)

    logger.info("sync_%s done: %d records written (%d symbols)", table, len(df), len(symbols))
    return len(df)


def sync_metrics(data_dir: Path, capset: CapabilitySet) -> int:
    """同步核心财务指标 (metrics)。"""
    symbols = _get_symbols(data_dir)
    return _sync_table("metrics", symbols, data_dir, capset, latest_only=True)


def sync_income(data_dir: Path, capset: CapabilitySet) -> int:
    """同步利润表。"""
    symbols = _get_symbols(data_dir)
    return _sync_table("income", symbols, data_dir, capset, latest_only=True)


def sync_balance_sheet(data_dir: Path, capset: CapabilitySet) -> int:
    """同步资产负债表。"""
    symbols = _get_symbols(data_dir)
    return _sync_table("balance_sheet", symbols, data_dir, capset, latest_only=True)


def sync_cash_flow(data_dir: Path, capset: CapabilitySet) -> int:
    """同步现金流量表。"""
    symbols = _get_symbols(data_dir)
    return _sync_table("cash_flow", symbols, data_dir, capset, latest_only=True)


def sync_quick(data_dir: Path, capset: CapabilitySet) -> int:
    """同步业绩快报。"""
    if not capset.has(Cap.FINANCIAL):
        logger.info("sync_quick skipped: no FINANCIAL capability")
        return 0
    symbols = _get_symbols(data_dir)
    rows = _sync_table("quick", symbols, data_dir, capset, latest_only=True)
    existing = get_financial_df(data_dir, "quick") if rows else pl.DataFrame()
    try:
        merged_rows = _sync_quick_from_eastmoney(data_dir, capset, existing=existing)
    except Exception as e:  # noqa: BLE001
        logger.warning("sync_quick eastmoney fallback failed: %s", e)
        return rows
    return merged_rows or rows


def sync_forecast(data_dir: Path, capset: CapabilitySet) -> int:
    """同步业绩预告。"""
    if not capset.has(Cap.FINANCIAL):
        logger.info("sync_forecast skipped: no FINANCIAL capability")
        return 0
    if _forecast_fstore_has_rows():
        symbols = _get_symbols(data_dir)
        rows = _sync_table("forecast", symbols, data_dir, capset, latest_only=True)
        if rows:
            return rows
    return _sync_forecast_from_eastmoney(data_dir, capset)


def _forecast_fstore_has_rows() -> bool:
    try:
        provider = _get_data_provider()
        fstore = getattr(provider, "_fstore", None)
        query = getattr(fstore, "query", None)
        if query is None:
            return False
        return bool(query("SELECT 1 FROM financial_report_forecast LIMIT 1", ()))
    except Exception as e:  # noqa: BLE001
        logger.warning("check financial_report_forecast failed: %s", e)
        return False


def _recent_report_dates(today: date | None = None) -> list[str]:
    today = today or date.today()
    quarters = [(3, 31), (6, 30), (9, 30), (12, 31)]
    dates: list[date] = []
    for year in (today.year, today.year - 1):
        for month, day in quarters:
            item = date(year, month, day)
            if item <= today:
                dates.append(item)
    return [d.isoformat() for d in sorted(dates, reverse=True)]


def _eastmoney_symbol(row: dict[str, Any]) -> str:
    secucode = row.get("SECUCODE")
    if secucode:
        return str(secucode)
    code = str(row.get("SECURITY_CODE") or "")
    market = str(row.get("TRADE_MARKET_CODE") or "")
    if market.startswith("069001002"):
        return f"{code}.SZ"
    if market.startswith("069001001"):
        return f"{code}.SH"
    return code


def _normalize_quick_rows(rows: list[dict[str, Any]]) -> pl.DataFrame:
    out: list[dict[str, Any]] = []
    for row in rows:
        report_date = str(row.get("REPORT_DATE") or "").split(" ")[0] or None
        notice_date = str(row.get("UPDATE_DATE") or row.get("NOTICE_DATE") or "").split(" ")[0] or None
        out.append({
            **row,
            "symbol": _eastmoney_symbol(row),
            "t_date": report_date,
            "report_date": report_date,
            "notice_date": notice_date,
            "source": "eastmoney:quick",
            "basic_eps": row.get("BASIC_EPS"),
            "total_income": row.get("TOTAL_OPERATE_INCOME"),
            "net_profit": row.get("PARENT_NETPROFIT"),
            "bps": row.get("PARENT_BVPS"),
            "weight_avg_roe": row.get("WEIGHTAVG_ROE"),
            "yoy_income": row.get("YSTZ"),
            "yoy_profit": row.get("JLRTBZCL"),
            "qoq_income": row.get("DJDYSHZ"),
            "qoq_profit": row.get("DJDJLHZ"),
        })
    return pl.DataFrame(out) if out else pl.DataFrame()


def _normalize_forecast_rows(rows: list[dict[str, Any]]) -> pl.DataFrame:
    out: list[dict[str, Any]] = []
    for row in rows:
        report_date = str(row.get("REPORT_DATE") or "").split(" ")[0] or None
        notice_date = str(row.get("NOTICE_DATE") or "").split(" ")[0] or None
        out.append({
            **row,
            "symbol": _eastmoney_symbol(row),
            "t_date": report_date,
            "report_date": report_date,
            "notice_date": notice_date,
            "source": "eastmoney:forecast",
            "predict_type": row.get("PREDICT_TYPE"),
            "predict_content": row.get("PREDICT_CONTENT"),
            "change_reason": row.get("CHANGE_REASON_EXPLAIN"),
            "forecast_net_profit": row.get("FORECAST_JZ"),
            "net_profit_lower": row.get("PREDICT_AMT_LOWER"),
            "net_profit_upper": row.get("PREDICT_AMT_UPPER"),
        })
    return pl.DataFrame(out) if out else pl.DataFrame()


def _sync_quick_from_eastmoney(
    data_dir: Path,
    capset: CapabilitySet,
    *,
    existing: pl.DataFrame | None = None,
) -> int:
    if not capset.has(Cap.FINANCIAL):
        logger.info("sync_quick eastmoney skipped: no FINANCIAL capability")
        return 0

    from app.services import eastmoney_client

    frames: list[pl.DataFrame] = []
    for report_date in _recent_report_dates():
        rows = eastmoney_client.get_datacenter_paged(
            _EASTMONEY_DATACENTER,
            {
                "sortColumns": "UPDATE_DATE,SECURITY_CODE",
                "sortTypes": "-1,-1",
                "reportName": "RPT_FCI_PERFORMANCEE",
                "columns": "ALL",
                "filter": (
                    '(SECURITY_TYPE_CODE in ("058001001","058001008"))'
                    '(TRADE_MARKET_CODE!="069001017")'
                    f"(REPORT_DATE='{report_date}')"
                ),
                "source": "WEB",
                "client": "WEB",
            },
            max_pages=20,
        )
        df = _normalize_quick_rows(rows)
        if df.is_empty():
            continue
        frames.append(df)

    if existing is not None and not existing.is_empty():
        frames.insert(0, existing)
    if not frames:
        return 0

    df = pl.concat(frames, how="diagonal_relaxed")
    if {"symbol", "t_date"}.issubset(df.columns):
        df = df.unique(subset=["symbol", "t_date"], keep="first")

    out_dir = data_dir / "financials" / "quick"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_dir / "part.parquet")
    logger.info(
        "sync_quick eastmoney merge done: %d records across %d source frames",
        len(df),
        len(frames),
    )
    return len(df)


def _sync_forecast_from_eastmoney(data_dir: Path, capset: CapabilitySet) -> int:
    if not capset.has(Cap.FINANCIAL):
        logger.info("sync_forecast eastmoney skipped: no FINANCIAL capability")
        return 0

    from app.services import eastmoney_client

    frames: list[pl.DataFrame] = []
    for report_date in _recent_report_dates():
        rows = eastmoney_client.get_datacenter_paged(
            _EASTMONEY_DATACENTER,
            {
                "sortColumns": "NOTICE_DATE,SECURITY_CODE",
                "sortTypes": "-1,-1",
                "reportName": "RPT_PUBLIC_OP_NEWPREDICT",
                "columns": "ALL",
                "filter": (
                    '(SECURITY_TYPE_CODE in ("058001001","058001008"))'
                    '(TRADE_MARKET_CODE!="069001017")'
                    f"(REPORT_DATE='{report_date}')"
                ),
                "source": "WEB",
                "client": "WEB",
            },
            max_pages=20,
        )
        df = _normalize_forecast_rows(rows)
        if df.is_empty():
            continue
        frames.append(df)
    if not frames:
        return 0

    df = pl.concat(frames, how="diagonal_relaxed")
    out_dir = data_dir / "financials" / "forecast"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_dir / "part.parquet")
    logger.info(
        "sync_forecast eastmoney done: %d records across %d report dates",
        len(df),
        len(frames),
    )
    return len(df)


def sync_all(data_dir: Path, capset: CapabilitySet) -> dict[str, int]:
    """同步所有财务表。返回 {table: rows}。"""
    if not capset.has(Cap.FINANCIAL):
        logger.info("sync_all financials skipped: no FINANCIAL capability")
        return {}

    results: dict[str, int] = {}
    sync_functions = _financial_sync_functions()
    for table in FINANCIAL_TABLES:
        results[table] = sync_functions[table](data_dir, capset)

    # 同步完成后注册 DuckDB 视图
    _refresh_financials_views(data_dir)

    return results


def _financial_sync_functions():
    return {
        "metrics": sync_metrics,
        "income": sync_income,
        "balance_sheet": sync_balance_sheet,
        "cash_flow": sync_cash_flow,
        "quick": sync_quick,
        "forecast": sync_forecast,
    }


# ================================================================
# DuckDB 视图
# ================================================================

def _refresh_financials_views(data_dir: Path) -> None:
    """刷新财务表 DuckDB 视图 (在 DataStore.db 上注册)。"""
    d = data_dir.as_posix()
    views = {
        f"financials_{table}": f"{d}/financials/{table}/*.parquet"
        for table in FINANCIAL_TABLES
    }
    for name, path in views.items():
        out = data_dir / "financials" / name.replace("financials_", "") / "part.parquet"
        if not out.exists():
            continue
        # 视图注册需要由 DataStore 完成,这里只做日志
        logger.debug("financial parquet ready: %s (%d rows)", name, out.stat().st_size)


def get_financial_df(data_dir: Path, table: str) -> pl.DataFrame:
    """读取本地财务 Parquet。"""
    path = data_dir / "financials" / table / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("读取 financials/%s 失败: %s", table, e)
        return pl.DataFrame()


# ================================================================
# 调度器
# ================================================================

class FinancialScheduler:
    """独立调度器: 每周同步 metrics, 每季度同步三张报表。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._data_dir: Path | None = None
        self._capset: CapabilitySet | None = None
        self._lock = threading.Lock()
        self._last_sync: dict[str, str] = {}  # {table: iso_timestamp}
        # 手动同步(run_now)是否正在进行。前端据此显示"同步中"并防重复点击。
        self._is_syncing = False

    def start(self, data_dir: Path, capset: CapabilitySet, *, auto_schedule: bool = False) -> None:
        """初始化调度器，并按需启动周期同步后台任务。

        auto_schedule=False (默认): 仅初始化 (设置数据目录/能力 + 恢复 last_sync),
            供 /api/financials/sync/* 手动同步使用, 不启动自动调度。
        auto_schedule=True: 额外启动每周一次的 metrics 自动同步 (启动后 60s 首跑)。
        """
        # 先记录 data_dir/capset, 即使当前无 FINANCIAL 也保留引用:
        # 用户稍后在「设置」页刷新数据源能力时, update_capabilities() 会把新 capset
        # 推进来,trigger()/run_now() 才能用上 FINANCIAL。否则 _capset 永远是 None,
        # 即便 app.state.capabilities 已更新, 调度器仍报 "no FINANCIAL capability"。
        self._data_dir = data_dir
        self._capset = capset
        if not capset.has(Cap.FINANCIAL):
            logger.info("FinancialScheduler skipped: no FINANCIAL capability")
            return
        # 从持久化恢复上次同步时间: 重启后前端仍能显示真实最后同步时间,而非"尚未同步"
        try:
            from app.services import preferences
            restored = dict(preferences.get_financial_sync_times())
            # 老用户迁移兜底: 若某表在 preferences 无记录但 parquet 已存在(升级前同步过),
            # 用 parquet 文件的修改时间作为同步时间并补写持久化。
            for table in FINANCIAL_TABLES:
                if table in restored:
                    continue
                parquet = data_dir / "financials" / table / "part.parquet"
                if parquet.exists():
                    mtime = datetime.fromtimestamp(parquet.stat().st_mtime, tz=timezone.utc).isoformat()
                    restored[table] = mtime
                    preferences.set_financial_sync_time(table, mtime)
                    logger.info("FinancialScheduler backfilled last_sync for %s from parquet mtime", table)
            self._last_sync = restored
            if self._last_sync:
                logger.info("FinancialScheduler restored last_sync: %s", list(self._last_sync.keys()))
        except Exception as e:  # noqa: BLE001
            logger.warning("restore financial_sync_times failed: %s", e)

        if not auto_schedule:
            # 仅初始化 (手动同步用), 不启动周期任务。
            logger.info("FinancialScheduler initialized (auto-schedule disabled; manual sync only)")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("FinancialScheduler started (auto-schedule enabled)")

    def _record_sync(self, table: str) -> None:
        """记录一张表的同步完成时间: 更新内存 + 持久化到 preferences.json。

        持久化确保即使重启,前端 /status 仍返回真实的最后同步时间,
        不会错误地显示"尚未同步"。
        """
        ts = datetime.now(timezone.utc).isoformat()
        self._last_sync[table] = ts
        try:
            from app.services import preferences
            preferences.set_financial_sync_time(table, ts)
        except Exception as e:  # noqa: BLE001
            logger.warning("persist financial_sync_time(%s) failed: %s", e)

    def update_capabilities(self, capset: CapabilitySet) -> None:
        """刷新调度器持有的能力集。

        用户在「设置」页新增/清除 API Key 后, settings API 会重新探测能力并更新
        app.state.capabilities; 必须同步推给本调度器, 否则 trigger()/run_now() 仍读
        启动时的旧 capset, 即便 app.state 已含 FINANCIAL, 调度器仍报
        "no FINANCIAL capability" 而拒绝同步 (表现为前端「全部同步」按钮闪一下无动作)。
        """
        prev = self._capset
        self._capset = capset
        had = bool(prev) and prev.has(Cap.FINANCIAL)
        now = capset.has(Cap.FINANCIAL)
        if had != now:
            logger.info(
                "FinancialScheduler capabilities updated: FINANCIAL %s -> %s", had, now
            )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("FinancialScheduler stopped")

    async def _run_loop(self) -> None:
        """每周执行一次 metrics 同步。"""
        try:
            while self._running:
                # 首次启动等 60s, 之后每 7 天执行一次
                await asyncio.sleep(60)
                if not self._running:
                    break

                # 每周: 只同步 metrics
                try:
                    rows = sync_metrics(self._data_dir, self._capset)
                    self._record_sync("metrics")
                    logger.info("FinancialScheduler: metrics synced, %d rows", rows)
                except Exception as e:
                    logger.warning("FinancialScheduler: metrics sync failed: %s", e)

                # 等待下一次 (7天)
                for _ in range(7 * 24 * 60):  # 每分钟检查一次 _running
                    if not self._running:
                        break
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass

    def _run_body(self, table: str | None) -> dict[str, int]:
        """同步逻辑本体(不加锁,假设调用方已持有 _is_syncing)。

        table=None 同步全部财务表;否则只同步指定表。
        每张表完成立即更新 last_sync,让前端轮询 /status 能看到进度递增。
        """
        if table:
            fn = _financial_sync_functions().get(table)
            if not fn:
                return {}
            rows = fn(self._data_dir, self._capset)
            self._record_sync(table)
            return {table: rows}
        # 全部同步
        result: dict[str, int] = {}
        sync_functions = _financial_sync_functions()
        for t in FINANCIAL_TABLES:
            result[t] = sync_functions[t](self._data_dir, self._capset)
            self._record_sync(t)
        _refresh_financials_views(self._data_dir)
        return result

    def run_now(self, table: str | None = None) -> dict[str, int]:
        """同步执行一次同步(阻塞调用线程)。

        ⚠ 全量同步需数分钟,务必在后台线程调用,不要直接在 HTTP 请求线程里阻塞,
        否则请求会长时间 pending 直至被浏览器/代理超时掐断(表现为"点击无反应")。
        HTTP 接口应调用 trigger() 立即返回,再让前端轮询 /status.syncing 看进度。

        用 _is_syncing 标志防并发:若已有同步在进行,本次直接跳过,
        避免重复请求拖慢服务端 / 触发上游限流。
        """
        if not self._capset or not self._capset.has(Cap.FINANCIAL):
            return {}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync skipped: already running")
                return {"_skipped": 1}
            self._is_syncing = True
        try:
            return self._run_body(table)
        finally:
            with self._lock:
                self._is_syncing = False

    def trigger(self, table: str | None = None) -> dict[str, int]:
        """触发一次同步(非阻塞,立即返回)。

        在后台线程执行同步体,HTTP 请求无需等待。
        返回 {"started": True/False}:
          - False = 能力不足或已有同步在进行(被防并发跳过)
          - True  = 已在后台开始,前端应轮询 /status.syncing 观察进度

        ⚠ _is_syncing 在此处置 True(持锁),确保 trigger 返回时前端轮询
        /status 已能看到 syncing=True,无竞态窗口;同时防止快速重复点击
        启动多个后台线程。后台线程复用 _run_body 执行真正的同步逻辑。
        """
        if not self._capset or not self._capset.has(Cap.FINANCIAL):
            return {"started": False, "reason": "no FINANCIAL capability"}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync trigger skipped: already running")
                return {"started": False, "reason": "already running"}
            # 持锁置位:保证 trigger 返回前 syncing 已为 True
            self._is_syncing = True

        def _bg() -> None:
            try:
                self._run_body(table)
            except Exception as e:  # noqa: BLE001
                logger.exception("background financial sync failed: %s", e)
            finally:
                with self._lock:
                    self._is_syncing = False

        t = threading.Thread(target=_bg, name="financial-sync", daemon=True)
        t.start()
        logger.info("financial sync triggered in background: table=%s", table or "all")
        return {"started": True}

    @property
    def is_syncing(self) -> bool:
        """手动同步是否正在进行(供 /status 返回,前端据此显示"同步中")。"""
        with self._lock:
            return self._is_syncing

    @property
    def last_sync(self) -> dict[str, str]:
        return dict(self._last_sync)


# 全局单例
financial_scheduler = FinancialScheduler()
