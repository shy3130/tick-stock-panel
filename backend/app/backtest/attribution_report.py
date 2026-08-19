"""行业交易窗口 Brinson 归因报告（纯计算模块）。

仅新增文件；复用 app.backtest.attribution.brinson_attribution 做底层。
纯本地计算：无网络、无磁盘 I/O、无交易/订单副作用、确定性、JSON-safe。
所有输出浮点为有限值或 None；样本/行业不足时 fail-closed（status+reason+coverage，无伪数字）。

口径（必须体现在输出中）:
- 交易窗口 + 当前行业分类 + 相对等权已执行交易样本的 Brinson-Fachler。
- 不是官方指数归因。
- 组合权重=entry_value；基准=每笔有效交易等权(1/N)；收益均用已实现 pnl_pct。
- 行业取映射列表首个非空标签；映射为调用时分类（非 point-in-time）。
"""

from __future__ import annotations

import math
from typing import Any

from app.backtest.attribution import brinson_attribution

__all__ = [
    "build_trade_industry_brinson_report",
    "fama_french_unavailable_report",
]


def _finite_or_none(value: object) -> float | None:
    """非有限值统一映射为 None；否则返回 float。"""
    if value is None:
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _fama_french_unavailable() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "factor_return_series_unavailable",
        "detail": "仓内没有冻结且可审计的本地因子收益序列；不得生成代理因子或假结果",
        "alpha": None,
        "betas": {},
        "contributions": {},
        "r_squared": None,
        "residual_volatility": None,
        "observations": 0,
    }


def build_trade_industry_brinson_report(
    trades: list[dict[str, Any]],
    industry_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """从已完成交易 dict 列表构建行业 Brinson 归因报告。

    输入过滤:
    - 仅 entry_value > 0 且 pnl_pct 有限 且能映射到行业的交易进入计算。
    - 行业 = 列表中首个非空字符串（strip 后）。
    - 零资金、非有限 pnl、缺行业映射的交易被过滤，不进入权重/收益。

    权重与基准:
    - portfolio_weights = entry_value（价值加权）
    - benchmark_weights = 每笔有效交易等权（1/N）
    - 两者对应的 returns 均使用该交易的 pnl_pct

    失败闭合:
    - 无行业映射 / 映射后有效样本 < 2 / 有效行业数 < 2 → status="insufficient_data"
      + 明确 reason + coverage + warnings；brinson=None；不生成任何分解数字。
    - fama_french 字段始终为 unavailable（无本地冻结因子序列）。

    输出字段（稳定、JSON-safe）:
    status, reason(失败时), scope, classification_note, input_trades,
    classified_trades, capital_coverage, warnings, brinson, fama_french
    """
    input_n = len(trades) if trades else 0

    positive_cap_trades = 0
    total_positive_cap = 0.0
    classified: list[dict[str, Any]] = []
    dropped_nonfinite = 0
    dropped_no_ind = 0

    imap: dict[str, list[str]] = industry_map or {}

    for t in trades or []:
        ev = _finite_or_none(t.get("entry_value"))
        if ev is None or ev <= 0.0:
            continue
        positive_cap_trades += 1
        total_positive_cap += ev

        pnl = _finite_or_none(t.get("pnl_pct"))
        if pnl is None:
            dropped_nonfinite += 1
            continue

        sym = str(t.get("symbol") or "").strip()
        ind_list = imap.get(sym) or imap.get(sym.split(".", 1)[0]) or []
        ind: str | None = None
        if isinstance(ind_list, (list, tuple)):
            for x in ind_list:
                if x:
                    s = str(x).strip()
                    if s:
                        ind = s
                        break
        if not ind:
            dropped_no_ind += 1
            continue

        classified.append(
            {
                "entry_value": ev,
                "pnl_pct": pnl,
                "industry": ind,
                "symbol": sym,
            }
        )

    classified_n = len(classified)
    if total_positive_cap > 0.0:
        cap_cov = sum(c["entry_value"] for c in classified) / total_positive_cap
    else:
        cap_cov = 0.0

    warnings: list[str] = []
    if dropped_nonfinite > 0:
        warnings.append(f"filtered {dropped_nonfinite} trades with non-finite pnl_pct")
    if dropped_no_ind > 0:
        warnings.append(f"filtered {dropped_no_ind} trades without mappable industry")

    map_present = bool(imap) and any(
        isinstance(v, (list, tuple)) and any(bool(str(x).strip()) for x in v)
        for v in imap.values()
    )

    if classified_n < 2:
        reason = (
            "no_completed_trades"
            if input_n == 0
            else "no_industry_mapping"
            if not map_present
            else "insufficient_classified_trades"
        )
        if reason == "no_industry_mapping":
            warnings.append("no_industry_mapping")
        return {
            "status": "insufficient_data",
            "reason": reason,
            "input_trades": input_n,
            "classified_trades": classified_n,
            "capital_coverage": _finite_or_none(cap_cov),
            "warnings": warnings,
            "brinson": None,
            "fama_french": _fama_french_unavailable(),
            "scope": "交易窗口、按当前行业分类、相对等权已执行交易样本的 Brinson-Fachler 归因（非官方指数归因）",
            "classification_note": "行业取首个非空标签；使用调用时刻映射，非 point-in-time 于交易窗口",
        }

    unique_inds = {c["industry"] for c in classified}
    if len(unique_inds) < 2:
        return {
            "status": "insufficient_data",
            "reason": "insufficient_industries",
            "input_trades": input_n,
            "classified_trades": classified_n,
            "capital_coverage": _finite_or_none(cap_cov),
            "warnings": warnings + ["less than 2 distinct industries after mapping"],
            "brinson": None,
            "fama_french": _fama_french_unavailable(),
            "scope": "交易窗口、按当前行业分类、相对等权已执行交易样本的 Brinson-Fachler 归因（非官方指数归因）",
            "classification_note": "行业取首个非空标签；使用调用时刻映射，非 point-in-time 于交易窗口",
        }

    # 构造 brinson 输入：每笔有效交易作为一项，benchmark 等权 + 同 pnl_pct
    wp = [c["entry_value"] for c in classified]
    rp = [c["pnl_pct"] for c in classified]
    n = len(wp)
    wb = [1.0 / n for _ in range(n)]
    rb = list(rp)
    groups = [c["industry"] for c in classified]

    brinson_res = brinson_attribution(
        portfolio_weights=wp,
        portfolio_returns=rp,
        benchmark_weights=wb,
        benchmark_returns=rb,
        groups=groups,
    )

    return {
        "status": "ok",
        "scope": "交易窗口、按当前行业分类、相对等权已执行交易样本的 Brinson-Fachler 归因（非官方指数归因）",
        "classification_note": "行业取首个非空标签；使用调用时刻映射，非 point-in-time 于交易窗口",
        "input_trades": input_n,
        "classified_trades": classified_n,
        "capital_coverage": _finite_or_none(cap_cov),
        "warnings": warnings,
        "brinson": brinson_res,
        "fama_french": _fama_french_unavailable(),
    }


def fama_french_unavailable_report() -> dict[str, Any]:
    """返回 Fama-French 不可用报告。

    显式 unavailable，因为仓内无冻结、可审计的本地因子收益序列。
    绝不生成任何代理、插值或模拟的因子序列。
    """
    return _fama_french_unavailable()
