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
# 活跃度窗口 (分钟): 板块活跃度 = 最近该时长内成分股成交额合计
_ACTIVITY_WINDOW_MIN = 30
# 自定义展示板块上限 / 活跃默认行数
_MAX_SERIES_ROWS = 20
_DEFAULT_SERIES_ROWS = 10
# 自动活跃榜默认排除的属性类板块 (名称子串匹配): 交易属性/指数成分/持仓/事件类
# "标签桶"成员数动辄数百上千, 成交额合计天然占优, 会挤出真实题材。用户可通过
# exclude_sectors 参数整体替换该名单; 传空数组 = 关闭名称过滤 (成员数上限仍生效)。
_DEFAULT_EXCLUDE_SECTORS = (
    '融资融券', '转融券', '转债标的', '含可转债', '沪股通', '深股通',
    'AH股', 'B股', 'GDR', 'MSCI', '富时罗素', '标普道琼斯',
    '上证50', '上证180', '沪深300', '中证500', '中证800', '中证1000', '科创50',
    '重仓', '证金持股', '汇金概念',
    '预盈预增', '预亏预减', '昨日涨停', '昨日连板', '昨日触涨停', '昨日曾涨停',
    '次新', 'ST板块', 'ST股', '破净', '壳资源', '股权激励', '员工持股', '并购重组',
)
# 自动活跃榜成员数上限: 成员数超过该值的板块不参与自动选取 (仍出现在 universe 与自定义清单)
_MAX_AUTO_MEMBERS = 300
# 自动榜排序维度: activity=近30分钟成交额合计, score=涨幅+资金流综合分,
# pct=现涨幅, rank_change=1h排名跃升(切入), momentum=近1小时动量(走强→走弱),
# flow=扩展资金流列
_SORT_MODES = ("activity", "score", "pct", "rank_change", "momentum", "flow")
# exclude_sectors 参数条目上限
_MAX_EXCLUDE_PARAM = 100
# 结果缓存条目上限: 缓存键含用户参数组合, 有界淘汰防止无限增长
_CACHE_MAX_ENTRIES = 32


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


def _minute_pcts(minute_dir: Path, target: str) -> tuple[pl.DataFrame | None, str, bool]:
    """当日全市场分钟 → (_bare, _bucket, _pct[, amount]); 读取失败返回 (None, reason, False)。

    amount 列缺失 (旧 schema) 时走精简读取, has_amount=False 由上层降级活跃度。
    """
    path = minute_dir / f"date={target}" / "part.parquet"
    try:
        bars = pl.read_parquet(path, columns=["symbol", "datetime", "close", "amount"])
        has_amount = True
    except Exception:
        try:
            bars = pl.read_parquet(path, columns=["symbol", "datetime", "close"])
            has_amount = False
        except Exception as exc:
            logger.warning("sector_rotation read minute partition failed: %s", exc)
            return None, "minute_schema", False
    bars = bars.drop_nulls(subset=["datetime", "close"])
    if bars.is_empty():
        return None, "minute_empty", has_amount
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
        return None, "minute_empty", has_amount
    return out, basis, has_amount


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


def _auto_eligible(name: str, n_members: int, exclude_sectors: tuple[str, ...]) -> bool:
    """板块能否参与自动活跃榜: 名称不含排除子串且成员数不超上限。"""
    if n_members > _MAX_AUTO_MEMBERS:
        return False
    return not any(pattern in name for pattern in exclude_sectors)


