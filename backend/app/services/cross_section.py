"""Cross-sectional analysis suite — pure Polars, local read-only.

Four analyses computed entirely from the local enriched parquet + financial
snapshot.  No external data sources, no writes, no order generation, no stock
recommendations.

Endpoints
---------
1. ``compute_correlation_matrix``  — pairwise Pearson correlation of daily returns
2. ``compute_relative_strength``   — stock NAV vs benchmark NAV + window returns
3. ``compute_peer_comparison``     — same-universe ranking by market/fundamental metrics
4. ``compute_reverse_screen``      — relaxed screener conditions from a stock's features

Design invariants
-----------------
* Every function takes ``repo`` (the :class:`KlineRepository`) and returns a plain
  ``dict`` ready for JSON serialisation.
* Strict parameter ceilings are enforced via :class:`ValueError`.
* Minimum-sample and flat-series (zero-variance) guards return ``null`` cells —
  never a spurious correlation.
* Date alignment is an inner-join (``drop_nulls`` after pivot): only common
  trading days contribute.
* Reverse-screen re-uses :func:`execute_query` from the screener — it never
  issues its own external requests.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.services.screener_financials import (
    FinancialSnapshotError,
    load_financial_snapshot,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

_WINDOW_CHOICES = (60, 120, 180)
_BENCHMARKS = ("000001.SH", "399001.SZ", "399006.SZ")
_BENCHMARK_LABELS = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
}
_RS_WINDOWS = (10, 20, 60)
_FLAT_VAR_THRESHOLD = 1e-12
_RELAX_LOWER = 0.4   # midpoint of upstream 0.25-0.5 band
_RELAX_UPPER = 2.0   # midpoint of upstream 1.8-2.5 band
_REVERSE_SCREEN_LIMIT = 80
_PEER_ALLROWS_CAP = 200
_MAX_POINTS = 130

_BOARD_NAMES = {
    "sh_main": "沪市主板",
    "sz_main": "深市主板",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
}

_PEER_SORT_MAP = {
    "amount": "amount_yi",
    "change_pct": "change_pct",
    "turnover_rate": "turnover_rate",
    "roe": "weight_avg_roe",
    "pe": "pe",
    "pb": "pb",
    "market_cap": "total_market_cap",
}

_BOUNDARY_NOTES = {
    "correlation": [
        "收益率基于 enriched 前复权 close 的 pct_change，对齐方式为日期内连接。",
        "停牌日因缺数据自动从共同窗口中排除；矩阵只反映共同交易日的收益相关。",
        "零方差（flat-series）单元格返回 null，不产生伪相关。",
    ],
    "relative_strength": [
        "个股收盘价取自 enriched 前复权 close；指数取自独立指数 enriched。",
        "行业等权代理为同行业成员当日 close 简单平均的合成序列，非官方指数，存在历史成分偏差。",
        "停牌/复权差异不精细处理；窗口回报基于对齐后的交易日。",
    ],
    "peer_comparison": [
        "数据来自 enriched 横截面快照（最新交易日）+ 财务公告口径 snapshot。",
        "industry 仅覆盖有财报的标的；缺 industry 的标的不进入同业 universe。",
    ],
    "reverse_screen": [
        "条件由标的最新横截面特征自动生成，宽松因子：下界 ×0.4 / 上界 ×2.0。",
        "结果仅为研究排序候选，不构成任何买卖建议或投资推荐。",
    ],
}


# ── Utility helpers ───────────────────────────────────────────────────────


def _safe_float(v: Any) -> float | None:
    """Return *v* if it is a finite float, else ``None``."""
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return float(v)
    return v


def _json_safe(v: Any) -> Any:
    """Sanitise scalar for JSON: NaN/Inf → None, date → ISO string."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _iso(d: Any) -> str | None:
    if d is None:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _detect_board(symbol: str) -> str | None:
    """Map an A-share symbol to its board code."""
    s = symbol.upper() if isinstance(symbol, str) else str(symbol or "")
    prefix = s[:3] if len(s) >= 3 else s
    if prefix in ("600", "601", "603", "605") and s.endswith(".SH"):
        return "sh_main"
    if prefix in ("000", "001", "002", "003") and s.endswith(".SZ"):
        return "sz_main"
    if prefix in ("300", "301") and s.endswith(".SZ"):
        return "chinext"
    if prefix in ("688", "689") and s.endswith(".SH"):
        return "star"
    if s.endswith(".BJ"):
        return "bse"
    return None


