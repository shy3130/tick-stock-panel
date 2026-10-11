"""自定义分钟数据源路由回归测试。

对应设计文档 §4 测试矩阵 (docs/superpowers/specs/2026-07-18-minute-provider-unification-design.md)。

覆盖的阻断问题 (历史回归):
1. 自定义分钟源异常直接 500 (无 try/except)
2. 插件化路由重复 + asset_type 未透传

mock 范式: monkeypatch 模块属性。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock

import httpx
import polars as pl

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


def _empty_capset():
    from app.tickflow.capabilities import CapabilitySet

    return CapabilitySet()


def _tickflow_minute_capset():
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    return CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})


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
        capset=_tickflow_minute_capset(),
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
    kline_sync.fetch_minute_single(
        "600519.SH", date(2026, 1, 15), asset_type="stock", capset=_empty_capset(),
    )
    kline_sync.fetch_minute_single(
        "510300.SH", date(2026, 1, 15), asset_type="etf", capset=_empty_capset(),
    )
    kline_sync.fetch_minute_single(
        "000001.SH", date(2026, 1, 15), asset_type="index", capset=_empty_capset(),
    )

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
        "600519.SH", date(2026, 1, 15), asset_type="stock", capset=_empty_capset(),
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
        # (如 GenericHTTPProvider/provider.py:127)
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
    契约: 本端点只接受 stock/ETF, 故两分支覆盖全部 incomplete。
    """
    from app.api import kline as kline_api

    # mock sync_minute_batch: stock 返回 df_s, etf 返回 df_e (不同 symbol 便于 concat 后 filter 验证)
    def fake_sync(symbols, *, start_time, end_time, batch_size, rpm, asset_type, raw_basis=False):
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


# ---------- 测试 9b: 取到即落盘 + 增量拉取 (尾部落后 / 中间洞 / fresh) ----------

def _bars(symbol: str, dts: list) -> pl.DataFrame:
    """构造 canonical 8 列分钟K帧 (dts 为 datetime 列表)。"""
    n = len(dts)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": dts,
        "open": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n, "close": [10.0] * n,
        "volume": [100.0] * n, "amount": [1000.0] * n,
    })


def _endpoint_mocks(monkeypatch, local_df: pl.DataFrame, sync_ret: pl.DataFrame | None):
    """get_minute_batch 的最小 mock: repo/capset + sync/落盘 spy。返回 (捕获, 落盘, request)。"""
    from app.api import kline as kline_api

    captured: list[dict] = []

    def fake_sync(symbols, *, start_time, end_time, batch_size, rpm, asset_type, raw_basis=False):
        captured.append({"symbols": list(symbols), "start": start_time, "asset": asset_type})
        return sync_ret if sync_ret is not None else pl.DataFrame()

    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", fake_sync)
    writes: list[pl.DataFrame] = []
    monkeypatch.setattr(kline_api.kline_sync, "_write_minute_partition",
                        lambda df, d: writes.append(df))

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_minute_batch.return_value = local_df
    mock_repo._write_lock = Lock()
    # 真实 Path 才会触发落盘分支 (kline.py 的 isinstance 守卫)
    mock_repo.store.data_dir = Path("data")

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    return captured, writes, mock_request


def test_minute_batch_tail_stale_pulls_incremental_and_persists(monkeypatch):
    """尾部落后 (本地连续但根数不足) → 从最后一根本身增量拉 (动态K重拉覆盖);
    拉取结果落盘, 响应为本地+增量合并去重。"""
    from app.api import kline as kline_api

    local = _bars("600519.SH", [
        datetime(2026, 1, 15, 9, 31), datetime(2026, 1, 15, 9, 32), datetime(2026, 1, 15, 9, 33),
    ])
    inc = _bars("600519.SH", [datetime(2026, 1, 15, 9, 34), datetime(2026, 1, 15, 9, 35)])
    captured, writes, req = _endpoint_mocks(monkeypatch, local, sync_ret=inc)

    result = kline_api.get_minute_batch(req, {"symbols": ["600519.SH"], "date": "2026-01-15"})

    assert len(captured) == 1
    assert captured[0]["start"] == datetime(2026, 1, 15, 9, 33)   # 从最后一根本身重拉 (动态K覆盖)
    assert captured[0]["asset"] == "stock"
    assert writes and writes[0].height == 2                        # 取到即落盘
    rows = result["data"]["600519.SH"]
    assert len(rows) == 5                                          # 3 本地 + 2 增量
    assert rows[-1]["datetime"] == datetime(2026, 1, 15, 9, 35)