def _rank_for_mode(
    sort_by: str,
    sectors: list[dict],
    activity_map: dict[str, float],
    has_amount: bool,
    flow_by_member: dict[str, float],
    flow_available: bool,
) -> list[str]:
    """自动榜按所选维度返回板块名降序列表 (未做排除/成员数过滤)。

    activity 量额缺失时退化综合分; flow 在扩展列不可用时退化综合分;
    pct/rank_change 的无值板块排最后; score 直接用 sectors 的既有排序。
    """
    if sort_by == "activity":
        if has_amount and activity_map:
            return [n for n, _ in sorted(activity_map.items(), key=lambda kv: kv[1], reverse=True)]
        return [item["name"] for item in sectors]
    if sort_by == "flow" and flow_available:
        return [
            name for name, _ in sorted(
                ((item["name"], flow_by_member.get(item["name"])) for item in sectors),
                key=lambda kv: (kv[1] is not None, kv[1] or 0.0),
                reverse=True,
            )
        ]
    if sort_by == "pct":
        return [
            item["name"] for item in sorted(
                sectors,
                key=lambda item: (item["pct_now"] is not None, item["pct_now"] or 0.0),
                reverse=True,
            )
        ]
    if sort_by == "rank_change":
        return [
            item["name"] for item in sorted(
                sectors,
                key=lambda item: (item["rank_change"] is not None, item["rank_change"] or 0),
                reverse=True,
            )
        ]
    if sort_by == "momentum":
        # 近端动量 = 现累计涨幅 - 1h 前累计涨幅: 走强 (加速上涨) 在前, 走弱 (减速/回落) 在后;
        # 热力图按此排序时, 上下分界直接呈现资金从走弱板块流向走强板块的方向
        return [
            item["name"] for item in sorted(
                sectors,
                key=lambda item: (
                    item["pct_now"] is not None and item["pct_prev"] is not None,
                    (item["pct_now"] or 0.0) - (item["pct_prev"] or 0.0),
                ),
                reverse=True,
            )
        ]
    return [item["name"] for item in sectors]


def build_sector_rotation(
    repo,
    *,
    kind: str = "concept",
    flow_field: str | None = None,
    top: int = 30,
    bucket_minutes: int = 5,
    series_names: list[str] | None = None,
    exclude_sectors: list[str] | None = None,
    auto_rows: int | None = None,
    sort_by: str = "activity",
) -> dict:
    """计算盘中板块切换走势 (概念/行业二选一)。

    exclude_sectors: 自动活跃榜排除板块 (名称子串匹配); None = 使用内置属性板块
    名单, [] = 清空名称过滤 (成员数上限仍生效)。仅影响自动选取, 自定义清单不过滤。
    auto_rows: 自动模式展示行数, 缺省 10, 范围 [1, 20]。
    sort_by: 自动榜排序维度 (activity/score/pct/rank_change/flow), 排除名单与
    成员数上限对全部维度生效, 不足展示行数时回退不过滤。

    返回结构:
      status/date/basis/kind/flow_field/bucket_minutes/member_count/flow_available/
      default_exclude_sectors: 内置排除名单 (供前端编辑器预填) /
      max_auto_members: 自动活跃榜成员数上限 /
      timeline: [{time, rotation, leader, leader_pct, market_pct,
                  cross_up, cross_down}] /
      cross_events: [{time, name, dir: "up"|"down", pct}] 0 轴穿越事件
        (全市场板块, 最新在前, 封顶 120 条; dir=up 转强/down 转弱) /
      sectors: [{name, pct_now, pct_prev, rank_now, rank_prev, rank_change,
                 flow, score, n_members, n_members_with_bars}] 按 score 降序截 top 条 /
      series: {buckets: [HH:MM], sectors: [展示板块名], matrix: [[各桶板块涨幅]]}
        展示板块与分钟桶涨幅矩阵, 供前端热力图/走势线; 展示板块 = series_names
        (自定义监控, ≤20, 不过滤) 或活跃榜 (sort_by 维度降序, 先剔除排除名单与
        成员数超上限的板块, 不足展示行数时回退不过滤; momentum 维度对半取样:
        前半=走强组, 后半=走弱组; N = auto_rows, 缺省 10); 某桶无该板块行情时为 null /
      universe: [{name, pct_now, activity, n_members, n_members_with_bars, excluded}]
        全部板块清单按活跃度降序 (自定义选择器的数据源, activity 为 None 排后,
        永不剔除; excluded=True 表示被自动活跃榜过滤, 仅影响自动选取)
    不可计算时返回 {status: "no_data"|"empty", reason, date?} (fail-closed, 不静默)。
    """
    if kind not in ("concept", "industry"):
        raise ValueError(f"不支持的板块维度: {kind} (可选 concept/industry)")
    if sort_by not in _SORT_MODES:
        raise ValueError(f"不支持的排序维度: {sort_by} (可选 {_SORT_MODES})")
    bucket_minutes = int(bucket_minutes)
    if bucket_minutes not in _ALLOWED_BUCKETS:
        raise ValueError(f"不支持的分钟桶: {bucket_minutes} (可选 {_ALLOWED_BUCKETS})")
    top = max(5, min(100, int(top)))
    flow_field = (flow_field or "").strip() or None
    if series_names:
        series_names = list(dict.fromkeys(str(n).strip() for n in series_names if str(n).strip()))[:_MAX_SERIES_ROWS] or None
    if exclude_sectors is None:
        exclude_effective: tuple[str, ...] = _DEFAULT_EXCLUDE_SECTORS
    else:
        # 显式入参 (含空数组) 整体替换内置名单; 空数组归一为 () = 关闭名称过滤
        exclude_effective = tuple(dict.fromkeys(
            str(n).strip() for n in exclude_sectors if str(n).strip()
        ))[:_MAX_EXCLUDE_PARAM]
    rows_limit = _DEFAULT_SERIES_ROWS if auto_rows is None else max(1, min(_MAX_SERIES_ROWS, int(auto_rows)))

    data_dir: Path = repo.store.data_dir
    cache_key = (
        kind, flow_field or "", top, bucket_minutes, tuple(series_names or ()),
        exclude_effective, rows_limit, sort_by,
    )
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit is not None and (now - _cache_ts.get(cache_key, 0.0)) < _CACHE_TTL:
        return hit

    result = _compute(repo, data_dir, kind, flow_field, top, bucket_minutes, series_names, exclude_effective, rows_limit, sort_by)
    _cache[cache_key] = result
    _cache_ts[cache_key] = time.monotonic()
    while len(_cache) > _CACHE_MAX_ENTRIES:
        oldest = min(_cache_ts, key=lambda k: _cache_ts.get(k, 0.0), default=None)
        if oldest is None or oldest == cache_key:
            break
        _cache.pop(oldest, None)
        _cache_ts.pop(oldest, None)
    return result


