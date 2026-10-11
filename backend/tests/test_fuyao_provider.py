"""FuyaoProvider 契约与单位标准化测试。

不依赖真实网络: 用假 FuyaoClient 返回样例快照页/历史K线/dump, 验证字段映射、
单位口径 (CONTRIBUTING §3.1: 百分数→小数、volume 股→手)、日K取数分档
(10d dump / 单标的接口)、除权因子推导 (交易所公式 + half-up 舍入 +
同日合并 + 涨跌停自检)、能力声明 (未声明数据集回退 tickflow) 与设置页试拉。
"""

from __future__ import annotations

import calendar
import itertools
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.provider import FuyaoProvider


class _FakeClient:
    """按调用次数返回预置页, 记录调用供分页断言。snapshot_all 同真实客户端语义。"""

    def __init__(
        self,
        pages: list[list[dict]],
        count: int,
        error: Exception | None = None,
        server_ts: int = 0,
    ):
        self.pages = pages
        self.count = count
        self.error = error
        self.server_ts = server_ts
        self.calls: list[dict] = []
        self.last_server_ts = server_ts

    def snapshot_page(self, limit=500, offset=0):
        self.calls.append({"limit": limit, "offset": offset})
        if self.error:
            raise self.error
        if not self.pages:
            return [], self.count
        # 按调用次数取页(provider 每轮 offset += len(rows), 页序与调用序一致)
        idx = min(len(self.calls) - 1, len(self.pages) - 1)
        return list(self.pages[idx]), self.count

    def snapshot_all(self):
        """与真实客户端同语义的分页循环 (无页间隔, 测试用)。返回 (rows, server_ts)。"""
        out: list[dict] = []
        offset = 0
        for _ in range(50):
            rows, count = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if count and len(out) >= count:
                break
            offset += len(rows)
        if not out:
            raise fc.FuyaoError("全市场快照为空")
        return out, self.server_ts

    def close(self):
        pass


def _row(thscode: str = "600519.SH", **over):
    """实测快照行结构(2026-08): high_price/low_price/prev_price 命名。"""
    row = {
        "thscode": thscode,
        "last_price": 1480.0,
        "price_change": 25.0,
        "price_change_ratio_pct": 1.72,
        "open_price": 1460.0,
        "high_price": 1490.5,
        "low_price": 1455.0,
        "prev_price": 1455.0,
        "volume": 1234500,
        "turnover": 1.83e9,
    }
    row.update(over)
    return row


def _provider_with(monkeypatch, pages, count=None, error=None, **fake_kwargs):
    fake = _FakeClient(
        pages, count if count is not None else sum(len(p) for p in pages), error, **fake_kwargs
    )
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    return FuyaoProvider(), fake


# ---- 单位与字段映射 ----


def test_snapshot_units_and_field_mapping(monkeypatch):
    """核心口径: price_change_ratio_pct 百分数 → change_pct 小数制 (1.72 → 0.0172)。"""
    provider, _ = _provider_with(monkeypatch, [[_row()]])
    records = provider.get_realtime()
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "600519.SH"
    assert r["change_pct"] == pytest.approx(0.0172)
    assert r["change_amount"] == pytest.approx(25.0)
    assert r["prev_close"] == 1455.0
    assert r["open"] == 1460.0
    assert r["high"] == 1490.5
    assert r["low"] == 1455.0
    # volume 单位股(1234500) → 内部契约手: floor(1234500/100) = 12345
    assert r["volume"] == 12345
    assert r["amount"] == 1.83e9
    assert r["timestamp"] > 0
    # 快照不提供的字段必须为 None, 不启发式伪造
    assert r["name"] is None
    assert r["amplitude"] is None
    assert r["turnover_rate"] is None


def test_missing_pct_derives_decimal_from_change_amount(monkeypatch):
    """涨跌幅缺失时按 change_amount/prev_close 推导, 仍为小数制。"""
    provider, _ = _provider_with(monkeypatch, [[_row(price_change_ratio_pct=None)]])
    r = provider.get_realtime()[0]
    assert r["change_pct"] == pytest.approx(25.0 / 1455.0)
    assert r["change_amount"] == pytest.approx(25.0)


def test_missing_change_amount_derived_from_prices(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(price_change=None)]])
    r = provider.get_realtime()[0]
    assert r["change_amount"] == pytest.approx(1480.0 - 1455.0)


def test_row_without_thscode_dropped(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(), {"last_price": 1.0}, _row("000001.SZ")]])
    records = provider.get_realtime()
    assert [r["symbol"] for r in records] == ["600519.SH", "000001.SZ"]


def test_all_rows_unrecognized_returns_empty_with_no_fake_data(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[{"foo": "bar"}, {"baz": 1}]])
    assert provider.get_realtime() == []


# ---- 客户端信封解析 (实测结构 vs 文档示例) ----


def _patch_http(monkeypatch, payload, status_code=200):
    class _Resp:
        def json(self):
            return payload

    _Resp.status_code = status_code

    class _Http:
        def get(self, path, params=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: _Http())


def test_client_parses_real_world_envelope(monkeypatch):
    """实测信封(2026-08): data={timestamp, total, item}。"""
    _patch_http(
        monkeypatch,
        {
            "code": 0,
            "message": "success",
            "data": {"timestamp": 1787542612000, "total": 2, "item": [_row(), _row("000001.SZ")]},
        },
    )
    c = fc.FuyaoClient(api_key="k")
    rows, total = c.snapshot_page()
    assert total == 2 and len(rows) == 2
    assert c.last_server_ts == 1787542612000


def test_client_parses_documented_envelope(monkeypatch):
    """官方文档示例信封: data={count, data}。"""
    _patch_http(
        monkeypatch,
        {
            "code": 0,
            "message": "OK",
            "data": {"count": 3, "data": [_row()]},
        },
    )
    c = fc.FuyaoClient(api_key="k")
    rows, total = c.snapshot_page()
    assert total == 3 and len(rows) == 1


def test_client_raises_on_error_code(monkeypatch):
    _patch_http(monkeypatch, {"code": 4001, "message": "rate limited", "data": None})
    c = fc.FuyaoClient(api_key="k")
    with pytest.raises(fc.FuyaoError, match="4001"):
        c.snapshot_page()


# ---- 字段名兼容与服务端时间戳 ----


def test_doc_style_field_names_fallback(monkeypatch):
    """文档示例字段名 (highest_price/lowest_price/prev_close_price) 也能映射。"""
    row = {
        "thscode": "600519.SH",
        "last_price": 1480.0,
        "price_change": 25.0,
        "price_change_ratio_pct": 1.72,
        "open_price": 1460.0,
        "highest_price": 1490.5,
        "lowest_price": 1455.0,
        "prev_close_price": 1455.0,
        "volume": 1234500,
        "turnover": 1.83e9,
    }
    provider, _ = _provider_with(monkeypatch, [[row]])
    r = provider.get_realtime()[0]
    assert r["high"] == 1490.5
    assert r["low"] == 1455.0
    assert r["prev_close"] == 1455.0


def test_realtime_uses_server_timestamp(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]], server_ts=1787542612000)
    assert provider.get_realtime()[0]["timestamp"] == 1787542612000


def test_realtime_falls_back_to_local_time_without_server_ts(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]], server_ts=0)
    assert provider.get_realtime()[0]["timestamp"] > 0


# ---- 分页 ----


