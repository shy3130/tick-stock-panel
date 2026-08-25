"""DepthService 外部 fallback 集成 + sealed 写守卫测试。

验证:
  - 外部 depth 派生结果只进展示缓存 (_external_display_cache, 带 source/degraded),
    绝不进入 get_sealed_map 消费的本地 sealed cache (避免无 provenance 影响总览/研究)。
  - 外部结果绝不落盘 depth5 parquet (主守卫 early-return + _persist 纵深防御)。
  - _persist 纵深防御: _sealed_source=tencent_quote → raise ValueError (fail-closed)。
  - 本地 provider depth 仍正常写/读 sealed cache + get_sealed_map 返回。
  - 外部源 ask1=0 → sealed_up=True (真封涨停检测不变量)。

全部 mock, 零真实网络。
"""
from __future__ import annotations

import time
from datetime import date

import polars as pl
import pytest

from app.services.depth_service import DepthService


def _fake_enriched() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "signal_limit_up": [True, False],
        "signal_limit_down": [False, True],
    })


class _FakeCaps:
    depth = False


class _FakeProvider:
    name = "test_provider"
    capabilities = _FakeCaps()


class _FakeCapsDepth:
    depth = True


class _FakeProviderWithDepth:
    """provider 有 depth 能力, get_depth 返回给定盘口数据。"""

    name = "test_provider_depth"
    capabilities = _FakeCapsDepth()

    def __init__(self, depth_map: dict) -> None:
        self._depth_map = depth_map

    def get_depth(self, chunk: list[str]) -> dict:
        return {s: self._depth_map[s] for s in chunk if s in self._depth_map}


class _FakeLimit:
    batch = 100
    rpm = 30


class _FakeCapset:
    def limits(self, cap):  # noqa: ARG002
        return _FakeLimit()


class _FakeStore:
    def __init__(self, data_dir) -> None:
        self.data_dir = data_dir


class _FakeRepo:
    def __init__(self, data_dir) -> None:
        self.store = _FakeStore(data_dir)

    def get_enriched_latest(self):
        return _fake_enriched(), date(2026, 8, 7)


def _make_depth_entry(ask1: int = 200, bid1: int = 100) -> dict:
    return {
        "bid_prices": [10.0, 9.99, 9.98, 9.97, 9.96],
        "bid_volumes": [bid1, 110, 120, 130, 140],
        "ask_prices": [10.01, 10.02, 10.03, 10.04, 10.05],
        "ask_volumes": [ask1, 210, 220, 230, 240],
        "timestamp": "2026-08-07T10:30:00+08:00",
        "stale_session": False,
        "source": "tencent_quote",
    }


@pytest.fixture
def svc(tmp_path) -> DepthService:
    s = DepthService()
    s._repo = _FakeRepo(tmp_path)
    return s

# ===========================================================================
# _persist 纵深防御守卫
# ===========================================================================

class TestPersistGuard:
    def test_persist_raises_for_external_source(self, svc):
        """_sealed_source=tencent_quote → _persist raise ValueError (fail-closed)。"""
        svc._sealed_source = "tencent_quote"
        svc._sealed_cache = {"600519.SH": {
            "sealed_up": True, "sealed_down": None,
            "ask1_vol": 0, "bid1_vol": 100, "status": "limit_up",
            "fetched_ts": time.time(),
        }}
        with pytest.raises(ValueError, match="external fallback source"):
            svc._persist(date(2026, 8, 7))

    def test_persist_writes_for_provider_source(self, svc):
        """provider 源正常落盘 depth5 parquet (零回归)。"""
        svc._sealed_source = "provider"
        svc._sealed_cache = {"600519.SH": {
            "sealed_up": True, "sealed_down": None,
            "ask1_vol": 0, "bid1_vol": 100, "status": "limit_up",
            "fetched_ts": time.time(),
        }}
        svc._persist(date(2026, 8, 7))
        out = svc._repo.store.data_dir / "depth5" / "date=2026-08-07" / "part.parquet"
        assert out.exists()


# ===========================================================================
# _fetch_and_seal 外部 fallback 集成
# ===========================================================================

class TestFetchAndSealExternal:
    def _patch_external(self, monkeypatch, depth_map, used_fallback=True):
        """Patch provider (no depth) + adapter (returns fake depth result)."""
        from app.services.external_fallback.adapter import DepthFallbackResult

        monkeypatch.setattr(
            "app.services.depth_service._get_data_provider",
            lambda: _FakeProvider(),
        )
        fake_result = DepthFallbackResult(
            depth_map=depth_map,
            used_fallback=used_fallback,
            source="tencent_quote" if used_fallback else None,
        )

        class _FakeAdapter:
            def resolve_depth(self, symbols, has_local_depth=False):  # noqa: ARG002
                return fake_result

        monkeypatch.setattr(
            "app.services.external_fallback.get_adapter",
            lambda: _FakeAdapter(),
        )

    def test_external_depth_no_persist_no_sealed_pollution(self, svc, monkeypatch):
        """外部 depth 数据进展示缓存但不落盘, 且绝不污染 sealed cache。"""
        depth_map = {
            "600519.SH": _make_depth_entry(),
            "000001.SZ": _make_depth_entry(),
        }
        self._patch_external(monkeypatch, depth_map)

        svc._fetch_and_seal(persist=True)

        # 外部结果只进展示缓存
        assert "600519.SH" in svc._external_display_cache
        assert "000001.SZ" in svc._external_display_cache
        # sealed cache 未被外部数据触碰
        assert svc._sealed_cache == {}
        assert not svc._sealed_ready
        assert svc._sealed_source == "provider"  # 保持默认, 不被外部覆盖
        # depth5 parquet 不存在 (外部源 early-return, 不落盘)
        out = svc._repo.store.data_dir / "depth5" / "date=2026-08-07" / "part.parquet"
        assert not out.exists()

    def test_external_disabled_no_data(self, svc, monkeypatch):
        """外部 disabled → adapter 返回空 → depth_data 空 → 早退 (零回归)。"""
        self._patch_external(monkeypatch, {}, used_fallback=False)

        svc._fetch_and_seal(persist=True)

        assert not svc._sealed_ready
        assert svc._sealed_cache == {}

    def test_sealed_detection_with_external_zero_volume(self, svc, monkeypatch):
        """外部源 ask1=0 → sealed_up=True (真封涨停检测不变量, 在展示缓存)。"""
        depth_map = {"600519.SH": _make_depth_entry(ask1=0)}
        self._patch_external(monkeypatch, depth_map)

        svc._fetch_and_seal(persist=True)

        entry = svc._external_display_cache["600519.SH"]
        assert entry["sealed_up"] is True   # ask1=0 → 真封涨停
        assert entry["ask1_vol"] == 0       # 0 不被丢弃

    def test_sealed_down_detection(self, svc, monkeypatch):
        """外部源 bid1=0 (跌停价买一) → sealed_down=True (真封跌停, 在展示缓存)。"""
        depth_map = {"000001.SZ": _make_depth_entry(bid1=0)}
        self._patch_external(monkeypatch, depth_map)

        svc._fetch_and_seal(persist=True)

        entry = svc._external_display_cache["000001.SZ"]
        assert entry["sealed_down"] is True
        assert entry["bid1_vol"] == 0