def _compute(repo, data_dir: Path, kind: str, flow_field: str | None, top: int, bucket_minutes: int, series_names: list[str] | None, exclude_sectors: tuple[str, ...], auto_rows: int, sort_by: str) -> dict:
    minute_dir = data_dir / "kline_minute"
    target = _latest_minute_partition(minute_dir)
    if not target:
        return {"status": "no_data", "reason": "minute_missing", "kind": kind}

    map_df, member_count = _load_concept_map_df(repo, kind)
    if map_df.is_empty() or member_count == 0:
        return {"status": "no_data", "reason": "members_missing", "date": target, "kind": kind}

    pcts, basis, has_amount = _minute_pcts(minute_dir, target)
    if pcts is None:
        return {"status": "no_data", "reason": basis, "date": target, "kind": kind}

    # 桶化 + 板块聚合: 先每股桶内均值 (成交额按桶合计), 再板块等权均值 (停牌/缺分钟不放大权重)
    member_df = map_df.rename({"_sym_up": "_bare", kind: "_member"})
    buckets = pcts.with_columns(pl.col("datetime").dt.truncate(f"{bucket_minutes}m").alias("_bucket"))
    stock_agg = [pl.col("_pct").mean().alias("_sym_pct")]
    if has_amount:
        stock_agg.append(pl.col("amount").sum().alias("_sym_amt"))
    by_sector = (
        buckets.join(member_df, on="_bare", how="inner")
        .group_by(["_bucket", "_member", "_bare"])
        .agg(stock_agg)
        .group_by(["_bucket", "_member"])
        .agg(
            pl.col("_sym_pct").mean().alias("_spct"),
            pl.len().alias("_n"),
            *( [pl.col("_sym_amt").sum().alias("_amt")] if has_amount else [] ),
        )
        .sort(["_bucket", "_spct"], descending=[False, True])
    )
    if by_sector.is_empty():
        return {"status": "empty", "reason": "no_member_bars", "date": target, "kind": kind}

    market = (
        buckets.group_by("_bucket").agg(pl.col("_pct").mean().alias("_mpct")).sort("_bucket")
    )

    # 每桶领涨板块 + top 集合 (供切换走势与排名对照) + 0 轴穿越统计 (涨跌切换:
    # 板块涨幅由负转正 = 转强/上穿, 由正转负 = 转弱/下穿; ≥0 计为多方)
    per_bucket: list[dict[str, Any]] = []
    cross_events: list[dict[str, Any]] = []
    prev_sign: dict[str, int] = {}
    for (bucket,), frame in by_sector.group_by("_bucket", maintain_order=True):
        names = frame["_member"].to_list()
        pcts = frame["_spct"].to_list()
        cross_up = cross_down = 0
        for name, pct in zip(names, pcts, strict=True):
            sign = 1 if pct >= 0 else -1
            prev = prev_sign.get(name)
            if prev is not None and prev != sign:
                event = {"time": bucket.strftime("%H:%M"), "name": name, "dir": "up" if sign > 0 else "down", "pct": _r4(pct)}
                if sign > 0:
                    cross_up += 1
                else:
                    cross_down += 1
                cross_events.append(event)
            prev_sign[name] = sign
        per_bucket.append({
            "bucket": bucket,
            "names": names,
            "pct": pcts,
            "top_set": set(names[:_TOP_OVERLAP]),
            "leader": names[0],
            "leader_pct": frame["_spct"][0],
            "cross_up": cross_up,
            "cross_down": cross_down,
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
            "cross_up": item["cross_up"],
            "cross_down": item["cross_down"],
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
    # 活跃度: 最近 _ACTIVITY_WINDOW_MIN 分钟成分股成交额合计 (量额列缺失时不可用)
    activity_map: dict[str, float] = {}
    if has_amount:
        k = max(1, round(_ACTIVITY_WINDOW_MIN / bucket_minutes))
        recent = [item["bucket"] for item in per_bucket[-k:]]
        activity_map = {
            row["_member"]: row["_amt"]
            for row in by_sector.filter(pl.col("_bucket").is_in(recent))
            .group_by("_member")
            .agg(pl.col("_amt").sum().alias("_amt"))
            .iter_rows(named=True)
        }
    all_names = sorted({name for item in per_bucket for name in item["names"]})
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

    # 全部板块清单 (自定义选择器数据源), 按活跃度降序, 无活跃度数据排后
    universe = [
        {
            "name": name,
            "pct_now": _r4(pcts_now.get(name)),
            "activity": _r4(activity_map.get(name)) if has_amount else None,
            "n_members": members_count_by_sector.get(name, 0),
            "n_members_with_bars": n_with_bars.get(name, 0),
            "excluded": not _auto_eligible(name, members_count_by_sector.get(name, 0), exclude_sectors),
        }
        for name in all_names
    ]
    universe.sort(key=lambda item: (item["activity"] is not None, item["activity"] or 0.0), reverse=True)

    # 展示板块与分钟桶涨幅矩阵 (前端热力图/走势线共用): 自定义监控板块 (≤20,
    # 剔除当日无行情的, 不过滤) 或活跃榜 (sort_by 维度降序, 先剔除排除名单与
    # 超成员数上限的板块, 不足展示行数时回退不过滤)
    if series_names:
        known = set(all_names)
        display_names = [n for n in series_names if n in known]
    else:
        ranked = _rank_for_mode(sort_by, sectors, activity_map, has_amount, flow_by_member, flow_available)
        eligible = [
            n for n in ranked
            if _auto_eligible(n, members_count_by_sector.get(n, 0), exclude_sectors)
        ]
        if sort_by == "momentum" and len(eligible) > auto_rows:
            # 强弱切换维度对半取样: 前半=走强组, 后半=走弱组 (中部平庸板块跳过)。
            # 否则 TopN 全是走强板块, 热力图看不到强弱对比
            half = (auto_rows + 1) // 2
            rest = auto_rows - half
            display_names = eligible[:half] + (eligible[-rest:] if rest > 0 else [])
        else:
            display_names = eligible[:auto_rows]
        if len(display_names) < auto_rows:
            display_names = ranked[:auto_rows]
    pct_maps = [dict(zip(item["names"], item["pct"], strict=True)) for item in per_bucket]
    series = {
        "buckets": [item["bucket"].strftime("%H:%M") for item in per_bucket],
        "sectors": display_names,
        "matrix": [[_r4(m.get(name)) for m in pct_maps] for name in display_names],
    }

    return {
        "status": "ok",
        "date": target,
        "kind": kind,
        "basis": basis,
        "flow_field": flow_field,
        "flow_available": flow_available,
        "bucket_minutes": bucket_minutes,
        "member_count": member_count,
        "default_exclude_sectors": list(_DEFAULT_EXCLUDE_SECTORS),
        "max_auto_members": _MAX_AUTO_MEMBERS,
        "as_of": per_bucket[-1]["bucket"].strftime("%H:%M"),
        "timeline": timeline,
        "cross_events": list(reversed(cross_events[-120:])),
        "sectors": sectors[:top],
        "series": series,
        "universe": universe,
    }
