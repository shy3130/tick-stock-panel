"""上游字段 → 内部 schema 转换函数（§4.1 / §5）。

按表/dataset 各一个转换函数。所有 Decimal/date 等 DB 特殊类型在此统一转 float/str。

覆盖（§5.2–§5.7）：
- ``wide_rows_to_daily`` / ``day_rows_to_daily`` / ``klines_rows_to_daily``
- ``xdxr_rows_to_events`` / ``chuquan_rows_to_events``
- ``minutes_rows_to_minute_df``
- ``base_infos_rows_to_instruments``
- ``financial_rows_to_df``
- ``moneyflow_daily_to_df`` / ``moneyflow_minute_to_df``
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import polars as pl

from app.data_providers.fquant.symbols import (
    code_to_symbol,
    exchange_of,
)
from app.json_safe import finite_float_or_none

# A 股交易时段：上午 09:30–11:30 / 下午 13:00–15:00（§4.6 / engine_stock_data.go:201）
# index 0..119 → 09:31..11:30，index 120..239 → 13:01..15:00
_MORNING_START = datetime(2000, 1, 1, 9, 31)
_AFTERNOON_START = datetime(2000, 1, 1, 13, 1)
_MORNING_END = datetime(2000, 1, 1, 11, 30)

# 港股交易时段：上午 09:31-12:00（150 分钟）/ 下午 13:01-16:00（180 分钟），
# 与 A 股的 120/120 分段不同——已在 engine 仓库 tdx-kline 用真实
# tdx-hkminutes.duckdb 数据验证过（tdx_kline_bucket.go tdxMinuteSessions）。
_HK_MORNING_LEN = 150
_HK_AFTERNOON_START = datetime(2000, 1, 1, 13, 1)




def _serialize_row(row: dict) -> dict:
    """将 DB 行中的 Decimal/date 等转为 JSON 可序列化类型。"""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = finite_float_or_none(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# §5.2 daily 字段映射
# --------------------------------------------------------------------------- #
def wide_rows_to_daily(rows: list[dict], symbol: str, source: str = "fquant") -> list[dict]:
    """engine-data ``wide`` → daily 归一行（§5.2）。

    wide 字段最全（含 last_close / change_rate / inner_*/outer_*），作为 daily 主源。
    注意 ``change_rate`` 实测已经是百分比（如 0.63 表示 0.63%），**不再次乘 100**（§5.2 备注）。
    """
    out: list[dict] = []
    for r in rows:
        date_val = r.get("date")
        out.append({
            "symbol": symbol,
            "date": str(date_val) if date_val else None,
            "open": finite_float_or_none(r.get("open")),
            "high": finite_float_or_none(r.get("high")),
            "low": finite_float_or_none(r.get("low")),
            "close": finite_float_or_none(r.get("close")),
            "volume": finite_float_or_none(r.get("volume")),
            "amount": finite_float_or_none(r.get("amount")),
            "pre_close": finite_float_or_none(r.get("last_close")),
            "change_pct": finite_float_or_none(r.get("change_rate")),
        })
    return out


def day_rows_to_daily(rows: list[dict], symbol: str, source: str = "fquant") -> list[dict]:
    """engine-data ``day`` → daily 归一行（§5.2 fallback）。

    day 没有 last_close / change_rate；pre_close / change_pct 留空。
    """
    out: list[dict] = []
    for r in rows:
        date_val = r.get("date")
        out.append({
            "symbol": symbol,
            "date": str(date_val) if date_val else None,
            "open": finite_float_or_none(r.get("open")),
            "high": finite_float_or_none(r.get("high")),
            "low": finite_float_or_none(r.get("low")),
            "close": finite_float_or_none(r.get("close")),
            "volume": finite_float_or_none(r.get("volume")),
            "amount": finite_float_or_none(r.get("amount")),
            "pre_close": None,
            "change_pct": None,
        })
    return out


def klines_rows_to_daily(
    rows: list[dict], symbol: str, source: str = "fquant", asset_type: str = "stock",
) -> list[dict]:
    """fstore ``day_klines`` → daily 归一行（§5.2 兜底）。

    fstore 字段名：tdate/open/close/high/low/cjl/cje/zf（涨跌幅）。

    输出 ``volume`` 的语义是**股数**。``cjl`` 到股数的倍数因市场而异，用
    ``cje / close`` 这个不依赖任何 volume 列的物理约束（成交额÷价格＝股数）
    跨 5 只港股 + 多个交易日核对：

    - A股/stock：``cje/close ÷ cjl ≈ 100``（如 000001 2025-10-20：
      implied=94,731,175 / cjl=952,641 = 99.44）→ 倍数 **×100**
    - 港股/hk：``cje/close ÷ cjl ≈ 1.00``（如 00700 2025-10-20：
      implied=14,947,219 / cjl=14,963,400 = 0.9989；00005 多日同样 ≈1.00）
      → **cjl 本身就已经是股数，倍数 ×1，不需要任何换算**

    注意港股不要拿 ``tdx-hk.duckdb`` 的 ``market_day_kline.volume`` 当参照：
    那一列是**手**（00700 同日 = 1,496，≈ 股数/10000），单位和这里要输出的
    股数不同。此前对所有市场统一 ×100 会让港股高估 100 倍；而按 ÷10000 去
    对齐 tdx-hk 的 volume 列则会输出"手"，与 A 股的"股"语义不一致、低估 1
    万倍——两种都是错的。
    """
    multiplier = 1 if asset_type == "hk" else 100
    out: list[dict] = []
    for r in rows:
        out.append({
            "symbol": symbol,
            "date": str(r.get("tdate", "")),
            "open": finite_float_or_none(r.get("open")),
            "high": finite_float_or_none(r.get("high")),
            "low": finite_float_or_none(r.get("low")),
            "close": finite_float_or_none(r.get("close")),
            "volume": (vol * multiplier if (vol := finite_float_or_none(r.get("cjl"))) is not None else None),
            "amount": finite_float_or_none(r.get("cje")),
            "pre_close": None,
            "change_pct": finite_float_or_none(r.get("zf")),
        })
    return out


# --------------------------------------------------------------------------- #
# §5.3 adj_factor 字段映射（事件提取，ex_factor 计算在 adj_factor.py）
# --------------------------------------------------------------------------- #
def xdxr_rows_to_events(rows: list[dict], symbol: str) -> list[dict]:
    """engine-data ``xdxr`` → 归一事件行（§5.3 主源）。

    输出字段供 ``adj_factor.py:compute_ex_factor_from_xdxr`` 消费：
    - symbol / trade_date / category / fenhong / fenshu / songzhuangu / peigu / peigujia
    """
    out: list[dict] = []
    for r in rows:
        date_val = r.get("date")
        out.append({
            "symbol": symbol,
            "trade_date": str(date_val) if date_val else None,
            "category": r.get("category"),
            "fenhong": finite_float_or_none(r.get("fenhong")) or 0.0,        # 每10股派现
            "fenshu": finite_float_or_none(r.get("fenshu")) or 0.0,          # 每10股送股
            "songzhuangu": finite_float_or_none(r.get("songzhuangu")) or 0.0,  # 送转股
            "peigu": finite_float_or_none(r.get("peigu")) or 0.0,            # 配股
            "peigujia": finite_float_or_none(r.get("peigujia")) or 0.0,
            "qianzongguben": finite_float_or_none(r.get("qianzongguben")),
            "houzongguben": finite_float_or_none(r.get("houzongguben")),
            "name": r.get("name"),
        })
    return out


def chuquan_rows_to_events(rows: list[dict], symbol: str) -> list[dict]:
    """fstore ``chuquan_chuxi`` → 归一事件行（§5.3 fallback）。

    fstore 字段：t_date/pgbl/pgjg/pxbl（派息比，每10股）/sgbl（送股比）/cqcxtype。
    """
    out: list[dict] = []
    for r in rows:
        t_date = r.get("t_date")
        out.append({
            "symbol": symbol,
            "trade_date": str(t_date) if t_date else None,
            "category": r.get("cqcxtype"),    # 1=除权除息 / 5=股本变化
            "fenhong": finite_float_or_none(r.get("pxbl")) or 0.0,    # 派息比（每10股）
            "fenshu": finite_float_or_none(r.get("sgbl")) or 0.0,     # 送股比
            "songzhuangu": 0.0,
            "peigu": finite_float_or_none(r.get("pgbl")) or 0.0,
            "peigujia": finite_float_or_none(r.get("pgjg")) or 0.0,
            "qianzongguben": None,
            "houzongguben": None,
            "name": None,
        })
    return out


# --------------------------------------------------------------------------- #
# §5.4 minute 字段映射
# --------------------------------------------------------------------------- #
def generated_minute_time(index: int, date_str: str, asset_type: str = "stock") -> str:
    """客户端重建分钟时间戳（§4.6 / engine_stock_data.go:201）。

    engine ``minutes`` 返回 ``[{price, volume}, ...]`` 无时间字段。
    按固定交易时段重建：
    - A股/stock（默认）：index 0..119 → 09:31..11:30（上午 120 分钟），
      index 120..239 → 13:01..15:00（下午 120 分钟）
    - 港股/hk：index 0..149 → 09:31..12:00（上午 150 分钟），
      index 150..329 → 13:01..16:00（下午 180 分钟）——分段点和总分钟数
      都和 A 股不同，不能共用 120/120 的判断，否则港股下午盘的分钟会被
      错误地往前挪（用 A 股分段会把港股 index 120..149 这段本该是上午的
      数据当成下午处理）。

    :param index: 0-based 桶序号
    :param date_str: ``YYYYMMDD`` 或 ``YYYY-MM-DD``
    :param asset_type: 资产类型，``"hk"`` 使用港股分段，其余按 A 股分段
    :return: ``YYYY-MM-DD HH:MM:SS``
    """
    # 统一日期格式为 YYYY-MM-DD
    if len(date_str) == 8 and "-" not in date_str:
        date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    else:
        date_iso = date_str

    if asset_type == "hk":
        if index < _HK_MORNING_LEN:
            t = _MORNING_START + timedelta(minutes=index)
        else:
            t = _HK_AFTERNOON_START + timedelta(minutes=index - _HK_MORNING_LEN)
    elif index < 120:
        t = _MORNING_START + timedelta(minutes=index)
    else:
        t = _AFTERNOON_START + timedelta(minutes=index - 120)
    return f"{date_iso} {t.strftime('%H:%M')}:00"


def minutes_rows_to_minute_df(
    rows: list[dict], symbol: str, asset_type: str, date_str: str,
    source: str = "fquant", freq: str = "1m",
) -> pl.DataFrame:
    """engine-data ``minutes`` → MINUTE_COLUMNS（§5.4 / §4.6）。

    每桶 open=high=low=close=price；amount=price*volume（best-effort）。
    """
    if not rows:
        return pl.DataFrame()

    out_rows: list[dict] = []
    for i, r in enumerate(rows):
        price = finite_float_or_none(r.get("price"))
        vol = finite_float_or_none(r.get("volume"))
        out_rows.append({
            "symbol": symbol,
            "asset_type": asset_type,
            "source": source,
            "datetime": generated_minute_time(i, date_str, asset_type=asset_type),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": vol,
            "amount": (price * vol) if (price is not None and vol is not None) else None,
            "freq": freq,
        })
    return pl.DataFrame(out_rows)


# --------------------------------------------------------------------------- #
# §5.5 instrument 字段映射
# --------------------------------------------------------------------------- #
def base_infos_rows_to_instruments(
    rows: list[dict], asset_type: str, source: str = "fquant",
) -> list[dict]:
    """fstore ``base_infos`` → INSTRUMENT_COLS（§5.5 / §4.3）。

    输出列（与 normalizer.INSTRUMENT_COLS 对齐）：
    symbol/name/code/exchange/asset_type/source
    （list_date/status 不在 normalizer select 列内，§5.5 备注，不输出）
    """
    out: list[dict] = []
    for item in rows:
        code = item.get("code")
        if not code:
            continue
        at_num = item.get("asset_type", 1)
        symbol = code_to_symbol(str(code), at_num)
        out.append({
            "symbol": symbol,
            "name": item.get("name") or str(code),
            "code": str(code),
            "exchange": exchange_of(str(code)),
            "asset_type": asset_type,  # 契约字符串，不输出 num
            "source": source,
            "total_shares": finite_float_or_none(item.get("zgb")),
            "float_shares": finite_float_or_none(item.get("ltgb")),
        })
    return out


# --------------------------------------------------------------------------- #
# §5.5b universes 字段映射（chengfen_gu → 指数/板块 instruments）
# --------------------------------------------------------------------------- #
# chengfen_gu.asset_type 数字分类（实测 fstore）：
#   15 = 申万行业 / 同花顺概念 / 中证指数等
#   37~42 = 各家板块分类（详见 fstore.AGENTS.md）
# 当前 fquant 内部不细分 37~42 的语义，对外统一归一为 ``"sector"``。
# 6 位数字 code（沪深京交易所发布）→ ``"index"``；其他（BK/801/...）→ ``"sector"``。
def chengfen_gu_rows_to_universes(
    rows: list[dict],
    asset_type: str = "sector",
    source: str = "fquant",
) -> list[dict]:
    """fstore ``chengfen_gu`` → INSTRUMENT_COLS（§5.5b / 阶段 3 #3.2）。

    输入行字段（实测）：
    - ``code``：6 位数字 → 指数 / ``BK``/``801`` 开头 → 板块/行业
    - ``name``
    - ``asset_type``：fstore 内部分类数字（15/37/38/39/40/41/42）

    输出列：symbol / name / code / exchange / asset_type / source
    """
    out: list[dict] = []
    for r in rows:
        code = r.get("code")
        name = r.get("name")
        if not code or not name:
            continue
        code_str = str(code)
        # 6 位数字 code 默认按指数归一（与 base_infos.asset_type=10 对齐）
        is_index_like = code_str.isdigit() and len(code_str) == 6
        resolved = "index" if is_index_like else asset_type
        # 指数走 .INDEX 后缀；板块/行业（BK/801/...）不带市场后缀
        if is_index_like:
            symbol = code_to_symbol(code_str, 10)
        else:
            symbol = code_str
        out.append({
            "symbol": symbol,
            "name": str(name),
            "code": code_str,
            "exchange": exchange_of(code_str) if is_index_like else "",
            "asset_type": resolved,
            "source": source,
        })
    return out


# --------------------------------------------------------------------------- #
# §5.6 financial 字段映射
# --------------------------------------------------------------------------- #
def financial_rows_to_df(
    rows: list[dict], symbol: str, table: str, source_tag: str = "fquant:fstore",
) -> pl.DataFrame:
    """fstore ``financial_report_*`` → 归一 df（§5.6 / §4.8）。

    内部 df 列统一：``symbol, t_date, ...<source cols>..., notice_date``。
    bigint/numeric → float（Decimal 转换）。
    """
    if not rows:
        return pl.DataFrame()
    out: list[dict] = []
    for r in rows:
        row = _serialize_row(r)
        row["symbol"] = symbol
        row["source"] = f"{source_tag}:{table}"
        out.append(row)
    return pl.DataFrame(out)


# --------------------------------------------------------------------------- #
# §5.7 moneyflow 字段映射
# --------------------------------------------------------------------------- #
def moneyflow_daily_to_df(
    data: dict[str, dict], code_to_sym: dict[str, str], date_iso: str,
    source: str = "fquant",
) -> pl.DataFrame:
    """moneyflow daily → 归一 df（§5.7）。

    输出列：symbol/date/source/main_net/total_net/main_inflow/main_outflow/volume/amount
    """
    if not data:
        return pl.DataFrame()
    out: list[dict] = []
    for code, total in data.items():
        sym = code_to_sym.get(str(code), str(code))
        out.append({
            "symbol": sym,
            "date": date_iso,
            "source": f"{source}:moneyflow:daily",
            "main_net": finite_float_or_none(total.get("main_net")),
            "total_net": finite_float_or_none(total.get("total_net")),
            "main_inflow": finite_float_or_none(total.get("main_inflow")),
            "main_outflow": finite_float_or_none(total.get("main_outflow")),
            "total_inflow": finite_float_or_none(total.get("total_inflow")),
            "total_outflow": finite_float_or_none(total.get("total_outflow")),
            "volume": finite_float_or_none(total.get("volume")),
            "amount": finite_float_or_none(total.get("amount")),
            "super_large_net": finite_float_or_none(total.get("super_large_net")),
            "large_net": finite_float_or_none(total.get("large_net")),
            "medium_net": finite_float_or_none(total.get("medium_net")),
            "small_net": finite_float_or_none(total.get("small_net")),
            "main_ratio": finite_float_or_none(total.get("main_ratio")),
            "super_large_ratio": finite_float_or_none(total.get("super_large_ratio")),
            "large_ratio": finite_float_or_none(total.get("large_ratio")),
            "medium_ratio": finite_float_or_none(total.get("medium_ratio")),
            "small_ratio": finite_float_or_none(total.get("small_ratio")),
        })
    return pl.DataFrame(out) if out else pl.DataFrame()


def moneyflow_minute_to_df(
    records: list[dict], symbol: str, date_iso: str, source: str = "fquant",
) -> pl.DataFrame:
    """moneyflow minute → 归一 df（§5.7 / §4.9）。

    输出列：symbol/trade_date/bucket_time/total_amount/net_amount/
            main_traditional_net/main_broad_net/large_net/super_large_net/
            medium_net/small_net/neutral_amount/valid_count/source
    """
    if not records:
        return pl.DataFrame()
    out: list[dict] = []
    for rec in records:
        out.append({
            "symbol": symbol,
            "trade_date": rec.get("TradeDate") or date_iso,
            "bucket_time": rec.get("BucketTime"),
            "total_amount": finite_float_or_none(rec.get("TotalAmount")),
            "net_amount": finite_float_or_none(rec.get("NetAmount")),
            "main_traditional_net": finite_float_or_none(rec.get("MainTraditionalNet")),
            "main_broad_net": finite_float_or_none(rec.get("MainBroadNet")),
            "large_net": finite_float_or_none(rec.get("LargeNet")),
            "super_large_net": finite_float_or_none(rec.get("SuperLargeNet")),
            "medium_net": finite_float_or_none(rec.get("MediumNet")),
            "small_net": finite_float_or_none(rec.get("SmallNet")),
            "neutral_amount": finite_float_or_none(rec.get("NeutralAmount")),
            "valid_count": finite_float_or_none(rec.get("ValidCount")),
            "source": f"{source}:moneyflow:minute",
        })
    return pl.DataFrame(out) if out else pl.DataFrame()


# --------------------------------------------------------------------------- #
# 扩展方法：trans（逐笔）→ df
# --------------------------------------------------------------------------- #
def trans_rows_to_df(
    rows: list[dict], symbol: str, date_str: str, source: str = "fquant",
) -> pl.DataFrame:
    """engine-data ``trans`` → 归一 df（§3.4 扩展方法 ``get_transactions``）。

    direction 实测为数字：0=中性 / 1=买 / 2=卖（§2.2）。
    """
    if not rows:
        return pl.DataFrame()
    # 统一日期格式
    if len(date_str) == 8 and "-" not in date_str:
        date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    else:
        date_iso = date_str

    out: list[dict] = []
    for r in rows:
        out.append({
            "symbol": symbol,
            "datetime": f"{date_iso} {r.get('time', '')}",
            "price": finite_float_or_none(r.get("price")),
            "volume": finite_float_or_none(r.get("volume")),
            "amount": finite_float_or_none(r.get("amount")),
            "order_count": r.get("order_count"),
            "direction": r.get("direction"),   # 0=中性 / 1=买 / 2=卖
            "source": f"{source}:engine-data:trans",
        })
    return pl.DataFrame(out) if out else pl.DataFrame()
