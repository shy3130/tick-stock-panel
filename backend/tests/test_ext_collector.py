"""ExtCollector 扩展采集器契约、PullScheduler 窗口与状态语义测试。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ext_data import router as ext_router
from app.extensions.contracts import (
    BACKEND_EXTENSION_API_VERSION,
    ExtCollector,
    ExtCollectorContext,
    ExtCollectorResult,
)
from app.extensions.registry import BackendExtensionRegistrar, BackendExtensionRegistry
from app.market_time import CN_TZ
from app.services import ext_config_transfer, ext_pull
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.ext_data_schema import PullConfigReq


class _StubCollector(ExtCollector):
    label = "Stub Collector"
    description = "Unit test stub collector"
    default_mode = "timeseries"
    default_fields = ({"name": "score", "dtype": "float", "label": "分值"},)
    params_schema: ClassVar[dict[str, Any]] = {"threshold": {"type": "float", "label": "阈值", "default": 1.5}}

    def __init__(self, fn) -> None:
        self._fn = fn

    def collect(self, context: ExtCollectorContext) -> ExtCollectorResult:
        return self._fn(context)


# ── 注册表与模型契约 ──────────────────────────────────────────────────────

def test_registry_registers_freezes_and_copies_ext_collectors():
    reg = BackendExtensionRegistry()
    collector = _StubCollector(lambda _ctx: ExtCollectorResult())
    registrar = BackendExtensionRegistrar("demo.ext", api_version=BACKEND_EXTENSION_API_VERSION)
    registrar.register_ext_collector("demo.c1", collector, order=20)
    reg.register(registrar)

    # mutable_copy 保留已注册采集器 (外部扩展包 staging 依赖)
    staged = reg.mutable_copy()
    registrar2 = BackendExtensionRegistrar("demo.ext2", api_version=BACKEND_EXTENSION_API_VERSION)
    registrar2.register_ext_collector("demo.c0", collector, order=10)
    staged.register(registrar2)
    staged.freeze()

    ids = [item.implementation_id for item in staged.ext_collectors()]
    assert ids == ["demo.c0", "demo.c1"]
    assert staged.get_ext_collector("demo.c1").extension_id == "demo.ext"
    assert staged.get_ext_collector("missing") is None


def test_registry_rejects_duplicate_and_invalid_collectors():
    reg = BackendExtensionRegistry()
    c = _StubCollector(lambda _ctx: ExtCollectorResult())

    r1 = BackendExtensionRegistrar("a", api_version=BACKEND_EXTENSION_API_VERSION)
    r1.register_ext_collector("dup.id", c)
    reg.register(r1)

    r2 = BackendExtensionRegistrar("b", api_version=BACKEND_EXTENSION_API_VERSION)
    r2.register_ext_collector("dup.id", c)
    with pytest.raises(ValueError, match="duplicate ext collector"):
        reg.register(r2)

    r3 = BackendExtensionRegistrar("c", api_version=BACKEND_EXTENSION_API_VERSION)
    r3.register_ext_collector("bad.type", object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ExtCollector"):
        reg.register(r3)


def test_pull_config_collector_roundtrip_and_validation(tmp_path):
    p = PullConfig(url="", collector="auction_scan.market_925", enabled=False)
    assert p.is_configured() is True
    assert PullConfig(url="", collector=None).is_configured() is False

    rt = PullConfig.from_dict(p.to_dict())
    assert rt.collector == "auction_scan.market_925"
    assert rt.url == ""

    # PullConfigReq: url 或 collector 至少填一个
    req = PullConfigReq(url="", collector="auction_scan.market_925", enabled=True)
    assert req.collector == "auction_scan.market_925"
    with pytest.raises(ValueError):
        PullConfigReq(url="", collector=None)

    # 配置导出/导入兼容 collector 表 (空 url 不被 _clean_url 拒绝)
    store = ExtConfigStore(tmp_path)
    store.create(ExtConfig(
        id="c_export",
        label="采集表",
        mode="timeseries",
        fields=[ExtField("score", "float", "分值")],
        pull=PullConfig(url="", collector="demo.c1", schedule_time="09:25", time_window_end="09:30"),
    ))
    zip_bytes, _ = ext_config_transfer.export_config(tmp_path, "c_export")
    preview = ext_config_transfer.inspect_config(tmp_path, zip_bytes)
    assert preview["has_pull"] is True


# ── 调度器: time_window_end 过窗不补录 + 节假日跳过 ───────────────────────

class _FakeStore:
    def __init__(self, config: ExtConfig) -> None:
        self._cfg = config

    def get(self, _config_id: str) -> ExtConfig:
        return self._cfg

    def upsert(self, config: ExtConfig, **_kw) -> None:
        self._cfg = config


async def test_daily_at_respects_time_window_end_no_late_backfill(monkeypatch):
    """10:00 启用 [09:25, 09:30) 窗口任务 → 当天绝不补录，等到次日 09:25 才执行一次。"""
    cfg = ExtConfig(
        id="win_test",
        label="t",
        mode="timeseries",
        fields=[],
        pull=PullConfig(
            url="",
            collector="demo.c",
            enabled=True,
            schedule_time="09:25",
            time_window_end="09:30",
            schedule_trading_only=True,
        ),
    )
    store = _FakeStore(cfg)
    # 周一 10:00 (已过 09:30 窗口)
    clock = {"now": datetime(2026, 3, 2, 10, 0, tzinfo=CN_TZ)}

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = clock["now"].astimezone(tz) if tz else clock["now"]
            return cls(
                moment.year, moment.month, moment.day, moment.hour,
                moment.minute, moment.second, moment.microsecond, moment.tzinfo,
            )

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])
    monkeypatch.setattr(ext_pull, "datetime", _FakeDateTime)
    monkeypatch.setattr(ext_pull, "_is_trading_day", lambda _now: True)

    ran_at: list[str] = []

    async def fake_fetch(config, data_dir, **_kw):
        ran_at.append(clock["now"].isoformat())
        config.pull.enabled = False
        return ext_pull.PullOutcome(2, clock["now"].date().isoformat())

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = Path("/tmp/nonexistent")

    await asyncio.wait_for(sched._run_daily_at("win_test", store, "09:25"), timeout=10)

    assert len(ran_at) == 1
    assert ran_at[0].startswith("2026-03-03T09:25")  # 跳过 3 月 2 日，次日 09:25 触发


async def test_daily_at_skips_holiday_without_invoking_collector(monkeypatch):
    """法定节假日 (工作日但 _is_trading_day=False) → 记 skipped 且绝不执行，等到下个交易日。"""
    cfg = ExtConfig(
        id="hol_test",
        label="t",
        mode="timeseries",
        fields=[],
        pull=PullConfig(
            url="",
            collector="demo.c",
            enabled=True,
            schedule_time="09:25",
            time_window_end="09:30",
            schedule_trading_only=True,
        ),
    )
    store = _FakeStore(cfg)
    # 2026-05-01 (周五，劳动节休市) 09:25; 2026-05-04 (周一，开市)
    clock = {"now": datetime(2026, 5, 1, 9, 25, tzinfo=CN_TZ)}

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = clock["now"].astimezone(tz) if tz else clock["now"]
            return cls(
                moment.year, moment.month, moment.day, moment.hour,
                moment.minute, moment.second, moment.microsecond, moment.tzinfo,
            )

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])
    monkeypatch.setattr(ext_pull, "datetime", _FakeDateTime)
    monkeypatch.setattr(
        ext_pull,
        "_is_trading_day",
        lambda now: now.date() != date(2026, 5, 1),
    )

    ran_dates: list[str] = []

    async def fake_fetch(config, data_dir, **_kw):
        ran_dates.append(clock["now"].date().isoformat())
        config.pull.enabled = False
        return ext_pull.PullOutcome(1, clock["now"].date().isoformat())

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = Path("/tmp/nonexistent")

    await asyncio.wait_for(sched._run_daily_at("hol_test", store, "09:25"), timeout=10)

    assert ran_dates == ["2026-05-04"]


async def test_interval_loop_respects_schedule_trading_only(tmp_path, monkeypatch):
    """秒级/分钟级间隔模式启用 schedule_trading_only 时，周末与节假日自动跳过，交易日窗口内才采集。"""
    store = ExtConfigStore(tmp_path)
    cfg = ExtConfig(
        id="int_td",
        label="早盘多盘",
        mode="timeseries",
        fields=[ExtField("score", "float", "分值")],
        pull=PullConfig(
            url="",
            collector="demo.c",
            enabled=True,
            schedule_seconds=30,
            schedule_trading_only=True,
            time_window_start="09:15",
            time_window_end="09:25",
        ),
    )
    store.create(cfg)

    # 先落在周六 09:20，再切到劳动节周五 09:20，最后切到交易日周一 09:20
    moments = [
        datetime(2026, 5, 2, 9, 20, tzinfo=CN_TZ),  # 周六
        datetime(2026, 5, 1, 9, 20, tzinfo=CN_TZ),  # 节假日
        datetime(2026, 5, 4, 9, 20, tzinfo=CN_TZ),  # 交易日
    ]
    idx = {"i": 0}
    monkeypatch.setattr(ext_pull, "cn_now", lambda: moments[min(idx["i"], len(moments) - 1)])
    monkeypatch.setattr(
        ext_pull,
        "_is_trading_day",
        lambda now: now.weekday() < 5 and now.date() != date(2026, 5, 1),
    )

    ran_dates: list[str] = []

    async def fake_fetch(config, data_dir, **_kw):
        cur = moments[min(idx["i"], len(moments) - 1)]
        ran_dates.append(cur.date().isoformat())
        fresh = store.get(config.id)
        fresh.pull.enabled = False
        store.upsert(fresh)
        return ext_pull.PullOutcome(3, cur.date().isoformat())

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = tmp_path

    async def fake_sleep_reloading(_cid, _st, _interval):
        idx["i"] += 1

    monkeypatch.setattr(sched, "_sleep_interval_reloading", fake_sleep_reloading)

    await asyncio.wait_for(sched._run_loop(cfg), timeout=10)

    assert ran_dates == ["2026-05-04"]
    assert store.get("int_td").pull.last_status == "success"


# ── API 端到端: 0 行分区、部分失败、跳过、倒序历史日期 ────────────────────

def _build_test_app(tmp_path: Path, collector: ExtCollector) -> FastAPI:
    app = FastAPI()
    app.include_router(ext_router)

    reg = BackendExtensionRegistry()
    registrar = BackendExtensionRegistrar("test.ext", api_version=BACKEND_EXTENSION_API_VERSION)
    registrar.register_ext_collector("test.collector", collector)
    reg.register(registrar)
    reg.freeze()

    app.state.extension_registry = reg
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_views=lambda: None,
        clear_cache=lambda: None,
        get_name_map=lambda: {"000001.SZ": "平安银行", "600519.SH": "贵州茅台"},
    )
    return app


def test_collector_api_empty_day_partial_error_and_desc_dates(tmp_path, monkeypatch):
    state = {"mode": "day1_hits"}

    def collect_fn(ctx: ExtCollectorContext) -> ExtCollectorResult:
        if state["mode"] == "day1_hits":
            return ExtCollectorResult(
                rows=(
                    {"symbol": "000001.SZ", "score": 12.5},
                    {"symbol": "600519.SH", "score": 10.0},
                ),
                message="day1 hits",
            )
        if state["mode"] == "day2_empty":
            return ExtCollectorResult(rows=(), message="0 hits", allow_empty=True)
        if state["mode"] == "day3_partial":
            return ExtCollectorResult(
                rows=({"symbol": "000001.SZ", "score": 15.0},),
                message="partial run",
                partial_errors=("第2批超时",),
            )
        return ExtCollectorResult(rows=(), skipped_reason="非交易日")

    app = _build_test_app(tmp_path, _StubCollector(collect_fn))
    store = ExtConfigStore(tmp_path)
    store.create(ExtConfig(
        id="ext_demo",
        label="Demo",
        mode="timeseries",
        fields=[ExtField("score", "float", "分值")],
        pull=PullConfig(url="", collector="test.collector", enabled=False),
    ))

    client = TestClient(app)

    # 1. collectors 元数据发现 (含 default_mode / default_fields / params_schema)
    col_resp = client.get("/api/ext-data/collectors")
    assert col_resp.status_code == 200
    col_meta = col_resp.json()["items"][0]
    assert col_meta["id"] == "test.collector"
    assert col_meta["default_mode"] == "timeseries"
    assert col_meta["default_fields"] == [{"name": "score", "dtype": "float", "label": "分值"}]
    assert col_meta["params_schema"]["threshold"]["default"] == 1.5

    # 2. test_pull 预览不写盘
    monkeypatch.setattr(ext_pull, "cn_today", lambda: date(2026, 4, 13))
    test_resp = client.post("/api/ext-data/ext_demo/pull/test")
    assert test_resp.status_code == 200
    assert test_resp.json()["total_rows"] == 2
    assert not (tmp_path / "ext_data" / "ext_demo" / "timeseries").exists()

    # 3. Day 1: 写入 2 行
    r1 = client.post("/api/ext-data/ext_demo/pull/run")
    assert r1.status_code == 200
    assert r1.json() == {"status": "ok", "rows": 2, "date": "2026-04-13"}

    # 4. Day 2: 0 命中写 0 行带类型分区，last_status=success
    state["mode"] = "day2_empty"
    monkeypatch.setattr(ext_pull, "cn_today", lambda: date(2026, 4, 14))
    r2 = client.post("/api/ext-data/ext_demo/pull/run")
    assert r2.status_code == 200
    assert r2.json() == {"status": "ok", "rows": 0, "date": "2026-04-14"}
    p2 = tmp_path / "ext_data" / "ext_demo" / "timeseries" / "date=2026-04-14" / "part.parquet"
    assert p2.exists()
    assert pl.read_parquet(p2).height == 0

    # 5. Day 3: 部分失败保留已成功行且 status="partial"
    state["mode"] = "day3_partial"
    monkeypatch.setattr(ext_pull, "cn_today", lambda: date(2026, 4, 15))
    r3 = client.post("/api/ext-data/ext_demo/pull/run")
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["status"] == "partial"
    assert body3["rows"] == 1
    assert body3["partial_errors"] == ["第2批超时"]

    # 6. Day 4: skipped 不落盘
    state["mode"] = "day4_skipped"
    monkeypatch.setattr(ext_pull, "cn_today", lambda: date(2026, 4, 16))
    r4 = client.post("/api/ext-data/ext_demo/pull/run")
    assert r4.status_code == 200
    assert r4.json()["status"] == "skipped"
    assert not (tmp_path / "ext_data" / "ext_demo" / "timeseries" / "date=2026-04-16").exists()

    # 7. /rows 默认返回最新日 + 倒序日期列表; 指定 date 可回看历史日与 0 命中日
    latest = client.get("/api/ext-data/ext_demo/rows").json()
    assert latest["date"] == "2026-04-15"
    assert latest["dates"] == ["2026-04-15", "2026-04-14", "2026-04-13"]
    assert latest["total"] == 1

    day2 = client.get("/api/ext-data/ext_demo/rows?date=2026-04-14").json()
    assert day2["date"] == "2026-04-14"
    assert day2["total"] == 0
    assert day2["rows"] == []

    day1 = client.get("/api/ext-data/ext_demo/rows?date=2026-04-13").json()
    assert day1["date"] == "2026-04-13"
    assert day1["total"] == 2

    # 8. list_configs 包含 running 字段和持久化状态
    listing = client.get("/api/ext-data").json()["items"]
    assert len(listing) == 1
    assert listing[0]["running"] is False
    assert listing[0]["pull"]["last_status"] == "skipped"


def test_collector_params_roundtrip_and_context_injection(tmp_path, monkeypatch):
    seen: dict = {}

    def collect_fn(ctx: ExtCollectorContext) -> ExtCollectorResult:
        seen["params"] = dict(ctx.params)
        seen["has_market"] = ctx.market is not None
        seen["name_map"] = ctx.market.get_name_map() if ctx.market else {}
        return ExtCollectorResult(rows=({"symbol": "000001.SZ", "score": float(ctx.params.get("threshold", 0))},))

    app = _build_test_app(tmp_path, _StubCollector(collect_fn))
    store = ExtConfigStore(tmp_path)
    store.create(ExtConfig(
        id="ext_param",
        label="参数化采集",
        mode="timeseries",
        fields=[ExtField("score", "float", "分值")],
        pull=PullConfig(
            url="",
            collector="test.collector",
            collector_params={"threshold": 9.5},
            enabled=False,
        ),
    ))

    # PullConfig 序列化/反序列化
    assert store.get("ext_param").pull.collector_params == {"threshold": 9.5}

    client = TestClient(app)
    monkeypatch.setattr(ext_pull, "cn_today", lambda: date(2026, 4, 20))

    # PUT /pull 不传 collector_params 时保留原值; 显式传入则更新
    monkeypatch.setattr(ext_pull.pull_scheduler, "refresh", lambda _d: None)
    r_keep = client.put(
        "/api/ext-data/ext_param/pull",
        json={"url": "", "collector": "test.collector", "enabled": False},
    )
    assert r_keep.status_code == 200
    assert r_keep.json()["pull"]["collector_params"] == {"threshold": 9.5}

    r_upd = client.put(
        "/api/ext-data/ext_param/pull",
        json={"url": "", "collector": "test.collector", "collector_params": {"threshold": 7.25}, "enabled": False},
    )
    assert r_upd.status_code == 200
    assert r_upd.json()["pull"]["collector_params"] == {"threshold": 7.25}

    # run_pull 注入 ctx.params 与 ctx.market
    r_run = client.post("/api/ext-data/ext_param/pull/run")
    assert r_run.status_code == 200
    assert seen["params"] == {"threshold": 7.25}
    assert seen["has_market"] is True
    assert seen["name_map"]["000001.SZ"] == "平安银行"
    rows = client.get("/api/ext-data/ext_param/rows").json()["rows"]
    assert rows[0]["score"] == pytest.approx(7.25)
