"""回归测试: Free 档自选实时 symbols 超过 capability batch 上限时分批请求 (PR #46 问题 4)。"""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import polars as pl

from app.services.quote_service import QuoteService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _make_svc(engine_rules: dict) -> QuoteService:
    """创建最小可用的 QuoteService 实例 (跳过 __init__)。"""
    svc = QuoteService.__new__(QuoteService)
    svc._app_state = MagicMock()
    svc._repo = MagicMock()
    svc._lock = MagicMock()

    engine = MagicMock()
    engine.rules = engine_rules
    svc._app_state.monitor_engine = engine
    svc._app_state.repo = svc._repo

    svc._repo.get_index_symbol_set.return_value = {"000001.SH"}
    svc._repo.get_etf_symbol_set.return_value = set()
    return svc


def _run_fetch(svc, tf, watchlist: list[str], capset: CapabilitySet):
    """在完整 patch 环境下执行 _fetch_watchlist_quotes。"""
    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.services.preferences.get_realtime_watchlist_symbols",
            return_value=watchlist,
        ))
        stack.enter_context(patch(
            "app.tickflow.client.get_paid_realtime_client", return_value=tf,
        ))
        stack.enter_context(patch(
            "app.tickflow.policy.detect_capabilities", return_value=capset,
        ))
        stack.enter_context(patch("app.tickflow.rate_limits.sleep_between_batches"))
        # patch 分批之后的下游处理
        stack.enter_context(patch.object(
            QuoteService, "_build_daily", return_value=pl.DataFrame(),
        ))
        stack.enter_context(patch.object(
            QuoteService, "_build_quote_extra", return_value=pl.DataFrame(),
        ))
        stack.enter_context(patch.object(
            QuoteService, "_build_index_quotes", return_value=pl.DataFrame(),
        ))
        stack.enter_context(patch.object(QuoteService, "_broadcast_quote_updated"))
        stack.enter_context(patch.object(QuoteService, "_evaluate_monitors"))
        stack.enter_context(patch("app.services.quote_service._persist_last_fetch"))
        svc._fetch_watchlist_quotes()


def test_watchlist_batch_respects_capability_limit():
    """6 symbols / batch 5 → 分 2 批请求, 不整轮失败。"""
    engine_rules = {
        "r_idx": {"enabled": True, "asset_type": "index", "scope": "symbols",
                  "symbols": ["000001.SH"]},
    }
    svc = _make_svc(engine_rules)

    tf = MagicMock()
    tf.quotes.get.return_value = [
        {"symbol": "600000.SH", "last_price": 10.0, "prev_close": 9.9, "ext": {}},
    ]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    _run_fetch(svc, tf,
               ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"],
               capset)

    # 5 股票 + 1 指数 = 6 symbols, batch 5 → 2 批
    assert tf.quotes.get.call_count == 2
    first_batch = tf.quotes.get.call_args_list[0][1]["symbols"]
    second_batch = tf.quotes.get.call_args_list[1][1]["symbols"]
    assert len(first_batch) == 5
    assert len(second_batch) == 1
    assert "000001.SH" in second_batch


def test_watchlist_batch_partial_failure_keeps_other_batches():
    """某一批拉取失败不影响其他批次 (已有股票实时刷新不丢失)。"""
    engine_rules = {
        "r_idx": {"enabled": True, "asset_type": "index", "scope": "symbols",
                  "symbols": ["000001.SH"]},
    }
    svc = _make_svc(engine_rules)

    tf = MagicMock()
    # 第一批 (股票) 成功, 第二批 (指数) 失败
    tf.quotes.get.side_effect = [
        [{"symbol": "600000.SH", "last_price": 10.0, "prev_close": 9.9, "ext": {}}],
        ConnectionError("timeout"),
    ]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    _run_fetch(svc, tf,
               ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"],
               capset)

    # 两批都被尝试 (第二批失败不阻断)
    assert tf.quotes.get.call_count == 2


def test_watchlist_no_index_rules_no_extra_symbols():
    """无指数监控规则时, symbols 不追加指数标的。"""
    svc = _make_svc({})  # 无规则

    tf = MagicMock()
    tf.quotes.get.return_value = [
        {"symbol": "600000.SH", "last_price": 10.0, "prev_close": 9.9, "ext": {}},
    ]
    capset = CapabilitySet({Cap.QUOTE_BY_SYMBOL: CapabilityLimits(batch=5, rpm=60)})

    _run_fetch(svc, tf, ["600000.SH", "600001.SH"], capset)

    # 2 symbols / batch 5 → 1 批
    assert tf.quotes.get.call_count == 1
    assert tf.quotes.get.call_args_list[0][1]["symbols"] == ["600000.SH", "600001.SH"]