def test_snapshot_pagination_merges_pages(monkeypatch):
    page1 = [_row(f"{600000 + i}.SH") for i in range(2)]
    page2 = [_row(f"{688000 + i}.SH") for i in range(1)]
    provider, fake = _provider_with(monkeypatch, [page1, page2], count=3)
    records = provider.get_realtime()
    assert len(records) == 3
    assert len(fake.calls) == 2
    assert fake.calls[1]["offset"] == 2


def test_snapshot_stops_when_page_empty(monkeypatch):
    provider, fake = _provider_with(monkeypatch, [[_row()], []], count=0)
    assert len(provider.get_realtime()) == 1
    assert len(fake.calls) == 2


# ---- 软失败 ----


def test_realtime_error_returns_empty_list(monkeypatch):
    provider, _ = _provider_with(
        monkeypatch, [[]], error=fc.FuyaoError("扶摇接口错误 code=4001: 频率超限")
    )
    assert provider.get_realtime() == []


# ---- 指数快照 (可选插件协议 get_realtime_indices) ----


class _FakeIndexClient:
    def __init__(self, rows=None, server_ts=0, error=None):
        self.rows = rows or []
        self.server_ts = server_ts
        self.error = error
        self.calls: list[list[str]] = []

    def index_snapshot(self, thscodes):
        self.calls.append(list(thscodes))
        if self.error:
            raise self.error
        return list(self.rows), self.server_ts

    def close(self):
        pass


def _index_provider_with(monkeypatch, **kwargs):
    fake = _FakeIndexClient(**kwargs)
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    return FuyaoProvider(), fake


def test_realtime_indices_maps_and_keeps_volume_unit(monkeypatch):
    """指数快照 → realtime record; volume 无股→手口径, 直接透传。"""
    provider, fake = _index_provider_with(
        monkeypatch,
        rows=[_row("000001.SH", volume=576656606, price_change_ratio_pct=0.86)],
        server_ts=1787542612000,
    )
    records = provider.get_realtime_indices(["000001.SH", "399001.SZ"])
    assert fake.calls == [["000001.SH", "399001.SZ"]]
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "000001.SH"
    assert r["change_pct"] == pytest.approx(0.0086)
    assert r["timestamp"] == 1787542612000
    assert r["volume"] == 576656606  # 不做 /100


def test_realtime_indices_skips_bj_symbols(monkeypatch):
    """北交所指数扶摇不支持, 未知代码会整批 1002 连坐 → .BJ 直接跳过不进请求。"""
    provider, fake = _index_provider_with(monkeypatch, rows=[])
    assert provider.get_realtime_indices(["899050.BJ", "000001.SH"]) == []
    assert fake.calls == [["000001.SH"]]
    assert provider.get_realtime_indices(["899050.BJ"]) == []
    assert fake.calls == [["000001.SH"]]  # 全 .BJ 时根本不发请求


def test_realtime_indices_error_returns_none(monkeypatch):
    provider, _ = _index_provider_with(
        monkeypatch, error=fc.FuyaoError("扶摇接口错误 code=1002: Unknown thscode")
    )
    assert provider.get_realtime_indices(["000001.SH"]) is None


def test_client_requires_api_key():
    with pytest.raises(fc.FuyaoError):
        fc.FuyaoClient(api_key="")


# ---- 能力声明与注册 ----


def test_datasets_declaration():
    """声明 realtime/daily/adj_factor/financial; minute 未声明 (回退 tickflow)。"""
    config = FuyaoProvider().config
    assert "realtime" in config.datasets
    assert "daily" in config.datasets
    assert "adj_factor" in config.datasets
    assert "financial" in config.datasets
    assert "minute" not in config.datasets


# ---- API Key 解析 (secrets.json > .env, 对齐 tickflow 语义) ----


def test_get_api_key_secrets_store_takes_priority(monkeypatch):
    from app import secrets_store

    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {fp.SECRETS_FIELD: "sk-from-ui"})
    assert fp.get_api_key() == "sk-from-ui"


def test_get_api_key_falls_back_to_env(monkeypatch):
    from app import secrets_store

    monkeypatch.setenv(fp.API_KEY_ENV, "sk-from-env")
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    assert fp.get_api_key() == "sk-from-env"


def test_availability_accepts_secrets_store_key(monkeypatch):
    from app import secrets_store

    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {fp.SECRETS_FIELD: "sk-from-ui"})
    assert fp.availability() == (True, "ok")


def test_availability_requires_env_key(monkeypatch):
    from app import secrets_store

    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    ok, reason = fp.availability()
    assert ok is False and fp.API_KEY_ENV in reason


# ---- 先探后存 (probe_api_key) ----


def _patch_client_cls(monkeypatch, fake):
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))


def test_probe_api_key_ok(monkeypatch):
    _patch_client_cls(monkeypatch, _FakeClient([[_row()]], 1))
    ok, reason = fp.probe_api_key("sk-candidate")
    assert ok is True and reason == "ok"


def test_probe_api_key_invalid_key(monkeypatch):
    _patch_client_cls(
        monkeypatch,
        _FakeClient([[]], 0, error=fc.FuyaoError("扶摇接口错误 code=1001: 无效 api key")),
    )
    ok, reason = fp.probe_api_key("sk-bad")
    assert ok is False and "无效" in reason


def test_loader_probe_plugin_key_dispatch(monkeypatch):
    import app.plugins.fuyao.provider as provider_mod
    from app.data_providers.custom import loader

    monkeypatch.setattr(
        provider_mod, "probe_api_key", lambda key: (True, "ok") if key == "good" else (False, "bad")
    )
    assert loader.probe_plugin_key("fuyao", "good") == (True, "ok")
    assert loader.probe_plugin_key("fuyao", "bad") == (False, "bad")


def test_loader_probe_plugin_key_unsupported_plugin(monkeypatch):
    from app.data_providers.custom import loader

    # 未声明 api_key_env 的插件 → 不支持界面配 Key (桩 manifest 驱动, 不依赖具体内置插件)
    monkeypatch.setattr(
        loader, "plugin_manifest",
        lambda name: {"name": name} if name == "no_key_plugin" else None,
    )
    ok, reason = loader.probe_plugin_key("no_key_plugin", "x")
    assert ok is False and "不支持" in reason
    ok, reason = loader.probe_plugin_key("no_such_plugin", "x")
    assert ok is False and "不存在" in reason


# ---- 保存/清除端点 (直接调用 handler, 先探后存语义) ----


def test_save_plugin_key_invalid_key_not_persisted(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    saved: dict = {}
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda n, k: (False, "Key 无效"))
    monkeypatch.setattr(
        settings_api.secrets_store, "save", lambda updates: saved.update(updates) or updates
    )
    out = settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="fuyao", api_key="bad"))
    assert out["ok"] is False and out["reason"] == "invalid"
    assert saved == {}  # 无效 Key 不落盘


def test_save_plugin_key_valid_persists_and_rescans(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    saved: dict = {}
    reloaded = []
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda n, k: (True, "ok"))
    monkeypatch.setattr(
        settings_api.secrets_store, "save", lambda updates: saved.update(updates) or updates
    )
    monkeypatch.setattr(
        settings_api.secrets_store, "mask", lambda key, prefix=4, suffix=4: "abcd••••wxyz"
    )
    monkeypatch.setattr(custom_sources, "load_all", lambda: reloaded.append(1))
    monkeypatch.setattr(
        custom_sources, "list_plugins", lambda: [{"name": "fuyao", "available": True}]
    )
    out = settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="fuyao", api_key="good-key"))
    assert out["ok"] is True
    assert saved == {"fuyao_api_key": "good-key"}  # 字段名与 provider.SECRETS_FIELD 一致
    assert out["plugin_available"] is True
    assert reloaded == [1]  # 保存后重扫, 插件即刻可用


