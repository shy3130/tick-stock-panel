"""市场主线(板块/概念)识别 — 基于涨停梯队的历史聚合。

用户判据的量化: 主升阶段的主线 = 同一概念内涨停家数多、最高连板高、
梯队档位填得满(2 板到最高板之间不断层)。对每个交易日按概念聚合涨停梯队,
截面 rank 归一后加权成主线分, 持久化为日频时序, 供市场环境页展示
"什么阶段走什么主升"。

口径限制(重要): 概念成分来自 ext_gn_ths 快照(本地自 2026-07 起留存, 无历史
版本)。历史主线是把"今天的成分"回看历史 — 早年存在归属漂移(新概念不会
出现在旧时段、成分调整会错归属)。MEMBERSHIP_NOTE 随 API 返回给前端展示。

性能: 全量回填只窄扫 enriched 的 4 列并先过滤连板 >=1(全历史 ~10 万行),
join 概念映射后 group_by, 峰值内存 <100MB。
"""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.services.rps_rotation import _load_concept_map_df

logger = logging.getLogger(__name__)

MEMBERSHIP_NOTE = (
    "概念成分为当前快照回看历史(本地自 2026-07 起留存, 无历史版本), "
    "早年主线存在归属漂移, 越近越准"
)

MAINLINE_DIR = "mainline_history"
_TOP_PER_DAY = 30          # 每日持久化的主线数(按分数截断)
_INDUSTRY_LEVEL = 2        # 行业主线取前两级(如 计算机-软件开发)
_MIN_LIMIT_UP = 3          # 单概念当日最少涨停家数(低于此不参与排名)

# 主线分权重: 概念内涨停家数 / 最高连板 / 梯队档位数 / 二板以上家数
_SCORE_WEIGHTS = {
    "limit_up_count": 0.35,
    "max_boards": 0.25,
    "rungs_filled": 0.25,
    "ge2_count": 0.15,
}


def _resolve_filter_config(filter_cfg: dict | None) -> dict:
    """解析过滤配置; None 时读用户偏好(宽基/风格标签过滤, 见 preferences 文档)。"""
    if filter_cfg is not None:
        return {
            "min_members": int(filter_cfg.get("min_members", 4)),
            "max_members": int(filter_cfg.get("max_members", 600)),
            "blacklist": {str(x) for x in filter_cfg.get("blacklist") or []},
        }
    try:
        from app.services import preferences

        cfg = preferences.get_mainline_filter_config()
        return {
            "min_members": int(cfg["min_members"]),
            "max_members": int(cfg["max_members"]),
            "blacklist": set(cfg["blacklist"]),
        }
    except Exception:
        return {"min_members": 4, "max_members": 600, "blacklist": set()}


def mainline_path(data_dir: Path) -> Path:
    return data_dir / MAINLINE_DIR / "part.parquet"


_ST_SYMBOLS_CACHE: tuple[float, frozenset[str]] | None = None


def load_risk_warning_symbols(data_dir: Path) -> frozenset[str]:
    """当前维表快照中名称含 ST 标记的 symbol 集合(大写), 供主线/情绪统计剔除。

    判定与 indicators 涨跌停口径共用同一权威实现(price_limits.polars_is_risk_warning_name,
    即名称含 "ST", 覆盖 ST/*ST/S*ST)。维表是快照无历史版本, 与概念成分同样的
    回看限制。600s 进程内缓存(维表 snapshot 进程内不变)。
    """
    global _ST_SYMBOLS_CACHE
    now = time.time()
    if _ST_SYMBOLS_CACHE is not None and now - _ST_SYMBOLS_CACHE[0] < 600:
        return _ST_SYMBOLS_CACHE[1]
    from app.price_limits import polars_is_risk_warning_name

    syms: frozenset[str] = frozenset()
    inst_dir = data_dir / "instruments"
    if inst_dir.exists():
        try:
            df = pl.read_parquet(inst_dir / "**" / "*.parquet").select(["symbol", "name"])
            st = df.filter(polars_is_risk_warning_name(pl.col("name")))
            syms = frozenset(s.upper() for s in st["symbol"].to_list())
        except Exception as e:
            logger.warning("load risk-warning symbols failed: %s", e)
    _ST_SYMBOLS_CACHE = (now, syms)
    return syms


