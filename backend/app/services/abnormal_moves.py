"""交易所口径近似异动监测 — 集中定义板块、基准指数、阈值与状态。

职责边界:
  - 纯计算层: 板块识别 / ST 标记 / 基准映射 / 阈值 / 状态分级 / 窗口偏离值,
    输入输出均为普通 Python 数据 (百分点口径), 可独立单测。
  - 集成层: 从 repository 读取 canonical/enriched 日线 (小数口径 → 统一 ×100),
    用 quote_service 本地指数实时缓存修正当日指数收益, 绝不直连外部行情,
    绝不以 0 伪装缺失指数。

异动口径 (交易所规则近似, 非交易所公告):
  deviate_Nd = 最近 N 个连续交易日 Σ(个股日涨跌幅 - 对应指数日涨跌幅), N ∈ {3,10,30}
  阈值: 3日 主板±20% / 创业板·科创板±30% / 北交所±40%;
        10日 +100% / -50%; 30日 +200% / -70% (全板块, 边界含等号)
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# 板块 → 基准指数 (canonical .INDEX; 主板沪/深按股票后缀二选一)
BOARD_BENCHMARKS: dict[str, tuple[str, ...]] = {
    "主板": ("000001.INDEX", "399001.INDEX"),
    "创业板": ("399006.INDEX",),
    "科创板": ("000680.INDEX",),
    "北交所": ("899050.INDEX",),
}
WINDOWS: tuple[int, ...] = (3, 10, 30)
HISTORY_CALENDAR_DAYS = 90  # 30 交易日窗口的日历日缓冲

STATUS_TRIGGERED = "triggered"
STATUS_EDGE = "edge"
STATUS_WATCH = "watch"
STATUS_NORMAL = "normal"


# ── 纯计算层 (百分点口径) ─────────────────────────────────


def board_for_symbol(symbol: str) -> str:
    """按代码前缀识别板块: 688 科创 / 300·301 创业 / .BJ·4·8·92 北交, 其余主板。"""
    s = str(symbol or "").upper()
    code = s.split(".", 1)[0]
    if s.endswith(".BJ") or code.startswith(("4", "8", "92")):
        return "北交所"
    if code.startswith("688"):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    return "主板"


def is_st_name(name: Any) -> bool:
    """名称含 ST (含 *ST) 即标记; 仅作展示过滤, 不降低阈值。"""
    return "ST" in str(name or "").upper()


def benchmark_for_symbol(symbol: str) -> str:
    """股票对应的基准指数 canonical (.INDEX); 主板沪 000001 / 深 399001。"""
    board = board_for_symbol(symbol)
    if board == "主板":
        return "399001.INDEX" if str(symbol).upper().endswith(".SZ") else "000001.INDEX"
    return BOARD_BENCHMARKS[board][0]


def threshold_pct(board: str, window: int, direction: str) -> float | None:
    """交易所近似阈值 (百分点); 未知板块/窗口返回 None (显式不可用)。"""
    if window == 3:
        return {"主板": 20.0, "创业板": 30.0, "科创板": 30.0, "北交所": 40.0}.get(board)
    if window == 10:
        return 100.0 if direction == "up" else 50.0
    if window == 30:
        return 200.0 if direction == "up" else 70.0
    return None


def status_for_ratio(ratio: float) -> str:
    """threshold ratio 分级: >=1 triggered / >=0.7 edge / >=0.5 watch / 其余 normal。"""
    if ratio >= 1.0:
        return STATUS_TRIGGERED
    if ratio >= 0.7:
        return STATUS_EDGE
    if ratio >= 0.5:
        return STATUS_WATCH
    return STATUS_NORMAL


def build_rows(
    stock_returns: Mapping[str, Mapping[date, float]],
    index_returns: Mapping[str, Mapping[date, float]],
    names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """计算检测行 (百分点口径输入)。

    窗口对齐规则: 取个股最近 N 个交易日, 逐日要求该日存在于基准指数收益表;
    任一日缺失 (基准不可用/停牌错位) → 该窗口显式跳过, 绝不以 0 伪装指数收益。
    每股产出可达 3 行 (3d/10d/30d), 数据不足的窗口不产出。
    """
    names = names or {}
    rows: list[dict[str, Any]] = []
    for symbol, returns in stock_returns.items():
        if not returns:
            continue
        board = board_for_symbol(symbol)
        benchmark = benchmark_for_symbol(symbol)
        bench = index_returns.get(benchmark) or {}
        name = names.get(symbol, "")
        benchmark_dates = sorted(bench)
        for window in WINDOWS:
            if len(benchmark_dates) < window:
                continue
            # 窗口由市场（基准指数）最近 N 个交易日定义；停牌导致个股缺任一
            # 市场交易日时该窗口不可用，禁止把停牌前旧交易日拼进窗口。
            tail = benchmark_dates[-window:]
            if any(d not in returns for d in tail):
                continue
            deviation = sum(returns[d] - bench[d] for d in tail)
            direction = "up" if deviation >= 0 else "down"
            threshold = threshold_pct(board, window, direction)
            if threshold is None:
                continue
            ratio = abs(deviation) / threshold
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "board": board,
                    "is_st": is_st_name(name),
                    "window": f"{window}d",
                    "direction": direction,
                    "deviation_pct": deviation,
                    "threshold_pct": threshold,
                    "ratio": ratio,
                    "status": status_for_ratio(ratio),
                    "benchmark_symbol": benchmark,
                    "benchmark_available": True,
                }
            )
    return rows


def filter_rows(
    rows: list[dict[str, Any]],
    *,
    status: str | None = None,
    board: str | None = None,
    direction: str | None = None,
    hide_st: bool = False,
    symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    """按状态/板块/方向/ST/标的集过滤检测行 (API 过滤与监控 scope 复用)。"""
    return [
        r
        for r in rows
        if (not status or r["status"] == status)
        and (not board or r["board"] == board)
        and (not direction or r["direction"] == direction)
        and (not hide_st or not r["is_st"])
        and (symbols is None or r["symbol"] in symbols)
    ]


# ── 集成层 (repository + quote_service 本地数据) ────────────


def _to_points(value: Any) -> float | None:
    """小数涨跌幅 → 百分点；非数值和非有限数返回 None。"""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x * 100.0 if math.isfinite(x) else None


def _series_to_points(df: Any) -> dict[date, float]:
    """本地日线 → {date: 日收益百分点}。

    个股优先用 ``raw_close``（与指标流水线 change_pct 的除权口径一致），逐行
    缺失时回退 ``close``；指数只有 ``close``。这样仍可直接投影存储列，避免
    请求 ``change_pct`` 触发整套指标重算。
    """
    if df is None or (hasattr(df, "is_empty") and df.is_empty()):
        return {}
    rows = df.to_dicts() if hasattr(df, "to_dicts") else list(df)

    dated_rows: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        raw_date = row.get("date")
        if isinstance(raw_date, str):
            try:
                raw_date = date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
        if isinstance(raw_date, date):
            dated_rows.append((raw_date, row))

    if any("raw_close" in row or "close" in row for _, row in dated_rows):
        out: dict[date, float] = {}
        previous: float | None = None
        for trading_date, row in sorted(dated_rows, key=lambda item: item[0]):
            raw_value = row.get("raw_close")
            try:
                raw_close = float(raw_value)
            except (TypeError, ValueError):
                raw_close = float("nan")
            price_value = (
                raw_close if math.isfinite(raw_close) and raw_close > 0 else row.get("close")
            )
            try:
                close = float(price_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(close) or close <= 0:
                continue
            if previous is not None:
                out[trading_date] = (close / previous - 1.0) * 100.0
            previous = close
        return out

    out: dict[date, float] = {}
    for trading_date, row in dated_rows:
        pct = _to_points(row.get("change_pct"))
        if pct is not None:
            out[trading_date] = pct
    return out


def _index_realtime_overrides(quote_service: Any, today: date) -> dict[str, float]:
    """从 quote_service 本地指数实时缓存取当日收益 (百分点), 仅当日有效。

    缓存中 change_pct 已是百分点口径 (见 QuoteService._build_index_quotes)。
    只读缓存, 不触发任何拉取; timestamp 不是今天的行一律忽略。
    """
    out: dict[str, float] = {}
    try:
        df = quote_service.get_index_quotes()
    except Exception as e:  # noqa: BLE001
        logger.warning("指数实时缓存读取失败 (异动监测继续用历史): %s", e)
        return out
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return out
    for row in df.to_dicts():
        ts = str(row.get("timestamp") or "")
        if ts[:10] != today.isoformat():
            continue
        try:
            pct = float(row.get("change_pct"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pct):
            continue
        out[str(row.get("symbol"))] = pct
    return out


def load_inputs(
    repo: Any,
    quote_service: Any | None = None,
    *,
    as_of: date | None = None,
) -> tuple[
    dict[str, dict[date, float]], dict[str, dict[date, float]], dict[str, str], dict[str, Any]
]:
    """加载个股/指数收益序列与股票名称。

    返回 (stock_returns, index_returns, names, provenance)。
    - 个股: enriched 历史窗口 + 实时 enriched_latest 覆盖当日行;
    - 指数: 本地指数日 K 历史 + 实时缓存修正当日 (仅 timestamp=今天);
    - 北交所基准 899050 本地缺失时不出现在 index_returns, 由
      build_overview 显式标注 benchmark 不可用。
    """
    today = as_of or date.today()
    start = today - timedelta(days=HISTORY_CALENDAR_DAYS)
    provenance: dict[str, Any] = {
        "as_of": today.isoformat(),
        "stock_history_source": "local_enriched",
        "index_history_source": "local_index_daily",
        "index_realtime": {},
        "benchmarks_missing": [],
    }

    stock_returns: dict[str, dict[date, float]] = {}
    stock_as_of: date | None = None
    try:
        hist = repo.get_enriched_range(
            start - timedelta(days=14),
            today,
            columns=["symbol", "date", "raw_close", "close"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("异动监测: enriched 历史加载失败: %s", e)
        hist = None
    if hist is not None and not hist.is_empty():
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in hist.to_dicts():
            symbol = row.get("symbol")
            if symbol:
                by_symbol.setdefault(str(symbol), []).append(row)
        for symbol, rows in by_symbol.items():
            series = _series_to_points(rows)
            if not series:
                continue
            stock_returns[symbol] = series
            symbol_as_of = max(series)
            stock_as_of = (
                symbol_as_of if stock_as_of is None or symbol_as_of > stock_as_of else stock_as_of
            )

    # 实时 enriched_latest 覆盖当日行 (quote_service 盘中每轮更新)
    realtime_as_of: date | None = None
    if quote_service is not None:
        try:
            live_df, live_date = quote_service.get_enriched_today()
        except Exception as e:  # noqa: BLE001
            logger.warning("异动监测: 实时 enriched 读取失败: %s", e)
            live_df, live_date = None, None
        if live_df is not None and not live_df.is_empty() and isinstance(live_date, date):
            realtime_as_of = live_date
            for row in live_df.to_dicts():
                sym = row.get("symbol")
                if not sym:
                    continue
                pct = _to_points(row.get("change_pct"))
                if pct is not None:
                    stock_returns.setdefault(str(sym), {})[live_date] = pct
    provenance["stock_as_of"] = (realtime_as_of or stock_as_of or today).isoformat()
    provenance["stock_source"] = "local_enriched+realtime" if realtime_as_of else "local_enriched"

    # 指数: 历史日 K + 实时当日修正
    overrides = _index_realtime_overrides(quote_service, today) if quote_service else {}
    all_benchmarks = {b for tup in BOARD_BENCHMARKS.values() for b in tup}
    index_returns: dict[str, dict[date, float]] = {}
    for bench in sorted(all_benchmarks):
        series: dict[date, float] = {}
        try:
            idx_df = repo.get_index_daily(
                bench,
                start - timedelta(days=14),
                today,
                columns=["date", "close"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("异动监测: 指数 %s 历史加载失败: %s", bench, e)
            idx_df = None
        if idx_df is not None and not idx_df.is_empty():
            series = _series_to_points(idx_df)
        if bench in overrides:
            series[today] = overrides[bench]
            provenance["index_realtime"][bench] = today.isoformat()
        if series:
            index_returns[bench] = series
        else:
            provenance["benchmarks_missing"].append(bench)

    # 股票名称 (ST 标记用); 缺失时容错为空
    names: dict[str, str] = {}
    try:
        inst = repo.get_instruments()
        if inst is not None and not inst.is_empty():
            for row in inst.select(["symbol", "name"]).to_dicts():
                if row.get("name"):
                    names[str(row["symbol"])] = str(row["name"])
    except Exception as e:  # noqa: BLE001
        logger.warning("异动监测: instruments 名称加载失败: %s", e)

    return stock_returns, index_returns, names, provenance


def build_overview(
    repo: Any,
    quote_service: Any | None = None,
    *,
    as_of: date | None = None,
    status: str | None = None,
    board: str | None = None,
    direction: str | None = None,
    hide_st: bool = False,
) -> dict[str, Any]:
    """构建异动总览 (只读, fail-soft): {rows, warnings, provenance}。

    数据缺失/基准缺失时返回空 rows + 明确 warnings, 绝不伪造 0 值。
    """
    warnings: list[str] = []
    try:
        stock_returns, index_returns, names, provenance = load_inputs(
            repo,
            quote_service,
            as_of=as_of,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("异动总览构建失败")
        return {
            "rows": [],
            "warnings": [f"数据加载失败: {e}"],
            "provenance": {"as_of": (as_of or date.today()).isoformat(), "source": "error"},
        }

    if not stock_returns:
        warnings.append("本地无个股 enriched 日线数据, 无法计算偏离值")
    for bench in provenance.get("benchmarks_missing", []):
        warnings.append(f"基准指数 {bench} 本地数据缺失, 对应板块实时修正与历史偏离不可用")

    rows = build_rows(stock_returns, index_returns, names)
    rows = filter_rows(rows, status=status, board=board, direction=direction, hide_st=hide_st)
    rows.sort(key=lambda r: (-r["ratio"], r["symbol"], r["window"]))
    return {"rows": rows, "warnings": warnings, "provenance": provenance}
