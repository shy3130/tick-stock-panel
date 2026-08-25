"""成交可达性诊断（B7）: 对已持久化 Run 的成交做分钟级价格带可达性抽查。

纯只读诊断:
- 不写任何数据; 分钟数据经注入的 get_minutes_fn(symbol, day) 获取, 生产侧由
  调用方传 provider 适配闭包, 本模块不感知 provider 细节。
- provider 调用失败 fail-soft: 逐笔、逐侧记为 no_data, 不中断整体诊断。

口径:
- 每笔成交诊断 entry/exit 两侧; fill 价即 entry_price/exit_price, 日期即
  entry_date/exit_date (均为成交口径日期)。
- band_notional = 价格带内当日分钟 amount 合计。用成交额(金额)口径求和,
  避免股/手单位歧义——不同数据源的 volume 可能是股、手或张, 金额口径统一。
- trade_notional = shares × fill_price; headroom = band_notional / trade_notional。
- trade_notional<=0 或 band_notional==0 → headroom=0.0; 分钟缺失/异常/字段
  无效 → 该侧 status='no_data', 不计 headroom。
- 按侧统计 sides_checked; 按样本(每笔成交)统计 no_data/reachable, 样本
  headroom 取两侧最差(最小值), 两侧都 no_data 的样本才计入 n_no_data(一次)。
- 全部字段 JSON-safe, 日期输出 ISO 字符串。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.json_safe import finite_float_or_none, json_safe

__all__ = ["diagnose_fill_reachability"]

# 口径说明, 原样透出到结果 note 字段
_NOTE = (
    "诊断口径: 成交价±price_band_pct 价格带内当日分钟成交额 vs 交易名义金额；"
    "只说明价格带内当日市场活动量级，不保证可实际成交，不模拟盘口"
)

# 侧定义: (侧名, 日期字段, 成交价字段)
_SIDES: tuple[tuple[str, str, str], ...] = (
    ("entry", "entry_date", "entry_price"),
    ("exit", "exit_date", "exit_price"),
)


def _parse_day(value: object) -> date | None:
    """把 entry_date/exit_date (ISO 字符串或 date/datetime) 解析为 date; 失败返回 None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _positive_price(value: object) -> float | None:
    """成交价必须是有限正数, 否则视为无效。"""
    number = finite_float_or_none(value)
    return number if number is not None and number > 0 else None


def _no_data_side() -> dict[str, Any]:
    """no_data 侧的结构化结果: 不计 headroom。"""
    return {
        "status": "no_data",
        "headroom": None,
        "band_notional": None,
        "trade_notional": None,
    }


def _diagnose_side(
    symbol: str,
    day: date | None,
    fill_price: float | None,
    shares: float | None,
    get_minutes_fn: Callable[[str, date], pl.DataFrame],
    price_band_pct: float,
) -> dict[str, Any]:
    """诊断单侧 (entry 或 exit) 的价格带可达性。

    返回 {status, headroom, band_notional, trade_notional}:
    - 分钟数据缺失/异常/字段无效 → status='no_data';
    - 否则 status='ok', headroom 按口径计算 (trade_notional<=0 或 band 为 0 → 0.0)。
    """
    if day is None or fill_price is None:
        return _no_data_side()

    try:
        # provider 失败 fail-soft: 单侧 no_data, 不向上抛
        minutes = get_minutes_fn(symbol, day)
        if not isinstance(minutes, pl.DataFrame) or minutes.height == 0:
            return _no_data_side()
        if "close" not in minutes.columns or "amount" not in minutes.columns:
            return _no_data_side()

        # 价格带过滤: |minute_close - fill_price| / fill_price <= price_band_pct
        # close 为 null/非有限或不可数值化的行不参与; amount 的 null 按 0 计。
        close = pl.col("close").cast(pl.Float64, strict=False)
        band = minutes.filter(
            close.is_not_null()
            & close.is_finite()
            & ((close - fill_price).abs() / fill_price <= price_band_pct)
        )
        band_notional = float(
            band.get_column("amount")
            .cast(pl.Float64, strict=False)
            .fill_null(0.0)
            .sum()
        )
        if not math.isfinite(band_notional) or band_notional < 0:
            return _no_data_side()

        trade_notional = (shares if shares is not None else 0.0) * fill_price
        # trade_notional<=0 或 band 为 0 → headroom=0.0
        if trade_notional > 0 and band_notional > 0:
            headroom = band_notional / trade_notional
        else:
            headroom = 0.0
        if not math.isfinite(headroom):
            return _no_data_side()
    except Exception:
        return _no_data_side()

    return {
        "status": "ok",
        "headroom": headroom,
        "band_notional": band_notional,
        "trade_notional": trade_notional,
    }