def test_minute_batch_middle_hole_falls_back_to_full_day(monkeypatch):
    """中间缺K (间距 2min) → 增量窗口永远回看不到洞, 必须退回全天重拉。"""
    from app.api import kline as kline_api

    local = _bars("600519.SH", [datetime(2026, 1, 15, 9, 31), datetime(2026, 1, 15, 9, 33)])
    captured, _, req = _endpoint_mocks(monkeypatch, local, sync_ret=pl.DataFrame())

    kline_api.get_minute_batch(req, {"symbols": ["600519.SH"], "date": "2026-01-15"})

    assert len(captured) == 1
    assert captured[0]["start"] == datetime(2026, 1, 15, 9, 25)    # 全天窗口


def test_minute_batch_fresh_local_skips_pull(monkeypatch):
    """本地完整 (240 根, 含午休 91min 间距) → 不发任何拉取请求, 直读本地。"""
    from app.api import kline as kline_api

    dts = ([datetime(2026, 1, 15, 9, 31) + timedelta(minutes=i) for i in range(120)]
           + [datetime(2026, 1, 15, 13, 1) + timedelta(minutes=i) for i in range(120)])
    local = _bars("600519.SH", dts)
    captured, writes, req = _endpoint_mocks(monkeypatch, local, sync_ret=pl.DataFrame())

    result = kline_api.get_minute_batch(req, {"symbols": ["600519.SH"], "date": "2026-01-15"})

    assert captured == []                                          # 零请求
    assert writes == []
    assert len(result["data"]["600519.SH"]) == 240


def test_minute_batch_since_returns_only_new_bars(monkeypatch):
    """since 增量响应: 只回 >= since 的K (含 since 本身 — 形成中的动态K需覆盖),
    无新增的 symbol 不出现在 data; incremental 标志为 True。"""
    from app.api import kline as kline_api

    dts = ([datetime(2026, 1, 15, 9, 31) + timedelta(minutes=i) for i in range(120)]
           + [datetime(2026, 1, 15, 13, 1) + timedelta(minutes=i) for i in range(120)])
    local = _bars("600519.SH", dts)   # fresh: 不触发补拉, 直读本地后做 since 过滤
    _, _, req = _endpoint_mocks(monkeypatch, local, sync_ret=pl.DataFrame())

    result = kline_api.get_minute_batch(req, {
        "symbols": ["600519.SH"], "date": "2026-01-15",
        "since": "2026-01-15T15:00:00",
    })
    rows = result["data"]["600519.SH"]
    assert [r["datetime"] for r in rows] == [datetime(2026, 1, 15, 15, 0)]   # 含 since 当根
    assert result["incremental"] is True

    # since 早于全部K → 全量返回; 无 since → incremental False
    full = kline_api.get_minute_batch(req, {
        "symbols": ["600519.SH"], "date": "2026-01-15", "since": "2026-01-15T09:00:00",
    })
    assert len(full["data"]["600519.SH"]) == 240
    plain = kline_api.get_minute_batch(req, {"symbols": ["600519.SH"], "date": "2026-01-15"})
    assert plain["incremental"] is False
    assert len(plain["data"]["600519.SH"]) == 240

    # 防御: 带 Z 的 UTC aware 输入 (toISOString 客户端) → 归一为北京 naive 再比,
    # 不抛 TypeError 且语义正确 (15:00 北京 = 07:00 UTC)
    utc = kline_api.get_minute_batch(req, {
        "symbols": ["600519.SH"], "date": "2026-01-15",
        "since": "2026-01-15T07:00:00Z",
    })
    assert [r["datetime"] for r in utc["data"]["600519.SH"]] == [datetime(2026, 1, 15, 15, 0)]


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


def test_sync_and_persist_minute_holds_repository_write_lock(monkeypatch, tmp_path):
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_latest_minute_datetime", lambda repo: None)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *a, **kw: MagicMock(batch=100, rpm=30))
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)

    write_lock = Lock()

    def assert_locked(df, minute_dir):
        assert not write_lock.acquire(blocking=False)
        return df.height

    monkeypatch.setattr(kline_sync, "_write_minute_partition", assert_locked)

    mock_repo = MagicMock()
    mock_repo.store.data_dir = tmp_path
    mock_repo.db.execute = MagicMock()
    mock_repo._write_lock = write_lock

    written = kline_sync.sync_and_persist_minute(
        ["600519.SH"], mock_repo, MagicMock(),
    )

    assert written == expected_df.height


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