def load_mainline_history(data_dir: Path, kind: str = "concept") -> pl.DataFrame:
    """读取主线时序(全部 kind), 不存在返回空 DataFrame。"""
    p = mainline_path(data_dir)
    if not p.exists():
        return pl.DataFrame()
    try:
        df = pl.read_parquet(p)
    except Exception as e:
        logger.warning("load_mainline_history failed: %s", e)
        return pl.DataFrame()
    if df.is_empty() or "kind" not in df.columns:
        return df
    return df.filter(pl.col("kind") == kind)


def _industry_member(member: str, kind: str) -> str:
    """行业维度取前 _INDUSTRY_LEVEL 级; 概念原样返回。"""
    if kind != "industry":
        return member
    return "-".join(member.split("-")[:_INDUSTRY_LEVEL])


def compute_mainline_range(repo, data_dir: Path, start: date, end: date,
                           kind: str = "concept",
                           filter_cfg: dict | None = None,
                           exclude_st: bool | None = None) -> pl.DataFrame:
    """计算 [start, end] 每日主线排行(按 _SCORE_WEIGHTS 加权截面分)。

    filter_cfg: {"min_members", "max_members", "blacklist"}; None 时读用户偏好。
    宽基/风格标签(融资融券/沪深股通等数千成分)按成员数上限过滤,
    用户黑名单按名称过滤(不论大小)。修改配置后重算主线生效。
    exclude_st: 是否剔除风险警示(ST)股(按当前维表名称); None 时读用户偏好
    (默认剔除 — ST 是状态桶非题材, 主板 5% 便宜板时代曾系统性霸榜)。

    返回列: date, kind, member, limit_up_count, ge2_count, max_boards,
    boards_sum, rungs_filled, leader_symbol, score, rank。空数据返回空表。
    """
    if start > end:
        return pl.DataFrame()
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return pl.DataFrame()

    # _load_concept_map_df 恒返回 (map_df, count), 命中缓存不再返回裸 DataFrame (#186)
    map_df, _ = _load_concept_map_df(repo, kind)
    if map_df.is_empty():
        return pl.DataFrame()

    cfg = _resolve_filter_config(filter_cfg)
    if cfg["min_members"] > 1 or cfg["max_members"] < 5000 or cfg["blacklist"]:
        member_counts = map_df.group_by(kind).len().rename({"len": "_members"})
        member_counts = member_counts.filter(
            pl.col("_members").ge(cfg["min_members"])
            & pl.col("_members").le(cfg["max_members"])
            & ~pl.col(kind).is_in(sorted(cfg["blacklist"]))
        )
        allowed = member_counts.select(kind)
        map_df = map_df.join(allowed, on=kind, how="semi")
        if map_df.is_empty():
            return pl.DataFrame()

    limit_rows = (
        pl.scan_parquet(enriched_dir / "**" / "*.parquet")
        .select(["date", "symbol", "consecutive_limit_ups", "amount"])
        .filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
            & (pl.col("consecutive_limit_ups") >= 1)
        )
        .collect()
    )
    if limit_rows.is_empty():
        return pl.DataFrame()

    limit_rows = limit_rows.with_columns(pl.col("symbol").str.to_uppercase().alias("_sym_up"))

    # 剔除风险警示股: ST 板块的涨停生态(主板曾 5% 便宜板)不代表题材主线。
    if exclude_st is None:
        try:
            from app.services import preferences
            exclude_st = preferences.get_sentiment_exclude_st()
        except Exception:
            exclude_st = True
    if exclude_st:
        st_syms = load_risk_warning_symbols(repo.store.data_dir)
        if st_syms:
            limit_rows = limit_rows.filter(~pl.col("_sym_up").is_in(sorted(st_syms)))

    joined = limit_rows.join(map_df, on="_sym_up", how="inner")
    if joined.is_empty():
        return pl.DataFrame()
    joined = joined.with_columns(
        pl.col(kind).map_elements(
            lambda m: _industry_member(str(m), kind),
            return_dtype=pl.Utf8,
        ).alias("member")
    )

    agg = (
        joined.group_by(["date", "member"])
        .agg(
            pl.len().alias("limit_up_count"),
            (pl.col("consecutive_limit_ups") >= 2).sum().alias("ge2_count"),
            pl.col("consecutive_limit_ups").max().alias("max_boards"),
            pl.col("consecutive_limit_ups").sum().alias("boards_sum"),
            pl.col("consecutive_limit_ups")
              .filter(pl.col("consecutive_limit_ups") >= 2)
              .n_unique()
              .alias("rungs_filled"),
            pl.col("symbol")
              .sort_by(
                  pl.col("consecutive_limit_ups"), pl.col("amount"),
                  descending=[True, True],
              )
              .first()
              .alias("leader_symbol"),
        )
    )

    # 截面 rank 归一(0-1) → 加权主线分(0-100)。分母 max(n-1,1) 保证单概念日不除零。
    agg = agg.filter(pl.col("limit_up_count") >= _MIN_LIMIT_UP)
    norm_exprs = []
    for col in _SCORE_WEIGHTS:
        norm_exprs.append(
            ((pl.col(col).rank(method="average") - 1.0)
             / pl.max_horizontal(pl.len().over("date") - 1, 1)).over("date").alias(f"_{col}_r")
        )
    agg = agg.with_columns(norm_exprs)
    agg = agg.with_columns(
        (
            100.0 * sum(
                _SCORE_WEIGHTS[col] * pl.col(f"_{col}_r") for col in _SCORE_WEIGHTS
            )
        ).alias("score")
    )
    agg = agg.with_columns(
        pl.col("score").rank(method="ordinal", descending=True).over("date").alias("rank")
    )
    result = (
        agg.filter(pl.col("rank") <= _TOP_PER_DAY)
        .drop([f"_{col}_r" for col in _SCORE_WEIGHTS])
        .with_columns(pl.lit(kind).alias("kind"))
        .select([
            "date", "kind", "member", "limit_up_count", "ge2_count",
            "max_boards", "boards_sum", "rungs_filled", "leader_symbol",
            "score", "rank",
        ])
        .sort(["date", "rank"])
    )
    return result