def _add_board_column(df: pl.DataFrame) -> pl.DataFrame:
    """Polars-native board classification via ``str.slice`` + ``is_in``."""
    sym = pl.col("symbol").cast(pl.String)
    return df.with_columns(
        pl.when(sym.str.slice(0, 3).is_in(["600", "601", "603", "605"]) & sym.str.ends_with(".SH"))
        .then(pl.lit("sh_main"))
        .when(sym.str.slice(0, 3).is_in(["000", "001", "002", "003"]) & sym.str.ends_with(".SZ"))
        .then(pl.lit("sz_main"))
        .when(sym.str.slice(0, 3).is_in(["300", "301"]) & sym.str.ends_with(".SZ"))
        .then(pl.lit("chinext"))
        .when(sym.str.slice(0, 3).is_in(["688", "689"]) & sym.str.ends_with(".SH"))
        .then(pl.lit("star"))
        .when(sym.str.ends_with(".BJ"))
        .then(pl.lit("bse"))
        .otherwise(None)
        .alias("_board")
    )


# ── Parameter validation ──────────────────────────────────────────────────


def _require_window(window: int) -> None:
    if window not in _WINDOW_CHOICES:
        raise ValueError(f"window must be one of {list(_WINDOW_CHOICES)}")


def _require_range(val: int, name: str, lo: int, hi: int) -> None:
    if not (lo <= val <= hi):
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {val}")


# ── Financial snapshot loader (sanitised) ─────────────────────────────────


def _load_financials_safe(data_dir: Path, as_of: date) -> pl.DataFrame:
    try:
        return load_financial_snapshot(data_dir, as_of)
    except FinancialSnapshotError:
        return pl.DataFrame()
    except Exception as exc:  # noqa: BLE001
        logger.debug("financial snapshot load failed: %s", exc)
        return pl.DataFrame()


# ── Peer universe resolution ──────────────────────────────────────────────


def _resolve_industry_peers(
    repo: Any,
    symbol: str,
    max_peers: int,
    latest: date,
) -> tuple[list[str], str | None]:
    """Return ``(peer_symbols, industry_label)``.

    Peers are same-industry members ranked by ``amount`` desc (top *max_peers*).
    Falls back to top-by-amount across the market when industry is unknown.
    """
    enriched, _ = repo.get_enriched_latest()
    if enriched is None or enriched.is_empty() or "amount" not in enriched.columns:
        return [], None

    fin = _load_financials_safe(Path(repo.store.data_dir), latest)

    # Determine selected's industry
    industry: str | None = None
    if fin is not None and not fin.is_empty() and "industry" in fin.columns:
        row = fin.filter(pl.col("symbol") == symbol)
        if not row.is_empty():
            industry = row.row(0, named=True).get("industry")

    candidates = enriched.filter(pl.col("symbol") != symbol)
    if "amount" in candidates.columns:
        candidates = candidates.sort("amount", descending=True)

    if industry:
        # Same-industry members
        if fin is not None and not fin.is_empty() and "industry" in fin.columns:
            members = fin.filter(pl.col("industry") == industry)["symbol"].to_list()
            if members:
                peers = (
                    candidates.filter(pl.col("symbol").is_in(members))
                    .head(max_peers)["symbol"]
                    .to_list()
                )
                return peers, industry

    # Fallback: top by amount regardless of industry
    peers = candidates.head(max_peers)["symbol"].to_list()
    return peers, None


# ── Correlation ───────────────────────────────────────────────────────────