def test_clear_plugin_key(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    cleared: list = []
    monkeypatch.setattr(custom_sources, "is_builtin", lambda n: n == "fuyao")
    monkeypatch.setattr(settings_api.secrets_store, "clear", lambda *keys: cleared.extend(keys))
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(
        custom_sources, "list_plugins", lambda: [{"name": "fuyao", "available": False}]
    )
    out = settings_api.clear_plugin_key("fuyao")
    assert out["ok"] is True and out["plugin_available"] is False
    assert cleared == ["fuyao_api_key"]


def test_manifest_declares_datasets():
    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("fuyao")
    assert manifest is not None
    assert manifest["entry"] == "app.plugins.fuyao.provider:FuyaoProvider"
    assert {"realtime", "daily", "adj_factor"} <= set(manifest.get("datasets") or [])
    assert manifest.get("runtime") == "none"
    assert manifest.get("api_key_env") == fp.API_KEY_ENV


def test_hidden_plugin_not_registered():
    """fuyao 已取消隐藏 (plugin.yaml 不再声明 hidden); hidden 机制本身仍生效。

    用合成清单验证: hidden: true 的插件不注册、不在数据源页展示。
    """
    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("fuyao")
    assert manifest is not None and not manifest.get("hidden"), (
        "fuyao 应保持可见; 如需重新隐藏请在 plugin.yaml 声明 hidden 并更新本测试"
    )
    loader._register_one_plugin(manifest)
    assert "fuyao" in loader._PLUGIN_STATUS

    hidden_manifest = dict(manifest, name="hidden_probe", hidden=True)
    loader._register_one_plugin(hidden_manifest)
    assert "hidden_probe" not in loader._PLUGIN_STATUS
    assert "hidden_probe" not in loader._PROVIDERS


# ---- 插件 Key 脱敏展示 (与 TickFlow Key 契约一致) ----


def test_plugin_key_masked_from_secrets_then_env(monkeypatch):
    """api_key_masked 随插件状态返回: secrets.json 优先, .env 兜底, 未配置为空。

    完整 Key 不出后端, 只出 mask() 结果 — 与 settings API 的
    tickflow_api_key_masked 同一展示契约。
    """
    from app import secrets_store
    from app.data_providers.custom import loader

    # 未声明 api_key_env / 未配置 Key → 空
    assert loader._plugin_key_masked("x", "") == ""
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    assert loader._plugin_key_masked("fuyao", fp.API_KEY_ENV) == ""

    # .env 兜底
    monkeypatch.setenv(fp.API_KEY_ENV, "env-secret-key-123456")
    assert loader._plugin_key_masked("fuyao", fp.API_KEY_ENV) == "env-••••••3456"

    # secrets.json 优先于 .env
    monkeypatch.setattr(secrets_store, "load", lambda: {"fuyao_api_key": "stored-secret-key-999"})
    assert loader._plugin_key_masked("fuyao", fp.API_KEY_ENV) == "stor••••••-999"

    # 注册进插件状态: 数据源列表接口据此常驻展示 (而非仅保存后瞬时显示)
    loader._register_one_plugin(loader.plugin_manifest("fuyao"))
    assert loader._PLUGIN_STATUS["fuyao"]["api_key_masked"] == "stor••••••-999"


# ---- 设置页试拉 ----


def test_test_dataset_realtime_preview(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(), _row("000001.SZ")]], count=5400)
    out = provider.test_dataset("realtime")
    assert out["provider"] == "fuyao"
    assert out["rows"] == 5400
    assert out["preview"][0]["symbol"] == "600519.SH"
    assert out["preview"][0]["change_pct"] == pytest.approx(0.0172)


def test_test_dataset_unsupported_dataset_reports_fallback(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]])
    out = provider.test_dataset("minute")
    assert "error" in out and "回退" in out["error"]


def test_close_is_idempotent(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]])
    provider.close()
    provider.close()


# =====================================================================
# daily / adj_factor (2026-08 接入)
# =====================================================================


def _sh_ms(d: date) -> int:
    """交易日 → 扶摇口径 ms(该日上海零点), 与 provider._ms_of_date 同式。"""
    return (calendar.timegm(d.timetuple()) - 28_800) * 1000


def _bar(d: date, close: float, volume: float = 1_612_611, open_: float | None = None):
    """historical/dump 原始行(价格元, volume 股)。"""
    return {
        "date_ms": _sh_ms(d),
        "open_price": open_ if open_ is not None else round(close * 0.99, 2),
        "high_price": round(close * 1.01, 2),
        "low_price": round(close * 0.98, 2),
        "close_price": close,
        "volume": volume,
        "turnover": close * volume,
    }


def _dump_bar(sym: str, d: date, close: float) -> dict:
    """daily dump 行(11 列形状, 含 thscode/adjusted)。"""
    return {
        "thscode": sym,
        "currency": "CNY",
        "interval": "1d",
        "adjusted": "none",
        **_bar(d, close),
    }


class _FakeHistClient:
    """单标的历史K线假客户端: 按窗口过滤预置 bars, 记录调用参数。"""

    def __init__(self, bars_by_symbol: dict | None = None, error_syms: tuple = ()):
        self.bars = bars_by_symbol or {}
        self.errors = set(error_syms)
        self.calls: list[dict] = []

    def historical_kline(self, thscode, start_ms, end_ms, adjust="none"):
        self.calls.append({"thscode": thscode, "start": start_ms, "end": end_ms, "adjust": adjust})
        if thscode in self.errors:
            raise fc.FuyaoError("扶摇接口错误 code=4001: 频率超限")
        return [b for b in self.bars.get(thscode, []) if start_ms <= b["date_ms"] <= end_ms]

    def dump_download_url(self, dump_kind):
        raise fc.FuyaoError("测试环境无 dump")

    def close(self):
        pass


def _hist_provider(monkeypatch, fake: _FakeHistClient, allow_dumps: bool = False) -> FuyaoProvider:
    p = FuyaoProvider()
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(fp, "_HIST_INTERVAL_S", 0.0)
    if not allow_dumps:
        # API 路径测试必须与开发机真实 dump 缓存隔离; dump 测试显式传 allow_dumps=True。
        p._ensure_daily_big_dump = lambda start_d: None  # type: ignore[assignment]
        p._daily_dump_info = lambda: None  # type: ignore[assignment]
    return p


def _daily10_dump(rows: list[dict]) -> pl.DataFrame:
    """11 列 daily-k-10d dump 形状。"""
    return pl.DataFrame(
        rows,
        schema={
            "thscode": pl.String,
            "currency": pl.String,
            "interval": pl.String,
            "adjusted": pl.String,
            "date_ms": pl.Int64,
            "open_price": pl.Float64,
            "high_price": pl.Float64,
            "low_price": pl.Float64,
            "close_price": pl.Float64,
            "volume": pl.Float64,
            "turnover": pl.Float64,
        },
    )


