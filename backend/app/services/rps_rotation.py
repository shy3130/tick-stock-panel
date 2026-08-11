"""概念涨幅轮动矩阵 service。

输出「每列(日期)各自把所有概念按当天涨幅从高到低排序」的矩阵,供前端
「概念分析 → 涨幅RPS轮动」对话框渲染。

数据来源全部复用现有资产, 不引入新数据源:
  - 个股历史涨跌幅: repo.get_enriched_range(..., columns=["symbol","date","change_pct"])
    命中启动时构建的 _enriched_history_cache (0ms, 含 change_pct 小数列)
  - 概念成分股映射: 复用 market_overview_builder 的 _dimension_field / _read_ext_rows /
    _symbol_keys / _dimension_values, 与看板/复盘的概念聚合口径完全一致

性能: 387 概念 × 30 天的 group_by + sort 是 polars 内存操作, 实测 <50ms;
另加进程级结果缓存 (_CACHE_TTL=120s), 重复请求 <1ms。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import polars as pl

from app.services.market_overview_builder import (
    _dimension_field,
    _dimension_values,
    _read_ext_rows,
    _symbol_keys,
)
from app.services.ext_data import ExtConfigStore

logger = logging.getLogger(__name__)

# 进程级结果缓存 (照搬 overview.py:18 的模式, TTL 拉长到 120s —— 轮动矩阵
# 不像看板那样需要近实时, 盘后数据稳定, 缓存久一点无妨)
_CACHE_TTL = 120.0
_cache: dict[str, dict] = {}
_cache_ts: dict[str, float] = {}


def invalidate_cache() -> None:
    """清空轮动矩阵结果缓存(数据管道完成后调用, 避免返回旧数据)。"""
    _cache.clear()
    _cache_ts.clear()


def _latest_enriched_date(repo) -> date | None:
    """取 enriched 缓存里的最新交易日(矩阵的右端=最新日期)。"""
    cache = repo._enriched_history_cache  # noqa: SLF001 —— 缓存字段无公开 getter
    if cache is None or cache.is_empty() or "date" not in cache.columns:
        return None
    return cache["date"].max()


def _load_concept_map_df(repo, kind: str = "concept") -> tuple[pl.DataFrame, int]:
    """构建并缓存 {symbol_upper → 维度成员} 的已展开 polars 映射表。

    kind: "concept"(概念) 或 "industry"(行业)。复用 market_overview_builder 的
    _dimension_field(config, kind) 识别维度 —— 该函数两种维度都支持。

    返回 (map_df, member_count):
      - map_df: 两列 (_sym_up: 大写 symbol, <kind>: 维度成员名), 已 explode。
        无数据时返回空 DataFrame。
      - member_count: 去重维度成员总数。

    缓存: 维度成分股是 snapshot, 进程内不变, 缓存 600s。按 kind 分别缓存。
    """
    now = time.time()
    cached = _map_cache.get(kind)
    if cached is not None and (now - _map_ts.get(kind, 0)) < 600:
        return cached

    data_dir = repo.store.data_dir
    store = ExtConfigStore(data_dir)
    pairs: list[tuple[str, str]] = []
    members_seen: set[str] = set()

    for config in store.load_all():
        field = _dimension_field(config, kind)
        if not field:
            continue
        for ext_row in _read_ext_rows(data_dir, config, field):
            members = _dimension_values(ext_row.get(field))
            if not members:
                continue
            keys = _symbol_keys(ext_row, config)
            for key in keys:
                for m in members:
                    pairs.append((key, m))
                    members_seen.add(m)

    if pairs:
        map_df = pl.DataFrame(
            {"_sym_up": [p[0] for p in pairs], kind: [p[1] for p in pairs]},
            schema={"_sym_up": pl.Utf8, kind: pl.Utf8},
        ).unique()
    else:
        map_df = pl.DataFrame(schema={"_sym_up": pl.Utf8, kind: pl.Utf8})
    _map_cache[kind] = map_df
    _map_ts[kind] = now
    return map_df, len(members_seen)


# 维度映射缓存: {kind: (map_df, count)}。按 kind 隔离(概念/行业分别缓存)。
_map_cache: dict[str, pl.DataFrame] = {}
_map_ts: dict[str, float] = {}


def build_rps_rotation(repo, days: int = 12, kind: str = "concept", level: int | None = None) -> dict:
    """构建维度涨幅轮动矩阵(概念或行业)。

    Args:
        repo: KlineRepository(含 _enriched_history_cache 内存历史)。
        days: 取最近 N 个交易日, 范围 [7, 30], 默认 12。
        kind: "concept"(概念) 或 "industry"(行业), 决定维度映射来源。
        level: 行业层级(仅 kind=industry 有效, 1/2/3 级)。None 表示用原始全路径名。
            行业名形如 "银行-银行-股份制银行", level=2 取第二段"银行", 同级下多个
            三级会合并聚合(与 _dimension_rank 的 level 口径一致)。

    Returns:
        {
          "dates": ["2026-06-30", ...],          # 最新在最前, 长度 ≤ days
          "columns": {"2026-06-30": [[成员, 涨幅], ...], ...},  # 每列各自排序(高→低)
          "concept_count": 387,                   # 去重维度成员总数(0 表示无数据)
        }
        涨幅是小数(0.0522 = +5.22%)。无数据时返回空 columns。
        字段名 concept_count 保留兼容(前端按 kind 显示"X 个概念/行业")。
    """
    days = max(7, min(30, days))

    # 结果缓存: 同 (kind, level, latest) 的请求在 TTL 内直接返回。
    latest = _latest_enriched_date(repo)
    if latest is None:
        return {"dates": [], "columns": {}, "concept_count": 0}

    cache_key = f"{kind}|{level}|{latest.isoformat()}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - _cache_ts.get(cache_key, 0)) < _CACHE_TTL:
        return _slice_cached(cached, days)

    # 1. 维度映射(symbol → 维度成员), 已按 kind 缓存为 polars DataFrame
    map_df, member_count = _load_concept_map_df(repo, kind)
    if map_df.is_empty():
        logger.info("rps_rotation: no %s data (ext dimension not fetched yet)", kind)
        return {"dates": [], "columns": {}, "concept_count": 0}

    # 2. 取最近 N 交易日的个股 change_pct(命中内存缓存)
    start = latest - timedelta(days=days * 2 + 10)  # 日历天 ≈ 2/3 交易日, 多取余量
    df = repo.get_enriched_range(
        start, latest, columns=["symbol", "date", "change_pct"]
    )
    if df is None or df.is_empty():
        return {"dates": [], "columns": {}, "concept_count": 0}

    # 3. 把个股 symbol 映射到维度成员, 一只股票拆成多行(每个成员一行)
    #    symbol 大写匹配(map_df 的 _sym_up 已大写)
    df = df.with_columns(pl.col("symbol").str.to_uppercase().alias("_sym_up"))
    joined = df.join(map_df, on="_sym_up", how="inner").drop("_sym_up")

    if joined.is_empty():
        return {"dates": [], "columns": {}, "concept_count": 0}

    # 行业层级聚合: kind=industry 且指定 level 时, 把 "一级行业-二级行业-三级行业"
    # 拆分取对应层级(level=2 → "二级行业"), 同级下多个三级会合并。
    # 与 market_overview_builder._dimension_rank 的 level 口径完全一致。
    if kind == "industry" and level is not None:
        # polars: 按 "-" 拆分取第 level 段; 段数不足时取最后一段(兜底)
        parts = pl.col(kind).str.split("-")
        idx = pl.min_horizontal(pl.lit(level - 1), pl.col(kind).str.count_matches("-"))
        joined = joined.with_columns(parts.list.get(idx).alias(kind))

    # 4. 按 (date, <kind>) 聚合 avg change_pct —— 与 _dimension_rank 的简单平均口径一致
    agg = joined.group_by(["date", kind]).agg(
        pl.col("change_pct").mean().alias("avg_pct")
    )
    # 去掉 NaN/Null(停牌等无行情的成员日)
    agg = agg.filter(pl.col("avg_pct").is_not_null() & pl.col("avg_pct").is_not_nan())

    # 5. 每个日期内按 avg_pct 降序排, 再 group_by 把每组的 (成员, avg_pct)
    #    收集成并行 list —— 一次 polars 操作拿到全部列, 避免 partition_by 的 tuple key 歧义
    agg = agg.sort(["date", "avg_pct"], descending=[False, True])
    grouped = agg.group_by("date", maintain_order=True).agg(
        pl.col(kind), pl.col("avg_pct")
    )
    # 最新日期排最前
    grouped = grouped.sort("date", descending=True)

    columns: dict[str, list[list]] = {}
    all_dates_sorted: list[str] = []
    for row in grouped.iter_rows(named=True):
        d_str = str(row["date"])
        all_dates_sorted.append(d_str)
        columns[d_str] = list(zip(row[kind], row["avg_pct"]))

    full = {
        "dates": [str(d) for d in all_dates_sorted],
        "columns": columns,
        "concept_count": member_count,
    }

    # 写缓存(存全量, 按需 slice)
    _cache[cache_key] = full
    _cache_ts[cache_key] = now

    return _slice_cached(full, days)


def _slice_cached(full: dict, days: int) -> dict:
    """从全量缓存截取最近 N 天(days)。"""
    dates_all = full["dates"]
    if len(dates_all) <= days:
        return full
    keep_dates = dates_all[:days]
    return {
        "dates": keep_dates,
        "columns": {d: full["columns"][d] for d in keep_dates},
        "concept_count": full["concept_count"],
    }