def upsert_mainline_history(data_dir: Path, new_rows: pl.DataFrame) -> None:
    """按 (date, kind) 整日覆盖 upsert; schema 以 new_rows 为权威(同 regime 模式)。"""
    if new_rows.is_empty() or "date" not in new_rows.columns:
        return
    p = mainline_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = pl.read_parquet(p) if p.exists() else pl.DataFrame()
    if old.is_empty():
        combined = new_rows
    else:
        # 按 (date, kind) 整日覆盖: anti-join 掉本次重算的 (日, 维度) 组合
        kept = old.join(
            new_rows.select(["date", "kind"]).unique(),
            on=["date", "kind"],
            how="anti",
        )
        target_cols = new_rows.columns
        keep_exprs = [
            pl.col(c) if c in kept.columns else pl.lit(None).alias(c)
            for c in target_cols
        ]
        kept = kept.select(keep_exprs)
        combined = pl.concat([kept, new_rows.select(target_cols)], how="vertical_relaxed")
    combined = combined.sort(["date", "kind", "rank"])
    atomic_write_parquet(combined, p)


def compute_mainline_incremental(repo, data_dir: Path, *, today: date | None = None,
                                 kind: str = "concept") -> pl.DataFrame:
    """增量补算主线(供 daily_pipeline / 手动触发): 补 enriched 已有而主线缺失的日。"""
    today = today or cn_today()
    from app.services.regime_builder import enriched_date_set

    enriched_dates = enriched_date_set(repo)
    existing = load_mainline_history(data_dir, kind)
    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    missing = sorted(d for d in enriched_dates if d not in existing_dates and d <= today)
    if not missing:
        return pl.DataFrame()
    logger.info("mainline incremental(%s): compute %d days", kind, len(missing))
    new_rows = compute_mainline_range(repo, data_dir, missing[0], missing[-1], kind=kind)
    if not new_rows.is_empty():
        upsert_mainline_history(data_dir, new_rows)
    return new_rows
