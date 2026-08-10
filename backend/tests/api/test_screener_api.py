from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener
from app.services import screener as screener_module


class _Service:
    def __init__(self, repo):
        self.repo = repo

    def latest_date(self):
        return date(2026, 7, 16)

    def _load_enriched_for_date(self, as_of):
        return pl.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ"],
                "date": [as_of, as_of],
                "close": [10.0, 20.0],
                "change_pct": [0.1, 0.2],
            }
        )


class _Repo:
    store = SimpleNamespace(data_dir=Path("."))

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["600001.SH", "000001.SZ"], "name": ["甲", "乙"]})


def _client(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    app = FastAPI()
    app.state.repo = _Repo()
    app.include_router(screener.router)
    return TestClient(app)


def test_screener_query_fields_and_nested_order(monkeypatch):
    client = _client(monkeypatch)
    fields = client.get("/api/screener/fields")
    assert fields.status_code == 200
    assert any(item["field"] == "change_pct" for item in fields.json()["fields"])
    response = client.post(
        "/api/screener/query",
        json={
            "conditions": [{"field": "change_pct", "op": ">", "value": 0}],
            "order_by": {"field": "close", "direction": "asc"},
            "limit": 1,
        },
    )
    assert response.status_code == 200
    assert set(response.json()) == {"rows", "total", "applied", "as_of", "elapsed_ms"}
    assert response.json()["rows"][0]["symbol"] == "600001.SH"


def test_screener_query_distinguishes_422_400_and_503(monkeypatch):
    client = _client(monkeypatch)
    assert client.post("/api/screener/query", json={"conditions": []}).status_code == 422
    semantic = client.post(
        "/api/screener/query",
        json={"conditions": [{"field": "does_not_exist", "op": "=", "value": 1}]},
    )
    assert semantic.status_code == 400
    assert semantic.json()["detail"]["code"] == "invalid_screener_semantics"
    unavailable = client.post(
        "/api/screener/query",
        json={"conditions": [{"field": "main_net_inflow", "op": ">", "value": 0}]},
    )
    assert unavailable.status_code == 400
    assert unavailable.json()["detail"]["reason"] == "unavailable_field"


def test_nl_presets_shape_and_legacy_routes(monkeypatch):
    client = _client(monkeypatch)
    presets = client.get("/api/screener/nl_presets")
    assert presets.status_code == 200
    assert len(presets.json()["presets"]) == 5
    assert all(item["predicate"]["conditions"] for item in presets.json()["presets"])
    assert client.get("/api/screener/strategies").status_code == 200


def test_query_missing_required_source_is_sanitized_503(monkeypatch):
    class BrokenService(_Service):
        def _load_enriched_for_date(self, as_of):
            return pl.DataFrame({"symbol": ["600001.SH"], "date": [as_of]})

    monkeypatch.setattr(screener_module, "ScreenerService", BrokenService)
    app = FastAPI()
    app.state.repo = _Repo()
    app.include_router(screener.router)
    response = TestClient(app).post(
        "/api/screener/query",
        json={"conditions": [{"field": "close", "op": ">", "value": 0}]},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "screener_data_unavailable", "fields": ["change_pct", "close"]}}
    assert "path" not in response.text and "exception" not in response.text



# ===========================================================================
# limit-ladder: 外部降级展示 map 接入(provenance 隔离)
# ===========================================================================
# 验收:
#   - external map 会显示 sealed_source/sealed_degraded, 但 counts/status 与 raw/enriched 一致
#   - provider map 仍按旧逻辑修正(fake→broken, counts 扣减)
#   - market_overview_builder 等消费方不变(本测试只覆盖 limit-ladder 端点)
# ===========================================================================


class _LadderService:
    """连板梯队专用 service: 按 as_of 返回不同 enriched(当日/前日)。"""

    def __init__(self, repo):
        self.repo = repo

    def latest_date(self):
        return date(2026, 8, 7)

    def _load_enriched_for_date(self, as_of):
        if as_of == date(2026, 8, 7):
            # 当日: 两只涨停股(600519 真封 / 600999 假封将由外部 map 标注)
            return pl.DataFrame({
                "symbol": ["600519.SH", "600999.SH", "000001.SZ"],
                "date": [as_of, as_of, as_of],
                "name": ["甲", "乙", "丙"],
                "close": [100.0, 10.0, 20.0],
                "change_pct": [10.0, 10.0, -10.0],
                "signal_limit_up": [True, True, False],
                "signal_limit_down": [False, False, True],
                "signal_broken_limit_up": [False, False, False],
                "consecutive_limit_ups": [3, 1, None],
                "consecutive_limit_downs": [None, None, 1],
            })
        if as_of == date(2026, 8, 6):
            # 前日: 提供 prev consecutive_limit_ups
            return pl.DataFrame({
                "symbol": ["600519.SH", "600999.SH", "000001.SZ"],
                "date": [as_of, as_of, as_of],
                "consecutive_limit_ups": [2, 0, None],
                "consecutive_limit_downs": [None, None, 0],
            })
        return pl.DataFrame()


class _FakeDepth:
    """可配置的 depth_service 替身: 分别控制 sealed_map / display_map / ready。

    sealed_map / display_map 可传 dict(两方向共用)或 {'up': ..., 'down': ...} 分方向。
    """

    def __init__(self, sealed_map=None, display_map=None, ready=False, sealed_age=None):
        self._sealed = self._split(sealed_map)
        self._display = self._split(display_map)
        self._ready = ready
        self._sealed_age = sealed_age

    @staticmethod
    def _split(m):
        if m is None:
            return {"up": {}, "down": {}}
        if isinstance(m, dict) and set(m) <= {"up", "down"}:
            up = m.get("up", {})
            down = m.get("down", {})
            return {"up": up, "down": down}
        return {"up": m, "down": m}

    def get_sealed_map(self, target_date, is_down):
        return self._sealed["down"] if is_down else self._sealed["up"]

    def get_display_depth_map(self, target_date, is_down):
        return self._display["down"] if is_down else self._display["up"]

    def is_sealed_ready(self, target_date):
        return self._ready

    def get_sealed_age(self, target_date):
        return self._sealed_age


