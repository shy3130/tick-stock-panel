"""#515 TdxQuant 鉴权熔断 — 连续空返回判定、快速失败与恢复。

Key 失效时 SDK 表现为逐只「返回空数据」(无独立异常)。锁定:
① 连续 _AUTH_BLANK_STREAK 只全空 → 熔断并中止本轮 (不再逐只打原生库);
② 熔断冷却期内再次调用 → 快速失败返回空帧;
③ 冷却期满或重建 provider 实例 → 恢复重试; 非空数据错误不触发熔断。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.plugins.tdxquant import client as tc
from app.plugins.tdxquant import provider as tdx_provider
from app.plugins.tdxquant.auth_breaker import AuthCircuitBreaker
from app.plugins.tdxquant.client import TdxQuantAuthError, TdxQuantError


class _FakeClient:
    """按脚本回放 his_dats: 每项为 None(抛空返回错误)/异常/原始 dict。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def his_dats(self, symbol, now, count, period="1m"):
        self.calls += 1
        step = self.script.pop(0) if self.script else {"Date": ["2026-10-27"]}
        if isinstance(step, Exception):
            raise step
        return {"Date": [], "Time": [], "Open": [], "High": [], "Low": [], "Close": [],
                "Volume": [], "Amount": []}


def _blank_error() -> TdxQuantError:
    return TdxQuantError("get_his_dats 返回空数据(第 2 次)")


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(tdx_provider, "_MINUTE_INTERVAL_S", 0.0)


def _provider_with(client) -> tdx_provider.TdxQuantProvider:
    p = tdx_provider.TdxQuantProvider()
    p._client = client  # 跳过 _get_client 的原生库构造
    return p


def test_consecutive_blanks_trip_breaker_and_stop_early():
    # 30 只全部空返回: 第 10 只熔断即停, 不再逐只烧轮
    fake = _FakeClient([_blank_error()] * 30)
    p = _provider_with(fake)
    df = p.get_intraday_latest(symbols=[f"S{i}" for i in range(30)])
    assert df.is_empty()
    assert fake.calls == tdx_provider.TdxQuantProvider._AUTH_BLANK_STREAK
    assert p._auth_failed_at is not None


def test_cooldown_fast_fail_skips_native_calls():
    fake = _FakeClient([_blank_error()] * 10)
    p = _provider_with(fake)
    p.get_intraday_latest(symbols=[f"S{i}" for i in range(10)])  # 触发熔断
    fake2 = _FakeClient([])
    p._client = fake2
    df = p.get_intraday_batch([f"S{i}" for i in range(50)])
    assert df.is_empty()
    assert fake2.calls == 0  # 熔断期内整轮快速失败, 不再调用原生库


def test_cooldown_expiry_recovers():
    fake = _FakeClient([_blank_error()] * 10)
    p = _provider_with(fake)
    p.get_intraday_latest(symbols=[f"S{i}" for i in range(10)])
    p._auth_failed_at -= tdx_provider.TdxQuantProvider._AUTH_COOLDOWN_S + 1  # 模拟冷却期满
    ok = _FakeClient([])
    p._client = ok
    assert p.get_intraday_latest(symbols=["A"]).is_empty()
    assert ok.calls == 1  # 恢复重试
    assert p._auth_failed_at is None


def test_rebuilt_provider_state_resets():
    fake = _FakeClient([_blank_error()] * 10)
    p = _provider_with(fake)
    p.get_intraday_latest(symbols=[f"S{i}" for i in range(10)])
    p.close()  # load_all 重建链路: close 后模拟新实例
    fresh = _provider_with(_FakeClient([]))
    assert fresh._auth_failed_at is None
    assert fresh._auth_cooldown_active() is False


def test_non_blank_errors_do_not_trip_breaker():
    net_err = TdxQuantError("get_his_dats: connection reset")
    fake = _FakeClient([net_err] * 20)
    p = _provider_with(fake)
    df = p.get_intraday_latest(symbols=[f"S{i}" for i in range(20)])
    assert df.is_empty()
    assert fake.calls == 20  # 普通网络错误不熔断, 走完整轮 (仅日志聚合)
    assert p._auth_failed_at is None


