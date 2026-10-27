"""#515 TdxQuant 鉴权熔断 — 连续空返回判定、快速失败与恢复。

Key 失效时 SDK 表现为逐只「返回空数据」(无独立异常)。锁定:
① 连续 _AUTH_BLANK_STREAK 只全空 → 熔断并中止本轮 (不再逐只打原生库);
② 熔断冷却期内再次调用 → 快速失败返回空帧;
③ 冷却期满或重建 provider 实例 → 恢复重试; 非空数据错误不触发熔断。
"""
from __future__ import annotations

import pytest

from app.plugins.tdxquant import provider as tdx_provider
from app.plugins.tdxquant.client import TdxQuantError


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
