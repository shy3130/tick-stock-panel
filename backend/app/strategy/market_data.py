"""策略可访问的指数/ETF 日K读取模块 — 白名单放行的只读数据入口。

供 Custom/AI 策略在 filter_history 内读取任意指数(及 ETF)的完整日K。
策略通过白名单 import 本模块, 调用纯读函数; 禁止写操作或任意文件访问。

设计要点:
  - 模块自身是框架侧信任代码, 对策略的沙箱逃逸拦截(ai_generator._validate_safety)照旧生效。
  - repo 线程安全懒加载(首次调用才构建); DataStore() 默认 settings.data_dir, 与 main.py 同源。
  - 未知 symbol / 数据缺失 → 返回空 DataFrame(不抛), 与 repo 语义一致。
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

# 完整历史默认区间下界(A股数据远晚于此, 仅作"全量"占位)。
_FULL_START = date(1990, 1, 1)

# ── repo 懒加载(线程安全) ─────────────────────────────
_repo = None
_lock = threading.Lock()


def _get_repo():
    global _repo
    if _repo is None:
        with _lock:
            if _repo is None:
                from app.tickflow.repository import DataStore, KlineRepository
                _repo = KlineRepository(DataStore())
    return _repo


def _set_repo(repo: Any) -> None:
    """测试注入: 用 fake repo 替换单例。"""
    global _repo
    with _lock:
        _repo = repo


def _reset_repo() -> None:
    """测试清理: 重置单例, 下次调用重新懒加载。"""
    global _repo
    with _lock:
        _repo = None


# ── 参数规范化 ─────────────────────────────────────────
def _norm_date(value, default: date) -> date:
    if value is None:
        return default
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


def _validate_symbol(symbol: Any) -> bool:
    return isinstance(symbol, str) and bool(symbol.strip())


# ── 公开只读 API ───────────────────────────────────────
def get_index_daily(symbol, start=None, end=None, columns=None):
    """读取指数日K(含技术指标)。未知 symbol / 无数据返回空 DataFrame。"""
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法指数 symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    # 窗口右端必须是北京今天: 策略对照指数/ETF 的分区日期是北京交易日。
    # 美西主机整个 A 股交易时段、UTC 主机北京 00:00-08:00, date.today() 比北京早一天,
    # 未传 end 时会把当日官方 K 排除在窗口外。
    e = _norm_date(end, cn_today())
    try:
        return _get_repo().get_index_daily(symbol, s, e, columns)
    except Exception as exc:
        logger.warning("market_data get_index_daily failed %s: %s", symbol, exc)
        return pl.DataFrame()


def get_etf_daily(symbol, start=None, end=None, columns=None):
    """读取 ETF 日K(含技术指标)。同 get_index_daily 语义。"""
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法 ETF symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    e = _norm_date(end, cn_today())
    try:
        return _get_repo().get_etf_daily(symbol, s, e, columns)
    except Exception as exc:
        logger.warning("market_data get_etf_daily failed %s: %s", symbol, exc)
        return pl.DataFrame()


def get_daily(symbol, start=None, end=None, columns=None):
    """按资产类型自动分派读取日K: 指数 → get_index_daily; ETF → get_etf_daily; 股票 → get_daily。"""
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法 symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    e = _norm_date(end, cn_today())
    repo = _get_repo()
    try:
        asset_type = repo.resolve_asset_type(symbol)
        if asset_type == "index":
            return repo.get_index_daily(symbol, s, e, columns)
        if asset_type == "etf":
            return repo.get_etf_daily(symbol, s, e, columns)
        return repo.get_daily(symbol, s, e, columns)
    except Exception as exc:
        logger.warning("market_data get_daily failed %s: %s", symbol, exc)
        return pl.DataFrame()


def list_index_symbols() -> list[dict]:
    """列出已收录的指数符号(含名称)。无数据返回空列表。"""
    try:
        df = _get_repo().get_instruments_asset("index")
    except Exception as exc:
        logger.warning("market_data list_index_symbols failed: %s", exc)
        return []
    if df.is_empty() or "symbol" not in df.columns:
        return []
    name_col = "name" if "name" in df.columns else None
    cols = ["symbol"] + ([name_col] if name_col else [])
    return [
        {"symbol": row["symbol"], "name": row.get("name")}
        for row in df.select(cols).iter_rows(named=True)
    ]


# ── 扩展时序表原始行 (日内多盘不收敛) ──────────────────
# config_id 进入文件路径, 必须与 ExtConfigStore._VALID_ID 同口径白名单校验
_EXT_CONFIG_ID_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_EXT_PARTITION_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def get_ext_timeseries(config_id, start=None, end=None, columns=None):
    """读取扩展时序表 (timeseries 模式) 的原始行, 供策略计算日内序列特征。

    与 ext_factors 因子通道的口径区别: 因子通道把每个 (symbol, 交易日) 收敛为
    最后一行 (末盘定型值); 序列特征 (如集合竞价 9:15-9:25 走势) 需要全天快照,
    这里原样返回全部行。date 列取分区目录名 (权威来源, 与因子通道同口径),
    行内自带的 date 列被覆盖。

    Args:
        config_id: 扩展表 id (data/ext_data/{config_id}); 非法/不存在/非
            timeseries 模式 → 空 DataFrame, 不抛异常。
        start/end: 日期范围 (str/date), 缺省全量。
        columns: 附加列白名单 (如 ["ts", "auction_price"]); symbol/date 恒有。
            None = 全部列。指定列在某个分区缺失时跳过该分区并记 warning。

    返回按 (symbol, date, ts?) 升序; 只读, 不写任何缓存。
    """
    if not isinstance(config_id, str) or not _EXT_CONFIG_ID_RE.match(config_id):
        logger.warning("market_data: 非法扩展表 config_id %r", config_id)
        return pl.DataFrame()
    repo = _get_repo()
    # 真仓是 KlineRepository (data_dir 挂在 .store 上); 测试 fake 直接挂 data_dir
    base = getattr(repo, "data_dir", None) or getattr(
        getattr(repo, "store", None), "data_dir", None
    )
    if base is None:
        logger.warning("market_data: 仓库对象未暴露 data_dir, 无法读取扩展表")
        return pl.DataFrame()
    root = Path(base) / "ext_data" / config_id / "timeseries"
    if not root.is_dir():
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    e = _norm_date(end, cn_today())
    want = (
        [c for c in dict.fromkeys(columns) if c not in ("symbol", "date")]
        if columns is not None else None
    )
    parts: list[pl.DataFrame] = []
    for d in sorted(root.glob("date=*")):
        m = _EXT_PARTITION_RE.match(d.name)
        if not m or not d.is_dir():
            continue
        day = date.fromisoformat(m.group(1))
        if not (s <= day <= e):
            continue
        part = d / "part.parquet"
        if not part.exists():
            continue
        try:
            frag = (
                pl.read_parquet(part, columns=["symbol", *want])
                if want is not None else pl.read_parquet(part)
            )
        except Exception as exc:  # noqa: BLE001 — 单分区损坏只跳过, 不炸整个调用
            logger.warning(
                "market_data: 扩展表 %s 分区 %s 读取失败, 跳过: %s",
                config_id, d.name, exc,
            )
            continue
        if frag.is_empty():
            continue
        if "date" in frag.columns:
            frag = frag.drop("date")
        parts.append(frag.with_columns(pl.lit(m.group(1)).alias("date")))
    if not parts:
        return pl.DataFrame()
    frame = pl.concat(parts, how="diagonal")
    sort_keys = ["symbol", "date"] + (["ts"] if "ts" in frame.columns else [])
    return frame.sort(sort_keys)
