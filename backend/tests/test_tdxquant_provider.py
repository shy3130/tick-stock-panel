"""TdxQuantProvider 契约与单位标准化测试。

不依赖真实网络与 tdxaidata 安装: 用假 TdxQuantClient 返回样例快照/K线/除权事件,
验证字段映射、单位口径 (CONTRIBUTING §3: 万元→元、股→手、每10股→每股、
小数制涨跌幅)、lib 物化与 token 注入、重试语义、能力声明与设置页试拉。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.plugins.tdxquant import client as tc
from app.plugins.tdxquant import provider as tp
from app.plugins.tdxquant.provider import TdxQuantProvider

_BEIJING = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------- fakes
class _FakeClient:
    """按测试需要预置返回值; 记录调用供批大小/参数断言。"""

    def __init__(self):
        self.universe: list[str] = []
        self.universe_error: Exception | None = None
        self.snapshot_results: list[object] = []  # 每次 snapshot_batch 弹出一个(dict 或 Exception)
        self.snapshot_calls: list[list[str]] = []
        self.his: dict[str, object] = {}  # code -> raw dict | Exception
        self.his_default: object = {}
        self.his_calls: list[dict] = []
        self.events: dict[str, object] = {}  # code -> list[dict] | Exception
        self.event_calls: list[dict] = []

    def close(self) -> None:
        pass

    def stock_universe(self) -> list[str]:
        if self.universe_error:
            raise self.universe_error
        return list(self.universe)

    def snapshot_batch(self, codes):
        self.snapshot_calls.append(list(codes))
        if not self.snapshot_results:
            return {}
        item = self.snapshot_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def his_dats(self, code, end_d, count, period="1d"):
        self.his_calls.append({"code": code, "end": end_d, "count": count, "period": period})
        item = self.his.get(code, self.his_default)
        if isinstance(item, Exception):
            raise item
        return item

    def divid_factors(self, code, start_d=None, end_d=None):
        self.event_calls.append({"code": code, "start": start_d, "end": end_d})
        item = self.events.get(code, [])
        if isinstance(item, Exception):
            raise item
        return item


def _provider(fake: _FakeClient) -> TdxQuantProvider:
    p = TdxQuantProvider()
    p._client = fake
    return p


def _snap_row(**over) -> dict:
    row = {
        "Now": "1263.00", "LastClose": "1255.79", "Open": "1255.42",
        "Max": "1282.00", "Min": "1255.42", "Volume": "35110",
        "Amount": "445322.34", "RefreshTime": "153017",
        "Buyp": ["1263.00", "1262.99"], "Buyv": ["73", "2"],
        "Sellp": ["1263.06", "1263.11"], "Sellv": ["1", "3"],
    }
    row.update(over)
    return row


# ---------------------------------------------------------------- client: lib 物化
def _make_source_lib(root: Path) -> Path:
    src = root / "src_lib"
    src.mkdir()
    (src / "TdxAiData.dll").write_bytes(b"FAKE_DLL_BYTES")
    (src / "libTdxAiData.so").write_bytes(b"FAKE_SO_BYTES")
    (src / "libTdxAiData.dylib").write_bytes(b"FAKE_DYLIB_BYTES")  # macOS
    (src / "TdxAiData.ini").write_text(
        "[TcHqHost]\nHostNum=2\nPrimaryHost=0\n\n[Token]\ntoken=OLD\n", encoding="utf-8"
    )
    (src / "user.ini").write_text("[Token]\ntoken=OLD\n", encoding="utf-8")
    return src


def test_materialize_lib_writes_token_into_both_ini(tmp_path):
    src = _make_source_lib(tmp_path)
    lib_root = tmp_path / "tdxquant_lib"
    dll = tc.materialize_lib("TDX-NEW-KEY", lib_root, source=src)
    assert dll.name == tc.LIB_DLL_NAME
    assert (lib_root / tc.LIB_DLL_NAME).exists()
    for ini_name in ("TdxAiData.ini", "user.ini"):
        text = (lib_root / ini_name).read_text(encoding="utf-8")
        assert "token=TDX-NEW-KEY" in text
        assert "token=OLD" not in text
    # 其它配置段保留
    assert "[TcHqHost]" in (lib_root / "TdxAiData.ini").read_text(encoding="utf-8")
    assert "HostNum=2" in (lib_root / "TdxAiData.ini").read_text(encoding="utf-8")


def test_materialize_lib_marker_skips_recopy(tmp_path, monkeypatch):
    src = _make_source_lib(tmp_path)
    lib_root = tmp_path / "tdxquant_lib"
    tc.materialize_lib("TDX-KEY-A", lib_root, source=src)

    copies = []
    real_copy = tc.shutil.copy2

    def _spy(src_p, dst_p):
        copies.append(str(src_p))
        return real_copy(src_p, dst_p)

    monkeypatch.setattr(tc.shutil, "copy2", _spy)
    tc.materialize_lib("TDX-KEY-A", lib_root, source=src)  # 同 Key 同 DLL → 复用
    assert copies == []
    tc.materialize_lib("TDX-KEY-B", lib_root, source=src)  # 换 Key → 重物化
    assert copies
    assert "token=TDX-KEY-B" in (lib_root / "user.ini").read_text(encoding="utf-8")


def test_write_token_ini_adds_missing_section(tmp_path):
    ini = tmp_path / "user.ini"
    ini.write_text("[Other]\nfoo=1\n", encoding="utf-8")
    tc.write_token_ini(ini, "K1")
    text = ini.read_text(encoding="utf-8")
    assert "token=K1" in text and "[Other]" in text and "foo=1" in text


# ---------------------------------------------------------------- client: 重试语义
def _client_with_raw(monkeypatch, returns: list[object], sleep_log: list[float]):
    c = object.__new__(tc.TdxQuantClient)
    c.api_key = "k"
    state = {"i": 0}

    def _raw_value(_method, *args):
        item = returns[min(state["i"], len(returns) - 1)]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    c._tqs = SimpleNamespace(_clean=lambda raw: raw)
    c._native_cls = SimpleNamespace(get_instance=lambda: SimpleNamespace(get_his_dats=_raw_value))
    c._universe, c._universe_at = [], 0.0
    monkeypatch.setattr(tc.time, "sleep", lambda s: sleep_log.append(s))
    return c


def test_raw_retries_transient_exception(monkeypatch):
    sleeps: list[float] = []
    c = _client_with_raw(
        monkeypatch,
        [RuntimeError("Invalid server response"), {"Date": ["20260101"]}],
        sleeps,
    )
    out = c.his_dats("600519.SH", date(2026, 1, 10), 5)
    assert out["Date"] == ["20260101"]
    assert sleeps  # 失败后有退避


def test_raw_gives_up_on_persistent_none(monkeypatch):
    sleeps: list[float] = []
    c = _client_with_raw(monkeypatch, [None, None], sleeps)
    with pytest.raises(tc.TdxQuantError):
        c.his_dats("600519.SH", date(2026, 1, 10), 5)


def test_snapshot_batch_retries_transient_failure(monkeypatch):
    sleeps: list[float] = []
    c = _client_with_raw(monkeypatch, [], sleeps)
    state = {"i": 0}

    def _snap(stock_list=None, field_list=None):
        state["i"] += 1
        if state["i"] == 1:
            raise RuntimeError("Invalid server response")
        return {"600519.SH": {"Now": "10.0"}}

    c._tqs = type("T", (), {"get_market_snapshot_batch": staticmethod(_snap)})()
    out = c.snapshot_batch(["600519.SH"])
    assert out == {"600519.SH": {"Now": "10.0"}}
    assert state["i"] == 2 and sleeps


def test_raw_retries_none_then_data(monkeypatch):
    sleeps: list[float] = []
    c = _client_with_raw(monkeypatch, [None, {"Date": ["20260101"]}], sleeps)
    assert c.his_dats("600519.SH", date(2026, 1, 10), 5)["Date"] == ["20260101"]


# ---------------------------------------------------------------- provider: 快照映射
class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 11, 15, 0, 0, tzinfo=tz)


def _map_row(monkeypatch, **over) -> dict:
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    return tp._map_snapshot_row("600519.SH", _snap_row(**over), 999)


def test_snapshot_row_units(monkeypatch):
    rec = _map_row(monkeypatch)
    assert rec["symbol"] == "600519.SH"
    assert rec["last_price"] == 1263.00
    assert rec["prev_close"] == 1255.79
    assert rec["high"] == 1282.00 and rec["low"] == 1255.42  # Max/Min → high/low
    assert rec["volume"] == 35110  # 快照已为手, 零换算
    assert rec["amount"] == pytest.approx(445322.34 * 10_000)  # 万元 → 元
    # 涨跌幅小数制: (1263.00-1255.79)/1255.79
    assert rec["change_pct"] == pytest.approx((1263.00 - 1255.79) / 1255.79)
    assert rec["change_amount"] == pytest.approx(1263.00 - 1255.79)
    assert rec["name"] is None and rec["turnover_rate"] is None  # 不伪造
    # RefreshTime 北京墙钟 → epoch ms
    expect = datetime(2026, 10, 11, 15, 30, 17, tzinfo=_BEIJING)
    assert rec["timestamp"] == int(expect.timestamp() * 1000)


def test_snapshot_row_bad_refresh_time_falls_back(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    rec = tp._map_snapshot_row("600519.SH", _snap_row(RefreshTime="99:99"), 123456)
    assert rec["timestamp"] == 123456


def test_compact_date_accepts_iso_and_compact():
    assert tp._compact_date("20260924") == date(2026, 9, 24)
    assert tp._compact_date("2024-12-20 00:00:00") == date(2024, 12, 20)  # DataFrame 归一形态
    assert tp._compact_date(None) is None
    assert tp._compact_date("bad") is None


def test_snapshot_row_dropped_without_prices(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    assert tp._map_snapshot_row("600519.SH", _snap_row(Now=None, LastClose=None), 1) is None


# ---------------------------------------------------------------- provider: realtime
def test_realtime_batches_and_merges(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    fake = _FakeClient()
    fake.universe = [f"60000{i}.SH" for i in range(tp._SNAPSHOT_BATCH)] + ["000001.SZ", "000002.SZ"]
    fake.snapshot_results = [
        {"600000.SH": _snap_row()},
        {"000001.SZ": _snap_row(Now="11.59", LastClose="11.78")},
    ]
    records = _provider(fake).get_realtime()
    assert [len(c) for c in fake.snapshot_calls] == [tp._SNAPSHOT_BATCH, 2]  # 600 只/批
    assert len(records) == 2
    assert {r["symbol"] for r in records} == {"600000.SH", "000001.SZ"}


def test_realtime_soft_fail_on_universe_error():
    fake = _FakeClient()
    fake.universe_error = tc.TdxQuantError("get_stock_list(上证主板): boom")
    assert _provider(fake).get_realtime() == []


def test_realtime_isolates_failed_batch(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    fake = _FakeClient()
    fake.universe = [f"6000{i:03d}.SH" for i in range(tp._SNAPSHOT_BATCH)] + ["000001.SZ"]
    fake.snapshot_results = [
        tc.TdxQuantError("snapshot_batch(600 只): invalid"),  # 首批失败被隔离
        {"000001.SZ": _snap_row(Now="11.59", LastClose="11.78")},
    ]
    records = _provider(fake).get_realtime()
    assert [r["symbol"] for r in records] == ["000001.SZ"]


def test_realtime_indices_maps_sh_index_code(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    fake = _FakeClient()
    fake.snapshot_results = [{"999999.SH": _snap_row(Now="3813.79", LastClose="3811.90")}]
    records = _provider(fake).get_realtime_indices(["000001.SH", "399001.SZ"])
    assert fake.snapshot_calls == [["999999.SH", "399001.SZ"]]  # 000001.SH → 999999.SH
    assert records and records[0]["last_price"] == 3813.79


def test_realtime_indices_none_on_failure():
    fake = _FakeClient()
    fake.snapshot_results = [tc.TdxQuantError("snapshot_batch(1 只): down")]
    assert _provider(fake).get_realtime_indices(["000001.SH"]) is None


# ---------------------------------------------------------------- provider: daily
def _his_raw(*, dates=("20260102", "20260105"), volume="3123935.00", amount="386731.09") -> dict:
    return {
        "Date": list(dates), "Time": ["0"] * len(dates),
        "Open": ["10.0"] * len(dates), "High": ["11.0"] * len(dates),
        "Low": ["9.5"] * len(dates), "Close": ["10.5"] * len(dates),
        "Volume": [volume] * len(dates), "Amount": [amount] * len(dates),
    }


def test_daily_units_and_window_filter():
    fake = _FakeClient()
    fake.his["600519.SH"] = _his_raw(dates=("20251231", "20260102", "20260105", "20260106"))
    df = _provider(fake).get_daily(
        ["600519.SH"],
        datetime(2026, 1, 1), datetime(2026, 1, 5),
    )
    assert df["date"].to_list() == [date(2026, 1, 2), date(2026, 1, 5)]  # 窗口过滤
    row = df.row(0, named=True)
    assert row["close"] == 10.5
    assert row["volume"] == math.floor(3123935.0 / 100)  # 股 → 手
    assert row["amount"] == pytest.approx(386731.09 * 10_000)  # 万元 → 元
    call = fake.his_calls[0]
    assert call["period"] == "1d"
    assert call["count"] >= 30  # count 模式根数下限


def test_daily_etf_asset_type_unsupported_reports_empty():
    fake = _FakeClient()
    chunks = list(
        _provider(fake).iter_daily(
            ["510300.SH"], datetime(2026, 1, 1), datetime(2026, 1, 31), asset_type="etf"
        )
    )
    assert chunks == []


def test_daily_symbol_error_isolated_but_iteration_continues():
    fake = _FakeClient()
    fake.his["600000.SH"] = tc.TdxQuantError("get_his_dats: invalid")
    fake.his["600001.SH"] = _his_raw()
    done: list[tuple[int, int]] = []
    df = _provider(fake).get_daily(
        ["600000.SH", "600001.SH"], datetime(2025, 12, 1), datetime(2026, 1, 31),
        on_chunk_done=lambda cur, total: done.append((cur, total)),
    )
    assert df.height == 2  # 只有 600001 的两根
    assert done == [(1, 2), (2, 2)]  # 空批次也推进度


# ---------------------------------------------------------------- provider: adj_factor
def _closes_raw(pairs: list[tuple[str, float]]) -> dict:
    return {
        "Date": [d for d, _ in pairs], "Close": [str(c) for _, c in pairs],
        "Open": ["1"] * len(pairs), "High": ["1"] * len(pairs), "Low": ["1"] * len(pairs),
        "Volume": ["100"] * len(pairs), "Amount": ["1"] * len(pairs),
    }


def test_adj_factor_per10_to_per_share_and_formula():
    # 10派30.876元 → 每股 D=3.0876; P=30.876 → ref=(30.876-3.0876)/1=27.7884 → 27.79
    fake = _FakeClient()
    fake.events["600519.SH"] = [
        {"Date": "20240619", "Type": "1", "Bonus": 30.876, "AllotPrice": 0.0,
         "ShareBonus": 0.0, "Allotment": 0.0},
    ]
    fake.his["600519.SH"] = _closes_raw([("20240618", 30.876), ("20240619", 28.0), ("20240620", 28.5)])
    df = _provider(fake).get_adj_factors(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 12, 31)
    )
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["trade_date"] == date(2024, 6, 19)
    assert row["dividend"] == pytest.approx(3.0876)  # 每 10 股 → 每股
    assert row["prev_close"] == 30.876
    ref = math.floor((30.876 - 3.0876) * 100 + 0.5) / 100  # half-up 两位
    assert row["ex_factor"] == pytest.approx(30.876 / ref)
    # 涨跌停自检: 复权后除权日涨跌 28.0/(30.876/factor)-1 ≈ 0.1% 在板限内 → 保留


def test_adj_factor_limit_check_drops_bad_factor():
    # 异常事件: 分红与价格严重不匹配 → 复权后除权日涨跌幅超板限 → 自检剔除
    fake = _FakeClient()
    fake.events["600519.SH"] = [
        {"Date": "20240619", "Type": "1", "Bonus": 200.0, "AllotPrice": 0.0,
         "ShareBonus": 0.0, "Allotment": 0.0},
    ]
    fake.his["600519.SH"] = _closes_raw([("20240618", 30.876), ("20240619", 28.0)])
    df = _provider(fake).get_adj_factors(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 12, 31)
    )
    assert df.is_empty()


def test_adj_factor_future_event_without_prev_close_skipped():
    fake = _FakeClient()
    fake.events["600519.SH"] = [
        {"Date": "20991231", "Type": "1", "Bonus": 30.876, "AllotPrice": 0.0,
         "ShareBonus": 0.0, "Allotment": 0.0},
    ]
    fake.his["600519.SH"] = _closes_raw([("20240618", 30.876)])
    df = _provider(fake).get_adj_factors(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2026, 12, 31)
    )
    assert df.is_empty()  # 无除权日前收盘 → 留给滚动增量


def test_adj_factor_schema_columns():
    # 真实量级事件: P=10, 10派2 + 10送1 + 10配2(配股价5) → 参考价 8.31, 除权日收盘 8.35
    fake = _FakeClient()
    fake.events["600000.SH"] = [
        {"Date": "20240619", "Type": "1", "Bonus": 2.0, "AllotPrice": 5.0,
         "ShareBonus": 1.0, "Allotment": 2.0},
    ]
    fake.his["600000.SH"] = _closes_raw([("20240618", 10.0), ("20240619", 8.35)])
    df = _provider(fake).get_adj_factors(
        ["600000.SH"], datetime(2024, 1, 1), datetime(2024, 12, 31)
    )
    assert df.columns == list(tp._ADJ_SCHEMA)
    row = df.row(0, named=True)
    assert row["dividend"] == pytest.approx(0.2)  # 10派2 → 每股 0.2
    assert row["bonus"] == pytest.approx(0.1)  # 10送1 → 每股 0.1
    assert row["allot"] == pytest.approx(0.2)  # 10配2 → 每股 0.2
    assert row["allot_price"] == 5.0


# ---------------------------------------------------------------- provider: depth5
def test_depth_batch_maps_five_levels(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    fake = _FakeClient()
    fake.snapshot_results = [{
        "600519.SH": _snap_row(),
        "600000.SH": _snap_row(Buyp=None, Sellp=None),  # 无五档 → 整标的省略
    }]
    out = _provider(fake).get_depth_batch(["600519.SH", "600000.SH"])
    assert set(out) == {"600519.SH"}
    entry = out["600519.SH"]
    assert entry["bid_prices"] == [1263.00, 1262.99]
    assert entry["bid_volumes"] == [73, 2]  # 手, 透传为 int
    assert entry["ask_prices"] == [1263.06, 1263.11]
    assert entry["ask_volumes"] == [1, 3]
    expect = datetime(2026, 10, 11, 15, 30, 17, tzinfo=_BEIJING)
    assert entry["timestamp"] == int(expect.timestamp() * 1000)


# ---------------------------------------------------------------- availability / 能力
def test_availability_missing_module(monkeypatch):
    monkeypatch.setattr(tp, "_module_available", lambda: False)
    ok, reason = tp.availability()
    assert not ok and "tdxaidata" in reason


def test_availability_missing_key(monkeypatch):
    monkeypatch.setattr(tp, "_module_available", lambda: True)
    monkeypatch.setattr(tp, "get_api_key", lambda: "")
    ok, reason = tp.availability()
    assert not ok and tp.API_KEY_ENV in reason


def test_availability_ok(monkeypatch):
    monkeypatch.setattr(tp, "_module_available", lambda: True)
    monkeypatch.setattr(tp, "get_api_key", lambda: "TDX-xxx")
    assert tp.availability() == (True, "ok")


def test_datasets_declaration():
    p = TdxQuantProvider()
    assert set(p.config.datasets) == {"realtime", "depth5", "daily", "adj_factor", "minute", "full_minute"}
    assert p.name == "tdxquant" and p.config.builtin is True
    for absent in ("financial",):
        assert absent not in p.config.datasets  # 未声明 → provider_has_dataset False → 回退 tickflow


# ---------------------------------------------------------------- minute / full_minute
def _minute_raw(rows: list[tuple[str, str, float, float, float, float, float, float]]) -> dict:
    """(Date, Time, O, H, L, C, Volume股, Amount万元) → 原生 1m 列式 dict。"""
    return {
        "Date": [r[0] for r in rows], "Time": [r[1] for r in rows],
        "Open": [r[2] for r in rows], "High": [r[3] for r in rows],
        "Low": [r[4] for r in rows], "Close": [r[5] for r in rows],
        "Volume": [r[6] for r in rows], "Amount": [r[7] for r in rows],
    }


def test_minute_units_and_wallclock_datetime():
    fake = _FakeClient()
    fake.his = {"600519.SH": _minute_raw([
        ("20261009", "093100", 1255.0, 1256.0, 1254.9, 1255.5, 123_400.0, 15_500.0),
        ("20261009", "150000", 1263.0, 1263.0, 1263.0, 1263.0, 38_600.00, 4869.62),
    ])}
    df = _provider(fake).get_minute(
        ["600519.SH"], datetime(2026, 10, 8), datetime(2026, 10, 9, 23, 59)
    )
    assert fake.his_calls[0]["period"] == "1m"
    assert df.height == 2
    rows = df.sort("datetime").to_dicts()
    assert rows[0]["datetime"] == datetime(2026, 10, 9, 9, 31, 0)   # 北京墙钟 naive
    assert rows[1]["datetime"] == datetime(2026, 10, 9, 15, 0, 0)
    assert rows[0]["volume"] == 1234.0        # 股 → floor(/100) 手
    assert rows[1]["volume"] == 386.0
    assert rows[1]["amount"] == pytest.approx(48_696_200.0)  # 万元 → 元


def test_minute_window_filter_and_gap_skip():
    fake = _FakeClient()
    fake.his = {"600519.SH": _minute_raw([
        ("20261008", "150000", 1250.0, 1250.0, 1250.0, 1250.0, 100_000.0, 12_500.0),  # 窗口外
        ("20261009", "093100", 1255.0, 1256.0, 1254.9, 1255.5, 123_400.0, 15_500.0),
        ("20261009", "093200", None, None, None, None, None, None),                    # 停牌缺口
    ])}
    df = _provider(fake).get_minute(
        ["600519.SH"], datetime(2026, 10, 9), datetime(2026, 10, 9, 23, 59)
    )
    assert df.height == 1
    assert df.to_dicts()[0]["datetime"] == datetime(2026, 10, 9, 9, 31, 0)


def test_minute_freq_and_asset_guards():
    fake = _FakeClient()
    p = _provider(fake)
    assert p.get_minute(["600519.SH"], None, datetime.now(), freq="5m").is_empty()
    assert p.get_minute(["510300.SH"], None, datetime.now(), asset_type="etf").is_empty()
    assert fake.his_calls == []  # 未发起取数


def test_minute_transport_error_soft():
    fake = _FakeClient()
    fake.his = {"600519.SH": tc.TdxQuantError("boom")}
    df = _provider(fake).get_minute(["600519.SH"], None, datetime.now())
    assert df.is_empty()  # 软失败不阻断


def test_get_intraday_batch_today_only():
    fake = _FakeClient()
    today = date.today().strftime("%Y%m%d")
    yday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    fake.his = {"600519.SH": _minute_raw([
        (yday, "150000", 1250.0, 1250.0, 1250.0, 1250.0, 100_000.0, 12_500.0),  # 前日余量 → 过滤
        (today, "093100", 1255.0, 1256.0, 1254.9, 1255.5, 123_400.0, 15_500.0),
        (today, "093200", 1255.5, 1257.0, 1255.0, 1256.0, 88_800.0, 11_160.0),
    ])}
    df = _provider(fake).get_intraday_batch(["600519.SH"])
    assert df.height == 2  # 只保留今日行
    assert fake.his_calls[0]["period"] == "1m"


def test_get_intraday_latest_universe_default():
    fake = _FakeClient()
    fake.universe = ["600000.SH", "000001.SZ"]
    today = date.today().strftime("%Y%m%d")
    for sym in fake.universe:
        fake.his[sym] = _minute_raw([
            (today, "093100", 10.0, 10.1, 9.9, 10.0, 50_000.0, 500.0),
            (today, "093200", 10.0, 10.1, 9.9, 10.0, 50_000.0, 500.0),
        ])
    df = _provider(fake).get_intraday_latest(count=3)
    assert df.height == 4  # 每只 2 根
    assert set(df["symbol"].to_list()) == {"600000.SH", "000001.SZ"}
    assert all(c["count"] <= 30 for c in fake.his_calls)


def test_test_dataset_minute(monkeypatch):
    # 试拉窗口 [now-3d, now] 依赖真实时钟, 冻结到 _FixedDT 保证任意时区/时刻可复现
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    fake = _FakeClient()
    fake.his = {"600519.SH": _minute_raw([
        ("20261011", "093100", 1255.0, 1256.0, 1254.9, 1255.5, 123_400.0, 15_500.0),
    ])}
    out = _provider(fake).test_dataset("minute", ["600519.SH"])
    assert out["rows"] == 1
    assert "datetime" in out["columns"]


# ---------------------------------------------------------------- provider: 试拉
def test_test_dataset_realtime(monkeypatch):
    monkeypatch.setattr(tp, "datetime", _FixedDT)
    fake = _FakeClient()
    fake.snapshot_results = [{"600519.SH": _snap_row()}]
    out = _provider(fake).test_dataset("realtime")
    assert out["rows"] == 1 and out["preview"][0]["symbol"] == "600519.SH"


def test_test_dataset_unsupported_dataset():
    out = _provider(_FakeClient()).test_dataset("financial")
    assert "error" in out and "回退" in out["error"]


# ---------------------------------------------------------------- loader 集成
def test_loader_manifest_parses_and_entry_importable():
    from importlib import import_module

    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("tdxquant")
    assert manifest is not None
    assert manifest["name"] == "tdxquant"
    assert manifest["display_name"] == "TdxQuant (TdxAiData)"
    assert set(manifest["datasets"]) == {"realtime", "depth5", "daily", "adj_factor", "minute", "full_minute"}
    # 计费标识与 Key/积分管理页 (设置页积分角标 + 商城入口的契约来源)
    assert manifest["billing"] == "points"
    assert manifest["key_manage_url"].startswith("https://vip.tdx.com.cn/")
    # 计费提示与使用要点 (设置页警示框 + 详情列表的契约来源)
    assert "积分" in manifest["billing_note"] and "间隔" in manifest["billing_note"]
    assert len(manifest["usage_notes"]) >= 4
    assert any("快照" in n for n in manifest["usage_notes"])
    module_path, _, class_name = manifest["entry"].partition(":")
    provider_cls = getattr(import_module(module_path), class_name)
    assert provider_cls.name == "tdxquant"


def test_loader_probe_plugin_key_dispatch(monkeypatch):
    from app.data_providers.custom import loader

    monkeypatch.setattr(tp, "probe_api_key", lambda key: (key == "good", "ok" if key == "good" else "bad"))
    assert loader.probe_plugin_key("tdxquant", "good") == (True, "ok")
    assert loader.probe_plugin_key("tdxquant", "bad") == (False, "bad")


def test_close_resets_client():
    fake = _FakeClient()
    p = _provider(fake)
    p.close()
    assert p._client is None
