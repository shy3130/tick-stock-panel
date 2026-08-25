"""EltdxProvider 归一化与桥接契约测试。

不依赖真实 eltdx / 网络: monkeypatch bridge 模块级函数返回样例 payload,
只验证 Python 侧的代码转换、单位转换、除权因子事件换算、区间裁剪、
空结果降级与注册接线。真实环境 smoke 由 eltdx-smoke 手动执行。
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from app.plugins.eltdx import bridge
from app.plugins.eltdx import provider as ep
from app.plugins.eltdx.provider import EltdxProvider

# ---- 代码转换 ----


def test_symbol_conversion_roundtrip():
    assert ep.to_tdx("000001.SZ") == "sz000001"
    assert ep.to_tdx("600519.SH") == "sh600519"
    assert ep.to_tdx("430047.BJ") == "bj430047"
    assert ep.from_tdx("sz000001") == "000001.SZ"
    assert ep.from_tdx("sh600000") == "600000.SH"
    assert ep.from_tdx("bj430047") == "430047.BJ"


# ---- 单位与时间解析 ----


def test_bars_to_rows_converts_share_volume_to_hand():
    bars = [
        {
            "time": "2026-08-20T15:00:00+08:00",
            "open": 11.1,
            "high": 11.3,
            "low": 11.0,
            "close": 11.2,
            "volume_lots": 808929.68,
            "amount": 896326720.0,
        }
    ]
    rows = ep._bars_to_rows(bars, "000001.SZ")
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "000001.SZ"
    assert r["time"] == dt.datetime(2026, 8, 20, 15, 0)  # naive 北京时间墙钟
    assert r["volume"] == 808929.68  # volume_lots 本身即手(与 tickflow canonical 对齐)
    assert r["amount"] == 896326720.0  # 元


def test_snapshot_rows_hand_volume():
    snaps = [
        {
            "exchange": "sz",
            "code": "000001",
            "last_price": 11.38,
            "pre_close_price": 11.41,
            "open_price": 11.38,
            "high_price": 11.38,
            "low_price": 11.38,
            "total_hand": 3612,
            "amount": 4110456.0,
        }
    ]
    rows = ep._snapshot_rows(snaps)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "000001.SZ"
    assert r["last_price"] == 11.38
    assert r["prev_close"] == 11.41
    assert r["volume"] == 3612  # total_hand 即手数(与 tickflow quotes.volume 同口径)
    assert r["amount"] == 4110456.0


def test_parse_naive_time_converts_utc_to_beijing():
    # eltdx 实测返回 +08:00; 若遇 UTC 等时区, 应统一转北京时间墙钟
    assert ep._parse_naive_time("2026-08-20T07:00:00Z") == dt.datetime(2026, 8, 20, 15, 0)
    assert ep._parse_naive_time("2026-08-20T15:00:00+08:00") == dt.datetime(2026, 8, 20, 15, 0)
    assert ep._parse_naive_time("bad-time") is None
    assert ep._parse_naive_time(None) is None


# ---- 除权因子: 逐日 qfq 因子 → 事件日比值 ----


def test_factor_rows_event_ratio():
    # qfq 累积因子: 前两日无事件, 第 3 日跳变 2 倍(送转), 后续平稳
    items = [
        {"time": "2026-01-05T15:00:00+08:00", "qfq_factor": 1.0},
        {"time": "2026-01-06T15:00:00+08:00", "qfq_factor": 1.0},
        {"time": "2026-01-07T15:00:00+08:00", "qfq_factor": 2.0},
        {"time": "2026-01-08T15:00:00+08:00", "qfq_factor": 2.0},
    ]
    rows = ep._factor_rows(items, "000001.SZ")
    assert rows == [
        {"symbol": "000001.SZ", "trade_date": dt.date(2026, 1, 7), "ex_factor": 2.0},
    ]


def test_factor_rows_ignores_invalid_and_flat():
    items = [
        {"time": "2026-01-05T15:00:00+08:00", "qfq_factor": None},
        {"time": "2026-01-06T15:00:00+08:00", "qfq_factor": 0.0},
        {"time": "2026-01-07T15:00:00+08:00", "qfq_factor": 1.5},
        {"time": "2026-01-08T15:00:00+08:00", "qfq_factor": 1.5},
        {"time": "bad-time", "qfq_factor": 3.0},
    ]
    rows = ep._factor_rows(items, "000001.SZ")
    assert rows == []


def test_event_ratio_cumprod_matches_qfq_semantics():
    """项目口径: adjusted = raw x cumprod(事件ex_factor) / total_cumprod。

    用事件序列重建的逐日乘数应等于 qfq_factor(D) / qfq_factor(最新)。
    """
    items = [
        {"time": "2026-01-05T15:00:00+08:00", "qfq_factor": 0.8},
        {"time": "2026-01-06T15:00:00+08:00", "qfq_factor": 0.8},
        {"time": "2026-01-07T15:00:00+08:00", "qfq_factor": 1.6},
        {"time": "2026-01-08T15:00:00+08:00", "qfq_factor": 2.4},
        {"time": "2026-01-09T15:00:00+08:00", "qfq_factor": 2.4},
    ]
    events = ep._factor_rows(items, "000001.SZ")
    ratios = {r["trade_date"]: r["ex_factor"] for r in events}
    total = 1.0
    for r in ratios.values():
        total *= r
    # 事件比值累积 = qfq(最新)/qfq(首日) = 2.4/0.8 = 3.0
    assert abs(total - 3.0) < 1e-9
    # 事件发生在因子跳变日: 01-07 (0.8→1.6, x2) 与 01-08 (1.6→2.4, x1.5)
    assert set(ratios) == {dt.date(2026, 1, 7), dt.date(2026, 1, 8)}
    assert abs(ratios[dt.date(2026, 1, 7)] - 2.0) < 1e-9
    assert abs(ratios[dt.date(2026, 1, 8)] - 1.5) < 1e-9


def test_factor_rows_ignores_bool_factor():
    """bool 是 int 子类, 必须排除否则 True/prev 产生垃圾事件行。"""
    items = [
        {"time": "2026-01-05T15:00:00+08:00", "qfq_factor": 1.0},
        {"time": "2026-01-06T15:00:00+08:00", "qfq_factor": True},
        {"time": "2026-01-07T15:00:00+08:00", "qfq_factor": 2.0},
    ]
    rows = ep._factor_rows(items, "000001.SZ")
    # True 被跳过: prev 仍为 1.0 → 仅 01-07 产生比值 2.0 事件
    assert rows == [
        {"symbol": "000001.SZ", "trade_date": dt.date(2026, 1, 7), "ex_factor": 2.0},
    ]


def test_symbol_whitelist():
    """资产类型 x 代码前缀白名单: 指数/ETF/B股按类型正确接受或拒绝。"""
    assert ep._is_supported_symbol("600519.SH", "stock")
    assert ep._is_supported_symbol("688981.SH", "stock")
    assert ep._is_supported_symbol("000001.SZ", "stock")
    assert ep._is_supported_symbol("301269.SZ", "stock")
    assert ep._is_supported_symbol("430047.BJ", "stock")
    assert ep._is_supported_symbol("920001.BJ", "stock")  # 北交所全量接受
    assert ep._is_supported_symbol("510300.SH", "etf")
    assert ep._is_supported_symbol("520000.SH", "etf")  # 沪市 52 段(实测存在)
    assert ep._is_supported_symbol("159915.SZ", "etf")
    # ETF 代码不在股票白名单; 指数/ B股 同理
    assert not ep._is_supported_symbol("510300.SH", "stock")
    assert not ep._is_supported_symbol("000001.SH", "stock")
    assert not ep._is_supported_symbol("399001.SZ", "stock")
    assert not ep._is_supported_symbol("900901.SH", "stock")
    # index 资产类型一律拒绝(协议不支持)
    assert not ep._is_supported_symbol("000001.SH", "index")
    assert not ep._is_supported_symbol("510300.SH", "index")
    assert not ep._is_supported_symbol("600519.SH", "index")
    # 非法代码
    assert not ep._is_supported_symbol("bad", "stock")


def test_asset_type_gating():
    """asset_type 分流: index 拒绝; adj_factor 仅 stock; 白名单挡指数/ETF 代码。"""
    p = EltdxProvider()
    assert p.get_daily(["000001.SZ"], None, None, asset_type="index").is_empty()
    assert p.get_adj_factors(["000001.SZ"], None, None, asset_type="etf").is_empty()
    assert p.get_adj_factors(["000001.SZ"], None, None, asset_type="index").is_empty()
    assert p.get_minute(["000001.SZ"], None, None, asset_type="index").is_empty()
    # stock 路径下指数/ETF 代码被白名单跳过, 不发起拉取
    assert p.get_daily(["000001.SH", "510300.SH"], None, None).is_empty()
    assert p.get_minute(["399001.SZ"], None, None).is_empty()


def test_daily_passes_since_for_incremental_fetch(monkeypatch):
    """start_time 透传给 bridge.bars_all 的 since, 触发增量分页而非全量。"""
    seen: list[tuple] = []
    monkeypatch.setattr(
        ep.bridge,
        "bars_all",
        lambda code, period, since=None: seen.append((code, period, since)) or [],
    )
    start = dt.datetime(2026, 1, 6)
    EltdxProvider().get_daily(["000001.SZ"], start, dt.datetime(2026, 1, 7))
    assert seen == [("sz000001", "day", start)]


def test_daily_and_clip_accept_date_start(monkeypatch):
    """start_time 为 date(上游从 Parquet Date 列取出)时不报错, 且按当日裁剪。"""
    monkeypatch.setattr(
        ep.bridge,
        "bars_all",
        lambda code, period, since=None: [
            {
                "time": "2026-01-05T15:00:00+08:00",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume_wire_value": 1000.0,
                "amount": 10000.0,
            },
            {
                "time": "2026-01-06T15:00:00+08:00",
                "open": 10.2,
                "high": 10.8,
                "low": 10.1,
                "close": 10.6,
                "volume_wire_value": 1200.0,
                "amount": 12000.0,
            },
        ],
    )
    df = EltdxProvider().get_daily(["000001.SZ"], dt.date(2026, 1, 6), dt.date(2026, 1, 6))
    assert df["date"].to_list() == [dt.date(2026, 1, 6)]


def test_minute_clip_accepts_date_bound(monkeypatch):
    """minute 的 _clip 对 date 型边界按当日 00:00/23:59 裁剪。"""
    df = pl.DataFrame({"datetime": [dt.datetime(2026, 1, 6, 9, 35)]})
    out = ep._clip(df, "datetime", dt.date(2026, 1, 6), dt.date(2026, 1, 6))
    assert out.height == 1
    out2 = ep._clip(df, "datetime", dt.date(2026, 1, 7), None)
    assert out2.is_empty()


def test_bridge_bars_all_accepts_date_since(monkeypatch):
    """bridge.bars_all 的 since 兼容 date 类型(归一化为当日末)。"""
    calls: list[tuple] = []

    class FakeBars:
        def get(self, code, period, start, count):
            calls.append((start, count))
            return {"bars": [{"time": "2026-01-06T15:00:00+08:00"}], "request_count": 1}

    monkeypatch.setattr(ep.bridge, "get_client", lambda: type("C", (), {"bars": FakeBars()})())
    out = ep.bridge.bars_all("sz000001", "day", since=dt.date(2026, 1, 6))
    assert out[0]["time"] == "2026-01-06T15:00:00+08:00"


def test_minute_etf_symbol_allowed(monkeypatch):
    """ETF 分钟线实测可用: asset_type='etf' 且代码命中 ETF 白名单时正常拉取。"""
    seen: list[str] = []
    monkeypatch.setattr(
        ep.bridge,
        "bars_all",
        lambda code, period, since=None: seen.append(code) or [],
    )
    EltdxProvider().get_minute(["510300.SH"], None, None, asset_type="etf")
    assert seen == ["sh510300"]


# ---- provider 数据集 ----


def test_get_daily_normalizes_and_clips(monkeypatch):
    monkeypatch.setattr(
        ep.bridge,
        "bars_all",
        lambda code, period, since=None: [
            {
                "time": "2026-01-05T15:00:00+08:00",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume_wire_value": 1000.0,
                "amount": 10000.0,
            },
            {
                "time": "2026-01-06T15:00:00+08:00",
                "open": 10.2,
                "high": 10.8,
                "low": 10.1,
                "close": 10.6,
                "volume_wire_value": 1200.0,
                "amount": 12000.0,
            },
            {
                "time": "2026-01-07T15:00:00+08:00",
                "open": 10.6,
                "high": 11.0,
                "low": 10.5,
                "close": 10.9,
                "volume_wire_value": 900.0,
                "amount": 9000.0,
            },
        ],
    )
    df = EltdxProvider().get_daily(["000001.SZ"], dt.datetime(2026, 1, 6), dt.datetime(2026, 1, 7))
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.height == 2
    assert df["date"].to_list() == [dt.date(2026, 1, 6), dt.date(2026, 1, 7)]
    assert df.schema["date"] == pl.Date
    assert df.schema["close"] == pl.Float64


def test_get_adj_factors_normalizes(monkeypatch):
    monkeypatch.setattr(
        ep.bridge,
        "factors",
        lambda code: [
            {"time": "2026-01-05T15:00:00+08:00", "qfq_factor": 1.0},
            {"time": "2026-01-06T15:00:00+08:00", "qfq_factor": 2.0},
            {"time": "2026-01-07T15:00:00+08:00", "qfq_factor": 2.0},
        ],
    )
    df = EltdxProvider().get_adj_factors(["000001.SZ"], None, None)
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 1
    assert df.schema["trade_date"] == pl.Date
    assert abs(df["ex_factor"][0] - 2.0) < 1e-9


def test_get_minute_datetime_is_beijing_wall_clock(monkeypatch):
    monkeypatch.setattr(
        ep.bridge,
        "bars_all",
        lambda code, period, since=None: [
            {
                "time": "2026-05-21T09:35:00+08:00",
                "open": 12.0,
                "high": 12.1,
                "low": 11.9,
                "close": 12.05,
                "volume_wire_value": 2740.0,
                "amount": 3.6e6,
            },
        ],
    )
    df = EltdxProvider().get_minute(["000001.SZ"], None, None)
    assert set(df.columns) == {
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }
    ts = df["datetime"][0]
    assert (ts.hour, ts.minute) == (9, 35)
    assert df["symbol"][0] == "000001.SZ"


def test_get_realtime_batches_and_converts(monkeypatch):
    codes = [f"sz{i:06d}" for i in range(85)]  # 85 只 → 两批
    monkeypatch.setattr(ep.bridge, "a_share_codes", lambda: codes)
    calls: list[list[str]] = []

    def fake_snapshots(chunk):
        calls.append(chunk)
        return [
            {
                "exchange": c[:2],
                "code": c[2:],
                "last_price": 10.0,
                "pre_close_price": 9.9,
                "open_price": 10.0,
                "high_price": 10.1,
                "low_price": 9.8,
                "total_hand": 100,
                "amount": 1.0e6,
            }
            for c in chunk
        ]

    monkeypatch.setattr(ep.bridge, "snapshots", fake_snapshots)
    rows = EltdxProvider().get_realtime()
    assert len(rows) == 85
    assert len(calls) == 2 and len(calls[0]) == 80 and len(calls[1]) == 5
    assert rows[0]["symbol"] == "000000.SZ"
    assert rows[0]["volume"] == 100  # total_hand 即手数


def test_get_realtime_snapshot_chunk_failure_degrades(monkeypatch):
    monkeypatch.setattr(ep.bridge, "a_share_codes", lambda: ["sz000001", "sz000002"])

    def boom(chunk):
        raise bridge.EltdxBridgeError("down")

    monkeypatch.setattr(ep.bridge, "snapshots", boom)
    assert EltdxProvider().get_realtime() == []


def test_get_instruments_stock_only(monkeypatch):
    monkeypatch.setattr(ep.bridge, "a_share_codes", lambda: ["sh600519", "sz000001"])
    monkeypatch.setattr(
        ep.bridge, "code_names", lambda: {"sh600519": "贵州茅台", "sz000001": "平安银行"}
    )
    monkeypatch.setattr(
        ep.bridge,
        "share_capitals",
        lambda: {
            "sh600519": {
                "float_shares": 1256197800.0,
                "total_shares": 1256197800.0,
                "ipo_date": "2001-08-27",
            },
            "sz000001": {
                "float_shares": 19405685000.0,
                "total_shares": 19405918750.0,
                "ipo_date": "1991-04-03",
            },
        },
    )
    rows = EltdxProvider().get_instruments("stock")
    assert rows[0] == {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "code": "600519",
        "exchange": "SH",
        "region": "CN",
        "type": "stock",
        "ext": {
            "float_shares": 1256197800.0,
            "total_shares": 1256197800.0,
            "listing_date": dt.date(2001, 8, 27),
        },
    }
    assert len(rows) == 2
    assert rows[1]["ext"]["float_shares"] == 19405685000.0
    assert rows[1]["ext"]["listing_date"] == dt.date(1991, 4, 3)
    # 未覆盖的资产类型降级回退 tickflow
    assert EltdxProvider().get_instruments("etf") == []
    assert EltdxProvider().get_instruments("index") == []


def test_bridge_share_capitals_converts_10k_to_shares(monkeypatch):
    """bridge.share_capitals: 财务快照万股 -> 股(乘以 10000), 按批拉取。"""
    monkeypatch.setattr(ep.bridge, "_share_capitals_cache", None)
    monkeypatch.setattr(ep.bridge, "a_share_codes", lambda: ["sz000001", "sh600519"])
    monkeypatch.setattr(ep.bridge, "_to_jsonable", lambda obj: obj)
    calls: list[list[str]] = []

    def fake_finance_batch(self, chunk):
        calls.append(list(chunk))
        return {
            "records": [
                {
                    "exchange": "sz",
                    "code": "000001",
                    "liu_tong_gu_ben_raw_float": 1940568.5,
                    "zong_gu_ben_raw_float": 1940591.875,
                    "ipo_date": "1991-04-03",
                },
                {
                    "exchange": "sh",
                    "code": "600519",
                    "liu_tong_gu_ben_raw_float": 125619.78,
                    "zong_gu_ben_raw_float": 125619.78,
                    "ipo_date": "2001-08-27",
                },
            ]
        }

    client = type("C", (), {"corporate": type("Co", (), {"finance_batch": fake_finance_batch})()})()
    monkeypatch.setattr(ep.bridge, "get_client", lambda: client)

    caps = ep.bridge.share_capitals()
    assert calls == [["sz000001", "sh600519"]]
    assert caps["sz000001"]["float_shares"] == 19405685000.0
    assert caps["sz000001"]["total_shares"] == 19405918750.0
    assert caps["sz000001"]["ipo_date"] == "1991-04-03"
    assert caps["sh600519"]["float_shares"] == 1256197800.0


def test_partial_failure_logs_warning(monkeypatch, caplog):
    """部分标的失败时聚合一条 WARNING, 成功部分仍返回, 失败标的可见。"""

    def bars_all(code, period, since=None):
        if code == "sz000002":
            raise bridge.EltdxBridgeError("down")
        return [
            {
                "time": "2026-01-05T15:00:00+08:00",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume_wire_value": 1000.0,
                "amount": 10000.0,
            }
        ]

    monkeypatch.setattr(ep.bridge, "bars_all", bars_all)
    with caplog.at_level(logging.WARNING, logger="app.plugins.eltdx.provider"):
        df = EltdxProvider().get_daily(["000001.SZ", "000002.SZ"], None, None)
    assert df.height == 1
    assert df["symbol"].to_list() == ["000001.SZ"]
    assert any("部分失败" in r.message and "000002.SZ" in r.message for r in caplog.records)


def test_empty_symbols_returns_empty():
    p = EltdxProvider()
    assert p.get_daily([], None, None).is_empty()
    assert p.get_adj_factors([], None, None).is_empty()
    assert p.get_minute([], None, None).is_empty()


def test_bridge_error_degrades_to_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise bridge.EltdxBridgeError("down")

    monkeypatch.setattr(ep.bridge, "bars_all", boom)
    monkeypatch.setattr(ep.bridge, "factors", boom)
    monkeypatch.setattr(ep.bridge, "a_share_codes", boom)
    assert EltdxProvider().get_daily(["000001.SZ"], None, None).is_empty()
    assert EltdxProvider().get_adj_factors(["000001.SZ"], None, None).is_empty()
    assert EltdxProvider().get_minute(["000001.SZ"], None, None).is_empty()
    assert EltdxProvider().get_realtime() == []
    assert EltdxProvider().get_instruments("stock") == []


def test_on_chunk_done_progress(monkeypatch):
    monkeypatch.setattr(ep.bridge, "bars_all", lambda code, period, since=None: [])
    progress: list[tuple[int, int]] = []
    EltdxProvider().get_daily(
        ["000001.SZ", "000002.SZ"],
        None,
        None,
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    )
    assert progress == [(1, 2), (2, 2)]


# ---- availability 探活 ----


def test_availability_probe_does_not_touch_singleton(monkeypatch):
    """availability 用独立临时 client 探活: 失败时不创建/关闭共享单例 _client。"""
    import sys
    import types

    fake_eltdx = types.ModuleType("eltdx")
    fake_eltdx.__version__ = "9.9.9"

    class FakeClient:
        def __init__(self, timeout=8.0):
            pass

        def codes(self):
            raise OSError("connection refused")

        def close(self):
            pass

    fake_eltdx.TdxClient = FakeClient
    monkeypatch.setitem(sys.modules, "eltdx", fake_eltdx)
    monkeypatch.setattr(
        bridge,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("availability must not touch singleton")),
    )
    ok, reason = bridge.availability()
    assert ok is False
    assert "连通性检测失败" in reason


# ---- 插件注册接线 ----


def test_plugin_discovered_in_loader():
    """插件被发现并记录状态 (即使依赖没装, 不可用)。"""
    from app.data_providers import custom as cs

    plugins = {p["name"]: p for p in cs.list_plugins()}
    assert "eltdx" in plugins
    assert plugins["eltdx"]["runtime"] == "python"
    assert "daily" in plugins["eltdx"]["datasets"]
    assert "realtime" in plugins["eltdx"]["datasets"]
    assert "financial" not in plugins["eltdx"]["datasets"]
    assert cs.is_builtin("eltdx")
    # 内置源不出现在用户自定义源列表
    assert "eltdx" not in [s["name"] for s in cs.list_sources()]


def test_plugin_registered_when_available(monkeypatch):
    """依赖可用时, 插件注册进 _PROVIDERS 并可路由。"""
    from app.data_providers import custom as cs
    from app.data_providers.custom import loader

    monkeypatch.setattr(loader, "_call_check", lambda ref: (True, "ok"))
    monkeypatch.setattr(loader, "_load_entry", _load_eltdx_entry)
    loader._load_builtin_plugins()

    assert "eltdx" in cs.names()
    assert cs.is_custom_provider("eltdx")
    assert cs.provider_has_dataset("eltdx", "daily")
    assert cs.provider_has_dataset("eltdx", "realtime")
    assert not cs.provider_has_dataset("eltdx", "financial")


def _load_eltdx_entry(entry_ref: str):
    """测试用: 无条件加载 eltdx provider 类 (跳过 check)。"""
    if "EltdxProvider" in entry_ref:
        return EltdxProvider
    if "availability" in entry_ref:
        return lambda: (True, "ok")
    raise ValueError(f"unexpected entry: {entry_ref}")