def test_success_resets_blank_streak():
    script = [_blank_error()] * 9 + [None] + [_blank_error()] * 9
    fake = _FakeClient(script)
    p = _provider_with(fake)
    p.get_intraday_latest(symbols=[f"S{i}" for i in range(19)])
    assert p._auth_failed_at is None  # 中间成功一次即清零, 不达熔断阈值


@pytest.mark.parametrize("method", ["get_intraday_latest", "get_intraday_batch"])
def test_explicit_auth_denial_stops_after_first_symbol(method):
    fake = _FakeClient([TdxQuantAuthError("凭据或额度不可用")] * 50)
    provider = _provider_with(fake)
    assert getattr(provider, method)([f"S{i}" for i in range(50)]).is_empty()
    assert fake.calls == 1
    assert provider._auth_cooldown_active()


# ---- 独立 AuthCircuitBreaker 单元测试 ----

def test_auth_circuit_breaker_lifecycle_with_injected_clock():
    now = [100.0]
    breaker = AuthCircuitBreaker(blank_threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    assert breaker.is_open() is False

    # 连续空返回未达阈值不熔断, 中途成功清零
    assert breaker.record_blank("空1", source="测试") is False
    assert breaker.record_blank("空2", source="测试") is False
    breaker.record_success()
    assert breaker.record_blank("空3", source="测试") is False
    assert breaker.is_open() is False

    # 达到阈值后熔断
    assert breaker.record_blank("空4", source="测试") is False
    assert breaker.record_blank("空5", source="测试") is True
    assert breaker.is_open() is True
    assert breaker.reason == "空5"

    # 冷却期内保持开启, 期满后自动放行
    now[0] += 59.0
    assert breaker.is_open() is True
    now[0] += 2.0
    assert breaker.is_open() is False


# ---- 原生会话热重载 (_bind_native_session) ----

def test_bind_native_session_restarts_native_dll_only_on_key_change(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_ACTIVE_SESSION_FP", None)
    dll_path = tmp_path / tc.LIB_DLL_NAME
    dll_path.write_bytes(b"fake")
    stops = []

    fake_native = SimpleNamespace(
        _dll=SimpleNamespace(stop=lambda: stops.append("stop") or 0),
        _dll_path=str(dll_path),
        _is_started=False,
        _start_lock=threading.Lock(),
    )
    fake_cls = SimpleNamespace(get_instance=lambda: fake_native)

    # 首次绑定: 未启动, 不调用 stop
    tc._bind_native_session(fake_cls, dll_path, "TDX-KEY-OLD")
    assert stops == []

    # 模拟原生层已启动
    fake_native._is_started = True

    # 同 Key 重复绑定: 不折腾原生会话
    tc._bind_native_session(fake_cls, dll_path, "TDX-KEY-OLD")
    assert stops == []
    assert fake_native._is_started is True

    # 进程内更换 Key: 必须 stop() 并复位 _is_started=False 以便重新读取 TdxAiData.ini
    tc._bind_native_session(fake_cls, dll_path, "TDX-KEY-NEW")
    assert stops == ["stop"]
    assert fake_native._is_started is False


# ---- client 层全接口鉴权错误直抛 (无重试、无 stdout 刷屏) ----

def test_snapshot_batch_and_stock_universe_preserve_auth_error_without_retry(monkeypatch, capsys):
    secret = "TDX-secret-live-key"
    auth_raw = json.dumps({"ErrorId": 13, "Error": f"TokenKey Insufficient Or Key Error {secret}"})
    calls = {"snap": 0, "list": 0}
    sleeps = []

    def snap(*args):
        calls["snap"] += 1
        return auth_raw

    def stock_list(*args):
        calls["list"] += 1
        return auth_raw

    native = SimpleNamespace(get_market_snapshot_batch=snap, get_stock_list=stock_list)
    client = object.__new__(tc.TdxQuantClient)
    client.api_key = secret
    client._native_cls = SimpleNamespace(get_instance=lambda: native)
    client._tqs = SimpleNamespace(_clean=lambda r: None, _batch_records=lambda v, s: {})
    client._universe, client._universe_at = [], 0.0
    monkeypatch.setattr(tc.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(TdxQuantAuthError, match="TokenKey Insufficient") as snap_exc:
        client.snapshot_batch(["600519.SH"])
    assert calls["snap"] == 1
    assert secret not in str(snap_exc.value)

    with pytest.raises(TdxQuantAuthError, match="TokenKey Insufficient") as list_exc:
        client.stock_universe()
    assert calls["list"] == 1
    assert secret not in str(list_exc.value)

    # _warmup 遇鉴权拒绝立刻中止, 不等待 5×1.2s
    client._warmup(attempts=5)
    assert calls["snap"] == 2
    assert sleeps == []
    assert capsys.readouterr().out == ""


# ---- provider 全数据集统一熔断 (#591) ----

class _AuthFailingAllClient:
    def __init__(self):
        self.universe_calls = 0
        self.snapshot_calls = 0
        self.his_calls = 0

    def stock_universe(self):
        self.universe_calls += 1
        return [f"600{i:03d}.SH" for i in range(tdx_provider._SNAPSHOT_BATCH + 2)]

    def snapshot_batch(self, codes):
        self.snapshot_calls += 1
        raise TdxQuantAuthError("TdxQuant 凭据、接口权限或额度不可用: TokenKey Insufficient Or Key Error")

    def his_dats(self, code, end_d, count, period="1d"):
        self.his_calls += 1
        raise TdxQuantAuthError("TdxQuant 凭据、接口权限或额度不可用: TokenKey Insufficient Or Key Error")


def test_realtime_auth_error_trips_breaker_and_stops_all_subsequent_datasets():
    fake = _AuthFailingAllClient()
    p = _provider_with(fake)

    # 首批快照鉴权失败 → 立刻熔断, 不再跑第 2 批
    assert p.get_realtime() == []
    assert fake.snapshot_calls == 1
    assert p._auth_cooldown_active() is True
    assert p.last_realtime_meta and "TokenKey Insufficient" in p.last_realtime_meta["errors"][0]

    # 冷却期内: realtime / symbols / indices / depth5 全部零调用快速失败
    assert p.get_realtime() == []
    assert p.get_realtime_symbols(["600519.SH"]) is None
    assert p.get_realtime_indices(["000001.SH"]) is None
    assert p.get_depth_batch(["600519.SH"]) == {}
    assert fake.snapshot_calls == 1
    assert fake.universe_calls == 1

    # 冷却期内: 日K 也立刻以 TdxQuantAuthError 快速失败
    with pytest.raises(TdxQuantAuthError, match="TokenKey Insufficient"):
        p.get_daily(["600519.SH", "000001.SZ"], None, datetime(2026, 11, 27))
    assert fake.his_calls == 0


def test_daily_auth_error_stops_immediately_and_surfaces_in_test_dataset():
    fake = _AuthFailingAllClient()
    p = _provider_with(fake)

    res = p.test_dataset("daily", ["600519.SH", "000001.SZ", "000002.SZ"])
    assert res["rows"] == 0
    assert "TokenKey Insufficient" in res["error"]
    assert fake.his_calls == 1
    assert p._auth_cooldown_active() is True


def test_stock_universe_does_not_amplify_transient_retries(monkeypatch):
    calls = 0

    def failing_list(*args):
        nonlocal calls
        calls += 1
        raise RuntimeError("network down")

    client = object.__new__(tc.TdxQuantClient)
    client.api_key = "k"
    client._native_cls = SimpleNamespace(get_instance=lambda: SimpleNamespace(get_stock_list=failing_list))
    client._tqs = SimpleNamespace(_clean=lambda r: r)
    client._universe, client._universe_at = [], 0.0
    monkeypatch.setattr(tc.time, "sleep", lambda _: None)

    with pytest.raises(TdxQuantError, match="network down"):
        client.stock_universe()
    assert calls == tc._RETRIES
