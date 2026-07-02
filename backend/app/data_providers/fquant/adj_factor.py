"""xdxr / chuquan 事件 → 累积 ex_factor 计算（§4.1 / §4.5）。

设计公式（§4.5 伪代码）：
- 首日 = 1.0
- 对每个事件（按 trade_date 升序）累乘：
  - fenhong > 0（每10股派现）：``adj *= 1 - (fenhong/10) / pre_close``
  - fenshu > 0（每10股送股）：``adj *= 10 / (10 + fenshu)``
  - songzhuangu > 0（送转股）：``adj *= 10 / (10 + songzhuangu)``
  - peigu > 0（配股）：一般不调整 ex_factor（本期忽略）

注意：fenhong 公式需要 ``pre_close``（除权除息日前收盘价）。
- 主源 xdxr 不直接提供 pre_close，需调用方传入 daily close 序列。
- fallback chuquan 同理。
- 若 pre_close 缺失，fenhong 项跳过（仅累乘 fenshu/songzhuangu），避免除零。
"""
from __future__ import annotations

import logging

import polars as pl

from app.data_providers.fquant.mapping import _to_float

logger = logging.getLogger(__name__)


def compute_ex_factor_from_xdxr(
    events: list[dict],
    daily_close: dict[str, float] | None = None,
) -> list[dict]:
    """从 xdxr 事件列表计算累积 ex_factor（§4.5）。

    :param events: ``mapping.xdxr_rows_to_events`` 的输出（含 trade_date/fenhong/fenshu/...）
    :param daily_close: ``{date_iso: close_price}``，用于 fenhong 除权除息计算
    :return: ``[{symbol, trade_date, ex_factor}]``，按 trade_date 升序
    """
    if not events:
        return []

    daily_close = daily_close or {}
    symbol = events[0].get("symbol", "")

    # 按 trade_date 升序排列
    sorted_events = sorted(
        [e for e in events if e.get("trade_date")],
        key=lambda e: str(e["trade_date"]),
    )

    results: list[dict] = []
    cum_factor = 1.0

    for ev in sorted_events:
        fenhong = _to_float(ev.get("fenhong")) or 0.0
        fenshu = _to_float(ev.get("fenshu")) or 0.0
        songzhuangu = _to_float(ev.get("songzhuangu")) or 0.0

        # 送股 / 送转股：每10股送 fenshu 股 → adj *= 10/(10+fenshu)
        if fenshu > 0:
            cum_factor *= 10.0 / (10.0 + fenshu)
        if songzhuangu > 0:
            cum_factor *= 10.0 / (10.0 + songzhuangu)

        # 现金分红（每10股派现 fenhong 元）：
        # adj *= 1 - (fenhong/10) / pre_close
        # 需要 pre_close（除权除息日前一交易日收盘价）
        if fenhong > 0:
            trade_date = str(ev["trade_date"])
            pre_close = _find_pre_close(daily_close, trade_date)
            if pre_close and pre_close > 0:
                cum_factor *= 1.0 - (fenhong / 10.0) / pre_close
            else:
                # pre_close 缺失，跳过 fenhong 项（避免除零），仅日志
                logger.debug(
                    "compute_ex_factor: fenhong=%.2f 但 pre_close 缺失 (%s %s)，跳过现金分红项",
                    fenhong, symbol, trade_date,
                )

        results.append({
            "symbol": symbol,
            "trade_date": ev["trade_date"],
            "ex_factor": round(cum_factor, 6),
        })

    return results


def _find_pre_close(daily_close: dict[str, float], trade_date: str) -> float | None:
    """从 daily_close 字典找除权除息日前一交易日的收盘价。

    :param daily_close: ``{date_iso: close_price}``
    :param trade_date: 除权除息日 ``YYYY-MM-DD``
    :return: pre_close 或 None
    """
    if not daily_close:
        return None
    # 找严格小于 trade_date 的最大日期
    earlier = [d for d in daily_close if d < trade_date]
    if not earlier:
        return None
    pre_date = max(earlier)
    return daily_close.get(pre_date)


def compute_ex_factor_from_chuquan(
    events: list[dict],
    daily_close: dict[str, float] | None = None,
) -> list[dict]:
    """从 fstore chuquan_chuxi 事件计算累积 ex_factor（§4.5 fallback）。

    chuquan 字段经 ``mapping.chuquan_rows_to_events`` 归一后与 xdxr 同构，
    复用同一计算逻辑。
    """
    return compute_ex_factor_from_xdxr(events, daily_close)


def build_ex_factor_df(results: list[dict]) -> pl.DataFrame:
    """将 compute_ex_factor_* 结果列表转 Polars df。

    输出列：``symbol, trade_date, ex_factor``（与 normalizer.ADj_FACTOR_COLS 对齐）。
    """
    if not results:
        return pl.DataFrame()
    return pl.DataFrame(results)