# ---------- 测试 20: sync_minute_single 拒绝指数 symbol (防污染 kline_minute) ----------

def test_sync_minute_single_rejects_index_symbol():
    """指数分钟K无本地存储, 落库会污染股票分钟表; 端点应显式 400 而非 500。"""
    import asyncio

    import pytest
    from fastapi import HTTPException

    from app.api import kline as kline_api

    mock_repo = MagicMock()
    mock_repo.resolve_asset_type.return_value = "index"
    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(kline_api.sync_minute_single(mock_request, {"symbol": "000001.SH"}))
    assert exc_info.value.status_code == 400
    assert "指数" in str(exc_info.value.detail)


# ---------- 测试: prefer_local (自选分时 × 全量分钟健康) ----------


def _mock_minute_rows(symbol: str, n: int) -> pl.DataFrame:
    """n 根分钟K (同日递增分钟), 用于构造'部分完整'的本地分区数据。"""
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": [datetime(2026, 1, 15, 9, 31, 0) + timedelta(minutes=i) for i in range(n)],
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.5] * n, "close": [100.5] * n,
        "volume": [1000.0] * n, "amount": [100500.0] * n,
    })


def _healthy_svc(monkeypatch, healthy: bool):
    """mock app.state.minute_refresh (is_healthy 可控)。"""
    svc = MagicMock()
    svc.is_healthy.return_value = healthy
    return svc


def test_get_minute_batch_prefer_local_healthy_skips_stock_refetch(monkeypatch):
    """全量分钟服务健康 + prefer_local: 股票 incomplete 不再批量补拉,
    本地现有数据(哪怕 <90%)直接返回; ETF 不在服务 universe, 维持补拉。"""
    from app.api import kline as kline_api

    sync_spy = MagicMock(return_value=_mock_minute_df(symbol="510300.SH"))
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = {"510300.SH"}
    # 股票本地 100 根 (expected=240 → <90% 判 incomplete), ETF 本地空
    mock_repo.get_minute_batch.side_effect = (
        lambda syms, d, asset_type="stock":
        _mock_minute_rows("600519.SH", 100) if asset_type == "stock" else pl.DataFrame()
    )

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.app.state.minute_refresh = _healthy_svc(monkeypatch, True)

    body = {"symbols": ["600519.SH", "510300.SH"], "date": "2026-01-15", "prefer_local": True}
    result = kline_api.get_minute_batch(mock_request, body)

    # 股票未被补拉: sync_minute_batch 只为 ETF 调了一次
    assert sync_spy.call_count == 1
    assert sync_spy.call_args.kwargs.get("asset_type") == "etf"
    assert sync_spy.call_args.args[0] == ["510300.SH"]
    # 股票返回的是本地 100 根 (部分数据, 不因 incomplete 而缺失)
    assert len(result["data"]["600519.SH"]) == 100
    assert result["full_minute_local"] is True


def test_get_minute_batch_prefer_local_unhealthy_falls_back(monkeypatch):
    """服务不健康 (挂了/停了) + prefer_local: 回落现状补拉兜底, 行为与不带标志一致。"""
    from app.api import kline as kline_api

    sync_spy = MagicMock(return_value=_mock_minute_df())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_minute_batch.return_value = _mock_minute_rows("600519.SH", 100)

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.app.state.minute_refresh = _healthy_svc(monkeypatch, False)

    body = {"symbols": ["600519.SH"], "date": "2026-01-15", "prefer_local": True}
    result = kline_api.get_minute_batch(mock_request, body)

    assert sync_spy.call_count == 1  # 股票照常补拉
    assert sync_spy.call_args.kwargs.get("asset_type") == "stock"
    assert result["full_minute_local"] is False