def _adj_dump(events: list[tuple]) -> pl.DataFrame:
    """8 列 adjustment-factors dump 形状。events: (thscode, ex_date, D, S, AR, AP)。"""
    rows = [
        {
            "thscode": s,
            "ticker": s.split(".")[0],
            "ex_date_ms": _sh_ms(d),
            "dividend_per_share": dv,
            "per_share_bonus": bn,
            "allotment_ratio": ar,
            "allotment_price": ap,
            "currency": "CNY",
        }
        for s, d, dv, bn, ar, ap in events
    ]
    return pl.DataFrame(
        rows,
        schema={
            "thscode": pl.String,
            "ticker": pl.String,
            "ex_date_ms": pl.Int64,
            "dividend_per_share": pl.Float64,
            "per_share_bonus": pl.Float64,
            "allotment_ratio": pl.Float64,
            "allotment_price": pl.Float64,
            "currency": pl.String,
        },
    )


def _adj_provider(monkeypatch, events: list[tuple], bars_by_symbol: dict) -> FuyaoProvider:
    p = _hist_provider(monkeypatch, _FakeHistClient(bars_by_symbol))
    p._dump_memo[fp._ADJ_DUMP_KIND] = _adj_dump(events)
    return p


# ---- daily: 单标的接口路径 ----


def test_daily_api_units_volume_shares_to_lots(monkeypatch):
    """核心口径: 原始价 + volume 股→手 floor(/100) + 上海零点时区。"""
    bars = [
        _bar(date(2026, 8, 26), 11.10),
        _bar(date(2026, 8, 27), 11.05),
        _bar(date(2026, 8, 28), 11.65),
    ]
    provider = _hist_provider(monkeypatch, _FakeHistClient({"000001.SZ": bars}))
    # 窗口跨度 28 天 > _RECENT_DUMP_DAYS → 走单标的接口
    df = provider.get_daily(["000001.SZ"], datetime(2026, 8, 1), datetime(2026, 8, 28))
    assert df.height == 3
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.schema["date"] == pl.Date
    assert df["date"].to_list() == [date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
    assert df["volume"].to_list() == [16126.0] * 3  # 1,612,611 股 → 16126 手
    assert df["close"].to_list() == [11.10, 11.05, 11.65]
    assert df["amount"].to_list() == [b["turnover"] for b in bars]


def test_daily_api_adjust_locked_to_none(monkeypatch):
    """adjust=none 锁定: 服务端默认 forward, 官方前复权序列不可用(事件间逐日漂移)。"""
    provider = _hist_provider(
        monkeypatch, _FakeHistClient({"000001.SZ": [_bar(date(2026, 8, 27), 11.05)]})
    )
    provider.get_daily(["000001.SZ"], datetime(2026, 8, 1), datetime(2026, 8, 28))
    fake = provider._get_client()
    assert fake.calls and all(c["adjust"] == "none" for c in fake.calls)


def test_daily_api_deep_window_splits_into_10y_chunks(monkeypatch):
    """超 10 年窗口自动分片(25.6 年 → 3 片), 分片连续不重叠且各 ≤10 年。"""
    provider = _hist_provider(monkeypatch, _FakeHistClient({"000001.SZ": []}))
    provider.get_daily(["000001.SZ"], datetime(2001, 1, 1), datetime(2026, 8, 28))
    calls = provider._get_client().calls
    assert len(calls) == 3
    for c in calls:
        assert c["end"] - c["start"] < fp._HIST_MAX_SPAN_MS
    for a, b in itertools.pairwise(calls):
        assert b["start"] == a["end"] + 1  # 连续且不重叠


def test_daily_api_soft_fail_per_symbol(monkeypatch):
    """单标的失败软跳过, 不阻断其余标的。"""
    bars = {"000001.SZ": [_bar(date(2026, 8, 27), 11.05)]}
    provider = _hist_provider(monkeypatch, _FakeHistClient(bars, error_syms=("600519.SH",)))
    df = provider.get_daily(["600519.SH", "000001.SZ"], datetime(2026, 8, 1), datetime(2026, 8, 28))
    assert df["symbol"].unique().to_list() == ["000001.SZ"]


def test_iter_daily_api_is_bounded_by_symbol_batch(monkeypatch):
    """历史 API 每批只产出固定 symbol 集合,不积累全市场 frames。"""
    bars = {
        "000001.SZ": [_bar(date(2018, 1, 2), 10.0)],
        "000002.SZ": [_bar(date(2018, 1, 2), 11.0)],
        "000003.SZ": [_bar(date(2018, 1, 2), 12.0)],
    }
    provider = _hist_provider(monkeypatch, _FakeHistClient(bars))
    monkeypatch.setattr(fp, "_HIST_SYMBOL_BATCH", 2)
    chunks = list(provider.iter_daily(
        list(bars), datetime(2018, 1, 1), datetime(2018, 1, 3)
    ))
    assert [set(df["symbol"].to_list()) for df in chunks] == [
        {"000001.SZ", "000002.SZ"}, {"000003.SZ"}
    ]


def test_daily_empty_symbols_or_non_stock_returns_empty(monkeypatch):
    provider = _hist_provider(monkeypatch, _FakeHistClient({}))
    assert provider.get_daily([], None, None).is_empty()
    assert provider.get_daily(
        ["510300.SH"], datetime(2026, 8, 1), datetime(2026, 8, 28), asset_type="etf"
    ).is_empty()


# ---- daily: 10d dump 路径 ----


def _recent_daily_dump() -> pl.DataFrame:
    rows = []
    for d in [date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]:
        b = _bar(d, 11.0 + hash(d) % 3, volume=97_570_170)
        rows.append(
            {"thscode": "000001.SZ", "currency": "CNY", "interval": "1d", "adjusted": "none", **b}
        )
        rows.append(
            {
                "thscode": "600519.SH",
                "currency": "CNY",
                "interval": "1d",
                "adjusted": "none",
                **_bar(d, 1480.0, volume=1_234_500),
            }
        )
    # 停牌行(open=high=0)应被过滤
    rows.append(
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 27), 11.0, open_=0.0),
        }
    )
    rows[-1]["high_price"] = 0.0
    return _daily10_dump(rows)


