"""本地风格因子构建与归因 (A5) — SMB / UMD / LMV, 面板内自建。

因子口径
--------
全部因子由回测引擎面板 (enriched 行情) 在本地构建, 不访问外部数据源:

* ``SMB``  规模: 按 ``float_mcap = close × shares`` 三分位,
            小盘组等权日收益 − 大盘组等权日收益。
* ``UMD``  动量: 按 ``mom_252_21 = close/close.shift(252) − 1`` 三分位,
            赢家组 − 输家组。
* ``LMV``  低波动: 按 ``vol_60 = ret.rolling(60).std`` 三分位,
            低波动组 − 高波动组。

股本口径: ``float_shares`` 缺失回退 ``total_shares``, 两者均缺则该
( symbol, 日期 ) 行不参与当日任何排序与均值 (fail-closed, 不伪造代理值)。

**本模块显式不含 HML (价值) 因子**: 本地面板没有账面市值 / ROE 等
财务历史序列, 价值因子必须待财务数据接入后再构建, 绝不用市盈率等
价格代理伪造。此声明为规格的一部分, 勿删。

设计约束 (与 metrics.py / attribution.py 一致)
----------------------------------------------
* 纯计算: 只接受 Polars/numpy 输入, 无网络 / 磁盘 I/O。
* fail-closed: 样本不足 / 数据缺失 / 因子共线一律返回 ``None`` 或
  结构化原因 (meta["reason"]), 非有限值输出 null。
* 年化只走 ``MetricContext.periods_per_year``, 调用方不得双写系数。
"""
from __future__ import annotations

import hashlib
import math
from datetime import date as _date, datetime as _datetime

import numpy as np
import polars as pl

from app.backtest.metrics import MetricContext

__all__ = [
    "build_style_factor_returns",
    "style_attribution",
    "FACTOR_SPEC",
    "FACTOR_VERSION",
]

# 有效交易日下限: 因子序列 / 归因样本共用同一规格门槛。
_MIN_VALID_DAYS = 120
_MIN_ATTR_OBS = 120

_MOM_LOOKBACK = 252   # mom_252_21 回看窗口 (需该 symbol ≥ 253 个观测)
_VOL_WINDOW = 60      # vol_60 波动率窗口

_FACTOR_NAMES = ("smb", "umd", "lmv")


def _factor_spec(min_cross_section: int) -> str:
    """因子构造规格串 — 只编码构造口径, 不编码数据内容。"""
    names = ",".join(_FACTOR_NAMES)
    return f"v1|{names}|tercile|cs>={min_cross_section}"


def _factor_version(min_cross_section: int) -> str:
    """规格指纹 = sha256(规格串)[:12]。

    数据版本 (面板快照哈希 / 构建时间) 由调用方在持久化时补充,
    本模块无法也不应伪造。
    """
    return hashlib.sha256(_factor_spec(min_cross_section).encode("utf-8")).hexdigest()[:12]


FACTOR_SPEC = _factor_spec(100)
FACTOR_VERSION = _factor_version(100)


def _finite_or_none(value: object) -> float | None:
    """非有限值统一映射为 None; 否则返回 float。"""
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _date_key(value: object) -> str:
    """日期归一化为字符串键 (date / datetime / str 均可对齐)。"""
    if isinstance(value, _datetime):  # datetime 是 date 的子类, 需先判
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    return str(value)


