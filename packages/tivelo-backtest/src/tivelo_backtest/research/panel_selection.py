"""Shared selection for the existing Polars daily panel path."""

import polars as pl

from .scoring import SCORING_DIRECTION_LOW, materialize_scoring_columns, scoring_value_expr

DEFAULT_BASIC_FILTER: dict = {
    "price_min": 3,
    "price_max": 300,
    "market_cap_min": 10e8,
    "float_cap_min": None,
    "float_cap_max": None,
    "amount_min": 0.2e8,
    "amount_max": None,
    "turnover_min": None,
    "turnover_max": None,
    "exclude_st": True,
    "exclude_new_days": 30,
    "boards": ["沪主板", "深主板", "创业板", "科创板", "北交所"],
}

MINUTE_JOIN_COLUMNS = (
    "name", "total_shares", "float_shares", "amount",
    "turnover_rate", "change_pct", "pre_close",
)


def join_minute_daily_fields(rows: pl.DataFrame, current: pl.DataFrame) -> pl.DataFrame:
    """Fill missing fields from the previous completed daily row."""
    columns = [name for name in MINUTE_JOIN_COLUMNS if name in current.columns and name not in rows.columns]
    if not columns:
        return rows
    values = current.select("symbol", *columns).unique(subset=["symbol"], keep="last")
    return rows.join(values, on="symbol", how="left")


def score_strategy_rows(frame, weights, directions=None, *, value_expr=scoring_value_expr) -> pl.DataFrame:
    """Score one candidate cross-section without adding scores for unscored strategies."""
    executable = [
        (str(name), value, weight) for name, weight in weights.items()
        if weight and (value := value_expr(frame.columns, str(name))) is not None
    ]
    total_weight = sum(weight for _, _, weight in executable)
    if total_weight <= 0:
        return frame

    parts = []
    for name, value, weight in executable:
        minimum = value.min()
        span = value.max() - minimum
        normalized = pl.when(span > 0).then((value - minimum) / span).otherwise(pl.lit(0.5))
        if (directions or {}).get(name) == SCORING_DIRECTION_LOW:
            normalized = 1.0 - normalized
        parts.append(normalized * (weight / total_weight))
    score = parts[0]
    for part in parts[1:]:
        score = score + part
    return frame.with_columns((score * 100).alias("score"))


def strategy_result_limit(meta, overrides) -> int | None:
    value = (overrides or {}).get("display_limit", meta.get("limit", 100))
    if value in (None, 0):
        return None
    return max(0, int(value))


def sort_minute_strategy_rows(frame: pl.DataFrame, order_by: str | None, descending: bool) -> pl.DataFrame:
    first = "score" if "score" in frame.columns else order_by
    keys = list(dict.fromkeys(key for key in (first, "symbol", "last_datetime") if key in frame.columns))
    return frame.sort(keys, descending=[descending if key == first else False for key in keys])


def materialize_strategy_frames(current, history, scoring, *, materialize=materialize_scoring_columns):
    """Prepare factor columns once on the history and bring its current values forward."""
    names = [str(name) for name, weight in scoring.items() if weight]
    if not names:
        return current, history
    if history is None or history.is_empty():
        return (
            materialize(current, names) if current is not None else None,
            history,
        )

    scored_history = materialize(history, names)
    if current is None or current.is_empty():
        return current, scored_history
    join_keys = [key for key in ("symbol", "date", "datetime") if key in current.columns and key in scored_history.columns]
    added = [name for name in names if name not in current.columns and name in scored_history.columns]
    if not join_keys or not added:
        return materialize(current, names), scored_history
    values = scored_history.select([*join_keys, *added]).unique(subset=join_keys, keep="last")
    return current.join(values, on=join_keys, how="left"), scored_history

def panel_basic_filter(df: pl.DataFrame, bf: dict) -> pl.Expr | None:
    """构建基础过滤表达式。回测可复用为买入候选 mask，不删除行情行。"""
    exprs: list[pl.Expr] = []
    if bf.get("price_min") is not None:
        exprs.append(pl.col("close") >= bf["price_min"])
    if bf.get("price_max") is not None:
        exprs.append(pl.col("close") <= bf["price_max"])
    if bf.get("market_cap_min") is not None and "total_shares" in df.columns:
        exprs.append(
            pl.col("close") * pl.col("total_shares") >= bf["market_cap_min"]
        )
    if bf.get("market_cap_max") is not None and "total_shares" in df.columns:
        exprs.append(
            pl.col("close") * pl.col("total_shares") <= bf["market_cap_max"]
        )
    # 流通市值
    if bf.get("float_cap_min") is not None and "float_shares" in df.columns:
        exprs.append(
            pl.col("close") * pl.col("float_shares") >= bf["float_cap_min"]
        )
    if bf.get("float_cap_max") is not None and "float_shares" in df.columns:
        exprs.append(
            pl.col("close") * pl.col("float_shares") <= bf["float_cap_max"]
        )
    if bf.get("amount_min") is not None:
        exprs.append(pl.col("amount") >= bf["amount_min"])
    if bf.get("amount_max") is not None:
        exprs.append(pl.col("amount") <= bf["amount_max"])
    # 换手率
    if bf.get("turnover_min") is not None and "turnover_rate" in df.columns:
        exprs.append(pl.col("turnover_rate") >= bf["turnover_min"])
    if bf.get("turnover_max") is not None and "turnover_rate" in df.columns:
        exprs.append(pl.col("turnover_rate") <= bf["turnover_max"])
    if bf.get("exclude_st") and "name" in df.columns:
        exprs.append(~pl.col("name").str.contains("(?i)ST|\\*ST|退"))
    elif bf.get("exclude_st") and "is_st" in df.columns:
        exprs.append(pl.col("is_st") == 0)
    # 板块过滤
    boards = bf.get("boards")
    if boards and isinstance(boards, list) and len(boards) > 0:
        board_exprs: list[pl.Expr] = []
        for b in boards:
            if b == "沪主板":
                board_exprs.append(pl.col("symbol").str.starts_with("60"))
            elif b == "深主板":
                board_exprs.append(
                    pl.col("symbol").str.starts_with("00")
                    | pl.col("symbol").str.starts_with("001")
                )
            elif b == "创业板":
                board_exprs.append(
                    pl.col("symbol").str.starts_with("300")
                    | pl.col("symbol").str.starts_with("301")
                    | pl.col("symbol").str.starts_with("302")
                )
            elif b == "科创板":
                board_exprs.append(
                    pl.col("symbol").str.starts_with("688")
                    | pl.col("symbol").str.starts_with("689")
                )
            elif b == "北交所":
                board_exprs.append(pl.col("symbol").str.contains(r"\.BJ$"))
        if board_exprs:
            exprs.append(pl.any_horizontal(board_exprs))
    if exprs:
        return pl.all_horizontal(exprs)
    return None