def test_get_minute_batch_no_flag_unaffected_even_if_healthy(monkeypatch):
    """不带 prefer_local (策略页等) 即使服务健康也维持现状补拉 — 本轮只辐射自选场景。"""
    from app.api import kline as kline_api

    sync_spy = MagicMock(return_value=_mock_minute_df())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_minute_batch.return_value = _mock_minute_rows("600519.SH", 100)

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.app.state.minute_refresh = _healthy_svc(monkeypatch, True)

    body = {"symbols": ["600519.SH"], "date": "2026-01-15"}
    result = kline_api.get_minute_batch(mock_request, body)

    assert sync_spy.call_count == 1
    assert result["full_minute_local"] is False


# ---------- 测试: 前部洞 (盘中重启/停机跨开盘的残留) ----------


def _mock_tail_rows(symbol: str, n: int, first: datetime) -> pl.DataFrame:
    """n 根从 first 开始的连续分钟K — 模拟重启后实时写入的尾部序列。"""
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": [first + timedelta(minutes=i) for i in range(n)],
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.5] * n, "close": [100.5] * n,
        "volume": [1000.0] * n, "amount": [100500.0] * n,
    })


def test_get_minute_batch_leading_hole_triggers_full_day_refetch(monkeypatch):
    """前部洞: 首根显著晚于开盘的连续尾部K → 全天重拉, 而非"最后一根+1min"增量。

    场景: 盘中重启/停机跨开盘后, 本地只剩 11:20 起的连续尾巴 (11 根)。
    旧逻辑判"仅尾部落后"走增量, 上午的洞永远不会被回看; 新逻辑判洞 → 全天拉。
    """
    from app.api import kline as kline_api

    sync_spy = MagicMock(return_value=_mock_minute_rows("600519.SH", 121))
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_minute_batch.return_value = _mock_tail_rows(
        "600519.SH", 11, datetime(2026, 1, 15, 11, 20)
    )

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.app.state.minute_refresh = _healthy_svc(monkeypatch, False)

    body = {"symbols": ["600519.SH"], "date": "2026-01-15", "prefer_local": True}
    result = kline_api.get_minute_batch(mock_request, body)

    # 全天拉: start_time = 当日开盘窗口 (09:25), 不是"最后一根 + 1min" (11:31)
    assert sync_spy.call_count == 1
    assert sync_spy.call_args.kwargs.get("start_time") == datetime(2026, 1, 15, 9, 25)
    # 合并结果包含上午: 首根回到开盘附近, 根数覆盖全天
    rows = result["data"]["600519.SH"]
    assert rows[0]["datetime"] == datetime(2026, 1, 15, 9, 31)
    assert len(rows) >= 121


def test_get_minute_batch_healthy_does_not_suppress_leading_hole_refetch(monkeypatch):
    """服务健康 + prefer_local: 前部洞的股票仍全天补拉 (服务增量锚定本地最新,
    补不了洞); 纯尾部落后的股票维持不补拉 (服务下一轮会补尾巴)。"""
    from app.api import kline as kline_api

    sync_spy = MagicMock(return_value=_mock_minute_rows("600519.SH", 121))
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    hole_local = _mock_tail_rows("600519.SH", 11, datetime(2026, 1, 15, 11, 20))
    stale_local = _mock_minute_rows("000001.SZ", 100)  # 09:31 开头的连续序列, 仅根数不足

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_minute_batch.return_value = pl.concat([hole_local, stale_local])

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.app.state.minute_refresh = _healthy_svc(monkeypatch, True)

    body = {"symbols": ["600519.SH", "000001.SZ"], "date": "2026-01-15", "prefer_local": True}
    result = kline_api.get_minute_batch(mock_request, body)

    # 洞票被全天补拉; 尾部票未被拉 (只调了一次, 只为 600519)
    assert sync_spy.call_count == 1
    assert sync_spy.call_args.args[0] == ["600519.SH"]
    assert sync_spy.call_args.kwargs.get("start_time") == datetime(2026, 1, 15, 9, 25)
    # 尾部票返回本地 100 根 (健康压制原样生效)
    assert len(result["data"]["000001.SZ"]) == 100
    # 洞票拿到全天
    assert result["data"]["600519.SH"][0]["datetime"] == datetime(2026, 1, 15, 9, 31)
    assert result["full_minute_local"] is True


