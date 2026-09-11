"""盘中板块切换监控 — 基于全量分钟数据的实时轮动走势。

数据来源 (零新增数据源, 全部复用现有资产):
  - 全市场当日分钟K: data/kline_minute/date=X/part.parquet (全量分钟能力落盘,
    MinuteRefreshService 盘中持续增量写入), 直读单日分区 4 列, 不走仓库层批量接口
  - 板块成分映射: rps_rotation._load_concept_map_df (概念/行业二选一, 600s 缓存)
  - 资金流向: 用户选择的扩展数据列 ("表id.列名", 运行时参数不写死),
    读该扩展表最新分区按板块聚合成分股数值

计算口径:
  - 个股分钟涨跌幅 pct = close/ref - 1, ref 优先前一交易日日K收盘
    (开盘跳空体现在板块曲线起点), 缺日K退化为当日首根分钟 close (混合基准)
  - 板块桶涨幅 = 成分股 pct 在桶内的等权均值 (停牌/无分钟按可得成分均值)
  - 切换走势 rotation(t) = 1 - top10 重合率 (t 与 t-1h 两个桶各自按板块涨幅
    取前 10, 交集占比); 越高代表领涨梯队换血越剧烈
  - 板块排名 rank_now/rank_prev: 最新桶与 1 小时前桶按涨幅降序的名次 (1=最强),
    rank_change = rank_prev - rank_now > 0 表示切入, < 0 表示退潮
  - 综合分 = 涨幅归一与资金流归一各 50% (min-max 到 0-100); 未选择资金流或
    数据不可用时退化为纯涨幅归一

性能:
  - 单日分区 polars 向量化 (group_by 桶 x 板块), 全市场 ~百万行 4 列毫秒级
    (先例: ext_data dimension-intraday 同款直读口径)
  - 进程内结果缓存 TTL 30s (分钟数据默认 6s 刷新一轮, 30s 已足够"实时"),
    缓存键含全部影响结果的参数
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import polars as pl

from app.services.ext_data import ExtConfigStore
from app.services.rps_rotation import _load_concept_map_df

logger = logging.getLogger(__name__)

_CACHE_TTL = 30.0
_cache: dict[tuple, dict] = {}
_cache_ts: dict[tuple, float] = {}

# 与 api/ext_data dimension-intraday 一致: 允许的分钟桶粒度
_ALLOWED_BUCKETS = (1, 5, 15)
# 切换走势的对照窗口 (分钟): t 桶与 t-_RANK_WINDOW_MIN 前的桶比较排名
_RANK_WINDOW_MIN = 60
# rotation 计算取的领涨梯队宽度
_TOP_OVERLAP = 10


def invalidate_cache() -> None:
    """清空板块切换结果缓存 (数据管道完成后调用, 避免返回旧数据)。"""
    _cache.clear()
    _cache_ts.clear()


def _bare(col: str = "symbol") -> pl.Expr:
    return pl.col(col).cast(pl.String).str.strip_chars().str.split(".").list.first()


def _latest_minute_partition(minute_dir: Path) -> str | None:
    if not minute_dir.exists():
        return None
    parts = sorted(
        d.name[5:]
        for d in minute_dir.iterdir()
        if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").is_file()
    )
    return parts[-1] if parts else None


def _prev_daily_close(data_dir: Path, target_date: str) -> pl.DataFrame | None:
    """目标日前最近一个日K分区的收盘价 → (_bare, prev_close); 无则 None。

    与 api/ext_data._prev_daily_close 同口径 (服务层不依赖 api 层, 就地实现)。
    """
    daily = data_dir / "kline_daily"
    if not daily.exists():
        return None
    dates = sorted(
        d.name[5:]
        for d in daily.iterdir()
        if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").is_file()
    )
    prevs = [d for d in dates if d < target_date]
    if not prevs:
        return None
    try:
        df = pl.read_parquet(daily / f"date={prevs[-1]}" / "part.parquet", columns=["symbol", "close"])
    except Exception:
        return None
    return (
        df.with_columns(_bare().alias("_bare"))
        .select([pl.col("_bare"), pl.col("close").cast(pl.Float64).alias("prev_close")])
        .unique(subset=["_bare"], keep="last")
    )


def _minute_pcts(minute_dir: Path, target: str) -> tuple[pl.DataFrame | None, str]:
    """当日全市场分钟 → (_bare, _bucket, _pct); 读取失败返回 (None, reason)。"""
    try:
        bars = pl.read_parquet(
            minute_dir / f"date={target}" / "part.parquet",
            columns=["symbol", "datetime", "close"],
        )
    except Exception as exc:
        logger.warning("sector_rotation read minute partition failed: %s", exc)
        return None, "minute_schema"
    bars = bars.drop_nulls(subset=["datetime", "close"])
    if bars.is_empty():
        return None, "minute_empty"
    bars = bars.with_columns(_bare().alias("_bare"))

    prev = _prev_daily_close(minute_dir.parent, target)
    joined = bars.join(prev, on="_bare", how="left") if prev is not None else bars.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("prev_close")
    )
    refs = joined.group_by("_bare").agg(
        pl.col("prev_close").first().alias("_prev"),
        pl.col("close").sort_by("datetime").first().alias("_first"),
    ).with_columns(pl.coalesce(["_prev", "_first"]).alias("_ref"))
    n_prev = refs["_prev"].is_not_null().sum()
    basis = "prev_close" if n_prev == refs.height else ("first_close" if n_prev == 0 else "mixed")
    out = (
        joined.join(refs.select(["_bare", "_ref"]), on="_bare", how="left")
        .with_columns((pl.col("close") / pl.col("_ref") - 1.0).alias("_pct"))
        .drop_nulls(subset=["_pct"])
    )
    if out.is_empty():
        return None, "minute_empty"
    return out, basis


def _load_sector_flow(data_dir: Path, flow_field: str) -> pl.DataFrame | None:
    """读取用户选择的资金流扩展列 → (_bare, _flow) 每标的一行; 不可用返回 None。

    flow_field 格式 "表id.列名"。读该扩展表最新分区 (snapshot 取 part.parquet,
    timeseries 取最新日分区), 列值转数值, 非数值/空剔除; symbol 列探测与
    _symbol_keys 同优先级。失败/缺列静默降级 (资金流是增强维度, 不阻断涨幅计算)。
    """
    if not flow_field or "." not in flow_field:
        return None
    config_id, _, column = flow_field.partition(".")
    if not config_id or not column:
        return None
    try:
        config = ExtConfigStore(data_dir).get(config_id)
    except Exception:
        return None
    if config is None:
        return None
    base = data_dir / "ext_data" / config.id
    if config.mode == "timeseries":
        root = base / "timeseries"
        parts = sorted(p for p in root.rglob("*.parquet") if p.is_file())
        path = parts[-1] if parts else None
    else:
        path = base / "part.parquet"
        path = path if path.exists() else None
    if path is None:
        return None
    try:
        df = pl.read_parquet(path)
    except Exception:
        return None
    if df.is_empty() or column not in df.columns:
        return None
    symbol_col = next((c for c in ("symbol", "code", "股票代码", "代码") if c in df.columns), None)
    mapped_col = None
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            mapped_col = str(mapping["col"])
            break
    if symbol_col is None and mapped_col is None:
        return None
    use_col = symbol_col if symbol_col is not None and symbol_col in df.columns else mapped_col
    if use_col not in df.columns:
        return None
    out = (
        df.select([
            _bare(use_col).alias("_bare"),
            pl.col(column).cast(pl.Float64, strict=False).alias("_flow"),
        ])
        .drop_nulls(subset=["_flow", "_bare"])
        .filter(pl.col("_flow").is_finite() & (pl.col("_flow") != 0.0))
        .unique(subset=["_bare"], keep="first")
    )
    return out if not out.is_empty() else None


def _r4(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, 4)


def _normalize_0_100(values: list[float | None]) -> list[float | None]:
    """min-max 归一到 0-100; 少于 2 个有效值时全部给 50 (无区分度)。"""
    valid = [v for v in values if v is not None]
    if not valid:
        return [None] * len(values)
    if len(valid) == 1 or max(valid) == min(valid):
        return [50.0 if v is not None else None for v in values]
    span = max(valid) - min(valid)
    return [
        (v - min(valid)) / span * 100.0 if v is not None else None
        for v in values
    ]


def build_sector_rotation(
    repo,
    *,
    kind: str = "concept",
    flow_field: str | None = None,
    top: int = 30,
    bucket_minutes: int = 5,
) -> dict:
    """计算盘中板块切换走势 (概念/行业二选一)。

    返回结构:
      status/date/basis/kind/flow_field/bucket_minutes/member_count/flow_available/
      timeline: [{time, rotation, leader, leader_pct, market_pct}] /
      sectors: [{name, pct_now, pct_prev, rank_now, rank_prev, rank_change,
                 flow, score, n_members, n_members_with_bars}] 按 score 降序截 top 条
    不可计算时返回 {status: "no_data"|"empty", reason, date?} (fail-closed, 不静默)。
    """
    if kind not in ("concept", "industry"):
        raise ValueError(f"不支持的板块维度: {kind} (可选 concept/industry)")
    bucket_minutes = int(bucket_minutes)
    if bucket_minutes not in _ALLOWED_BUCKETS:
        raise ValueError(f"不支持的分钟桶: {bucket_minutes} (可选 {_ALLOWED_BUCKETS})")
    top = max(5, min(100, int(top)))
    flow_field = (flow_field or "").strip() or None

    data_dir: Path = repo.store.data_dir
    cache_key = (kind, flow_field or "", top, bucket_minutes)
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit is not None and (now - _cache_ts.get(cache_key, 0.0)) < _CACHE_TTL:
        return hit

    result = _compute(repo, data_dir, kind, flow_field, top, bucket_minutes)
    _cache[cache_key] = result
    _cache_ts[cache_key] = time.monotonic()
    return result


def _compute(repo, data_dir: Path, kind: str, flow_field: str | None, top: int, bucket_minutes: int) -> dict:
    minute_dir = data_dir / "kline_minute"
    target = _latest_minute_partition(minute_dir)
    if not target:
        return {"status": "no_data", "reason": "minute_missing", "kind": kind}

    map_df, member_count = _load_concept_map_df(repo, kind)
    if map_df.is_empty() or member_count == 0:
        return {"status": "no_data", "reason": "members_missing", "date": target, "kind": kind}

    pcts, basis = _minute_pcts(minute_dir, target)
    if pcts is None:
        return {"status": "no_data", "reason": basis, "date": target, "kind": kind}

    # 桶化 + 板块聚合: 先每股桶内均值, 再板块等权均值 (停牌/缺分钟不放大权重)
    member_df = map_df.rename({"_sym_up": "_bare", kind: "_member"})
    buckets = pcts.with_columns(pl.col("datetime").dt.truncate(f"{bucket_minutes}m").alias("_bucket"))
    by_sector = (
        buckets.join(member_df, on="_bare", how="inner")
        .group_by(["_bucket", "_member", "_bare"])
        .agg(pl.col("_pct").mean().alias("_sym_pct"))
        .group_by(["_bucket", "_member"])
        .agg(pl.col("_sym_pct").mean().alias("_spct"), pl.len().alias("_n"))
        .sort(["_bucket", "_spct"], descending=[False, True])
    )
    if by_sector.is_empty():
        return {"status": "empty", "reason": "no_member_bars", "date": target, "kind": kind}

    market = (
        buckets.group_by("_bucket").agg(pl.col("_pct").mean().alias("_mpct")).sort("_bucket")
    )

    # 每桶领涨板块 + top 集合 (供切换走势与排名对照)
    per_bucket: list[dict[str, Any]] = []
    for (bucket,), frame in by_sector.group_by("_bucket", maintain_order=True):
        names = frame["_member"].to_list()
        per_bucket.append({
            "bucket": bucket,
            "names": names,
            "pct": frame["_spct"].to_list(),
            "top_set": set(names[:_TOP_OVERLAP]),
            "leader": names[0],
            "leader_pct": frame["_spct"][0],
        })
    per_bucket.sort(key=lambda item: item["bucket"])
    market_map = {row["_bucket"]: row["_mpct"] for row in market.iter_rows(named=True)}

    # 对照窗口: 距当前桶约 _RANK_WINDOW_MIN 分钟的最近历史桶
    def _reference_index(index: int) -> int | None:
        current = per_bucket[index]["bucket"]
        for back in range(index - 1, -1, -1):
            delta_min = (current - per_bucket[back]["bucket"]).total_seconds() / 60.0
            if delta_min >= _RANK_WINDOW_MIN:
                return back
        return 0 if index > 0 else None  # 不足一小时: 与最早桶比; 首桶无对照

    rank_now = {name: i + 1 for i, name in enumerate(per_bucket[-1]["names"])}
    ref_index = _reference_index(len(per_bucket) - 1)
    if ref_index is not None:
        rank_prev = {name: i + 1 for i, name in enumerate(per_bucket[ref_index]["names"])}
        pct_prev_map = dict(zip(per_bucket[ref_index]["names"], per_bucket[ref_index]["pct"], strict=True))
    else:
        rank_prev, pct_prev_map = {}, {}

    timeline = []
    for index, item in enumerate(per_bucket):
        back = _reference_index(index)
        if back is not None:
            # 除数取梯队宽与两侧实际板块数的较小值: 板块总数不足 10 时
            # top 集合恒为全集, 按 10 归一会稀释换血信号
            width = min(_TOP_OVERLAP, len(item["top_set"]), len(per_bucket[back]["top_set"]))
            overlap = len(item["top_set"] & per_bucket[back]["top_set"]) / width if width else 1.0
        else:
            overlap = 1.0
        timeline.append({
            "time": item["bucket"].strftime("%H:%M"),
            "rotation": round(1.0 - min(1.0, max(0.0, overlap)), 4),
            "leader": item["leader"],
            "leader_pct": _r4(item["leader_pct"]),
            "market_pct": _r4(market_map.get(item["bucket"])),
        })

    # 资金流 (扩展数据, 用户选择): 板块 = 成分股数值合计
    flow_by_member: dict[str, float] = {}
    flow_available = False
    if flow_field:
        flow_df = _load_sector_flow(data_dir, flow_field)
        if flow_df is not None and not flow_df.is_empty():
            flow_available = True
            flow_by_member = {
                row["_member"]: row["_flow"]
                for row in member_df.join(flow_df, on="_bare", how="inner")
                .group_by("_member")
                .agg(pl.col("_flow").sum().alias("_flow"))
                .iter_rows(named=True)
            }

    # 成分总数: 映射表同时含全代码与裸代码两行, 去掉带点的全代码避免双计
    bare_members = member_df.filter(~pl.col("_bare").str.contains(r"\."))
    members_count_by_sector = {
        row["_member"]: row["_n"]
        for row in bare_members.group_by("_member").agg(pl.len().alias("_n")).iter_rows(named=True)
    }
    n_with_bars = {
        row["_member"]: row["_n"]
        for row in by_sector.filter(pl.col("_bucket") == per_bucket[-1]["bucket"])
        .select(["_member", "_n"]).iter_rows(named=True)
    }
    names = per_bucket[-1]["names"]
    pcts_now = dict(zip(names, per_bucket[-1]["pct"], strict=True))
    pct_values = [pcts_now.get(name) for name in names]
    pct_norm = _normalize_0_100(pct_values)
    flow_values = [flow_by_member.get(name) for name in names] if flow_available else [None] * len(names)
    flow_norm = _normalize_0_100(flow_values) if flow_available else [None] * len(names)

    sectors = []
    for i, name in enumerate(names):
        score = (
            0.5 * pct_norm[i] + 0.5 * flow_norm[i]
            if flow_available and flow_norm[i] is not None
            else pct_norm[i]
        )
        sectors.append({
            "name": name,
            "pct_now": _r4(pcts_now.get(name)),
            "pct_prev": _r4(pct_prev_map.get(name)),
            "rank_now": rank_now.get(name),
            "rank_prev": rank_prev.get(name),
            "rank_change": (
                rank_prev[name] - rank_now[name]
                if name in rank_prev and name in rank_now else None
            ),
            "flow": _r4(flow_by_member.get(name)) if flow_available else None,
            "score": round(score, 2) if score is not None else None,
            "n_members": members_count_by_sector.get(name, 0),
            "n_members_with_bars": n_with_bars.get(name, 0),
        })
    sectors.sort(key=lambda item: (item["score"] is not None, item["score"] or 0.0), reverse=True)

    return {
        "status": "ok",
        "date": target,
        "kind": kind,
        "basis": basis,
        "flow_field": flow_field,
        "flow_available": flow_available,
        "bucket_minutes": bucket_minutes,
        "member_count": member_count,
        "as_of": per_bucket[-1]["bucket"].strftime("%H:%M"),
        "timeline": timeline,
        "sectors": sectors[:top],
    }
