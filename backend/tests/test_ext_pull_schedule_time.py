"""扩展数据拉取的每日固定时刻调度 (PullConfig.schedule_time)。

间隔制 (schedule_minutes) 锁相在启用时刻, 表达不了「每天 09:30 拉一次」;
schedule_time 按北京时间墙钟对齐。判据不依赖跑测试机器的时区: 钉住 cn_now。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.market_time import CN_TZ
from app.services import ext_pull
from app.services.ext_data import ExtConfig, PullConfig

# ── 模型: 默认值 / 序列化 / 非法值归一 ──────────────────────────────

def test_schedule_time_defaults_to_none_and_roundtrips():
    assert PullConfig().schedule_time is None
    assert PullConfig(url="https://x", schedule_time="09:30").schedule_time == "09:30"

    d = PullConfig(url="https://x", schedule_time="21:45").to_dict()
    assert d["schedule_time"] == "21:45"
    assert PullConfig.from_dict(d).schedule_time == "21:45"


def test_schedule_time_missing_in_old_config_falls_back_to_none():
    legacy = PullConfig(url="https://x").to_dict()
    del legacy["schedule_time"]  # 模拟旧版本 config.json
    assert PullConfig.from_dict(legacy).schedule_time is None


@pytest.mark.parametrize("bad", ["", "9:30", "09:3", "24:00", "09:60", "0930", "aa:bb", 930, True])
def test_schedule_time_invalid_values_normalized_to_none(bad):
    assert PullConfig(url="https://x", schedule_time=bad).schedule_time is None


@pytest.mark.parametrize("good", ["00:00", "09:30", "23:59", "09:26"])
def test_schedule_time_valid_values_preserved(good):
    assert PullConfig(url="https://x", schedule_time=good).schedule_time == good


# ── 调度决策: 当日目标时刻 / 是否已拉过 ─────────────────────────────

def test_today_target_replaces_time_keeps_timezone():
    now = datetime(2026, 3, 2, 8, 0, tzinfo=CN_TZ)
    target = ext_pull._today_target(now, "09:30")
    assert (target.year, target.month, target.day, target.hour, target.minute) == (2026, 3, 2, 9, 30)
    assert target.tzinfo == CN_TZ


def test_ran_at_or_after_by_last_run():
    pull = PullConfig(url="https://x")
    # 从未拉过 → 未拉
    assert ext_pull._ran_at_or_after(pull, datetime(2026, 3, 2, 9, 30, tzinfo=CN_TZ)) is False

    # last_run 北京 09:31 ≥ 当日目标 09:30 → 已拉
    pull.last_run = "2026-03-02T01:31:00+00:00"  # UTC = 北京 09:31
    assert ext_pull._ran_at_or_after(pull, datetime(2026, 3, 2, 9, 30, tzinfo=CN_TZ)) is True

    # last_run 北京 09:29 < 目标 → 未拉 (到点会再拉)
    pull.last_run = "2026-03-02T01:29:00+00:00"
    assert ext_pull._ran_at_or_after(pull, datetime(2026, 3, 2, 9, 30, tzinfo=CN_TZ)) is False

    # 昨天拉过 → 今天目标未拉
    pull.last_run = "2026-03-01T01:31:00+00:00"
    assert ext_pull._ran_at_or_after(pull, datetime(2026, 3, 2, 9, 30, tzinfo=CN_TZ)) is False


def test_ran_at_or_after_dirty_last_run_counts_as_not_run():
    """脏 last_run 按「未拉过」处理: 宁可幂等重拉, 不整天静默跳过。"""
    pull = PullConfig(url="https://x", last_run="not-a-date")
    assert ext_pull._ran_at_or_after(pull, datetime(2026, 3, 2, 9, 30, tzinfo=CN_TZ)) is False


# ── API: 请求体校验 ─────────────────────────────────────────────────

def test_pull_config_req_rejects_invalid_schedule_time():
    from app.api.ext_data import PullConfigReq

    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_time="25:00")
    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_time="9:30")

    req = PullConfigReq(url="https://x", schedule_time="09:30")
    assert req.schedule_time == "09:30"
    # None (清除固定时刻, 回退间隔制) 合法
    assert PullConfigReq(url="https://x").schedule_time is None


# ── 调度循环: 假时钟驱动, 两天各到点拉一次 ──────────────────────────

class _FakeStore:
    """最小 store: get/upsert 围绕同一个配置对象。"""

    def __init__(self, config: ExtConfig) -> None:
        self._cfg = config

    def get(self, _config_id: str) -> ExtConfig:
        return self._cfg

    def upsert(self, config: ExtConfig, **_kw) -> None:
        self._cfg = config


async def test_daily_at_loop_pulls_once_per_day(monkeypatch):
    cfg = ExtConfig(
        id="c1", label="t", mode="timeseries", fields=[],
        pull=PullConfig(url="https://x", enabled=True, schedule_time="09:30"),
    )
    store = _FakeStore(cfg)
    clock = {"now": datetime(2026, 3, 2, 8, 0, tzinfo=CN_TZ)}

    class _FakeDateTime(datetime):
        """now() 跟随假时钟 (UTC); 其余行为继承真 datetime。"""

        @classmethod
        def now(cls, tz=None):
            moment = clock["now"].astimezone(tz) if tz else clock["now"]
            return cls(moment.year, moment.month, moment.day, moment.hour,
                       moment.minute, moment.second, moment.microsecond, moment.tzinfo)

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])
    monkeypatch.setattr(ext_pull, "datetime", _FakeDateTime)

    pulls: list[str] = []

    async def fake_fetch_ingest(config, data_dir, **_kw):
        pulls.append(clock["now"].isoformat())
        if len(pulls) >= 2:
            # 第二次拉完即停用 → 循环返回, 任务收尾
            config.pull.enabled = False
        return 5, clock["now"].date().isoformat()

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch_ingest)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)  # +1s 防同秒边界抖动

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = "/tmp/nonexistent"

    await asyncio.wait_for(
        sched._run_daily_at("c1", store, "09:30"), timeout=10
    )

    # 两天各恰好一次, 时刻都落在 09:30 出头 (分片轮询的秒级迟到内)
    assert len(pulls) == 2
    assert all(p[:16] in ("2026-03-02T09:30", "2026-03-03T09:30") for p in pulls)
    assert pulls[0][:10] == "2026-03-02" and pulls[1][:10] == "2026-03-03"
    # 状态与下次运行预告已写回
    assert cfg.pull.last_status == "success" and cfg.pull.last_rows == 5
    assert cfg.pull.next_run is not None


async def test_daily_at_loop_catches_up_if_enabled_after_target(monkeypatch):
    """启用时已过当日时刻且当日未拉过 → 立即补拉一次 (启用即对齐)。"""
    cfg = ExtConfig(
        id="c1", label="t", mode="timeseries", fields=[],
        pull=PullConfig(url="https://x", enabled=True, schedule_time="09:30"),
    )
    store = _FakeStore(cfg)
    clock = {"now": datetime(2026, 3, 2, 22, 0, tzinfo=CN_TZ)}

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])

    async def fake_fetch_ingest(config, data_dir, **_kw):
        config.pull.enabled = False  # 拉一次即停
        return 1, "2026-03-02"

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch_ingest)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = "/tmp/nonexistent"

    await asyncio.wait_for(sched._run_daily_at("c1", store, "09:30"), timeout=10)
    assert cfg.pull.last_status == "success"  # 22:00 启用, 当天 09:30 已过 → 立即拉
