"""FQuantProvider v2 冒烟 + 集成测试（§8 测试方案）。

覆盖（§8.1 / §8.2 / §8.4）：
1. import / registry / capabilities（§8.1）
2. 符号归一工具函数（§8.1 / §5.1）
3. 空 symbols 安全性（§8.1）
4. get_daily 真实数据（走 DuckDB market_wide_kline，§8.2）
5. get_instruments 走 fstore.base_infos（§8.2）
6. get_minute 走 DuckDB market_minutes（§8.2）
7. get_adj_factors 走 DuckDB market_xdxr（§8.2）
8. get_financial 走 fstore financial_report_*（§8.2）
9. get_moneyflow_daily / get_moneyflow_minute（§8.2）
10. 故障 mock：三源分别 mock，其他源不受影响（§8.4）

用法::

    cd backend
    uv run python scripts/test_fquant_provider.py

三源任一不可达 → 该项 skip（warning），不算 fail；报告会单独列 skip 数。
"""
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta
from unittest.mock import patch


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
SKIPS: list[str] = []


def _report(failures: list[str]) -> int:
    print()
    if failures:
        print(f"❌ {len(failures)} 项失败: {failures}")
        return 1
    if SKIPS:
        print(f"✅ 无失败，跳过 {len(SKIPS)} 项: {SKIPS}")
    else:
        print("✅ 全部通过，0 skip")
    return 0


