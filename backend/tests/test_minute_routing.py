"""自定义分钟数据源路由回归测试。

对应设计文档 §4 测试矩阵 (docs/superpowers/specs/2026-07-18-minute-provider-unification-design.md)。

覆盖三个阻断问题:
1. stock-sdk 默认 freq 漂移 (5m → 1m)
2. 自定义源异常直接 500 (无 try/except)
3. 插件化路由重复 + asset_type 未透传

mock 范式沿用 test_stocksdk_provider.py (monkeypatch 模块属性)。
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import httpx
import polars as pl

from app.plugins.stocksdk import provider as sp
from app.plugins.stocksdk.provider import StockSDKProvider
from app.services import kline_sync


# ---------- 辅助 ----------

def _mock_minute_df(symbol: str = "600519.SH") -> pl.DataFrame:
    """构造非空分钟 K df, 用于 mock provider.get_minute 返回值。"""
    return pl.DataFrame({
        "symbol": [symbol],
        "datetime": [datetime(2026, 1, 15, 9, 35, 0)],
        "open": [100.0],
        "high": [101.0],
        "low": [99.5],
        "close": [100.5],
        "volume": [1000.0],
        "amount": [100500.0],
    })


def _setup_custom_provider(monkeypatch, provider: object, has_dataset: bool = True) -> None:
    """统一 mock 自定义分钟源路由前置: preferences + provider_has_dataset + get_provider。

    - preferences.get_minute_data_provider → "mock_src"
    - custom.provider_has_dataset → has_dataset
    - custom.get_provider → provider
    """
    monkeypatch.setattr(
        kline_sync.preferences,
        "get_minute_data_provider",
        lambda: "mock_src",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: has_dataset,
    )
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider",
        lambda name: provider,
    )


# ---------- 测试 1: 自定义源成功返回 1 分钟 K ----------

def test_custom_minute_provider_returns_1m_k(monkeypatch):
    """§4 测试 1: 自定义源成功返回 1m K, 且 provider 收到 freq="1m"。"""
    spy = MagicMock(return_value=_mock_minute_df())
    mock_provider = MagicMock()
    mock_provider.get_minute = spy
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"],
        datetime(2026, 1, 15, 9, 25, 0),
        datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
    )

    assert fallback is False
    assert df is not None
    assert not df.is_empty()
    # spy 收到 freq="1m" 和 asset_type="stock"
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("freq") == "1m"
    assert kwargs.get("asset_type") == "stock"


# ---------- 测试 2: stock-sdk 收到 freq=1m → bridge job period="1" ----------

def test_stocksdk_get_minute_receives_freq_1m(monkeypatch):
    """§4 测试 2: StockSDKProvider.get_minute(freq="1m") → bridge job period == "1"。

    bridge.mjs opMinute 用 String(period), 1m → "1"。
    """
    captured: dict = {}

    def fake_run_job(job, timeout=None):
        captured["job"] = job
        # 返回空结果, 测试只验证 job.period
        return {"ok": True, "op": job["op"], "rows": {}}

    monkeypatch.setattr(sp.bridge, "run_job", fake_run_job)

    StockSDKProvider().get_minute(
        ["600519.SH"], None, None, freq="1m",
    )

    assert captured["job"]["op"] == "minute"
    assert captured["job"]["period"] == "1"


# ---------- 测试 3: 自定义源异常 + TickFlow 也失败 → 返回空 (非 500) ----------

def test_custom_provider_exception_no_500(monkeypatch):
    """§4 测试 3: 自定义源抛异常 + TickFlow 也失败,
    fetch_minute_single / sync_minute_batch 返回空 df。
    """
    # 自定义源抛异常
    mock_provider = MagicMock()
    mock_provider.get_minute.side_effect = httpx.TimeoutException("timeout")
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # mock get_client 返回 mock client, 其 klines.batch raise (TickFlow 也失败)
    mock_tf = MagicMock()
    mock_tf.klines.batch.side_effect = Exception("tickflow fail")
    monkeypatch.setattr(kline_sync, "get_client", lambda: mock_tf)

    # fetch_minute_single: 自定义源异常 → fall through → TickFlow 异常 → 返回空
    df_single = kline_sync.fetch_minute_single(
        "600519.SH", date(2026, 1, 15), asset_type="stock",
    )
    assert isinstance(df_single, pl.DataFrame)
    assert df_single.is_empty()

    # sync_minute_batch: 同一路径, 返回空
    df_batch = kline_sync.sync_minute_batch(
        ["600519.SH"],
        start_time=datetime(2026, 1, 15, 9, 25, 0),
        end_time=datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
    )
    assert isinstance(df_batch, pl.DataFrame)
    assert df_batch.is_empty()


# ---------- 测试 4: 未配 minute dataset → 回退 TickFlow ----------

def test_provider_without_minute_dataset_fallback(monkeypatch):
    """§4 测试 4: provider_has_dataset 返回 False → (None, True) 回退 TickFlow。"""
    mock_provider = MagicMock()
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=False)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], None, None, asset_type="stock",
    )

    assert fallback is True
    assert df is None
    # provider.get_minute 不应被调用 (回退决策在前)
    mock_provider.get_minute.assert_not_called()


# ---------- 测试 5: asset_type 透传到 provider ----------

def test_asset_type_threaded_to_provider(monkeypatch):
    """§4 测试 5: stock/etf/index asset_type 透传到 provider.get_minute。"""
    spy = MagicMock(return_value=_mock_minute_df())
    mock_provider = MagicMock()
    mock_provider.get_minute = spy
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # 三次调用不同 asset_type
    kline_sync.fetch_minute_single("600519.SH", date(2026, 1, 15), asset_type="stock")
    kline_sync.fetch_minute_single("510300.SH", date(2026, 1, 15), asset_type="etf")
    kline_sync.fetch_minute_single("000001.SH", date(2026, 1, 15), asset_type="index")

    # spy 被调 3 次, 每次收到对应 asset_type
    assert spy.call_count == 3
    received_assets = [call.kwargs.get("asset_type") for call in spy.call_args_list]
    assert received_assets == ["stock", "etf", "index"]


# ---------- 测试 6: 自定义源成功时不调 TickFlow ----------

def test_custom_success_skips_tickflow(monkeypatch):
    """§4 测试 6: fetch_minute_single 自定义源成功 → 不调 get_client。"""
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # get_client 设为 spy, 若被调说明路由失败
    get_client_spy = MagicMock(name="get_client_spy")
    monkeypatch.setattr(kline_sync, "get_client", get_client_spy)

    df = kline_sync.fetch_minute_single(
        "600519.SH", date(2026, 1, 15), asset_type="stock",
    )

    # 返回的是 mock provider 的 df
    assert df is expected_df
    # TickFlow 路径未进入
    get_client_spy.assert_not_called()


# ---------- 测试 7: sync_minute_batch 自定义源成功直接返回 ----------

def test_sync_minute_batch_custom_success_returns_directly(monkeypatch):
    """§4 测试 7: sync_minute_batch 自定义源成功 + 未传 on_segment → 原样返回 df (实时补拉契约)。

    传了 on_segment 时走流式落盘分支 (见测试 10), 此处验证未传时的实时补拉契约。
    """
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    get_client_spy = MagicMock(name="get_client_spy")
    monkeypatch.setattr(kline_sync, "get_client", get_client_spy)

    df = kline_sync.sync_minute_batch(
        ["600519.SH"],
        start_time=datetime(2026, 1, 15, 9, 25, 0),
        end_time=datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
    )

    # 返回 mock provider 的 df, 不走 segment
    assert df is expected_df
    get_client_spy.assert_not_called()


# ---------- 测试 8: on_chunk_done 包装 (2参 → 3参补 seg_label='custom') ----------

def test_on_chunk_done_wrapped_to_3_args(monkeypatch):
    """on_chunk_done 包装: provider 内部以 2 参 (cur, total) 调用 →
    上层 3 参 (cur, total, seg_label) spy 收到 seg_label='custom'。

    设计文档 §2: 保证自定义源路径进度展示不降级 (与 TickFlow 路径 3 参回调对齐)。
    """
    upper_cb = MagicMock(name="upper_3arg_cb")

    def provider_get_minute_side_effect(symbols, *, start_time, end_time,
                                        asset_type, freq, on_chunk_done):
        # 模拟 provider 实现内部以 2 参调用 on_chunk_done
        # (如 GenericHTTPProvider/provider.py:127 / StockSDKProvider/provider.py:166)
        if on_chunk_done is not None:
            on_chunk_done(1, 3)
        return _mock_minute_df()

    mock_provider = MagicMock()
    mock_provider.get_minute.side_effect = provider_get_minute_side_effect
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"],
        datetime(2026, 1, 15, 9, 25, 0),
        datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
        on_chunk_done=upper_cb,
    )

    assert fallback is False
    assert df is not None
    # 上层 3 参 spy 被调用一次, 收到 (1, 3, "custom")
    upper_cb.assert_called_once_with(1, 3, "custom")


# ---------- 测试 9: get_minute_batch 按 asset_type 拆分调用 sync_minute_batch ----------

def test_get_minute_batch_splits_stock_and_etf(monkeypatch):
    """get_minute_batch 把 incomplete 拆成 stock/ETF 两组, 分别以
    asset_type='stock'/'etf' 调用 sync_minute_batch, 结果 concat 返回。

    覆盖 kline.py get_minute_batch 的双调用拼接逻辑 (本次提交改动量最大的部分)。
    契约: 本端点只接受 stock/ETF (指数走 /api/index/minute), 故两分支覆盖全部 incomplete。
    """
    from app.api import kline as kline_api

    # mock sync_minute_batch: stock 返回 df_s, etf 返回 df_e (不同 symbol 便于 concat 后 filter 验证)
    def fake_sync(symbols, *, start_time, end_time, batch_size, rpm, asset_type):
        if asset_type == "stock":
            return _mock_minute_df(symbol="600519.SH")
        if asset_type == "etf":
            return _mock_minute_df(symbol="510300.SH")
        return pl.DataFrame()
    sync_spy = MagicMock(side_effect=fake_sync)
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    # mock repo: ETF 集合含 510300.SH; 本地分钟K返回空 (强制走 incomplete 补拉)
    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = {"510300.SH"}
    mock_repo.get_minute_batch.return_value = pl.DataFrame()

    # mock capset: 有权限, limits 返回 None (lim.batch 访问被 `if lim else` 守护)
    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset

    body = {"symbols": ["600519.SH", "510300.SH"], "date": "2026-01-15"}
    result = kline_api.get_minute_batch(mock_request, body)

    # sync_minute_batch 被调 2 次, asset_type 分别为 stock 和 etf
    assert sync_spy.call_count == 2
    call_assets = sorted(call.kwargs.get("asset_type") for call in sync_spy.call_args_list)
    assert call_assets == ["etf", "stock"]

    # 两个 symbol 都在结果里 (concat 后按 symbol filter 命中)
    assert "600519.SH" in result["data"]
    assert "510300.SH" in result["data"]


# ---------- 测试 10: sync_minute_batch 自定义源成功时调 on_segment (Issue 1) ----------

def test_sync_minute_batch_custom_calls_on_segment(monkeypatch):
    """Issue 1: sync_minute_batch 自定义源成功 + 传了 on_segment →
    调 on_segment(df), 返回空 df (数据已落盘)。
    """
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    get_client_spy = MagicMock(name="get_client_spy")
    monkeypatch.setattr(kline_sync, "get_client", get_client_spy)

    on_segment_spy = MagicMock(name="on_segment_spy")
    df = kline_sync.sync_minute_batch(
        ["600519.SH"],
        start_time=datetime(2026, 1, 15, 9, 25, 0),
        end_time=datetime(2026, 1, 15, 15, 5, 0),
        on_segment=on_segment_spy,
        asset_type="stock",
    )

    on_segment_spy.assert_called_once_with(expected_df)
    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()
    get_client_spy.assert_not_called()


# ---------- 测试 11: 自定义源返回空 df 时不调 on_segment (Issue 1 边界) ----------

def test_sync_minute_batch_custom_empty_df_skips_on_segment(monkeypatch):
    """Issue 1 边界: 自定义源返回空 df → 不调 on_segment (与 TickFlow `if seg_out:` 对称)。
    """
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = pl.DataFrame()
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    on_segment_spy = MagicMock(name="on_segment_spy")
    df = kline_sync.sync_minute_batch(
        ["600519.SH"],
        start_time=datetime(2026, 1, 15, 9, 25, 0),
        end_time=datetime(2026, 1, 15, 15, 5, 0),
        on_segment=on_segment_spy,
        asset_type="stock",
    )

    on_segment_spy.assert_not_called()
    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()


# ---------- 测试 12: sync_and_persist_minute + custom provider 端到端落盘 (Issue 1) ----------

def test_sync_and_persist_minute_custom_persists(monkeypatch, tmp_path):
    """Issue 1 端到端: sync_and_persist_minute + 自定义源 →
    _write_minute_partition 被调, written > 0。
    """
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # mock sync_and_persist_minute 内部依赖 (通过 monkeypatch kline_sync 模块属性)
    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_latest_minute_datetime", lambda repo: None)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *a, **kw: MagicMock(batch=100, rpm=30))
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)

    # _write_minute_partition spy: 记录调用, 返回行数
    write_spy = MagicMock(return_value=expected_df.height)
    monkeypatch.setattr(kline_sync, "_write_minute_partition", write_spy)

    # get_client spy: 自定义源成功时不应走 TickFlow
    get_client_spy = MagicMock(name="get_client_spy")
    monkeypatch.setattr(kline_sync, "get_client", get_client_spy)

    # mock repo
    mock_repo = MagicMock()
    mock_repo.store.data_dir = tmp_path
    mock_repo.db.execute = MagicMock()

    # mock capset (minute_is_custom=True 绕过 has() 检查, resolve_limit 已 mock)
    mock_capset = MagicMock()

    written = kline_sync.sync_and_persist_minute(
        ["600519.SH"], mock_repo, mock_capset,
    )

    assert write_spy.called
    assert written == expected_df.height
    assert written > 0
    get_client_spy.assert_not_called()


# ---------- 测试 13: get_provider 异常时 fall through TickFlow (Issue 2) ----------

def test_get_provider_exception_falls_back_to_tickflow(monkeypatch):
    """Issue 2: get_provider raise ValueError →
    _try_custom_minute 返回 (None, True), 无异常穿透。
    """
    monkeypatch.setattr(
        kline_sync.preferences,
        "get_minute_data_provider",
        lambda: "mock_src",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: True,  # provider 存在, 但 get_provider 会抛
    )

    def _raising_get_provider(name):
        raise ValueError("not found")
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider",
        _raising_get_provider,
    )

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], None, None, asset_type="stock",
    )

    assert fallback is True
    assert df is None


# ---------- 测试 14: provider_has_dataset 异常时 fall through (Issue 2) ----------

def test_provider_has_dataset_exception_falls_back(monkeypatch):
    """Issue 2: provider_has_dataset raise →
    _try_custom_minute 返回 (None, True), 无异常穿透。
    """
    monkeypatch.setattr(
        kline_sync.preferences,
        "get_minute_data_provider",
        lambda: "mock_src",
    )

    def _raising_has_dataset(name, ds):
        raise RuntimeError("registry corrupted")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        _raising_has_dataset,
    )

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], None, None, asset_type="stock",
    )

    assert fallback is True
    assert df is None


# ---------- 测试 15-17: GenericHTTPProvider opt-in 参数传递 (Issue 3) ----------

from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.provider import GenericHTTPProvider


def _make_minute_config(**extra) -> CustomSourceConfig:
    """构造带 minute dataset 的最小 CustomSourceConfig, extra 传给 DatasetConfig。"""
    field_map = {f: f for f in (
        "symbol", "datetime", "open", "high", "low", "close", "volume", "amount"
    )}
    return CustomSourceConfig(
        name="test_src",
        display_name="Test Source",
        datasets={"minute": DatasetConfig(
            url="http://example.com/minute", field_map=field_map, **extra,
        )},
    )


def _capture_request_rows(provider):
    """替换 _request_rows 为捕获 spy, 返回 captured dict。"""
    captured: dict = {}

    def fake_request_rows(cfg, *, symbols=None, start_time=None, end_time=None,
                          override_params=None, override_body=None):
        captured["override_params"] = override_params
        captured["override_body"] = override_body
        return []  # 空行 → 空 df

    provider._request_rows = fake_request_rows
    return captured


def test_generic_http_get_minute_passes_asset_type_when_configured():
    """Issue 3: 配了 asset_type_param="asset" → override 含 {"asset": "etf"}。"""
    config = _make_minute_config(asset_type_param="asset")
    provider = GenericHTTPProvider(config)
    captured = _capture_request_rows(provider)

    provider.get_minute(["600519.SH"], None, None, asset_type="etf", freq="1m")

    assert captured["override_params"] == {"asset": "etf"}
    assert captured["override_body"] == {"asset": "etf"}


def test_generic_http_get_minute_passes_freq_when_configured():
    """Issue 3: 配了 freq_param="period" → override 含 {"period": "1m"}。"""
    config = _make_minute_config(freq_param="period")
    provider = GenericHTTPProvider(config)
    captured = _capture_request_rows(provider)

    provider.get_minute(["600519.SH"], None, None, asset_type="stock", freq="1m")

    assert captured["override_params"] == {"period": "1m"}
    assert captured["override_body"] == {"period": "1m"}


def test_generic_http_get_minute_omits_params_when_not_configured():
    """Issue 3 向后兼容: 未配 asset_type_param/freq_param → override 为 None, 不传上游。"""
    config = _make_minute_config()  # 无 asset_type_param / freq_param
    provider = GenericHTTPProvider(config)
    captured = _capture_request_rows(provider)

    provider.get_minute(["600519.SH"], None, None, asset_type="etf", freq="1m")

    # override 为 None (空 dict → `override or None`), 不传上游
    assert captured["override_params"] is None
    assert captured["override_body"] is None


# ---------- 测试 18: sync_and_persist_minute resolver 异常时优雅返回 0 (观察项加固) ----------

def test_sync_and_persist_minute_resolver_exception_returns_zero(monkeypatch, tmp_path):
    """观察项加固: sync_and_persist_minute 开头 _resolve_minute_provider 异常 →
    不向接口抛 500, 优雅降级 (minute_is_custom=False → 走 capset 检查 → 无权限 return 0)。
    """
    monkeypatch.setattr(
        kline_sync.preferences,
        "get_minute_data_provider",
        lambda: "mock_src",
    )
    # provider_has_dataset 抛异常 (模拟 registry 损坏)
    def _raising_has_dataset(name, ds):
        raise RuntimeError("registry corrupted")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        _raising_has_dataset,
    )

    # 无 KLINE_MINUTE_BATCH 权限 → resolver 异常视为非 custom → capset 检查失败 → return 0
    mock_capset = MagicMock()
    mock_capset.has.return_value = False

    mock_repo = MagicMock()
    mock_repo.store.data_dir = tmp_path

    # 不应抛异常, 优雅降级到 0
    written = kline_sync.sync_and_persist_minute(
        ["600519.SH"], mock_repo, mock_capset,
    )

    assert written == 0


# ---------- 测试 19: _resolve_minute_provider helper 单元测试 ----------

def test_resolve_minute_provider_tickflow_returns_silent_fallback():
    """观察项加固: provider_name == "tickflow" → (None, True, None) 静默降级, 无 err。"""
    provider, fallback, err = kline_sync._resolve_minute_provider("tickflow")
    assert provider is None
    assert fallback is True
    assert err is None


def test_resolve_minute_provider_no_dataset_returns_silent_fallback(monkeypatch):
    """观察项加固: 配了 custom 但未配 minute dataset → (None, True, None) 静默降级。"""
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: False,  # 已注册但未配 minute
    )
    provider, fallback, err = kline_sync._resolve_minute_provider("mock_src")
    assert provider is None
    assert fallback is True
    assert err is None  # 未配 ≠ 异常, 不应触发 warning


def test_resolve_minute_provider_has_dataset_exception_returns_err(monkeypatch):
    """观察项加固: provider_has_dataset 抛异常 → (None, True, str(e)), 上层据此 warning。"""
    def _raising(name, ds):
        raise RuntimeError("registry corrupted")
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", _raising)
    provider, fallback, err = kline_sync._resolve_minute_provider("mock_src")
    assert provider is None
    assert fallback is True
    assert err is not None
    assert "registry corrupted" in err


def test_resolve_minute_provider_get_provider_exception_returns_err(monkeypatch):
    """观察项加固: provider_has_dataset 返回 True 但 get_provider 抛 → (None, True, str(e))。"""
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: True,
    )
    def _raising_get(name):
        raise ValueError("not found")
    monkeypatch.setattr("app.data_providers.custom.get_provider", _raising_get)
    provider, fallback, err = kline_sync._resolve_minute_provider("mock_src")
    assert provider is None
    assert fallback is True
    assert err is not None
    assert "not found" in err


def test_resolve_minute_provider_success_returns_provider(monkeypatch):
    """观察项加固: 正常路径 → (provider, False, None)。"""
    mock_provider = object()  # 任意 truthy 对象即可
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: True,
    )
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider",
        lambda name: mock_provider,
    )
    provider, fallback, err = kline_sync._resolve_minute_provider("mock_src")
    assert provider is mock_provider
    assert fallback is False
    assert err is None


def test_minute_allowed_resolver_exception_returns_false(monkeypatch):
    """权限入口复用安全 resolver, 插件注册异常不再穿透为 500。"""
    from app.api import kline as kline_api
    from app.tickflow.capabilities import CapabilitySet

    monkeypatch.setattr(
        "app.services.preferences.get_minute_data_provider",
        lambda: "broken",
    )

    def _raising(name, dataset):
        raise RuntimeError("registry corrupted")

    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", _raising)

    assert kline_api._minute_allowed(CapabilitySet()) is False


def test_intraday_monitor_support_resolver_exception_falls_back(monkeypatch):
    """监控入口解析自定义源失败后继续按 TickFlow 能力判断。"""
    from app.tickflow.capabilities import Cap, CapabilitySet

    monkeypatch.setattr(
        kline_sync.preferences,
        "get_minute_data_provider",
        lambda: "broken",
    )

    def _raising(name, dataset):
        raise RuntimeError("registry corrupted")

    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", _raising)
    capset = CapabilitySet()
    capset.grant(Cap.KLINE_MINUTE_BATCH)

    support = kline_sync.intraday_monitor_support(capset)

    assert support["available"] is True
    assert support["source"] == "minute_batch"