def build_style_factor_returns(
    panel: pl.DataFrame,
    *,
    min_cross_section: int = 100,
) -> tuple[pl.DataFrame | None, dict]:
    """从行情面板构建 SMB / UMD / LMV 日度因子收益序列。

    Args:
        panel: 引擎面板, 至少含 ``date / symbol / close``, 股本列
            ``float_shares`` (缺失回退 ``total_shares``, 均缺则该行剔除)。
        min_cross_section: 当日有效股票数 (ret 与 float_mcap 均非空) 下限,
            不足则跳过该日并计入 ``skipped_days``。

    Returns:
        ``(df, meta)``; ``df`` 为按日期一行、含 ``date / smb / umd / lmv``
        的序列 (早期日 mom/vol 未满窗口时对应因子为 null)。有效日
        ``< 120`` 时返回 ``(None, meta)``。
        ``meta``: ``valid_days / skipped_days / median_cross_section /
        min_cross_section / factor_version`` (+ 失败时的 ``reason``)。
    """
    meta: dict = {
        "valid_days": 0,
        "skipped_days": 0,
        "median_cross_section": None,
        "min_cross_section": int(min_cross_section),
        "factor_version": _factor_version(min_cross_section),
    }

    if panel.height == 0:
        meta["reason"] = "empty_panel"
        return None, meta
    required = ("date", "symbol", "close")
    missing = [c for c in required if c not in panel.columns]
    if missing:
        meta["reason"] = f"missing_columns: {','.join(missing)}"
        return None, meta

    # 股本: float_shares 优先, 缺列/缺值均回退 total_shares
    share_cols = [c for c in ("float_shares", "total_shares") if c in panel.columns]
    if share_cols:
        shares_expr = pl.coalesce([pl.col(c).cast(pl.Float64) for c in share_cols])
    else:
        shares_expr = pl.lit(None, dtype=pl.Float64)

    # 逐 symbol 时序特征 (先去重再排序: unique 不保序, shift/rolling 依赖组内日期顺序)
    feats = (
        panel.unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
        .with_columns(shares=shares_expr)
        .with_columns(
            ret=(pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0),
            float_mcap=(pl.col("close") * pl.col("shares")),
            mom_252_21=(pl.col("close") / pl.col("close").shift(_MOM_LOOKBACK).over("symbol") - 1.0),
        )
        .with_columns(
            vol_60=pl.col("ret").rolling_std(window_size=_VOL_WINDOW).over("symbol"),
        )
    )
    # 非有限值 → null: 不参与当日排序与均值 (fail-closed)
    feats = feats.with_columns(
        [
            pl.when(pl.col(c).is_finite()).then(pl.col(c)).alias(c)
            for c in ("ret", "float_mcap", "mom_252_21", "vol_60")
        ]
    )

    # 当日基础有效截面: ret 与 float_mcap 均可得 (SMB 的最低要求)
    base = feats.filter(pl.col("ret").is_not_null() & pl.col("float_mcap").is_not_null())

    counts = base.group_by("date").agg(n=pl.len())
    valid_counts = counts.filter(pl.col("n") >= min_cross_section)
    valid_days = int(valid_counts.height)
    meta["valid_days"] = valid_days
    meta["skipped_days"] = int(panel["date"].n_unique() - valid_days)
    if valid_days > 0:
        meta["median_cross_section"] = float(valid_counts["n"].median())
    if valid_days < _MIN_VALID_DAYS:
        if valid_days == 0:
            meta["reason"] = "no_valid_cross_section"
        else:
            meta["reason"] = f"valid_days<{_MIN_VALID_DAYS}"
        return None, meta

    # 三分位: 组内序数排名均匀切三份, (rank-1)*3 // n ∈ {0,1,2}
    # 各排序变量在自己的非空子集内排名 (mom/vol 未满窗口的行不入当日排序)
    valid = (
        base.join(valid_counts.select("date"), on="date", how="semi")
        .with_columns(
            n_base=pl.len().over("date"),
            mom_n=pl.col("mom_252_21").is_not_null().sum().over("date"),
            vol_n=pl.col("vol_60").is_not_null().sum().over("date"),
        )
        .with_columns(
            size_tercile=((pl.col("float_mcap").rank("ordinal").over("date") - 1) * 3 // pl.col("n_base")),
            mom_tercile=((pl.col("mom_252_21").rank("ordinal").over("date") - 1) * 3 // pl.col("mom_n")),
            vol_tercile=((pl.col("vol_60").rank("ordinal").over("date") - 1) * 3 // pl.col("vol_n")),
        )
    )

    factors = (
        valid.group_by("date")
        .agg(
            # 组内等权日收益之差; 组空 (子集不足三分) → null
            smb=(
                pl.col("ret").filter(pl.col("size_tercile") == 0).mean()
                - pl.col("ret").filter(pl.col("size_tercile") == 2).mean()
            ),
            umd=(
                pl.col("ret").filter(pl.col("mom_tercile") == 2).mean()
                - pl.col("ret").filter(pl.col("mom_tercile") == 0).mean()
            ),
            lmv=(
                pl.col("ret").filter(pl.col("vol_tercile") == 0).mean()
                - pl.col("ret").filter(pl.col("vol_tercile") == 2).mean()
            ),
        )
        .sort("date")
    )
    return factors, meta


def style_attribution(
    strategy_returns: np.ndarray,
    factor_dates: list,
    factor_df: pl.DataFrame,
    context: MetricContext,
) -> dict | None:
    """策略日收益对本地风格因子的 OLS 归因。

    按日期对齐策略收益与因子收益后回归 ``y = alpha + Σ β_k·f_k``
    (numpy lstsq)。因子日期未覆盖 / 任一因子为 null 的观测被剔除,
    剔除后 ``n_obs < 120`` 或设计矩阵秩不足 (因子共线) → ``None``。

    已知局限 (口径声明):
    * t 统计量用普通 OLS 标准误, 未做 Newey-West (HAC) 修正 —
      日频因子回归的残差常呈自相关 / 条件异方差, t 值可能偏乐观。
    * 未减无风险利率 (规格为 y = alpha + Σβf); 风格因子为多空组合,
      alpha 中含无风险收益贡献。
    * ``factor_version`` 只是构造规格指纹; 数据版本 (面板快照) 由
      调用方持久化时补充。

    Returns:
        ``{n_obs, alpha_per_period, alpha_annualized, betas{smb,umd,lmv},
        t_stats{alpha,smb,umd,lmv}, r_squared, factor_version}``;
        alpha 年化 = 每期 alpha × ``context.periods_per_year`` (与
        relative_performance_metrics 口径一致)。
    """
    needs = ("date", *_FACTOR_NAMES)
    if factor_df is None or factor_df.height == 0 or any(c not in factor_df.columns for c in needs):
        return None

    y_all = np.asarray(strategy_returns, dtype=float).ravel()

    lookup: dict[str, tuple[float, float, float] | None] = {}
    for d, smb, umd, lmv in factor_df.select(*needs).iter_rows():
        vals = (smb, umd, lmv)
        if all(v is not None and math.isfinite(float(v)) for v in vals):
            lookup[_date_key(d)] = (float(smb), float(umd), float(lmv))
        else:
            lookup[_date_key(d)] = None  # 因子缺失日: 即使对齐也不可用

    xs: list[tuple[float, float, float]] = []
    ys: list[float] = []
    for r, d in zip(y_all, factor_dates):
        f = lookup.get(_date_key(d))
        if f is None or not math.isfinite(float(r)):
            continue
        xs.append(f)
        ys.append(float(r))

    n = len(ys)
    if n < _MIN_ATTR_OBS:
        return None

    y = np.asarray(ys, dtype=float)
    x = np.column_stack([np.ones(n), np.asarray(xs, dtype=float)])
    p = x.shape[1]

    coef, _resid, rank, _sv = np.linalg.lstsq(x, y, rcond=None)
    if rank < p:
        # 因子共线 / 常数因子 → 无法唯一归因 (fail-closed)
        return None

    resid = y - x @ coef
    sse = float(resid @ resid)
    dof = n - p
    sigma2 = sse / dof
    try:
        xtx_inv = np.linalg.inv(x.T @ x)
    except np.linalg.LinAlgError:
        return None
    # 普通 OLS 标准误 (无 Newey-West/HAC 修正): 日频残差自相关/异方差时 t 值偏乐观
    se = np.sqrt(np.maximum(np.diag(sigma2 * xtx_inv), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t_vals = np.where(se > 0.0, coef / np.where(se > 0.0, se, 1.0), np.nan)

    tss = float(((y - y.mean()) ** 2).sum())
    r_squared = (1.0 - sse / tss) if tss > 0.0 else None

    alpha = _finite_or_none(coef[0])
    return {
        "n_obs": int(n),
        "alpha_per_period": alpha,
        "alpha_annualized": _finite_or_none(alpha * context.periods_per_year) if alpha is not None else None,
        "betas": {name: _finite_or_none(coef[i + 1]) for i, name in enumerate(_FACTOR_NAMES)},
        "t_stats": {
            "alpha": _finite_or_none(t_vals[0]),
            **{name: _finite_or_none(t_vals[i + 1]) for i, name in enumerate(_FACTOR_NAMES)},
        },
        "r_squared": _finite_or_none(r_squared),
        "factor_version": FACTOR_VERSION,
    }