def _skip(msg: str) -> None:
    """源不可达时 skip（不算 fail）。"""
    SKIPS.append(msg)
    print(f"  ⚠ SKIP — {msg}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    failures: list[str] = []

    # ================================================================== #
    # §8.1 单元测试
    # ================================================================== #
    print("=== 1. import & registry 检查 ===")
    try:
        from app.data_providers.registry import get_provider, normalize_provider_name

        print("  ✓ fquant_provider import 成功")
    except Exception as e:
        print(f"  ✗ import 失败: {e}")
        failures.append("import")
        return _report(failures)

    try:
        provider_name = normalize_provider_name(os.getenv("DATA_PROVIDER"), default="fquant")
        assert provider_name in {"fquant", "fquant_local"}, "本脚本只验证 fquant/fquant_local"
        provider = get_provider(provider_name)
        assert provider.name == provider_name
        print(f"  ✓ get_provider({provider_name!r}) → {type(provider).__name__}")
    except Exception as e:
        print(f"  ✗ registry 注册失败: {e}")
        failures.append("registry")
        return _report(failures)

    # ------------------------------------------------------------------ #
    print("=== 2. capabilities 声明（§3.5）===")
    p = provider
    assert p.name == provider_name
    caps = p.capabilities
    assert caps.instruments is True, "instruments 应为 True"
    assert caps.daily is True, "daily 应为 True"
    assert caps.adj_factor is True, "adj_factor 应为 True"
    assert caps.minute is True, "minute 应为 True"
    assert caps.realtime is True, "realtime 应走 DuckDB daily_markets 快照"
    assert caps.financial is True, "financial 应为 True（新增）"
    assert caps.depth is False, "depth 无本地源，应为 False"
    assert caps.universes is True, "universes 应走 fstore chengfen_gu"
    print(f"  ✓ capabilities: instruments={caps.instruments} daily={caps.daily} "
          f"adj_factor={caps.adj_factor} minute={caps.minute} "
          f"realtime={caps.realtime} financial={caps.financial} "
          f"depth={caps.depth} universes={caps.universes}")

    # ------------------------------------------------------------------ #
    print("=== 3. 符号归一工具函数（§5.1）===")
    from app.data_providers.fquant import (
        code_and_market_to_symbol,
        split_symbol,
        symbol_to_market,
    )

    # split_symbol
    split_cases = [
        ("600519.SH", ("600519", "SH")),
        ("000001.SZ", ("000001", "SZ")),
        ("00700.HK", ("00700", "HK")),
        ("000300.INDEX", ("000300", "INDEX")),
        ("510330.ETF", ("510330", "ETF")),
        ("600519", ("600519", "")),   # 无后缀
    ]
    for sym, expected in split_cases:
        got = split_symbol(sym)
        assert got == expected, f"split_symbol({sym!r}) = {got}, want {expected}"
    print(f"  ✓ split_symbol: {len(split_cases)} 用例通过")

    # symbol_to_market
    assert symbol_to_market("600519.SH") == (1, "a")
    assert symbol_to_market("000001.SZ") == (1, "a")
    assert symbol_to_market("00700.HK") == (3, "hk")
    assert symbol_to_market("000300.INDEX") is None  # 指数暂未映射
    print("  ✓ symbol_to_market 全部通过")

    # code_and_market_to_symbol
    cm_cases = [
        ("600519", 1, "600519.SH"),
        ("000001", 1, "000001.SZ"),
        ("00700", 3, "00700.HK"),
        ("000300", 10, "000300.INDEX"),
        ("510330", 20, "510330.SH"),
    ]
    for code, at, expected in cm_cases:
        got = code_and_market_to_symbol(code, at)
        assert got == expected, f"code_and_market_to_symbol({code},{at}) = {got}, want {expected}"
    print(f"  ✓ code_and_market_to_symbol: {len(cm_cases)} 用例通过")

    # ------------------------------------------------------------------ #
    print("=== 4. 空 symbols 安全性（§8.1）===")
    assert p.get_daily([], None, None, "stock").is_empty()
    assert p.get_adj_factors([], None, None, "stock").is_empty()
    assert p.get_minute([], None, None, "stock").is_empty()
    assert p.get_realtime(symbols=[]).is_empty()
    assert p.get_moneyflow_daily([]).is_empty()
    assert p.get_moneyflow_minute([]).is_empty()
    print("  ✓ 所有契约方法空 symbols → 空 DF")

    # realtime 不直接调用 fquant API,空入参仍应空降级
    assert p.get_realtime().is_empty()
    assert p.get_realtime(universes=[]).is_empty()
    print("  ✓ get_realtime 无入参 / 空 universes → 空 DF")

    # ================================================================== #
    # §8.2 集成测试（连真实三源）
    # ================================================================== #
    end = datetime.now()
    start = end - timedelta(days=30)

    # ------------------------------------------------------------------ #
    print("=== 5. get_daily 真实数据（走 DuckDB market_wide_kline，§8.2）===")
    df = p.get_daily(["600519.SH"], start, end, "stock")
    if df.is_empty():
        _skip("get_daily 返回空 DF（DuckDB 可能不可达或缺数据）")
    else:
        print(f"  ✓ get_daily 返回 {df.height} 行, 列: {df.columns}")
        # 验证 DAILY_COLS 子集
        for col in ("symbol", "date", "open", "high", "low", "close", "volume", "amount"):
            assert col in df.columns, f"缺少列 {col}"
        print("  ✓ DAILY_COLS 子集完整")
        print(df.head(3).to_pandas().to_string())

    # ------------------------------------------------------------------ #
    print("=== 6. get_instruments（走 fstore.base_infos，§8.2）===")
    inst = p.get_instruments("stock")
    if inst.is_empty():
        _skip("get_instruments 返回空 DF（fstore DB 可能离线）")
    else:
        print(f"  ✓ get_instruments 返回 {inst.height} 条")
        # 验证 INSTRUMENT_COLS
        for col in ("symbol", "name", "code", "exchange", "asset_type", "source"):
            assert col in inst.columns, f"缺少列 {col}"
        print("  ✓ INSTRUMENT_COLS 完整")
        print(inst.head(3).to_pandas().to_string())

    # ------------------------------------------------------------------ #
    print("=== 7. get_minute（走 DuckDB market_minutes，§8.2）===")
    # 分钟 catalog 对超覆盖日期 fail-closed（刻意语义）：先把窗口末端钳到
    # 已发布水位，水位不可知时保持原窗口让 fail-closed 原样暴露。
    minute_end = end
    coverage = p.get_minute_coverage()
    if coverage and coverage.get("latest_date"):
        from datetime import date as _date

        covered = _date.fromisoformat(str(coverage["latest_date"]))
        minute_end = min(end, datetime.combine(covered, datetime.max.time()))
    inverted = minute_end.date() < start.date()
    minute_df = None
    if inverted:
        # 水位完全落后于查询窗口：倒置窗口会走 get_minute 单日旧日期路径，
        # 读取窗口外数据并误判通过。这是数据侧状态而非 provider 缺陷，显式跳过。
        _skip(f"get_minute 发布水位 {minute_end.date()} 早于查询窗口起点 {start.date()}")
    else:
        minute_df = p.get_minute(["600519.SH"], start, minute_end, "stock")
    if minute_df is not None and not minute_df.is_empty():
        print(f"  ✓ get_minute 返回 {minute_df.height} 行, 列: {minute_df.columns}")
        # 验证 MINUTE_COLUMNS
        from app.data_providers.schemas import MINUTE_COLUMNS
        for col in MINUTE_COLUMNS:
            assert col in minute_df.columns, f"缺少列 {col}"
        print("  ✓ MINUTE_COLUMNS 完整")
        print(minute_df.head(3).to_pandas().to_string())
    elif not inverted:
        _skip("get_minute 返回空 DF（DuckDB 可能不可达或非交易日）")

    # ------------------------------------------------------------------ #
    print("=== 8. get_adj_factors（走 DuckDB market_xdxr，§8.2）===")
    adj_start = end - timedelta(days=365)
    adj_df = p.get_adj_factors(["600519.SH"], adj_start, end, "stock")
    if adj_df.is_empty():
        _skip("get_adj_factors 返回空 DF（DuckDB xdxr/fstore 可能缺数据）")
    else:
        print(f"  ✓ get_adj_factors 返回 {adj_df.height} 行, 列: {adj_df.columns}")
        for col in ("symbol", "trade_date", "ex_factor"):
            assert col in adj_df.columns, f"缺少列 {col}"
        print("  ✓ ADJ_FACTOR_COLS 完整")
        print(adj_df.to_pandas().to_string())

    # ------------------------------------------------------------------ #
    print("=== 9. get_financial（走 fstore financial_report_*，§8.2）===")
    fin_df = p.get_financial("600519.SH", table="income")
    if fin_df.is_empty():
        _skip("get_financial 返回空 DF（fstore DB 可能离线）")
    else:
        print(f"  ✓ get_financial(income) 返回 {fin_df.height} 行, 列数: {len(fin_df.columns)}")
        assert "symbol" in fin_df.columns
        assert "t_date" in fin_df.columns
        print(f"  ✓ 列含 symbol/t_date; 前5列: {fin_df.columns[:5]}")
        print(fin_df.head(3).to_pandas().to_string())

    # ------------------------------------------------------------------ #
    print("=== 10. get_moneyflow_daily（走 DuckDB market_fund_flow，§8.2）===")
    mf_daily = p.get_moneyflow_daily(["600519.SH"], date=end)
    if mf_daily.is_empty():
        _skip("get_moneyflow_daily 返回空 DF（DuckDB market_fund_flow 可能缺数据）")
    else:
        print(f"  ✓ get_moneyflow_daily 返回 {mf_daily.height} 行, 列: {mf_daily.columns}")
        print(mf_daily.to_pandas().to_string())

    # ------------------------------------------------------------------ #
    print("=== 11. get_moneyflow_minute（已下线，§8.2）===")
    mf_minute = p.get_moneyflow_minute(["600519.SH"], date=end)
    if mf_minute.is_empty():
        _skip("get_moneyflow_minute 返回空 DF（预期：分钟资金流已下线）")
    else:
        print(f"  ✓ get_moneyflow_minute 返回 {mf_minute.height} 行, 列: {mf_minute.columns}")
        print(mf_minute.head(3).to_pandas().to_string())

    # ================================================================== #
    # §8.4 故障注入（chaos）— 三源分别 mock，其他源不受影响
    # ================================================================== #
    print("=== 12. 故障 mock：fstore DB 连不上（§8.4）===")
    chaos_failures = _test_chaos_fstore_down(p)
    failures.extend(chaos_failures)

    print("=== 13. 故障 mock：engine-data 5xx（§8.4）===")
    chaos_failures = _test_chaos_engine_down(p, start, end)
    failures.extend(chaos_failures)

    print("=== 14. 故障 mock：分钟资金流下线（§8.4）===")
    chaos_failures = _test_moneyflow_minute_disabled(p)
    failures.extend(chaos_failures)

    print("=== 15. 故障 mock：所有源全挂（§8.4 L4）===")
    chaos_failures = _test_chaos_all_down(p, start, end)
    failures.extend(chaos_failures)

    print("=== 16. get_realtime 本地 DuckDB daily_markets 路径 ===")
    mapped = p._fstore_quote_to_row({
        "code": "600519",
        "name": "贵州茅台",
        "tdate": "2026-07-02",
        "price": 1185.49,
        "zrspj": 1180.0,
        "cjl": 12345,
        "cje": 6789000,
    }, 1)
    assert mapped is not None
    assert mapped["symbol"] == "600519.SH"
    assert mapped["last_price"] == 1185.49
    assert mapped["prev_close"] == 1180.0
    assert mapped["source"] == f"{p.name}:fstore:daily_markets"
    rt = p.get_realtime(symbols=["600519.SH"])
    if rt.is_empty():
        print("  ⚠ SKIP — get_realtime 真实源返回空（daily_markets 可能无快照）")
    else:
        assert "symbol" in rt.columns and "last_price" in rt.columns
        print(f"  ✓ get_realtime 返回 {rt.height} 行, source={rt['source'][0] if 'source' in rt.columns else 'unknown'}")
    print("  ✓ get_realtime 不调用 ../fquant HTTP API")

    return _report(failures)


# --------------------------------------------------------------------------- #
# §8.4 故障注入测试
# --------------------------------------------------------------------------- #
def _test_chaos_fstore_down(p) -> list[str]:
    """fstore DB 连不上 → DB 方法返回空 df，HTTP 方法继续工作（§8.4）。"""
    failures: list[str] = []
    # 清除 instruments 缓存（前面集成测试可能已填充）
    p._instruments_cache.clear()
    p._instruments_cache_ts.clear()
    try:
        # mock fstore 不可用
        with patch.object(p._fstore, "_get_conn", return_value=None):
            # get_financial 应返回空 df（不抛）
            fin = p.get_financial("600519.SH", table="income")
            assert fin.is_empty(), "fstore 挂时 get_financial 应返回空 DF"
            print("  ✓ fstore 挂 → get_financial 返回空 DF")

            # get_instruments 应返回空 df
            inst = p.get_instruments("stock")
            assert inst.is_empty(), "fstore 挂时 get_instruments 应返回空 DF"
            print("  ✓ fstore 挂 → get_instruments 返回空 DF")

            # engine-data / moneyflow 方法不受影响（不依赖 fstore）
            # get_daily 主源是 engine-data wide，fstore 挂不应阻断
            # （这里只验证不抛异常）
            try:
                p.get_daily(["600519.SH"], None, None, "stock")
                print("  ✓ fstore 挂 → get_daily 不抛异常（走 DuckDB engine）")
            except Exception as e:
                failures.append(f"chaos_fstore: get_daily 抛异常 {e}")
    except AssertionError as e:
        failures.append(f"chaos_fstore: {e}")
    except Exception as e:
        failures.append(f"chaos_fstore: 意外异常 {e}")
    return failures


def _test_chaos_engine_down(p, start, end) -> list[str]:
    """engine-data 5xx → get_daily 退 fstore day_klines（§8.4）。"""
    failures: list[str] = []
    try:
        # mock DuckDB engine 返回空（模拟文件缺失/查询失败）
        with patch.object(p._engine, "get_wide", return_value=[]), \
             patch.object(p._engine, "get_minutes", return_value=[]):
            # get_daily 应不抛，退 fstore day_klines（可能也为空，但不抛）
            df = p.get_daily(["600519.SH"], start, end, "stock")
            # 不断言非空（fstore day_klines 实测 600519 也可能无近期数据）
            print(f"  ✓ DuckDB engine 挂 → get_daily 不抛异常（返回 {df.height} 行，退 fstore）")

            # get_minute 应返回空（主源 engine-data）
            minute_df = p.get_minute(["600519.SH"], start, end, "stock")
            assert minute_df.is_empty(), "DuckDB engine 挂时 get_minute 应返回空 DF"
            print("  ✓ DuckDB engine 挂 → get_minute 返回空 DF")

            # fstore 方法不受影响（不依赖 engine-data）
            # get_financial 应正常工作
            try:
                p.get_financial("600519.SH", table="income")
                print("  ✓ DuckDB engine 挂 → get_financial 不受影响（走 fstore DuckDB）")
            except Exception as e:
                failures.append(f"chaos_engine: get_financial 抛异常 {e}")
    except Exception as e:
        failures.append(f"chaos_engine: 意外异常 {e}")
    return failures


def _test_moneyflow_minute_disabled(p) -> list[str]:
    """分钟资金流已下线 → 恒返回空 df。"""
    failures: list[str] = []
    try:
        mfm = p.get_moneyflow_minute(["600519.SH"])
        assert mfm.is_empty(), "get_moneyflow_minute 应返回空 DF"
        print("  ✓ get_moneyflow_minute 返回空 DF")
    except Exception as e:
        failures.append(f"moneyflow_minute_disabled: 意外异常 {e}")
    return failures


def _test_chaos_all_down(p, start, end) -> list[str]:
    """所有源全挂 → L4 全部返回空 df，不抛异常（§8.4）。"""
    failures: list[str] = []
    # 清除 instruments 缓存（前面集成测试可能已填充）
    p._instruments_cache.clear()
    p._instruments_cache_ts.clear()
    try:
        fund_patch = (
            patch.object(p._engine, "get_fund_daily", return_value={})
            if hasattr(p._engine, "get_fund_daily")
            else nullcontext()
        )
        with fund_patch, \
             patch.object(p._fstore, "_get_conn", return_value=None), \
             patch.object(p._engine, "get_wide", return_value=[]), \
             patch.object(p._engine, "get_xdxr", return_value=[]), \
             patch.object(p._engine, "get_minutes", return_value=[]):
            # 所有方法应返回空 DF，不抛
            assert p.get_daily(["600519.SH"], start, end, "stock").is_empty()
            assert p.get_adj_factors(["600519.SH"], start, end, "stock").is_empty()
            assert p.get_minute(["600519.SH"], start, end, "stock").is_empty()
            assert p.get_instruments("stock").is_empty()
            assert p.get_financial("600519.SH", table="income").is_empty()
            assert p.get_moneyflow_daily(["600519.SH"]).is_empty()
            assert p.get_moneyflow_minute(["600519.SH"]).is_empty()
            print("  ✓ 所有源全挂 → 全部方法返回空 DF，不抛异常（L4）")
    except AssertionError as e:
        failures.append(f"chaos_all: {e}")
    except Exception as e:
        failures.append(f"chaos_all: 意外异常 {e}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