def test_daily_recent_window_uses_dump_without_api_calls(monkeypatch):
    """近端窗口: 一次 dump 覆盖全部标的, 不打单标的接口; volume 股→手; 停牌行过滤。"""
    provider = _hist_provider(monkeypatch, _FakeHistClient({}))
    provider._dump_memo[fp._DAILY10_DUMP_KIND] = _recent_daily_dump()
    progress = []
    df = provider.get_daily(
        ["000001.SZ"],
        datetime(2026, 8, 26),
        datetime(2026, 8, 28),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert provider._get_client().calls == []  # 未走单标的接口
    assert progress == [(1, 1)]
    assert set(df["symbol"].to_list()) == {"000001.SZ"}  # 未请求的标的不混入
    assert df["volume"].max() == 975_701.0  # 97,570,170 股 → 975,701 手
    assert date(2026, 8, 26) in df["date"].to_list()
    # 停牌行(open=high=0)被 filter_halt_days 剔除, 08-27 仅保留正常行
    assert df.filter(pl.col("date") == date(2026, 8, 27)).height == 1


def test_daily_dump_stale_falls_back_to_api(monkeypatch):
    """dump 末端落后且 end 是工作日 → 视为有缺口, 回退单标的接口。"""
    bars = {"000001.SZ": [_bar(date(2026, 8, 27), 11.05)]}
    provider = _hist_provider(monkeypatch, _FakeHistClient(bars))
    provider._dump_memo[fp._DAILY10_DUMP_KIND] = _recent_daily_dump()  # max=08-28
    provider.get_daily(["000001.SZ"], datetime(2026, 8, 24), datetime(2026, 9, 4))  # 09-04 周五
    assert provider._get_client().calls != []


def test_daily_dump_weekend_end_covered(monkeypatch):
    """end 为周末且紧随 dump 末端 → 自然缺口, 仍走 dump。"""
    provider = _hist_provider(monkeypatch, _FakeHistClient({}))
    provider._dump_memo[fp._DAILY10_DUMP_KIND] = _recent_daily_dump()  # max=周五 08-28
    df = provider.get_daily(["000001.SZ"], datetime(2026, 8, 26), datetime(2026, 8, 30))  # 周日
    assert provider._get_client().calls == []
    assert not df.is_empty()


def test_daily_dump_rejects_non_raw_adjustment(monkeypatch):
    """防御: dump 变为复权口径(adjusted != none)时拒绝输出, 不污染原始K线库。"""
    dump = _recent_daily_dump().with_columns(pl.lit("forward").alias("adjusted"))
    provider = _hist_provider(monkeypatch, _FakeHistClient({}))
    provider._dump_memo[fp._DAILY10_DUMP_KIND] = dump
    assert provider.get_daily(
        ["000001.SZ"], datetime(2026, 8, 26), datetime(2026, 8, 28)
    ).is_empty()


def test_daily_old_window_bypasses_dump(monkeypatch):
    """深窗口且 dump 不可用 → 单标的接口兜底。"""
    provider = _hist_provider(monkeypatch, _FakeHistClient({"000001.SZ": []}))
    df = provider.get_daily(["000001.SZ"], datetime(2020, 1, 1), datetime(2026, 8, 28))
    assert df.is_empty()
    assert provider._get_client().calls  # 走了单标的接口


# ---- daily: 10 年全量 dump 档 ----


def _bigdump_provider(
    monkeypatch, tmp_path, big_rows: list[dict], ten_memo: pl.DataFrame | None = None
):
    """缓存目录重定向到 tmp_path 并放置 daily_k__<release>.parquet; 单标的接口若被调用会被断言暴露。"""
    provider = _hist_provider(monkeypatch, _FakeHistClient({}), allow_dumps=True)
    monkeypatch.setattr(fp, "_cache_dir", lambda: tmp_path)
    _daily10_dump(big_rows).write_parquet(tmp_path / "daily_k__20260101.parquet")
    if ten_memo is not None:
        provider._dump_memo[fp._DAILY10_DUMP_KIND] = ten_memo
    return provider


def test_daily_deep_window_uses_big_dump(monkeypatch, tmp_path):
    """深窗口走 10 年 dump: 不打单标的接口, volume 股→手, 未请求标的不混入。"""
    rows = [
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 1, 5), 11.0, volume=97_570_170),
        },
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 6, 12), 11.7, volume=1_234_500),
        },
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 14), 11.4, volume=1_234_500),
        },
        {
            "thscode": "600519.SH",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 14), 1480.0, volume=1_234_500),
        },
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, rows)
    progress = []
    df = provider.get_daily(
        ["000001.SZ"],
        datetime(2026, 1, 5),
        datetime(2026, 8, 14),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert provider._get_client().calls == []
    assert progress == [(1, 1)]
    assert set(df["symbol"].to_list()) == {"000001.SZ"}
    assert df["date"].to_list() == [date(2026, 1, 5), date(2026, 6, 12), date(2026, 8, 14)]
    assert df["volume"].to_list() == [975_701.0, 12_345.0, 12_345.0]


def test_iter_daily_big_dump_reads_bounded_record_batches(monkeypatch, tmp_path):
    """多年 dump 分批读取,不经 LazyFrame collect() 物化整份文件。"""
    rows = [_dump_bar("000001.SZ", date(2026, 1, 1) + timedelta(days=i), 10.0 + i)
            for i in range(5)]
    provider = _bigdump_provider(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(fp, "_DAILY_DUMP_BATCH_ROWS", 2)
    chunks = list(provider.iter_daily(
        ["000001.SZ"], datetime(2026, 1, 1), datetime(2026, 1, 5)
    ))
    assert [chunk.height for chunk in chunks] == [2, 2, 1]
    assert [d for chunk in chunks for d in chunk["date"].to_list()] == [
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
        date(2026, 1, 4), date(2026, 1, 5),
    ]


def test_iter_daily_splits_older_history_from_big_dump(monkeypatch, tmp_path):
    """请求跨 dump 起点时,只把 dump 外那段回退到历史 API。"""
    provider = _bigdump_provider(
        monkeypatch,
        tmp_path,
        [_dump_bar("000001.SZ", date(2020, 1, 2), 10.0)],
    )
    provider._client = _FakeHistClient({"000001.SZ": [_bar(date(2019, 12, 31), 9.0)]})
    rows = pl.concat(list(provider.iter_daily(
        ["000001.SZ"], datetime(2019, 12, 30), datetime(2020, 1, 2)
    )))
    assert rows["date"].to_list() == [date(2019, 12, 31), date(2020, 1, 2)]
    assert len(provider._get_client().calls) == 1


def test_daily_big_dump_tail_filled_by_10d(monkeypatch, tmp_path):
    """大 dump 末端缺口(dmax 旧)由 10d dump 补尾, 两段拼接无缝。"""
    big_rows = [
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 10), 10.9),
        },
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 14), 11.0),
        },
    ]
    ten_rows = [
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 15), 11.2),
        },
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 28), 11.65),
        },
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows, ten_memo=_daily10_dump(ten_rows))
    df = provider.get_daily(["000001.SZ"], datetime(2026, 8, 10), datetime(2026, 8, 28))
    assert df["date"].to_list() == [
        date(2026, 8, 10),
        date(2026, 8, 14),
        date(2026, 8, 15),
        date(2026, 8, 28),
    ]
    assert df["close"].to_list() == [10.9, 11.0, 11.2, 11.65]


def test_daily_big_dump_weekend_end_needs_no_tail(monkeypatch, tmp_path):
    """多年 dump 覆盖到周五时,紧邻周末不应回退逐标的 API。"""
    provider = _bigdump_provider(
        monkeypatch,
        tmp_path,
        [
            _dump_bar("000001.SZ", date(2026, 8, 1), 10.9),
            _dump_bar("000001.SZ", date(2026, 8, 28), 11.65),
        ],
    )

    df = provider.get_daily(
        ["000001.SZ"], datetime(2026, 8, 1), datetime(2026, 8, 30)
    )

    assert df["date"].to_list() == [date(2026, 8, 1), date(2026, 8, 28)]
    assert provider._get_client().calls == []


def test_daily_big_dump_midgap_falls_back(monkeypatch, tmp_path):
    """大 dump 与 10d dump 之间有中段缺口 → 整体回退, 不拼缺口数据。"""
    big_rows = [
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 1), 10.9),
        },
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 5), 11.0),
        },
    ]
    # 10d 从 08-27 起, 与大 dump 末端 08-05 之间有中段缺口
    ten_rows = [
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2026, 8, 27), 11.5),
        },
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows, ten_memo=_daily10_dump(ten_rows))
    df = provider.get_daily(["000001.SZ"], datetime(2026, 8, 1), datetime(2026, 8, 28))
    assert df.is_empty()  # 兜底走单标的接口(fake 为空) → 不返回缺口拼接数据