def _empty_result(n_trades: int, seed: int, price_band_pct: float) -> dict[str, Any]:
    """trades 为空或 sample<1 时的结构化空结果 (保持汇总字段形状)。"""
    return json_safe(
        {
            "n_trades": n_trades,
            "n_sampled": 0,
            "sample_seed": seed,
            "price_band_pct": price_band_pct,
            "sides_checked": 0,
            "n_no_data": 0,
            "no_data_pct": 0.0,
            "n_reachable": 0,
            "reachable_pct": 0.0,
            "headroom_p10": None,
            "headroom_p50": None,
            "worst": [],
            "note": _NOTE,
        }
    )


def diagnose_fill_reachability(
    trades: list[dict],
    get_minutes_fn: Callable[[str, date], pl.DataFrame],
    *,
    sample: int = 50,
    seed: int = 0,
    price_band_pct: float = 0.005,
) -> dict[str, Any]:
    """对成交样本做分钟级价格带可达性抽查, 返回 JSON-safe 汇总诊断。

    Args:
        trades: 持久化 Run 的成交 dict 列表, 字段含 symbol/entry_date(str)/
            entry_price/shares/exit_date/exit_price 等。
        get_minutes_fn: 注入的分钟数据源, 签名 (symbol, day) -> pl.DataFrame,
            返回含 close/amount 的 normalized 分钟行 (可能有 null)。
        sample: 抽样笔数上限; 与 trades 数取 min。
        seed: 确定性抽样种子。
        price_band_pct: 价格带半宽 (相对成交价比例)。

    Returns:
        汇总 dict; n_reachable 为样本最差 headroom>=1 的笔数, headroom_p10/p50
        仅对有效样本计算 (无有效样本 → None), worst 为按 headroom 升序前 5 的侧。
    """
    n_trades = len(trades)
    if n_trades == 0 or sample < 1:
        return _empty_result(n_trades, seed, price_band_pct)

    # 确定性抽样: 同 seed 可复现; 抽样后按索引排序保持 trades 原顺序
    rng = np.random.default_rng(seed)
    n_sampled = min(int(sample), n_trades)
    picked = sorted(int(i) for i in rng.choice(n_trades, size=n_sampled, replace=False))

    sides_checked = 0
    n_no_data = 0
    n_reachable = 0
    sample_worst_headrooms: list[float] = []
    ok_sides: list[dict[str, Any]] = []

    for idx in picked:
        trade = trades[idx]
        if not isinstance(trade, dict):
            n_no_data += 1
            continue
        symbol = str(trade.get("symbol") or "")
        shares = finite_float_or_none(trade.get("shares"))

        headrooms: list[float] = []
        for side, date_key, price_key in _SIDES:
            day = _parse_day(trade.get(date_key))
            fill_price = _positive_price(trade.get(price_key))
            diag = _diagnose_side(
                symbol, day, fill_price, shares, get_minutes_fn, price_band_pct
            )
            if diag["status"] != "ok":
                continue
            sides_checked += 1
            headrooms.append(diag["headroom"])
            ok_sides.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "side": side,
                    "headroom": diag["headroom"],
                    "band_notional": diag["band_notional"],
                    "trade_notional": diag["trade_notional"],
                }
            )

        if not headrooms:
            # 两侧都 no_data → 该样本计入 n_no_data 一次
            n_no_data += 1
            continue
        # 样本口径: 取两侧最差
        worst_headroom = min(headrooms)
        sample_worst_headrooms.append(worst_headroom)
        if worst_headroom >= 1.0:
            n_reachable += 1

    arr = np.asarray(sample_worst_headrooms, dtype=float)
    # 分位数仅对有效样本计算; 空 → None (非有限由 json_safe 统一兜底为 null)
    headroom_p10: float | None = float(np.quantile(arr, 0.10)) if arr.size else None
    headroom_p50: float | None = float(np.quantile(arr, 0.50)) if arr.size else None

    worst = sorted(ok_sides, key=lambda item: item["headroom"])[:5]

    return json_safe(
        {
            "n_trades": n_trades,
            "n_sampled": n_sampled,
            "sample_seed": seed,
            "price_band_pct": price_band_pct,
            "sides_checked": sides_checked,
            "n_no_data": n_no_data,
            "no_data_pct": n_no_data / n_sampled,
            "n_reachable": n_reachable,
            "reachable_pct": n_reachable / n_sampled,
            "headroom_p10": headroom_p10,
            "headroom_p50": headroom_p50,
            "worst": worst,
            "note": _NOTE,
        }
    )