def test_get_minute_batch_normal_open_not_treated_as_leading_hole(monkeypatch):
    """无集合竞价K的源首根 09:31/09:35 → 不算前部洞, 维持增量语义 (不全天重拉)。"""
    from app.api import kline as kline_api

    sync_spy = MagicMock(return_value=_mock_minute_df())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    # 首根 09:31, 仅 5 根 (历史日 expected=240, 根数不足但非洞)
    mock_repo.get_minute_batch.return_value = _mock_minute_rows("600519.SH", 5)

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.app.state.minute_refresh = _healthy_svc(monkeypatch, False)

    body = {"symbols": ["600519.SH"], "date": "2026-01-15", "prefer_local": True}
    kline_api.get_minute_batch(mock_request, body)

    # 增量拉: start_time = 最后一根本身 (09:35), 不是 09:25 全天窗口
    assert sync_spy.call_count == 1
    assert sync_spy.call_args.kwargs.get("start_time") == datetime(2026, 1, 15, 9, 35)


def test_minute_refresh_is_healthy_requires_recent_round(monkeypatch):
    """is_healthy 三条件: 偏好开 + 线程活 + 最近一轮距现在 ≤ max(2×间隔, 30s)。"""
    import time as time_mod
    from app.services import minute_refresh as mr

    svc = mr.MinuteRefreshService(MagicMock())  # is_healthy 不触达 repo

    monkeypatch.setattr(mr.preferences, "get_minute_refresh_enabled", lambda: True)
    monkeypatch.setattr(mr.preferences, "get_minute_refresh_interval", lambda: 6)
    # 线程未启动 → False
    assert svc.is_healthy() is False

    svc._thread = MagicMock()
    svc._thread.is_alive.return_value = True
    # 无轮次记录 → False
    assert svc.is_healthy() is False

    # 最近一轮在 10s 前 (≤ max(12, 30)) → True
    svc._state.last_round_at = time_mod.time() - 10
    assert svc.is_healthy() is True

    # 最近一轮在 120s 前 (> 30) → False (连续失败不更新 last_round_at, 自动超时)
    svc._state.last_round_at = time_mod.time() - 120
    assert svc.is_healthy() is False

    # 偏好关闭 → False
    monkeypatch.setattr(mr.preferences, "get_minute_refresh_enabled", lambda: False)
    svc._state.last_round_at = time_mod.time() - 10
    assert svc.is_healthy() is False


# ---------- 测试: 分时批量传输压缩 (网络设置开关) ----------


def test_minute_batch_compress_preference_default_and_toggle(monkeypatch):
    """偏好默认开启; 关闭后 getter 立即反映 (逐请求读取, 无缓存)。"""
    from app.services import preferences as prefs

    monkeypatch.setattr(prefs, "load", lambda: {})
    assert prefs.get_minute_batch_compress() is True
    monkeypatch.setattr(prefs, "load", lambda: {"minute_batch_compress": False})
    assert prefs.get_minute_batch_compress() is False


def _compress_mock_env(monkeypatch, *, compress_on, accept="gzip, deflate"):
    """构造 get_minute_batch 压缩路径的最小 mock 环境, 返回 (mock_request, sync_spy)。"""
    from app.api import kline as kline_api
    from app.services import preferences as prefs

    monkeypatch.setattr(prefs, "get_minute_batch_compress", lambda: compress_on)

    sync_spy = MagicMock(return_value=_mock_minute_df())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_minute_batch.return_value = _mock_minute_rows("600519.SH", 100)

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.headers = {"accept-encoding": accept} if accept else {}
    return mock_request, sync_spy


def test_get_minute_batch_gzip_response_when_enabled(monkeypatch):
    """开关开 + 客户端接受 gzip + 响应超阈值 → 返回 gzip Response, 解压后 JSON 完整。"""
    import gzip as gzip_mod
    from app.api import kline as kline_api
    from fastapi import Response

    mock_request, _ = _compress_mock_env(monkeypatch, compress_on=True)
    result = kline_api.get_minute_batch(
        mock_request, {"symbols": ["600519.SH"], "date": "2026-01-15"}
    )
    assert isinstance(result, Response)
    assert result.headers["content-encoding"] == "gzip"
    import json as json_mod
    payload = json_mod.loads(gzip_mod.decompress(result.body))
    assert payload["full_minute_local"] is False
    assert len(payload["data"]["600519.SH"]) > 0


def test_get_minute_batch_plain_when_disabled(monkeypatch):
    """开关关 → 恒返回普通 dict, 不做压缩。"""
    from app.api import kline as kline_api

    mock_request, _ = _compress_mock_env(monkeypatch, compress_on=False)
    result = kline_api.get_minute_batch(
        mock_request, {"symbols": ["600519.SH"], "date": "2026-01-15"}
    )
    assert isinstance(result, dict) and "600519.SH" in result["data"]