def _compute_returns(close_df: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol daily returns via ``pct_change().over('symbol')``; first-day null dropped."""
    return (
        close_df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        .sort(["symbol", "date"])
        .with_columns(pl.col("close").pct_change().over("symbol").alias("ret"))
        .filter(pl.col("ret").is_not_null())
        .select(["date", "symbol", "ret"])
    )


def _align_returns(returns: pl.DataFrame, instruments: list[str]) -> pl.DataFrame:
    """Pivot to date × symbol wide form and inner-join on dates (``drop_nulls``)."""
    symbols_present = set(returns["symbol"].unique().to_list()) if not returns.is_empty() else set()
    available = [s for s in instruments if s in symbols_present]
    if not available:
        return pl.DataFrame()
    pivoted = returns.pivot("symbol", index="date", values="ret")
    cols = ["date"] + [s for s in available if s in pivoted.columns]
    return pivoted.select(cols).drop_nulls()


def _pair_stats(
    wide: pl.DataFrame,
    col_x: str,
    col_y: str,
    min_samples: int,
    var_cache: dict[str, float | None],
) -> dict[str, Any] | None:
    """Pearson correlation + sample covariance for one pair, with flat-series guard."""
    if col_x not in wide.columns or col_y not in wide.columns:
        return None
    n = wide.height
    if n < max(min_samples, 3):
        return None
    vx = var_cache.get(col_x)
    vy = var_cache.get(col_y)
    if vx is None or vy is None:
        return None
    if vx <= _FLAT_VAR_THRESHOLD or vy <= _FLAT_VAR_THRESHOLD:
        return None
    row = wide.select(
        pl.corr(col_x, col_y, method="pearson").alias("correlation"),
        pl.cov(col_x, col_y).alias("covariance"),
    ).row(0, named=True)
    return {
        "correlation": _safe_float(row["correlation"]),
        "covariance": _safe_float(row["covariance"]),
        "varianceX": vx,
        "varianceY": vy,
        "samples": n,
    }


def _compute_corr_matrix_from_returns(
    aligned: pl.DataFrame,
    instruments: list[str],
    window: int,
    min_samples: int,
) -> dict[str, Any]:
    """Pure computation of correlation matrix from an aligned (pivoted) returns frame.

    ``instruments[0]`` is treated as the selected symbol for pair-row / beta purposes.
    """
    available = [s for s in instruments if s in aligned.columns]
    if not available or aligned.height < 2:
        return _empty_matrix(available or instruments)

    n = aligned.height
    current = aligned.tail(window) if n > window else aligned
    prev_end = max(0, n - window)
    prev_len = max(0, min(window, prev_end))
    previous = aligned.slice(max(0, prev_end - window), prev_len) if prev_len > 0 else pl.DataFrame()

    # Variance cache — single pass per window
    var_cur: dict[str, float | None] = {}
    if current.height > 0:
        row = current.select([pl.col(s).var().alias(s) for s in available]).row(0, named=True)
        var_cur = {s: _safe_float(row.get(s)) for s in available}

    var_prev: dict[str, float | None] = {}
    if previous.height > 0:
        row = previous.select([pl.col(s).var().alias(s) for s in available]).row(0, named=True)
        var_prev = {s: _safe_float(row.get(s)) for s in available}

    # N × N matrices
    corr_m: list[list[float | None]] = []
    cov_m: list[list[float | None]] = []
    samples_m: list[list[int | None]] = []
    for si in available:
        c_row: list[float | None] = []
        cv_row: list[float | None] = []
        s_row: list[int | None] = []
        for sj in available:
            st = _pair_stats(current, si, sj, min_samples, var_cur)
            c_row.append(st["correlation"] if st else None)
            cv_row.append(st["covariance"] if st else None)
            s_row.append(st["samples"] if st else None)
        corr_m.append(c_row)
        cov_m.append(cv_row)
        samples_m.append(s_row)

    # Pair rows — selected (available[0]) vs each peer
    pair_rows: list[dict[str, Any]] = []
    selected = available[0]
    sel_var = var_cur.get(selected)
    for peer in available[1:]:
        st = _pair_stats(current, selected, peer, min_samples, var_cur)
        pst = _pair_stats(previous, selected, peer, min_samples, var_prev)
        corr = st["correlation"] if st else None
        prev_corr = pst["correlation"] if pst else None
        delta = corr - prev_corr if (corr is not None and prev_corr is not None) else None
        beta = st["covariance"] / sel_var if (st and sel_var and sel_var > _FLAT_VAR_THRESHOLD) else None
        pair_rows.append({
            "peer": peer,
            "correlation": corr,
            "covariance": st["covariance"] if st else None,
            "beta": _safe_float(beta),
            "samples": st["samples"] if st else None,
            "previousCorrelation": prev_corr,
            "correlationDelta": _safe_float(delta),
        })

    corrs = [pr["correlation"] for pr in pair_rows if pr["correlation"] is not None]
    avg_corr = sum(corrs) / len(corrs) if corrs else None

    top_pos = top_neg = None
    if pair_rows:
        ranked = sorted(pair_rows, key=lambda x: x["correlation"] if x["correlation"] is not None else 0.0)
        if ranked[-1]["correlation"] is not None:
            top_pos = {"peer": ranked[-1]["peer"], "correlation": ranked[-1]["correlation"]}
        if ranked[0]["correlation"] is not None:
            top_neg = {"peer": ranked[0]["peer"], "correlation": ranked[0]["correlation"]}

    return {
        "alignedDays": current.height,
        "pairRows": pair_rows,
        "matrix": {
            "instruments": available,
            "correlation": corr_m,
            "covariance": cov_m,
            "samples": samples_m,
        },
        "averageCorrelation": _safe_float(avg_corr),
        "topPositive": top_pos,
        "topNegative": top_neg,
    }


def _empty_matrix(instruments: list[str]) -> dict[str, Any]:
    n = len(instruments)
    return {
        "alignedDays": 0,
        "pairRows": [],
        "matrix": {
            "instruments": instruments,
            "correlation": [[None] * n for _ in range(n)],
            "covariance": [[None] * n for _ in range(n)],
            "samples": [[None] * n for _ in range(n)],
        },
        "averageCorrelation": None,
        "topPositive": None,
        "topNegative": None,
    }


def compute_correlation_matrix(
    repo: Any,
    symbol: str,
    *,
    window: int = 120,
    min_samples: int = 20,
    max_peers: int = 6,
) -> dict[str, Any]:
    """Pairwise Pearson correlation matrix of daily returns.

    Parameters bound by :data:`_WINDOW_CHOICES`, ``[3, window]`` and ``[1, 12]``.
    """
    _require_window(window)
    _require_range(min_samples, "min_samples", 3, window)
    _require_range(max_peers, "max_peers", 1, 12)

    notes = list(_BOUNDARY_NOTES["correlation"])

    latest = repo.enriched_latest_date()
    if latest is None:
        return _empty_corr(symbol, [], window, min_samples, notes + ["无 enriched 最新日"])

    peers, industry = _resolve_industry_peers(repo, symbol, max_peers, latest)
    instruments = [symbol, *peers]

    # Fetch enough calendar days for current + previous window of returns
    calendar_lb = int((2 * window + 90) * 1.4)
    start = latest - timedelta(days=calendar_lb)
    close_df = repo.get_enriched_range(
        start, latest,
        symbols=instruments,
        columns=["symbol", "date", "close"],
    )
    if close_df is None or close_df.is_empty():
        return _empty_corr(symbol, peers, window, min_samples, notes + ["无历史收盘价数据"])

    returns = _compute_returns(close_df)
    if returns.is_empty():
        return _empty_corr(symbol, peers, window, min_samples, notes + ["无有效收益率序列"])

    aligned = _align_returns(returns, instruments)
    if symbol not in aligned.columns or aligned.height < 2:
        return _empty_corr(symbol, peers, window, min_samples, notes + ["对齐交易日不足或标的无数据"])

    result = _compute_corr_matrix_from_returns(aligned, instruments, window, min_samples)
    result.update(
        selected=symbol,
        peers=peers,
        window=window,
        minSamples=min_samples,
        industry=industry,
        boundaryNotes=notes,
    )
    return result


def _empty_corr(
    symbol: str,
    peers: list[str],
    window: int,
    min_samples: int,
    notes: list[str],
) -> dict[str, Any]:
    instruments = [symbol, *peers]
    m = _empty_matrix(instruments)
    m.update(
        selected=symbol,
        peers=peers,
        window=window,
        minSamples=min_samples,
        industry=None,
        boundaryNotes=notes,
    )
    return m


# ── Relative strength ─────────────────────────────────────────────────────


def _compute_rs_from_aligned(
    aligned: pl.DataFrame,
    key: str,
    label: str,
    days: int,
    windows: tuple[int, ...] = _RS_WINDOWS,
) -> dict[str, Any] | None:
    """NAV normalisation, relative-pct curve, and window returns from aligned close."""
    if aligned.height < 2:
        return None
    window_df = aligned.tail(days)
    n = window_df.height
    if n < 2:
        return None

    first = window_df.row(0, named=True)
    base_s = first["stock_close"]
    base_b = first["bench_close"]
    if not base_s or not base_b or base_s <= 0 or base_b <= 0:
        return None

    nav = window_df.with_columns(
        (pl.col("stock_close") / base_s * 100.0).alias("stockNav"),
        (pl.col("bench_close") / base_b * 100.0).alias("benchNav"),
    ).with_columns(
        (pl.col("stockNav") / pl.col("benchNav") - 1.0).alias("relativePct"),
    )

    # Down-sample points to cap response size
    step = max(1, n // _MAX_POINTS)
    points = [
        {
            "date": _iso(r["date"]),
            "stockNav": _safe_float(r["stockNav"]),
            "benchmarkNav": _safe_float(r["benchNav"]),
            "relativePct": _safe_float(r["relativePct"]),
        }
        for r in nav.to_dicts()
    ][::step]

    last = nav.row(-1, named=True)
    latest_rel = _safe_float(last["relativePct"])

    window_returns: dict[int, dict[str, float | None]] = {}
    stock_returns: dict[int, float | None] = {}
    for w in windows:
        if n <= w:
            window_returns[w] = {"returnPct": None, "relativeReturnPct": None}
            stock_returns[w] = None
            continue
        start_row = nav.row(n - 1 - w, named=True)
        stock_ret = (last["stock_close"] / start_row["stock_close"] - 1.0) * 100.0
        bench_ret = (last["bench_close"] / start_row["bench_close"] - 1.0) * 100.0
        window_returns[w] = {
            "returnPct": _safe_float(bench_ret),
            "relativeReturnPct": _safe_float(stock_ret - bench_ret),
        }
        stock_returns[w] = _safe_float(stock_ret)

    return {
        "key": key,
        "label": label,
        "latestRelativePct": latest_rel,
        "points": points,
        "windowReturns": window_returns,
        "stockReturns": stock_returns,
        "alignedDays": n,
    }


def _process_benchmark_pair(
    stock_df: pl.DataFrame,
    bench_df: pl.DataFrame,
    key: str,
    label: str,
    days: int,
) -> dict[str, Any] | None:
    """Inner-join stock and benchmark close on date, then compute RS."""
    stock = stock_df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
    bench = bench_df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
    if stock.is_empty() or bench.is_empty():
        return None
    aligned = (
        stock.rename({"close": "stock_close"})
        .join(bench.rename({"close": "bench_close"}), on="date", how="inner")
        .sort("date")
    )
    return _compute_rs_from_aligned(aligned, key, label, days)


def _build_industry_proxy(
    repo: Any,
    symbol: str,
    start: date,
    end: date,
    latest: date,
) -> tuple[pl.DataFrame, str] | None:
    """Equal-weight industry proxy: mean of same-industry members' close per date."""
    enriched, _ = repo.get_enriched_latest()
    if enriched is None or enriched.is_empty():
        return None
    fin = _load_financials_safe(Path(repo.store.data_dir), latest)
    if fin is None or fin.is_empty() or "industry" not in fin.columns:
        return None

    sym_fin = fin.filter(pl.col("symbol") == symbol)
    if sym_fin.is_empty():
        return None
    industry = sym_fin.row(0, named=True).get("industry")
    if not industry:
        return None

    members = fin.filter(pl.col("industry") == industry)["symbol"].to_list()
    if len(members) < 2:
        return None

    close_df = repo.get_enriched_range(
        start, end, symbols=members,
        columns=["symbol", "date", "close"],
    )
    if close_df is None or close_df.is_empty():
        return None

    proxy = (
        close_df.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        .group_by("date")
        .agg(pl.col("close").mean().alias("close"))
        .sort("date")
    )
    if proxy.is_empty():
        return None
    return proxy, industry


def compute_relative_strength(
    repo: Any,
    symbol: str,
    *,
    days: int = 120,
    benchmark: str = "000001.SH",
) -> dict[str, Any]:
    """Stock NAV vs benchmark NAV with 10/20/60-day window return comparison."""
    _require_range(days, "days", 30, 250)
    if benchmark not in _BENCHMARKS:
        raise ValueError(f"benchmark must be one of {list(_BENCHMARKS)}")

    notes = list(_BOUNDARY_NOTES["relative_strength"])

    latest = repo.enriched_latest_date()
    if latest is None:
        return _empty_rs(symbol, days, benchmark, notes + ["无 enriched 最新日"])

    start = latest - timedelta(days=int(days * 1.6))
    stock_df = repo.get_daily(symbol, start, latest, columns=["date", "close"])
    bench_df = repo.get_index_daily(benchmark, start, latest, columns=["date", "close"])

    if stock_df.is_empty():
        return _empty_rs(symbol, days, benchmark, notes + ["个股数据不足"])

    primary = _process_benchmark_pair(
        stock_df, bench_df, benchmark,
        _BENCHMARK_LABELS.get(benchmark, benchmark), days,
    )
    if primary is None:
        return _empty_rs(symbol, days, benchmark, notes + ["个股与指数对齐交易日不足"])

    benchmarks_out = [primary]

    # Industry equal-weight proxy (optional)
    proxy = _build_industry_proxy(repo, symbol, start, latest, latest)
    if proxy is not None:
        proxy_df, industry_name = proxy
        ind_rs = _process_benchmark_pair(
            stock_df, proxy_df, "industry",
            f"行业等权({industry_name})", days,
        )
        if ind_rs is not None:
            benchmarks_out.append(ind_rs)

    # Tone from primary
    latest_rel = primary["latestRelativePct"]
    if latest_rel is not None and latest_rel >= 3.0:
        tone = "bull"
    elif latest_rel is not None and latest_rel <= -3.0:
        tone = "risk"
    else:
        tone = "neutral"

    if latest_rel is not None:
        if tone == "bull":
            tone_label = f"跑赢{primary['label']} {latest_rel:.1f}%"
        elif tone == "risk":
            tone_label = f"落后{primary['label']} {abs(latest_rel):.1f}%"
        else:
            tone_label = f"贴近{primary['label']} ({latest_rel:+.1f}%)"
    else:
        tone_label = "数据不足"

    # Windows
    win_list: list[dict[str, Any]] = []
    for w in _RS_WINDOWS:
        entry: dict[str, Any] = {
            "days": w,
            "label": f"{w}日",
            "stockReturnPct": primary["stockReturns"].get(w),
        }
        entry["benchmarks"] = [
            {
                "key": b["key"],
                "label": b["label"],
                "returnPct": b["windowReturns"].get(w, {}).get("returnPct"),
                "relativeReturnPct": b["windowReturns"].get(w, {}).get("relativeReturnPct"),
            }
            for b in benchmarks_out
        ]
        win_list.append(entry)

    return {
        "selected": symbol,
        "summary": {
            "label": tone_label,
            "detail": (
                f"最近 {primary['alignedDays']} 个对齐交易日，"
                f"相对{primary['label']} {latest_rel:+.1f}%"
                if latest_rel is not None
                else "数据不足"
            ),
            "tone": tone,
            "latestDate": _iso(latest),
            "dataLimitations": notes,
        },
        "benchmarks": [
            {
                "key": b["key"],
                "label": b["label"],
                "latestRelativePct": b["latestRelativePct"],
                "points": b["points"],
            }
            for b in benchmarks_out
        ],
        "windows": win_list,
        "boundaryNotes": notes,
    }


def _empty_rs(
    symbol: str,
    days: int,
    benchmark: str,
    notes: list[str],
) -> dict[str, Any]:
    return {
        "selected": symbol,
        "summary": {
            "label": "数据不足",
            "detail": "无法计算相对强度",
            "tone": "neutral",
            "latestDate": None,
            "dataLimitations": notes,
        },
        "benchmarks": [],
        "windows": [
            {
                "days": w,
                "label": f"{w}日",
                "stockReturnPct": None,
                "benchmarks": [],
            }
            for w in _RS_WINDOWS
        ],
        "boundaryNotes": notes,
    }


# ── Peer comparison ───────────────────────────────────────────────────────


def compute_peer_comparison(
    repo: Any,
    symbol: str,
    *,
    mode: str = "industry",
    limit: int = 12,
    sort_key: str = "amount",
) -> dict[str, Any]:
    """Rank a stock within its peer universe by market or fundamental metrics."""
    _require_range(limit, "limit", 1, 50)
    if mode not in ("industry", "amount", "board", "concept"):
        raise ValueError("mode must be 'industry', 'amount', 'board', or 'concept'")
    if sort_key not in _PEER_SORT_MAP and sort_key != "score":
        raise ValueError(f"sort_key must be one of {sorted(_PEER_SORT_MAP)} or 'score'")

    notes = list(_BOUNDARY_NOTES["peer_comparison"])

    if mode == "concept":
        return _empty_peer(symbol, mode, sort_key, notes + ["概念模式暂不可用（需 ext_data 配置）"])

    latest = repo.enriched_latest_date()
    if latest is None:
        return _empty_peer(symbol, mode, sort_key, notes + ["无 enriched 最新日"])

    enriched, _ = repo.get_enriched_latest()
    if enriched is None or enriched.is_empty():
        return _empty_peer(symbol, mode, sort_key, notes + ["无 enriched 数据"])

    df = enriched.unique(subset=["symbol"], keep="last")

    # Join instruments for name
    instruments = repo.get_instruments()
    if (
        instruments is not None
        and not instruments.is_empty()
        and "name" not in df.columns
        and "symbol" in instruments.columns
    ):
        inst_cols = [c for c in ["symbol", "name"] if c in instruments.columns]
        df = df.join(
            instruments.select(inst_cols).unique(subset=["symbol"]),
            on="symbol",
            how="left",
        )

    # Join financials
    fin = _load_financials_safe(Path(repo.store.data_dir), latest)
    if fin is not None and not fin.is_empty():
        fin_u = fin.unique(subset=["symbol"], keep="last")
        fin_cols = [c for c in fin_u.columns if c != "symbol"]
        stale = [c for c in fin_cols if c in df.columns]
        if stale:
            df = df.drop(stale)
        df = df.join(fin_u.select(["symbol", *fin_cols]), on="symbol", how="left")

    # Derived columns
    if "amount" in df.columns:
        df = df.with_columns((pl.col("amount") / 1e8).alias("amount_yi"))
    if "close" in df.columns:
        if "eps_annualized" in df.columns:
            df = df.with_columns(
                pl.when(pl.col("eps_annualized").is_not_null() & (pl.col("eps_annualized") > 0))
                .then(pl.col("close") / pl.col("eps_annualized"))
                .otherwise(None)
                .alias("pe"),
            )
        if "bps" in df.columns:
            df = df.with_columns(
                pl.when(pl.col("bps").is_not_null() & (pl.col("bps") > 0))
                .then(pl.col("close") / pl.col("bps"))
                .otherwise(None)
                .alias("pb"),
            )

    # Determine universe
    universe_label: str | None = None
    sym_row = df.filter(pl.col("symbol") == symbol)

    if mode == "industry":
        if sym_row.is_empty():
            return _empty_peer(symbol, mode, sort_key, notes + ["标的无数据"])
        industry = sym_row.row(0, named=True).get("industry")
        if not industry:
            return _empty_peer(symbol, mode, sort_key, notes + ["标的无行业信息，无法按行业筛选同业"])
        universe = df.filter(pl.col("industry") == industry)
        universe_label = industry
    elif mode == "board":
        board = _detect_board(symbol)
        if not board:
            return _empty_peer(symbol, mode, sort_key, notes + ["无法识别标的板块"])
        universe = _add_board_column(df).filter(pl.col("_board") == board).drop("_board")
        universe_label = _BOARD_NAMES.get(board, board)
    else:  # amount
        universe = df
        universe_label = "全市场"

    if universe.is_empty():
        return _empty_peer(symbol, mode, sort_key, notes + ["universe 为空"])

    # Score (composite rank sum)
    if sort_key == "score":
        parts: list[pl.Expr] = []
        for col in ("change_pct", "amount_yi", "turnover_rate"):
            if col in universe.columns:
                parts.append(pl.col(col).rank(method="average").fill_null(0))
        if parts:
            universe = universe.with_columns(sum(parts).alias("_score"))
            sort_col = "_score"
        else:
            sort_col = "amount_yi"
        descending = True
    else:
        sort_col = _PEER_SORT_MAP.get(sort_key, "amount_yi")
        descending = sort_key not in ("pe", "pb")

    if sort_col in universe.columns:
        universe = universe.sort(sort_col, descending=descending, nulls_last=True)

    total = universe.height
    universe = universe.with_row_index("rank", offset=1)

    # Current stock's rank
    current_rank: int | None = None
    rank_row = universe.filter(pl.col("symbol") == symbol)
    if not rank_row.is_empty():
        current_rank = rank_row.row(0, named=True)["rank"]

    # Build rows
    display_cols = [
        c for c in [
            "symbol", "name", "close", "change_pct", "amount_yi", "turnover_rate",
            "weight_avg_roe", "pe", "pb", "rank", "industry", "gross_margin",
            "consecutive_limit_ups",
        ]
        if c in universe.columns
    ]

    all_rows_raw = universe.select(display_cols).to_dicts()
    all_rows: list[dict[str, Any]] = []
    for r in all_rows_raw:
        clean = {k: _json_safe(v) for k, v in r.items()}
        # Alias weight_avg_roe → roe for display
        if "weight_avg_roe" in clean:
            clean["roe"] = clean["weight_avg_roe"]
        clean["isCurrent"] = clean.get("symbol") == symbol
        all_rows.append(clean)
    all_rows = all_rows[:_PEER_ALLROWS_CAP]

    # Display: top *limit*, ensure selected included, pin to front
    displayed = list(all_rows[:limit])
    if not any(r.get("symbol") == symbol for r in displayed):
        sym_in_all = [r for r in all_rows if r.get("symbol") == symbol]
        if sym_in_all:
            displayed.append(sym_in_all[0])
    # Pin selected to front (always first row)
    sel_rows = [r for r in displayed if r.get("symbol") == symbol]
    if sel_rows:
        displayed = [sel_rows[0], *(r for r in displayed if r.get("symbol") != symbol)]

    # Averages
    averages: dict[str, float | None] = {}
    for k in ("change_pct", "amount_yi", "turnover_rate", "roe"):
        vals = [r.get(k) for r in all_rows if r.get(k) is not None]
        averages[k] = sum(vals) / len(vals) if vals else None

    return {
        "selected": symbol,
        "mode": mode,
        "sortKey": sort_key,
        "universe": universe_label,
        "rows": displayed,
        "allRows": all_rows,
        "summary": {
            "total": total,
            "displayed": len(displayed),
            "averages": averages,
            "currentRank": current_rank,
            "currentTotal": total,
        },
        "boundaryNotes": notes,
    }


def _empty_peer(
    symbol: str,
    mode: str,
    sort_key: str,
    notes: list[str],
) -> dict[str, Any]:
    return {
        "selected": symbol,
        "mode": mode,
        "sortKey": sort_key,
        "universe": None,
        "rows": [],
        "allRows": [],
        "summary": {
            "total": 0,
            "displayed": 0,
            "averages": {},
            "currentRank": None,
            "currentTotal": 0,
        },
        "boundaryNotes": notes,
    }


# ── Reverse screen ────────────────────────────────────────────────────────


def build_reverse_screen_conditions(
    features: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build relaxed screener conditions from a feature dict.

    Each numeric lower-bound is multiplied by :data:`_RELAX_LOWER` (0.4);
    each bounded metric uses :data:`_RELAX_LOWER` / :data:`_RELAX_UPPER`.
    Conditions with null / non-positive features are silently skipped.
    """
    conditions: list[dict[str, Any]] = []
    reasons: list[str] = []

    industry = features.get("industry")
    if industry:
        conditions.append({"field": "industry", "op": "=", "value": industry})
        reasons.append(f"同行业：{industry}")

    change_pct = _safe_float(features.get("change_pct"))
    if change_pct is not None and change_pct > 0:
        lower = round(change_pct * _RELAX_LOWER, 4)
        conditions.append({"field": "change_pct", "op": ">=", "value": lower})
        reasons.append(f"涨跌幅 ≥ {lower:.2f}%（当前 {change_pct:.2f}% × {_RELAX_LOWER}）")

    amount = features.get("amount")
    if amount is not None and isinstance(amount, (int, float)) and amount > 0:
        lower = round(amount * _RELAX_LOWER, 2)
        conditions.append({"field": "amount", "op": ">=", "value": lower})
        reasons.append(
            f"成交额 ≥ {lower / 1e8:.2f} 亿元（当前 {amount / 1e8:.2f} 亿 × {_RELAX_LOWER}）"
        )

    turnover = _safe_float(features.get("turnover_rate"))
    if turnover is not None and turnover > 0:
        lower = round(turnover * _RELAX_LOWER, 4)
        conditions.append({"field": "turnover_rate", "op": ">=", "value": lower})
        reasons.append(f"换手率 ≥ {lower:.2f}%（当前 {turnover:.2f}% × {_RELAX_LOWER}）")

    roe = _safe_float(features.get("roe"))
    if roe is not None and roe > 0:
        lower = round(roe * _RELAX_LOWER, 4)
        conditions.append({"field": "roe", "op": ">=", "value": lower})
        reasons.append(f"ROE ≥ {lower:.2f}%（当前 {roe:.2f}% × {_RELAX_LOWER}）")

    pe = _safe_float(features.get("pe_approx"))
    if pe is not None and pe > 0:
        lo = round(pe * _RELAX_LOWER, 2)
        hi = round(pe * _RELAX_UPPER, 2)
        conditions.append({"field": "pe_approx", "op": "between", "value": [lo, hi]})
        reasons.append(f"PE 介于 {lo:.1f}–{hi:.1f} 倍（当前 {pe:.1f} 倍）")

    # Always exclude ST
    conditions.append({"field": "exclude_st", "op": "=", "value": True})
    reasons.append("排除 ST / 退市")

    return conditions, reasons


def _extract_features(repo: Any, symbol: str, latest: date) -> dict[str, Any] | None:
    """Extract the latest cross-section features for *symbol*."""
    enriched, _ = repo.get_enriched_latest()
    if enriched is None or enriched.is_empty():
        return None

    sym_row = enriched.filter(pl.col("symbol") == symbol)
    if sym_row.is_empty():
        return None

    row = sym_row.row(0, named=True)
    features: dict[str, Any] = {
        "change_pct": _safe_float(row.get("change_pct")),
        "amount": row.get("amount"),
        "turnover_rate": _safe_float(row.get("turnover_rate")),
        "close": _safe_float(row.get("close")),
    }

    fin = _load_financials_safe(Path(repo.store.data_dir), latest)
    if fin is not None and not fin.is_empty():
        fin_row = fin.filter(pl.col("symbol") == symbol)
        if not fin_row.is_empty():
            f = fin_row.row(0, named=True)
            features["industry"] = f.get("industry")
            features["roe"] = _safe_float(f.get("weight_avg_roe"))
            eps_ann = _safe_float(f.get("eps_annualized"))
            bps = _safe_float(f.get("bps"))
            close = features.get("close")
            if close and eps_ann and eps_ann > 0:
                features["pe_approx"] = close / eps_ann
            if close and bps and bps > 0:
                features["pb_approx"] = close / bps

    return features


def compute_reverse_screen(repo: Any, symbol: str) -> dict[str, Any]:
    """Build relaxed screener conditions from *symbol*'s features and execute via screener."""
    notes = list(_BOUNDARY_NOTES["reverse_screen"])

    latest = repo.enriched_latest_date()
    if latest is None:
        return _empty_rs_screen(symbol, notes + ["无 enriched 最新日"])

    features = _extract_features(repo, symbol, latest)
    if features is None:
        return _empty_rs_screen(symbol, notes + ["标的无数据"])

    conditions, reasons = build_reverse_screen_conditions(features)

    request_dict = {
        "conditions": conditions,
        "order_by": {"field": "amount", "direction": "desc"},
        "limit": _REVERSE_SCREEN_LIMIT,
    }

    # Execute via existing screener path (local-only, no external requests)
    result: dict[str, Any] | None = None
    try:
        from app.services.screener_query import ScreenerQueryRequest, execute_query

        req = ScreenerQueryRequest(
            conditions=conditions,
            order_by={"field": "amount", "direction": "desc"},
            limit=_REVERSE_SCREEN_LIMIT,
        )
        result = execute_query(repo, req)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reverse screen execute failed for %s: %s", symbol, exc)
        result = None

    features_safe = {
        k: _safe_float(v) if isinstance(v, float) else v
        for k, v in features.items()
    }

    return {
        "selected": symbol,
        "request": request_dict,
        "result": result,
        "reasons": reasons,
        "features": features_safe,
        "boundaryNotes": notes,
    }


def _empty_rs_screen(symbol: str, notes: list[str]) -> dict[str, Any]:
    return {
        "selected": symbol,
        "request": None,
        "result": None,
        "reasons": [],
        "features": {},
        "boundaryNotes": notes,
    }


__all__ = [
    "build_reverse_screen_conditions",
    "compute_correlation_matrix",
    "compute_peer_comparison",
    "compute_relative_strength",
    "compute_reverse_screen",
]
