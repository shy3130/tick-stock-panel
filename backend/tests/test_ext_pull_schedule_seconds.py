"""扩展数据拉取的秒级间隔 (PullConfig.schedule_seconds)。

schedule_seconds 优先于 schedule_minutes (秒级拉取, 如集合竞价 30s 快照);
缺省/非法回退分钟制, 存量 config.json 与旧导出包行为不变。
判据不依赖跑测试机器的时区: 钉住 cn_now 与假时钟。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_data as api
from app.config import settings
from app.market_time import CN_TZ
from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.ext_data_schema import PullConfigReq

# ── 模型: 默认值 / 序列化 / 非法值归一 ──────────────────────────────


def test_schedule_seconds_defaults_none_and_roundtrips():
    assert PullConfig().schedule_seconds is None

    d = PullConfig(url="https://x", schedule_seconds=30).to_dict()
    assert d["schedule_seconds"] == 30
    assert PullConfig.from_dict(d).schedule_seconds == 30


def test_schedule_seconds_missing_in_old_config_falls_back_to_none():
    legacy = PullConfig(url="https://x", schedule_minutes=5).to_dict()
    legacy.pop("schedule_seconds", None)  # 模拟旧版本 config.json
    restored = PullConfig.from_dict(legacy)
    assert restored.schedule_seconds is None
    assert restored.schedule_minutes == 5  # 分钟制不受影响


@pytest.mark.parametrize("bad", [9, 0, -30, 2_592_001, 1.5, "30", True])
def test_schedule_seconds_invalid_values_normalized_to_none(bad):
    assert PullConfig(url="https://x", schedule_seconds=bad).schedule_seconds is None


def test_schedule_seconds_valid_boundary_preserved():
    assert PullConfig(url="https://x", schedule_seconds=10).schedule_seconds == 10
    assert PullConfig(url="https://x", schedule_seconds=2_592_000).schedule_seconds == 2_592_000


# ── 请求体校验 ──────────────────────────────────────────────────────


def test_pull_config_req_validates_schedule_seconds():
    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_seconds=9)   # 低于下限 10s
    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_seconds=3_000_000)  # 超过 30 天

    req = PullConfigReq(url="https://x", schedule_seconds=30)
    assert req.schedule_seconds == 30 and req.schedule_minutes == 1440
    # 缺省 = 沿用分钟制 (存量行为)
    assert PullConfigReq(url="https://x").schedule_seconds is None


# ── 有效间隔: 秒优先 / 回退 / 下限钳制 ─────────────────────────────


def test_interval_seconds_prefers_seconds_over_minutes():
    pull = PullConfig(url="https://x", schedule_minutes=5, schedule_seconds=30)
    assert ext_pull._interval_seconds(pull) == 30


def test_interval_seconds_falls_back_to_minutes():
    assert ext_pull._interval_seconds(PullConfig(url="https://x", schedule_minutes=5)) == 300


def test_interval_seconds_clamps_floor():
    """手改 config.json 写过小值: 模型归一后调度侧下限兜底, 不打爆接口。"""
    assert ext_pull._interval_seconds(PullConfig(url="https://x", schedule_minutes=0)) == ext_pull._MIN_INTERVAL_S
    assert ext_pull._interval_seconds(PullConfig(url="https://x", schedule_seconds=10)) == 10


# ── 调度循环: 假时钟驱动, 30s 间隔真实生效 ─────────────────────────


class _FakeStore:
    """最小 store: get/upsert 围绕同一个配置对象。"""

    def __init__(self, config: ExtConfig) -> None:
        self._cfg = config

    def get(self, _config_id: str) -> ExtConfig:
        return self._cfg

    def upsert(self, config: ExtConfig, **_kw) -> None:
        self._cfg = config


async def test_interval_loop_pulls_every_30_seconds(monkeypatch):
    """schedule_seconds=30 → 两次拉取间隔 ~30s; 分钟制下会是 ~61s, 断言可区分。"""
    cfg = ExtConfig(
        id="c1", label="t", mode="timeseries", fields=[],
        pull=PullConfig(url="https://x", enabled=True, schedule_minutes=1, schedule_seconds=30),
    )
    store = _FakeStore(cfg)
    clock = {"now": datetime(2026, 3, 2, 9, 0, tzinfo=CN_TZ)}

    class _FakeDateTime(datetime):
        """now() 跟随假时钟 (UTC); 其余行为继承真 datetime。"""

        @classmethod
        def now(cls, tz=None):
            moment = clock["now"].astimezone(tz) if tz else clock["now"]
            return cls(moment.year, moment.month, moment.day, moment.hour,
                       moment.minute, moment.second, moment.microsecond, moment.tzinfo)

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])
    monkeypatch.setattr(ext_pull, "datetime", _FakeDateTime)
    monkeypatch.setattr(ext_pull, "ExtConfigStore", lambda _dir: store)

    pulls: list[datetime] = []

    async def fake_fetch_ingest(config, data_dir, **_kw):
        pulls.append(clock["now"])
        if len(pulls) >= 2:
            config.pull.enabled = False  # 第二次拉完即停用 → 外层循环退出
        return 5, clock["now"].date().isoformat()

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch_ingest)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)  # +1s 防同秒边界抖动

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = "/tmp/nonexistent"

    await asyncio.wait_for(sched._run_loop(cfg), timeout=10)

    assert len(pulls) == 2
    gap = (pulls[1] - pulls[0]).total_seconds()
    assert 30 <= gap <= 32, f"秒级间隔未生效: 实际间隔 {gap}s (期望 ~31s, 分钟制会是 ~61s)"
    # 状态已写回
    assert cfg.pull.last_status == "success" and cfg.pull.last_rows == 5


# ── API: PUT 透传与回读、旧包字段缺失兼容 ──────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda _id: "")
    monkeypatch.setattr(api, "_refresh_views", lambda _request: None)
    monkeypatch.setattr(api.pull_scheduler, "refresh", lambda _path: None)
    ExtConfigStore(tmp_path).upsert(ExtConfig(
        id="snap", label="快照", mode="snapshot", fields=[ExtField("price", "float")],
        pull=PullConfig(url="https://example.test"),
    ))
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(api.router)
    with TestClient(app) as http:
        yield http, tmp_path


def test_configure_pull_persists_schedule_seconds(client):
    http, tmp_path = client

    saved = http.put("/api/ext-data/snap/pull", json={
        "url": "https://example.test", "schedule_minutes": 1, "schedule_seconds": 30,
    })
    assert saved.status_code == 200
    assert saved.json()["pull"]["schedule_seconds"] == 30
    assert ExtConfigStore(tmp_path).get("snap").pull.schedule_seconds == 30

    # 清除秒级 → 回退分钟制 (双字段互斥语义)
    cleared = http.put("/api/ext-data/snap/pull", json={
        "url": "https://example.test", "schedule_minutes": 5, "schedule_seconds": None,
    })
    assert cleared.status_code == 200
    assert cleared.json()["pull"]["schedule_seconds"] is None
    assert ExtConfigStore(tmp_path).get("snap").pull.schedule_minutes == 5


def test_configure_pull_rejects_sub_floor_seconds(client):
    http, _ = client
    rejected = http.put("/api/ext-data/snap/pull", json={
        "url": "https://example.test", "schedule_seconds": 3,
    })
    assert rejected.status_code == 422