def test_get_minute_batch_plain_without_accept_encoding(monkeypatch):
    """开关开但客户端未声明 gzip (如裸 curl) → 尊重协商, 原样返回。"""
    from app.api import kline as kline_api

    mock_request, _ = _compress_mock_env(monkeypatch, compress_on=True, accept=None)
    result = kline_api.get_minute_batch(
        mock_request, {"symbols": ["600519.SH"], "date": "2026-01-15"}
    )
    assert isinstance(result, dict)


# ---------- 测试: 日K批量传输压缩 (与分时独立开关) ----------


def _daily_mock_env(monkeypatch, *, compress_on, accept="gzip, deflate"):
    """构造 get_daily_batch 压缩路径的最小 mock。"""
    from app.api import kline as kline_api
    from app.services import preferences as prefs

    monkeypatch.setattr(prefs, "get_daily_batch_compress", lambda: compress_on)

    # 20 根日K (date 列), 足以过 1KB 阈值
    n = 20
    daily_df = pl.DataFrame({
        "symbol": ["600519.SH"] * n,
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": [100.0] * n, "high": [101.0] * n,
        "low": [99.0] * n, "close": [100.5] * n, "volume": [1000.0] * n,
    })
    mock_repo = MagicMock()
    mock_repo.resolve_asset_type.return_value = "stock"
    mock_repo.get_daily_batch.return_value = daily_df

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.headers = {"accept-encoding": accept} if accept else {}
    return mock_request


def test_daily_batch_gzip_when_enabled(monkeypatch):
    """日K压缩开 + 接受 gzip → 压缩 Response, 解压 JSON 完整。"""
    import gzip as gzip_mod
    import json as json_mod
    from app.api import kline as kline_api
    from fastapi import Response

    req = _daily_mock_env(monkeypatch, compress_on=True)
    result = kline_api.get_daily_batch(req, {"symbols": ["600519.SH"], "days": 20})
    assert isinstance(result, Response)
    assert result.headers["content-encoding"] == "gzip"
    payload = json_mod.loads(gzip_mod.decompress(result.body))
    assert len(payload["data"]["600519.SH"]) == 20


def test_daily_batch_plain_when_disabled(monkeypatch):
    from app.api import kline as kline_api

    req = _daily_mock_env(monkeypatch, compress_on=False)
    result = kline_api.get_daily_batch(req, {"symbols": ["600519.SH"], "days": 20})
    assert isinstance(result, dict) and "600519.SH" in result["data"]


def test_daily_batch_independent_from_minute_switch(monkeypatch):
    """日K与分时独立: 分时关、日K开 → 日K仍压缩 (helper 按 pref_key 走各自 getter)。"""
    import gzip as gzip_mod
    from app.api import kline as kline_api
    from app.services import preferences as prefs
    from fastapi import Response

    monkeypatch.setattr(prefs, "get_minute_batch_compress", lambda: False)
    req = _daily_mock_env(monkeypatch, compress_on=True)
    result = kline_api.get_daily_batch(req, {"symbols": ["600519.SH"], "days": 20})
    assert isinstance(result, Response) and gzip_mod.decompress(result.body)


def test_preferences_parallel_saves_do_not_lose_each_other(tmp_path, monkeypatch):
    """回归: 并行 save 不同键不得互相覆盖 (压缩总开关并行 PUT 两键的竞态)。

    save 是 read-modify-write, 无锁时两线程同时基于旧快照写盘,
    后写者会把先写者的更新覆盖掉。
    """
    import threading
    from app.services import preferences as prefs

    monkeypatch.setattr(prefs, "_path", lambda: tmp_path / "preferences.json")
    prefs._invalidate_cache()
    prefs.save({"minute_batch_compress": True})

    barrier = threading.Barrier(2)

    def write_key(key: str) -> None:
        barrier.wait()  # 尽量同时进入 save
        prefs.save({key: False})

    t1 = threading.Thread(target=write_key, args=("minute_batch_compress",))
    t2 = threading.Thread(target=write_key, args=("daily_batch_compress",))
    t1.start(); t2.start(); t1.join(); t2.join()

    final = prefs.load()
    assert final["minute_batch_compress"] is False
    assert final["daily_batch_compress"] is False