# ===========================================================================
# 验收: sealed cache 隔离 (外部 fallback 不污染 get_sealed_map)
# ===========================================================================

class TestSealedCacheIsolation:
    """外部 fallback 命中后, get_sealed_map (共享 sealed map, 供总览/研究)
    不含外部结果; 外部结果仅在 get_display_depth_map 中带 source/degraded。"""

    def _patch_external(self, monkeypatch, depth_map, used_fallback=True):
        from app.services.external_fallback.adapter import DepthFallbackResult

        monkeypatch.setattr(
            "app.services.depth_service._get_data_provider",
            lambda: _FakeProvider(),
        )
        fake_result = DepthFallbackResult(
            depth_map=depth_map,
            used_fallback=used_fallback,
            source="tencent_quote" if used_fallback else None,
        )

        class _FakeAdapter:
            def resolve_depth(self, symbols, has_local_depth=False):  # noqa: ARG002
                return fake_result

        monkeypatch.setattr(
            "app.services.external_fallback.get_adapter",
            lambda: _FakeAdapter(),
        )

    def test_get_sealed_map_excludes_external_result(self, svc, monkeypatch):
        """验收: 外部 fallback 命中后, get_sealed_map 不含该外部结果。"""
        depth_map = {"600519.SH": _make_depth_entry(ask1=0)}
        self._patch_external(monkeypatch, depth_map)

        svc._fetch_and_seal(persist=False)

        sealed = svc.get_sealed_map(date(2026, 8, 7), is_down=False)
        assert sealed == {}                      # 共享 sealed map 不含外部结果
        assert "600519.SH" not in sealed
        # 反向亦然
        sealed_down = svc.get_sealed_map(date(2026, 8, 7), is_down=True)
        assert "600519.SH" not in sealed_down

    def test_display_depth_map_marks_source_degraded(self, svc, monkeypatch):
        """验收: 外部 fallback 命中后, get_display_depth_map 带 source/degraded。"""
        depth_map = {"600519.SH": _make_depth_entry(ask1=0)}
        self._patch_external(monkeypatch, depth_map)

        svc._fetch_and_seal(persist=False)

        display = svc.get_display_depth_map(date(2026, 8, 7), is_down=False)
        assert "600519.SH" in display
        entry = display["600519.SH"]
        assert entry["source"] == "tencent_quote"
        assert entry["degraded"] is True
        assert entry["sealed"] is True           # ask1=0 → 真封
        assert entry["ready"] is False           # degraded, 非权威 sealed

    def test_display_depth_map_empty_for_other_date(self, svc, monkeypatch):
        """外部展示缓存日期 != target_date → get_display_depth_map 返回空。"""
        depth_map = {"600519.SH": _make_depth_entry(ask1=0)}
        self._patch_external(monkeypatch, depth_map)

        svc._fetch_and_seal(persist=False)

        assert svc.get_display_depth_map(date(2026, 8, 6), is_down=False) == {}


# ===========================================================================
# 验收: 本地 provider depth 仍写/读 sealed cache
# ===========================================================================

class TestProviderSealedCache:
    """本地 provider depth 仍正常写/读 sealed cache, get_sealed_map 返回 provider 结果。"""

    def test_provider_depth_writes_and_reads_sealed(self, svc, monkeypatch):
        monkeypatch.setattr(
            "app.services.depth_service._get_data_provider",
            lambda: _FakeProviderWithDepth({"600519.SH": _make_depth_entry(ask1=0)}),
        )
        monkeypatch.setattr(svc, "_get_capset", lambda: _FakeCapset())

        svc._fetch_and_seal(persist=False)

        # 写: sealed cache 含 provider 结果
        assert svc._sealed_source == "provider"
        assert svc._sealed_ready
        assert "600519.SH" in svc._sealed_cache
        assert svc._sealed_cache["600519.SH"]["sealed_up"] is True
        # 读: get_sealed_map 返回 provider 结果
        sealed = svc.get_sealed_map(date(2026, 8, 7), is_down=False)
        assert "600519.SH" in sealed
        assert sealed["600519.SH"]["sealed"] is True
        assert sealed["600519.SH"]["ready"] is True