def test_daily_big_dump_start_before_dmin_falls_back(monkeypatch, tmp_path):
    """窗口早于大 dump 起点(>10 年) → 单标的接口兜底。"""
    rows = [
        {
            "thscode": "000001.SZ",
            "currency": "CNY",
            "interval": "1d",
            "adjusted": "none",
            **_bar(date(2020, 1, 2), 10.0),
        },
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, rows)
    # 缓存不覆盖起点 → 会尝试拉最新 release; 模拟"最新 release 就是这份缓存文件"
    provider._ensure_dump_path = lambda kind, prefix: tmp_path / "daily_k__20260101.parquet"  # type: ignore[assignment]
    bars = {"000001.SZ": [_bar(date(2015, 6, 1), 9.0)]}
    provider._dump_memo.clear()
    monkeypatch.setattr(
        fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: _FakeHistClient(bars)})
    )
    df = provider.get_daily(["000001.SZ"], datetime(2015, 1, 1), datetime(2015, 12, 31))
    assert df.height == 1 and df["close"][0] == 9.0  # 单标的兜底成功


def test_tail_ok_weekend_tolerance():
    """末端容忍: 周末/节假日的自然缺口(≤3 天且 end 是周末)不算缺失。"""
    assert fp._tail_ok(date(2026, 8, 28), date(2026, 8, 28)) is True
    assert fp._tail_ok(date(2026, 8, 29), date(2026, 8, 28)) is True  # 周六
    assert fp._tail_ok(date(2026, 8, 30), date(2026, 8, 28)) is True  # 周日
    assert fp._tail_ok(date(2026, 8, 31), date(2026, 8, 28)) is False  # 周一(可能有行情)
    assert fp._tail_ok(date(2026, 9, 5), date(2026, 8, 28)) is False  # 隔了一周


# ---- adj_factor: 推导 ----

# get_adj_factors 输出契约: 规范三列 + 事件明细列 (等差显示/全精度链重建用)
_ADJ_OUT_COLS = [
    "symbol", "trade_date", "ex_factor",
    "dividend", "bonus", "allot", "allot_price", "prev_close",
]