def _ladder_client(monkeypatch, depth_service=None):
    monkeypatch.setattr(screener, "ScreenerService", _LadderService)
    app = FastAPI()
    app.state.repo = _Repo()
    if depth_service is not None:
        app.state.depth_service = depth_service
    app.include_router(screener.router)
    return TestClient(app)


def test_limit_ladder_external_map_shows_source_degraded(monkeypatch):
    """外部降级 map: 显示 sealed_source/sealed_degraded, 但 counts/status 与 raw 一致。"""
    # 权威 sealed_map 空 + 外部 display_map 命中(600519 真封, 600999 假封)
    depth = _FakeDepth(
        sealed_map={},
        display_map={
            "600519.SH": {"sealed": True, "vol": 5000, "source": "tencent_quote", "degraded": True},
            "600999.SH": {"sealed": False, "vol": 0, "source": "tencent_quote", "degraded": True},
        },
        ready=False,
    )
    client = _ladder_client(monkeypatch, depth_service=depth)

    resp = client.get("/api/screener/limit-ladder?direction=up")
    assert resp.status_code == 200
    body = resp.json()

    # provenance 标识
    assert body["sealed_degraded"] is True
    assert body["sealed_source"] == "tencent_quote"
    # 外部 map 非权威 → sealed_ready 保持 False
    assert body["sealed_ready"] is False

    # counts 不被外部 map 修正: 与 raw 一致(signal_limit_up 当日 2 只)
    assert body["counts"] == {"up": 2, "down": 1}
    assert body["counts_raw"] == {"up": 2, "down": 1}

    # status 不被外部 map 降级: 两只仍是 limit_up(不得把假封降成 broken)
    up_stocks = {
        s["symbol"]: s
        for t in body["tiers"]
        for s in t["stocks"]
        if s["status"] == "limit_up"
    }
    assert set(up_stocks) == {"600519.SH", "600999.SH"}

    # 展示字段来自外部 map
    assert up_stocks["600519.SH"]["sealed_status"] == "real"
    assert up_stocks["600519.SH"]["sealed_vol"] == 5000
    assert up_stocks["600999.SH"]["sealed_status"] == "fake"
    assert up_stocks["600999.SH"]["sealed_vol"] == 0


def test_limit_ladder_provider_map_still_corrects(monkeypatch):
    """provider 权威 map: 仍按旧逻辑修正(假封→broken, counts 扣减), degraded=False。"""
    depth = _FakeDepth(
        sealed_map={
            # 仅 up 方向提供权威 sealed_map; down 方向留空(避免污染 count_down)
            "up": {
                "600519.SH": {"sealed": True, "vol": 8000},
                "600999.SH": {"sealed": False, "vol": 0},
            },
            "down": {},
        },
        display_map={
            # display_map 存在但权威 map 命中时应被忽略
            "600519.SH": {"sealed": False, "vol": 0, "source": "tencent_quote", "degraded": True},
        },
        ready=True,
        sealed_age=12.0,
    )
    client = _ladder_client(monkeypatch, depth_service=depth)

    resp = client.get("/api/screener/limit-ladder?direction=up")
    assert resp.status_code == 200
    body = resp.json()

    # 权威路径: 不降级
    assert body["sealed_degraded"] is False
    assert body["sealed_source"] is None
    assert body["sealed_ready"] is True
    assert body["sealed_age"] == 12.0

    # counts 被权威 map 修正: fake(600999) 扣减 → up=1; down 方向无权威 map → 不修正
    assert body["counts_raw"] == {"up": 2, "down": 1}
    assert body["counts"] == {"up": 1, "down": 1}
    # 600999 假封被权威 map 降级为 broken(归炸板), 600519 保留 limit_up
    by_sym = {s["symbol"]: s for t in body["tiers"] for s in t["stocks"]}
    assert by_sym["600519.SH"]["status"] == "limit_up"
    assert by_sym["600519.SH"]["sealed_status"] == "real"
    assert by_sym["600999.SH"]["status"] == "broken"
    assert by_sym["600999.SH"]["sealed_status"] == "fake"


def test_limit_ladder_no_depth_service(monkeypatch):
    """无 depth_service: degraded=False, source=null, sealed 字段全空。"""
    client = _ladder_client(monkeypatch, depth_service=None)

    resp = client.get("/api/screener/limit-ladder?direction=up")
    assert resp.status_code == 200
    body = resp.json()

    assert body["sealed_degraded"] is False
    assert body["sealed_source"] is None
    assert body["sealed_ready"] is False
    assert body["counts"] == body["counts_raw"]
    for t in body["tiers"]:
        for s in t["stocks"]:
            assert s["sealed_status"] is None
            assert s["sealed_vol"] is None


def test_limit_ladder_external_map_no_display_data(monkeypatch):
    """权威 map 空且外部 display map 也空: degraded=False, 不误标降级。"""
    depth = _FakeDepth(sealed_map={}, display_map={}, ready=False)
    client = _ladder_client(monkeypatch, depth_service=depth)

    resp = client.get("/api/screener/limit-ladder?direction=up")
    assert resp.status_code == 200
    body = resp.json()

    assert body["sealed_degraded"] is False
    assert body["sealed_source"] is None
    assert body["counts"] == body["counts_raw"]