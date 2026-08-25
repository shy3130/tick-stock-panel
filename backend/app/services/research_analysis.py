"""研究分析服务 — 首个只读切片：单标的日收益的风险/绩效/ADF/GARCH。

边界
----
- 仅通过 :func:`app.backtest.portfolio.load_price_panel` 读取 canonical enriched
  日 K；不直连 ``data/``、不调用 provider、不触发 HTTP。
- 纯计算：调用 :func:`portfolio_risk_models` / :func:`performance_metrics` /
  :func:`quant_stats_suite`；不产生任何交易方向、价格、订单建议。
- fail-soft：样本不足时各计算保持 ``status="insufficient_data"``，绝不伪造数值。

移植来源
--------
复用本仓已有的 quant-stats / risk-models / metrics 纯函数，不复制 provider 或
repository 逻辑。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np

from app.backtest.metrics import performance_metrics
from app.backtest.portfolio import load_price_panel, returns_from_prices
from app.backtest.quant_stats import quant_stats_suite
from app.services.trading.risk_models import portfolio_risk_models

logger = logging.getLogger(__name__)

#: canonical enriched 数据来源标签（与契约 envelope ``source`` 一致）。
SOURCE_LABEL = "canonical-enriched"

#: 单标的分析的最大区间（年）。超过则视为非法请求。
MAX_RANGE_YEARS = 5

def _insufficient_analysis(
    *,
    symbol: str,
    start: date,
    end: date,
    data_as_of: date | None,
    observations: int,
    warnings: list[str],
) -> dict[str, Any]:
    """Build the normal 200-envelope payload for a valid but undersized query."""
    return {
        "source": SOURCE_LABEL,
        "symbol": symbol,
        "start": start,
        "end": end,
        "data_as_of": data_as_of,
        "observations": observations,
        "result": {
            "risk": portfolio_risk_models(np.empty(0)),
            "performance": performance_metrics(returns=None),
            "statistics": {
                "adf": quant_stats_suite("adf", series=np.empty(0)),
                "garch": quant_stats_suite("garch", returns=np.empty(0)),
            },
        },
        "warnings": warnings,
    }


def analyze_symbol_returns(
    repo: Any,
    symbol: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    """装配单标的日收益的风险/绩效/统计结果。

    Args:
        repo: ``KlineRepository``（duck-typed，仅需 ``get_daily_asset``）。
        symbol: canonical A 股代码 ``[0-9]{6}.(SH|SZ|BJ)``。
        start / end: 查询区间（闭区间，已由 router 校验 ``start <= end`` 且 <= 5 年）。

    Returns:
        分析 envelope dict（未经 ``json_safe``；由 router 在 HTTP 边界统一转换）。

    读取异常时抛 ``_DataUnavailable``；router 映射为 503。合法但没有
    canonical 行、或有效收益不足时，返回 ``insufficient_data`` 结果。
    """
    # ── 1. 读取 canonical enriched 日 K ──────────────────────────────────
    try:
        panel, kept = load_price_panel(
            repo,
            [symbol],
            start,
            end,
            raise_on_error=True,
        )
    except Exception as exc:  # repository/catalog 不可达
        logger.warning("research_analysis load_price_panel failed for %s: %s", symbol, exc)
        raise _DataUnavailable(
            f"canonical data source unavailable: {exc}"
        ) from exc

    if not kept or panel.height == 0:
        return _insufficient_analysis(
            symbol=symbol,
            start=start,
            end=end,
            data_as_of=None,
            observations=0,
            warnings=[
                f"no canonical enriched data for {symbol} in [{start}, {end}]"
            ],
        )

    # 实际数据截止日 = 价格序列的最后一个日期（保留真实锚点，不用请求 end 冒充）。
    # panel["date"] 是 pl.Date 列；.to_list()[-1] 返回 Python date 对象。
    last_date = panel["date"].to_list()[-1]
    data_as_of: date | None = last_date if isinstance(last_date, date) else None

    # ── 2. 价格矩阵 → 收益序列 ───────────────────────────────────────────
    prices = panel.select(kept).to_numpy().astype(float)
    returns = returns_from_prices(prices)  # [T-1, N]
    returns_1d = returns[:, 0] if returns.ndim == 2 and returns.shape[0] > 0 else np.empty(0)
    raw_observations = int(returns_1d.size)
    returns_1d = returns_1d[np.isfinite(returns_1d)]
    observations = int(returns_1d.size)
    warnings: list[str] = []

    if observations < raw_observations:
        warnings.append(
            f"dropped {raw_observations - observations} non-finite daily returns"
        )

    if observations == 0:
        if raw_observations == 0:
            warnings.append(
                f"insufficient price points ({panel.height}) to compute returns; "
                "need at least 2 observations"
            )
        else:
            warnings.append("no finite daily returns remain after validation")
        return _insufficient_analysis(
            symbol=symbol,
            start=start,
            end=end,
            data_as_of=data_as_of,
            observations=0,
            warnings=warnings,
        )

    # ── 3. 调用底层纯函数 ────────────────────────────────────────────────
    risk = portfolio_risk_models(returns_1d)
    performance = performance_metrics(returns=returns_1d)
    adf = quant_stats_suite("adf", series=returns_1d)
    garch = quant_stats_suite("garch", returns=returns_1d)
    # GARCH conditional_variance 是 numpy ndarray；json_safe 只解包标量，不猜数组
    # 语义，因此在这里显式转为 list[float]（非有限值 → None）。
    _cv = garch.get("conditional_variance")
    if isinstance(_cv, np.ndarray):
        garch["conditional_variance"] = [
            float(v) if np.isfinite(v) else None for v in _cv
        ]

    if risk.get("status") == "insufficient_data":
        warnings.append(
            f"risk estimates insufficient (observations={observations} < min "
            f"{risk.get('minSamples')})"
        )
    if performance.get("status") == "insufficient_data":
        warnings.append("performance metrics insufficient")
    if adf.get("status") == "insufficient_data":
        warnings.append("ADF test insufficient")
    if garch.get("status") == "insufficient_data":
        warnings.append("GARCH volatility insufficient")

    return {
        "source": SOURCE_LABEL,
        "symbol": symbol,
        "start": start,
        "end": end,
        "data_as_of": data_as_of,
        "observations": observations,
        "result": {
            "risk": risk,
            "performance": performance,
            "statistics": {"adf": adf, "garch": garch},
        },
        "warnings": warnings,
    }


class _DataUnavailable(Exception):
    """canonical 数据源无法读取（router 映射为 503）。"""