def test_adj_dividend_factor_derivation(monkeypatch):
    """纯分红: P=32.8, D=0.68 → ref=32.12 → factor=32.8/32.12 (与 tickflow 对拍值一致)。"""
    events = [("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0)]
    bars = {"600519.SH": [_bar(date(2026, 6, 11), 32.8), _bar(date(2026, 6, 12), 32.0)]}
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["600519.SH"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["trade_date"] == date(2026, 6, 12)
    assert row["ex_factor"] == pytest.approx(32.8 / 32.12)


def test_adj_output_keeps_event_details(monkeypatch):
    """扩表: 输出保留事件明细 (每股分红/送转/配股/前收盘), 供等差显示与全精度链重建。"""
    events = [("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0)]
    bars = {"600519.SH": [_bar(date(2026, 6, 11), 32.8), _bar(date(2026, 6, 12), 32.0)]}
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["600519.SH"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    row = df.row(0, named=True)
    assert row["dividend"] == pytest.approx(0.68)
    assert row["bonus"] == 0.0
    assert row["allot"] == 0.0
    assert row["allot_price"] == 0.0
    assert row["prev_close"] == pytest.approx(32.8)


def test_adj_bonus_and_allotment_formula(monkeypatch):
    """送转+配股混合: P=5.23, AR=0.4, AP=3.36 → ref=(5.23+1.344)/1.4=4.6957→4.70(half-up)。"""
    events = [("300176.SZ", date(2026, 8, 21), 0.0, 0.0, 0.4, 3.36)]
    bars = {"300176.SZ": [_bar(date(2026, 8, 20), 5.23), _bar(date(2026, 8, 21), 4.68)]}
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["300176.SZ"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df.height == 1
    assert df["ex_factor"][0] == pytest.approx(5.23 / 4.70)


def test_adj_half_up_rounding_not_bankers(monkeypatch):
    """舍入口径: x=2.625 → half-up 2.63 (银行家舍入会给 2.62, 对拍实证偏离)。"""
    events = [("600000.SH", date(2026, 6, 12), 8.0, 0.0, 0.0, 0.0)]
    bars = {"600000.SH": [_bar(date(2026, 6, 11), 10.625), _bar(date(2026, 6, 12), 2.70)]}
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["600000.SH"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df["ex_factor"][0] == pytest.approx(10.625 / 2.63)


def test_adj_price_limit_selfcheck_drops_impossible_factor(monkeypatch):
    """涨跌停自检: 复权后除权日涨幅超板限的事件剔除, 不让坏因子落库。"""
    events = [("600000.SH", date(2026, 6, 12), 8.0, 0.0, 0.0, 0.0)]
    bars = {"600000.SH": [_bar(date(2026, 6, 11), 10.625), _bar(date(2026, 6, 12), 4.05)]}  # +54%
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["600000.SH"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df.is_empty()


def test_adj_merges_same_day_components_before_derivation(monkeypatch):
    """同日拆行(分红行+送股行)必须先合并再推导。"""
    events = [
        ("000812.SZ", date(2026, 6, 12), 0.3, 0.0, 0.0, 0.0),
        ("000812.SZ", date(2026, 6, 12), 0.0, 0.1, 0.0, 0.0),
    ]
    bars = {"000812.SZ": [_bar(date(2026, 6, 11), 10.0), _bar(date(2026, 6, 12), 9.0)]}
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["000812.SZ"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df.height == 1  # 合并成一个事件
    assert df["ex_factor"][0] == pytest.approx(10.0 / 8.82)  # ref=(10-0.3)/1.1=8.8181→8.82


def test_adj_filters_zero_rows_and_missing_allot_price(monkeypatch):
    """全零行与配股价缺失行过滤, 不参与推导。"""
    events = [
        ("000001.SZ", date(2026, 6, 12), 0.0, 0.0, 0.0, 0.0),  # 全零
        ("000002.SZ", date(2026, 6, 12), 0.0, 0.0, 0.3, 0.0),  # 配股无价
        ("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0),  # 正常
    ]
    bars = {
        "600519.SH": [_bar(date(2026, 6, 11), 32.8), _bar(date(2026, 6, 12), 32.0)],
        "000001.SZ": [_bar(date(2026, 6, 11), 10.0), _bar(date(2026, 6, 12), 10.0)],
        "000002.SZ": [_bar(date(2026, 6, 11), 8.0), _bar(date(2026, 6, 12), 8.0)],
    }
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["000001.SZ", "000002.SZ", "600519.SH"], None, None)
    assert df["symbol"].unique().to_list() == ["600519.SH"]


def test_adj_skips_future_announced_events(monkeypatch):
    """未来已公告事件无前收盘, 跳过留给滚动增量窗口。"""
    future = date.today() + timedelta(days=5)
    events = [("600519.SH", future, 0.68, 0.0, 0.0, 0.0)]
    provider = _adj_provider(monkeypatch, events, {"600519.SH": []})
    df = provider.get_adj_factors(["600519.SH"], datetime(2026, 1, 1), None)
    assert df.is_empty()


def test_adj_window_filter(monkeypatch):
    events = [
        ("600519.SH", date(2026, 1, 10), 0.5, 0.0, 0.0, 0.0),
        ("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0),
    ]
    bars = {
        "600519.SH": [
            _bar(date(2026, 1, 9), 30.0),
            _bar(date(2026, 1, 10), 30.0),
            _bar(date(2026, 6, 11), 32.8),
            _bar(date(2026, 6, 12), 32.0),
        ]
    }
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["600519.SH"], datetime(2026, 3, 1), datetime(2026, 12, 31))
    assert df["trade_date"].to_list() == [date(2026, 6, 12)]


def test_adj_output_schema_sorted_and_deduped(monkeypatch):
    events = [
        ("000001.SZ", date(2026, 6, 12), 0.36, 0.0, 0.0, 0.0),
        ("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0),
        ("600519.SH", date(2025, 6, 20), 0.5, 0.0, 0.0, 0.0),
    ]
    bars = {
        "600519.SH": [
            _bar(date(2025, 6, 19), 31.0),
            _bar(date(2025, 6, 20), 31.0),
            _bar(date(2026, 6, 11), 32.8),
            _bar(date(2026, 6, 12), 32.0),
        ],
        "000001.SZ": [_bar(date(2026, 6, 11), 11.7), _bar(date(2026, 6, 12), 11.5)],
    }
    provider = _adj_provider(monkeypatch, events, bars)
    df = provider.get_adj_factors(["000001.SZ", "600519.SH"], None, None)
    assert df.columns == _ADJ_OUT_COLS
    assert df.schema["trade_date"] == pl.Date and df.schema["ex_factor"] == pl.Float64
    assert df.height == 3
    assert df.sort(["symbol", "trade_date"]).equals(df)  # 已排序
    assert df["ex_factor"].min() > 1.0  # 分红因子必 >1


def test_adj_etf_and_empty_symbols_return_empty(monkeypatch):
    provider = _adj_provider(monkeypatch, [], {})
    empty = provider.get_adj_factors([], None, None)
    assert empty.is_empty() and empty.columns == _ADJ_OUT_COLS
    etf = provider.get_adj_factors(["510300.SH"], None, None, asset_type="etf")
    assert etf.is_empty()


def test_adj_dump_unavailable_returns_empty(monkeypatch):
    """dump 加载失败软返回空(不阻断管道), 不抛异常。"""
    provider = _hist_provider(monkeypatch, _FakeHistClient({}))

    def _boom(dump_kind, cache_prefix):
        raise fc.FuyaoError("dump 下载网络失败")

    provider._ensure_dump = _boom  # type: ignore[assignment]
    df = provider.get_adj_factors(["600519.SH"], None, None)
    assert df.is_empty() and df.columns == _ADJ_OUT_COLS


def test_adj_progress_callback_per_symbol(monkeypatch):
    events = [
        ("000001.SZ", date(2026, 6, 12), 0.36, 0.0, 0.0, 0.0),
        ("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0),
    ]
    bars = {
        "600519.SH": [_bar(date(2026, 6, 11), 32.8), _bar(date(2026, 6, 12), 32.0)],
        "000001.SZ": [_bar(date(2026, 6, 11), 11.7), _bar(date(2026, 6, 12), 11.5)],
    }
    provider = _adj_provider(monkeypatch, events, bars)
    progress = []
    provider.get_adj_factors(
        ["600519.SH", "000001.SZ"], None, None, on_chunk_done=lambda c, t: progress.append((c, t))
    )
    assert progress == [(1, 2), (2, 2)]


# ---- adj_factor: 本地 dump 配价(2026-08 优化) ----


def test_adj_closes_from_local_dump_no_http(monkeypatch, tmp_path):
    """前收盘从本地日K dump 一次取齐: 零单标的请求, 因子值与接口路径同公式。"""
    events = [("000001.SZ", date(2026, 6, 12), 0.3, 0.0, 0.0, 0.0)]
    big_rows = [
        _dump_bar("000001.SZ", date(2026, 5, 10), 10.2),  # 覆盖窗口起点(first_ex-30d)
        _dump_bar("000001.SZ", date(2026, 6, 11), 10.625),
        _dump_bar("000001.SZ", date(2026, 6, 12), 9.6),
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows)
    provider._dump_memo[fp._ADJ_DUMP_KIND] = _adj_dump(events)
    progress = []
    df = provider.get_adj_factors(
        ["000001.SZ"],
        datetime(2026, 1, 1),
        datetime(2026, 12, 31),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert provider._get_client().calls == []  # 零 HTTP
    assert progress == [(1, 1)]
    assert df.height == 1
    assert df["trade_date"][0] == date(2026, 6, 12)
    assert df["ex_factor"][0] == pytest.approx(10.625 / fp._ref_price(10.625, 0.3, 0.0, 0.0, 0.0))


def test_adj_missing_symbol_in_dump_falls_back_to_http(monkeypatch, tmp_path):
    """dump 缺价的标的回退单标的接口; dump 内标的仍零请求。"""
    events = [
        ("000001.SZ", date(2026, 6, 12), 0.3, 0.0, 0.0, 0.0),
        ("600519.SH", date(2026, 6, 12), 1.0, 0.0, 0.0, 0.0),
    ]
    big_rows = [
        _dump_bar("000001.SZ", date(2026, 5, 10), 10.2),
        _dump_bar("000001.SZ", date(2026, 6, 11), 10.625),
        _dump_bar("000001.SZ", date(2026, 6, 12), 9.6),
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows)
    provider._dump_memo[fp._ADJ_DUMP_KIND] = _adj_dump(events)
    provider._get_client().bars["600519.SH"] = [
        _bar(date(2026, 6, 11), 1500.0),
        _bar(date(2026, 6, 12), 1400.0),
    ]
    df = provider.get_adj_factors(
        ["000001.SZ", "600519.SH"], datetime(2026, 1, 1), datetime(2026, 12, 31)
    )
    assert [c["thscode"] for c in provider._get_client().calls] == ["600519.SH"]
    assert set(df["symbol"].to_list()) == {"000001.SZ", "600519.SH"}


def test_adj_ancient_event_does_not_drag_universe_to_http(monkeypatch, tmp_path):
    """一只远古事件(早于 dump 覆盖起点)只拖累自己; dump 覆盖内的标的零请求。

    回归: 配价曾是"全有或全无" — 全市场最早事件早于 dump 起点时整轮放弃
    dump, 全部标的逐只回补历史K线 (全市场一次 ~15 分钟)。
    """
    events = [
        ("000001.SZ", date(2026, 6, 12), 0.3, 0.0, 0.0, 0.0),
        ("600519.SH", date(2005, 6, 30), 1.0, 0.0, 0.0, 0.0),  # 早于 dump 起点
    ]
    big_rows = [
        _dump_bar("000001.SZ", date(2026, 5, 10), 10.2),
        _dump_bar("000001.SZ", date(2026, 6, 11), 10.625),
        _dump_bar("000001.SZ", date(2026, 6, 12), 9.6),
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows)
    provider._dump_memo[fp._ADJ_DUMP_KIND] = _adj_dump(events)
    provider._get_client().bars["600519.SH"] = [
        _bar(date(2005, 6, 29), 50.0),
        _bar(date(2005, 6, 30), 45.0),
    ]
    df = provider.get_adj_factors(["000001.SZ", "600519.SH"], None, None)
    assert [c["thscode"] for c in provider._get_client().calls] == ["600519.SH"]
    assert "000001.SZ" in set(df["symbol"].to_list())  # dump 路径产出未被连累


def test_adj_ten_day_dump_overlays_fresh_ex_close(monkeypatch, tmp_path):
    """大 dump 缺除权日收盘(发布滞后)时由 10d dump 叠加补, 涨跌停自检仍生效。"""
    events = [("000001.SZ", date(2026, 8, 28), 8.0, 0.0, 0.0, 0.0)]  # 异常大额分红
    big_rows = [
        _dump_bar("000001.SZ", date(2026, 7, 25), 10.0),  # 覆盖窗口起点
        _dump_bar("000001.SZ", date(2026, 8, 27), 10.625),  # 大 dump 只到除权前一日
    ]
    ten_rows = [_dump_bar("000001.SZ", date(2026, 8, 28), 4.05)]  # +54% 超涨跌停
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows, ten_memo=_daily10_dump(ten_rows))
    provider._dump_memo[fp._ADJ_DUMP_KIND] = _adj_dump(events)
    df = provider.get_adj_factors(["000001.SZ"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df.is_empty()  # 自检剔除(无叠加时 ex 日收盘缺失, 该行会被保留)


def test_adj_local_pricing_corrupt_dump_falls_back(monkeypatch, tmp_path):
    """本地配价读盘异常不致命: 整轮回退单标的接口。"""
    events = [("000001.SZ", date(2026, 6, 12), 0.3, 0.0, 0.0, 0.0)]
    big_rows = [
        _dump_bar("000001.SZ", date(2026, 5, 10), 10.2),
        _dump_bar("000001.SZ", date(2026, 6, 11), 10.625),
        _dump_bar("000001.SZ", date(2026, 6, 12), 9.6),
    ]
    provider = _bigdump_provider(monkeypatch, tmp_path, big_rows)
    provider._dump_memo[fp._ADJ_DUMP_KIND] = _adj_dump(events)

    def _boom(_lf):  # 签名对齐被替换的 _closes_scan
        raise OSError("disk error")

    provider._closes_scan = _boom  # type: ignore[assignment]
    provider._get_client().bars["000001.SZ"] = [
        _bar(date(2026, 6, 11), 10.625),
        _bar(date(2026, 6, 12), 9.6),
    ]
    df = provider.get_adj_factors(["000001.SZ"], datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert df.height == 1
    assert provider._get_client().calls  # 回退了接口


# ---- 客户端: 历史日K / dump ----


class _RecordingHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        resp = type("R", (), {"json": lambda self_: self.payload, "status_code": 200})()
        return resp

    def close(self):
        pass


def test_client_historical_kline_sends_params(monkeypatch):
    http = _RecordingHttp({"code": 0, "data": {"item": [_bar(date(2026, 8, 28), 11.65)]}})
    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: http)
    c = fc.FuyaoClient(api_key="k")
    rows = c.historical_kline("000001.SZ", 1, 2, adjust="none")
    assert len(rows) == 1
    path, params = http.calls[0]
    assert path == "/api/a-share/prices/historical"
    assert params == {
        "thscode": "000001.SZ",
        "interval": "1d",
        "adjust": "none",
        "start": 1,
        "end": 2,
    }


def test_client_historical_kline_missing_item(monkeypatch):
    http = _RecordingHttp({"code": 0, "data": {}})
    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: http)
    c = fc.FuyaoClient(api_key="k")
    assert c.historical_kline("000001.SZ", 1, 2) == []


def test_client_dump_download_url(monkeypatch):
    info = {
        "presigned_url": "https://o.thsi.cn/.../releases/20260828/x.parquet?sig=1",
        "presigned_url_expires_at": "2026-08-29T12:00:00+08:00",
        "expires_in_seconds": 300,
    }
    http = _RecordingHttp({"code": 0, "data": info})
    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: http)
    c = fc.FuyaoClient(api_key="k")
    assert c.dump_download_url("adjustment-factors") == info
    assert http.calls[0][0] == "/api/dump/market-dumps/adjustment-factors/download-url"


def test_client_download_dump_atomic_and_key_never_leaked(monkeypatch, tmp_path):
    """/S3 预签名下载不带 X-api-key; 成功后原子改名, .part 清理。"""
    http = _RecordingHttp(
        {
            "code": 0,
            "data": {
                "presigned_url": "https://o.thsi.cn/fuyao/market-dump/adj/releases/20260828/a.parquet?sig=1"
            },
        }
    )
    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: http)

    captured: dict = {}

    class _Resp:
        status_code = 200

        def iter_bytes(self, n):
            yield b"parquet-bytes-"

    class _Ctx:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    def _stream(method, url, **kw):
        captured["method"], captured["url"], captured["kw"] = method, url, kw
        return _Ctx()

    monkeypatch.setattr(fc.httpx, "stream", _stream)
    dest = tmp_path / "adj__20260828.parquet"
    fc.FuyaoClient(api_key="k").download_dump("adjustment-factors", dest)
    assert captured["method"] == "GET" and "o.thsi.cn" in captured["url"]
    assert "headers" not in captured["kw"] or "X-api-key" not in (
        captured["kw"].get("headers") or {}
    )
    assert dest.read_bytes() == b"parquet-bytes-"
    assert not list(tmp_path.glob("*.part"))


def test_client_download_dump_http_error(monkeypatch, tmp_path):
    http = _RecordingHttp({"code": 0, "data": {"presigned_url": "https://o.thsi.cn/x.parquet"}})
    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: http)

    class _Resp:
        status_code = 403

    class _Ctx:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fc.httpx, "stream", lambda m, u, **kw: _Ctx())
    dest = tmp_path / "x.parquet"
    with pytest.raises(fc.FuyaoError, match="403"):
        fc.FuyaoClient(api_key="k").download_dump("adjustment-factors", dest)
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


# ---- 时区与 release 解析 ----


def test_shanghai_midnight_ms_roundtrip():
    """上海零点戳 ↔ 日期: +8h 换算不得偏移一天。"""
    d = date(2026, 6, 12)
    assert fp._date_of_ms(_sh_ms(d)) == d
    assert fp._ms_of_date(d) == _sh_ms(d)


def test_release_of_extracts_from_presigned_url():
    url = "https://o.thsi.cn/x/market-dump/adj/releases/20260828/a.parquet?sig=1"
    assert fp._release_of(url) == "20260828"
    assert fp._release_of("https://no-release-here/x") == "unknown"


# ---- 设置页试拉: daily / adj_factor ----


def test_test_dataset_daily_preview(monkeypatch):
    # 实现窗口为 now-30天..now, 数据日期必须相对今天生成; 硬编码日期会随时间
    # 滑出窗口 (2026-09-27 起原硬编码 8/27 的版本必然失败)。
    d1 = date.today() - timedelta(days=2)
    d2 = date.today() - timedelta(days=1)
    bars = {"000001.SZ": [_bar(d1, 11.05), _bar(d2, 11.65)]}
    provider = _hist_provider(monkeypatch, _FakeHistClient(bars))
    out = provider.test_dataset("daily", ["000001.SZ"])
    assert out["provider"] == "fuyao" and out["dataset"] == "daily"
    assert out["rows"] == 2
    assert out["preview"][0]["date"] == d1.isoformat()  # date → ISO 字符串


def test_test_dataset_adj_factor_preview(monkeypatch):
    events = [("600519.SH", date(2026, 6, 12), 0.68, 0.0, 0.0, 0.0)]
    bars = {"600519.SH": [_bar(date(2026, 6, 11), 32.8), _bar(date(2026, 6, 12), 32.0)]}
    provider = _adj_provider(monkeypatch, events, bars)
    out = provider.test_dataset("adj_factor", ["600519.SH"])
    assert out["rows"] == 1
    assert out["preview"][0]["trade_date"] == "2026-06-12"
    assert out["preview"][0]["ex_factor"] == pytest.approx(32.8 / 32.12)