def panel_history_mask(panel: pl.DataFrame, hits: pl.DataFrame | None) -> pl.Series:
    """Project the strategy's returned keys back onto the original bar order."""
    if hits is not None and not isinstance(hits, pl.DataFrame):
        raise TypeError("History filter must return a Polars DataFrame")
    if hits is None or hits.is_empty():
        return pl.repeat(False, panel.height, eager=True)
    keys = ["symbol", "date"]
    if not set(keys).issubset(hits.columns):
        raise ValueError("History filter must return symbol and date keys")
    return panel.select(keys).join(
        hits.select(keys).unique().with_columns(pl.lit(True).alias("_hit")),
        on=keys, how="left", maintain_order="left",
    )["_hit"].fill_null(False)


def panel_signal_column(signal: str) -> str:
    if signal.startswith(("signal_", "csg_")):
        return signal
    return f"signal_{signal}"


def panel_signal_mask(panel: pl.DataFrame, signals: list[str], name: str) -> pl.Series:
    """向量化合并信号列，多个信号 OR。支持内置 signal_ 与自定义 csg_ 前缀。"""
    masks: list[pl.Series] = []
    for sig in signals:
        col = panel_signal_column(sig)
        if col in panel.columns:
            masks.append(panel[col].fill_null(False).cast(pl.Boolean))

    if not masks:
        return pl.Series(name, [False] * len(panel), dtype=pl.Boolean)

    combined = masks[0]
    for m in masks[1:]:
        combined = combined | m
    return combined

def apply_panel_score(
    panel,
    scoring,
    directions,
    *,
    order_by=None,
    descending=True,
    universe_mask=None,
    factor_snapshot=None,
    materialize=materialize_scoring_columns,
    value_expr=scoring_value_expr,
) -> pl.DataFrame:
    """Rank the candidate universe, before applying the buy trigger."""
    work = materialize(panel, scoring.keys())
    temporary_scoring_columns = [name for name in scoring if name not in panel.columns and name in work.columns]
    has_universe = universe_mask is not None and len(universe_mask) == len(panel)
    if has_universe:
        work = work.with_columns(universe_mask.rename("_score_universe"))

    # 因子归因快照: 在临时因子列被 _finish 丢弃前, 截取候选行的
    # (symbol, date, 因子值)。与评分共用同一份物化结果, 无第二次计算。
    if factor_snapshot is not None:
        snapshot_cols = ["symbol", "date"] + [
            name for name in scoring if name in work.columns
        ]
        if len(snapshot_cols) > 2:
            frame = work
            if has_universe:
                frame = frame.filter(pl.col("_score_universe"))
            factor_snapshot["frame"] = frame.select(snapshot_cols)

    def _value_in_universe(value: pl.Expr) -> pl.Expr:
        if has_universe:
            return pl.when(pl.col("_score_universe")).then(value).otherwise(None)
        return value

    def _finish(df: pl.DataFrame) -> pl.DataFrame:
        temporary = [
            name
            for name in ["_score_universe", *temporary_scoring_columns]
            if name in df.columns
        ]
        return df.drop(temporary) if temporary else df

    if scoring:
        executable = [
            (str(col), value, weight)
            for col, weight in scoring.items()
            if weight and (value := value_expr(work.columns, str(col))) is not None
        ]
        total_weight = sum(weight for _, _, weight in executable)
        if total_weight > 0:
            score_parts: list[pl.Expr] = []
            for name, score_value, weight in executable:
                w = weight / total_weight
                value = _value_in_universe(score_value)
                col_min = value.min().over("date")
                col_max = value.max().over("date")
                col_range = col_max - col_min
                normalized = pl.when(col_range > 0).then(
                    (score_value - col_min) / col_range
                ).otherwise(pl.lit(0.5))
                if directions.get(name) == SCORING_DIRECTION_LOW:
                    normalized = 1.0 - normalized
                if has_universe:
                    normalized = pl.when(pl.col("_score_universe")).then(normalized).otherwise(0.0)
                score_parts.append(normalized * w)
            if score_parts:
                score_expr = score_parts[0]
                for part in score_parts[1:]:
                    score_expr = score_expr + part
                return _finish(work.with_columns((score_expr * 100).fill_null(0).alias("score")))

    if order_by and order_by != "score" and order_by in work.columns:
        direction = 1 if descending else -1
        score_expr = pl.col(order_by).fill_null(0) * direction
        if has_universe:
            score_expr = pl.when(pl.col("_score_universe")).then(score_expr).otherwise(0.0)
        return _finish(work.with_columns(score_expr.alias("score")))
    return _finish(work.with_columns(pl.lit(0.0).alias("score")))
