import gc
import weakref

import pytest

from app.data_providers import registry
from app.services import preferences


def test_fquant_local_registered():
    assert registry.normalize_provider_name("fquant_local") == "fquant_local"


def test_daily_provider_uses_daily_preference(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fquant_local")

    assert registry.get_active_provider_name("daily") == "fquant_local"


def test_financial_and_depth_use_capability_preferences(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(preferences, "get_financial_data_provider", lambda: "fquant")
    monkeypatch.setattr(preferences, "get_depth_data_provider", lambda: "fquant_local")

    assert registry.get_active_provider_name("financial") == "fquant"
    assert registry.get_active_provider_name("depth") == "fquant_local"


def test_env_provider_overrides_capability_preference(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "fquant")
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fquant_local")

    assert registry.get_active_provider_name("daily") == "fquant"


def test_unknown_env_provider_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "tickflow")

    assert registry.get_active_provider_name("daily") == "fquant_local"


def test_fquant_local_uses_duckdb_engine():
    provider = registry.get_provider("fquant_local")

    assert provider.name == "fquant_local"
    assert provider._engine.__class__.__name__ == "TdxDuckDBClient"


class _StubProvider:
    """轻量 stub：替代 FQuantProvider 验证 WeakSet 跟踪与 close 契约。"""

    def __init__(self, name: str = "stub") -> None:
        self.name = name
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1




def test_get_provider_tracks_returned_provider():
    monkey = pytest.MonkeyPatch()
    monkey.setitem(registry._PROVIDERS, "stub", lambda: _StubProvider("tracked"))
    try:
        provider = registry.get_provider("stub")
        # 持有引用 → 必须留在 WeakSet 里。
        assert provider in _iter_live(registry)
        # 释放引用 + GC → 应从 WeakSet 消失（弱引用，不持有强引用）。
        ident = id(provider)
        del provider
        gc.collect()
        assert all(id(p) != ident for p in _iter_live(registry))
    finally:
        monkey.undo()


def test_registry_does_not_hold_strong_reference():
    """registry 不应阻止 provider 被 GC 回收（WeakSet 而非 list）。"""

    monkey = pytest.MonkeyPatch()
    monkey.setitem(registry._PROVIDERS, "stub", lambda: _StubProvider("ephemeral"))
    try:
        provider = registry.get_provider("stub")
        ref = weakref.ref(provider)
        del provider
        gc.collect()
        assert ref() is None  # 没有强引用存活
    finally:
        monkey.undo()


def test_close_all_providers_closes_tracked_providers():
    """close_all_providers 关闭所有被跟踪 provider。"""
    monkey = pytest.MonkeyPatch()
    monkey.setitem(registry._PROVIDERS, "a", lambda: _StubProvider("a"))
    monkey.setitem(registry._PROVIDERS, "b", lambda: _StubProvider("b"))
    try:
        pa = registry.get_provider("a")
        pb = registry.get_provider("b")
        # 只断言这两个 stub 被关了一次（不关心其它残留的 tracked provider）。
        before_a, before_b = pa.close_calls, pb.close_calls
        registry.close_all_providers()
        assert pa.close_calls == before_a + 1
        assert pb.close_calls == before_b + 1
    finally:
        monkey.undo()


def test_close_all_providers_skips_objects_without_close(monkeypatch):
    """没有 close 属性的对象不会被跟踪，close_all_providers 仍不抛错。"""
    class NoClose:
        name = "noclose"

    monkeypatch.setitem(registry._PROVIDERS, "noclose", lambda: NoClose())
    registry.get_provider("noclose")  # 不会进入 WeakSet（无 close）
    # 幂等：不抛错。
    registry.close_all_providers()


def test_close_all_providers_continues_after_one_close_raises(monkeypatch):
    """单个 provider close 抛异常不应中断其余 provider 的关闭。"""
    class Boom:
        name = "boom"
        closed = 0

        def close(self):
            raise OSError("boom")

    class Ok:
        name = "ok"
        closed = 0

        def close(self):
            self.closed += 1

    monkeypatch.setitem(registry._PROVIDERS, "boom", lambda: Boom())
    monkeypatch.setitem(registry._PROVIDERS, "ok", lambda: Ok())
    boom = registry.get_provider("boom")
    ok = registry.get_provider("ok")
    registry.close_all_providers()  # 不抛
    assert ok.closed == 1


def _iter_live(reg):
    with reg._LIVE_PROVIDERS_LOCK:
        return list(reg._LIVE_PROVIDERS)