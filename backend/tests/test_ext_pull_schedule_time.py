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


# ── 周期扩展: schedule_period / schedule_day / schedule_trading_only ──

def test_schedule_period_defaults_and_roundtrips():
    assert PullConfig().schedule_period == "daily"
    assert PullConfig().schedule_day == 1
    assert PullConfig().schedule_trading_only is False

    p = PullConfig(url="https://x", schedule_time="09:30",
                   schedule_period="weekly", schedule_day=3, schedule_trading_only=True)
    d = p.to_dict()
    assert d["schedule_period"] == "weekly"
    assert d["schedule_day"] == 3
    assert d["schedule_trading_only"] is True
    r = PullConfig.from_dict(d)
    assert (r.schedule_period, r.schedule_day, r.schedule_trading_only) == ("weekly", 3, True)


def test_schedule_period_invalid_normalized():
    for bad in ["hourly", "", None, 5]:
        assert PullConfig(url="https://x", schedule_period=bad).schedule_period == "daily"
    for bad in [0, 32, -1, "x", None]:
        assert PullConfig(url="https://x", schedule_day=bad).schedule_day == 1


def test_pull_config_req_accepts_period_fields():
    from app.api.ext_data import PullConfigReq

    req = PullConfigReq(url="https://x", schedule_time="09:30",
                        schedule_period="weekly", schedule_day=3, schedule_trading_only=True)
    assert (req.schedule_period, req.schedule_day, req.schedule_trading_only) == ("weekly", 3, True)

    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_period="hourly")
    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_day=0)
    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", schedule_day=32)
    # 旧调用方不带周期字段 → 缺省合法 (回退每日)
    assert PullConfigReq(url="https://x").schedule_period is None


def test_is_scheduled_day_by_period():
    # 2026-03-02 是周一, 03-07 周六, 03-08 周日
    daily = PullConfig(url="https://x", schedule_time="09:30")
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 2, tzinfo=CN_TZ), daily) is True
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 7, tzinfo=CN_TZ), daily) is True

    weekly = PullConfig(url="https://x", schedule_period="weekly", schedule_day=1)
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 2, tzinfo=CN_TZ), weekly) is True
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 3, tzinfo=CN_TZ), weekly) is False

    # 手改 config.json 写入越界 day (weekly 8..31) → 视为 1=周一
    weekly_bad = PullConfig(url="https://x", schedule_period="weekly", schedule_day=9)
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 2, tzinfo=CN_TZ), weekly_bad) is True

    monthly = PullConfig(url="https://x", schedule_period="monthly", schedule_day=15)
    assert ext_pull._is_scheduled_day(datetime(2026, 4, 15, tzinfo=CN_TZ), monthly) is True
    assert ext_pull._is_scheduled_day(datetime(2026, 4, 14, tzinfo=CN_TZ), monthly) is False

    # 2 月无 30 日 → 整月不命中 (该月跳过)
    m30 = PullConfig(url="https://x", schedule_period="monthly", schedule_day=30)
    assert all(
        ext_pull._is_scheduled_day(datetime(2026, 2, d, tzinfo=CN_TZ), m30) is False
        for d in range(1, 29)
    )


def test_is_scheduled_day_trading_only_skips_weekend():
    pull = PullConfig(url="https://x", schedule_trading_only=True)
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 6, tzinfo=CN_TZ), pull) is True   # 周五
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 7, tzinfo=CN_TZ), pull) is False  # 周六
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 8, tzinfo=CN_TZ), pull) is False  # 周日
    assert ext_pull._is_scheduled_day(datetime(2026, 3, 9, tzinfo=CN_TZ), pull) is True   # 周一


def test_next_scheduled_target_advances_to_plan_day():
    weekly = PullConfig(url="https://x", schedule_period="weekly", schedule_day=1)

    # 周二 10:00 (本周一已过) → 下周一 09:30
    nxt = ext_pull._next_scheduled_target(datetime(2026, 3, 3, 10, 0, tzinfo=CN_TZ), "09:30", weekly)
    assert nxt == datetime(2026, 3, 9, 9, 30, tzinfo=CN_TZ)

    # 周一 08:00 (计划日, 时刻未到) → 今天
    nxt2 = ext_pull._next_scheduled_target(datetime(2026, 3, 2, 8, 0, tzinfo=CN_TZ), "09:30", weekly)
    assert nxt2 == datetime(2026, 3, 2, 9, 30, tzinfo=CN_TZ)

    # 周一 10:00 (已过时刻) → 下周一
    nxt3 = ext_pull._next_scheduled_target(datetime(2026, 3, 2, 10, 0, tzinfo=CN_TZ), "09:30", weekly)
    assert nxt3.date().isoformat() == "2026-03-09"

    # 仅交易日: 周五过点 → 跳过周末, 下一个是周一
    daily_to = PullConfig(url="https://x", schedule_trading_only=True)
    nxt4 = ext_pull._next_scheduled_target(datetime(2026, 3, 6, 10, 0, tzinfo=CN_TZ), "09:30", daily_to)
    assert nxt4.date().isoformat() == "2026-03-09"

    # 每月 31 号, 当前 2 月 (无 31 日) → 推进到 3 月 31
    m31 = PullConfig(url="https://x", schedule_period="monthly", schedule_day=31)
    nxt5 = ext_pull._next_scheduled_target(datetime(2026, 2, 1, 8, 0, tzinfo=CN_TZ), "09:30", m31)
    assert nxt5.date().isoformat() == "2026-03-31"


async def test_weekly_loop_waits_for_plan_day_no_catchup_on_off_day(monkeypatch):
    """周二 10:00 启用「每周一 09:30」→ 今天非计划日不补拉, 等到下周一到点才拉。"""
    cfg = ExtConfig(
        id="c1", label="t", mode="timeseries", fields=[],
        pull=PullConfig(url="https://x", enabled=True, schedule_time="09:30",
                        schedule_period="weekly", schedule_day=1),
    )
    store = _FakeStore(cfg)
    clock = {"now": datetime(2026, 3, 3, 10, 0, tzinfo=CN_TZ)}  # 周二 10:00

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = clock["now"].astimezone(tz) if tz else clock["now"]
            return cls(moment.year, moment.month, moment.day, moment.hour,
                       moment.minute, moment.second, moment.microsecond, moment.tzinfo)

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])
    monkeypatch.setattr(ext_pull, "datetime", _FakeDateTime)

    async def fake_fetch_ingest(config, data_dir, **_kw):
        config.pull.enabled = False  # 拉一次即停
        return 3, clock["now"].date().isoformat()

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch_ingest)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = "/tmp/nonexistent"

    await asyncio.wait_for(sched._run_daily_at("c1", store, "09:30"), timeout=10)

    # 拉取发生在下周一 2026-03-09 09:30 出头, 而非启用的周二 (无补拉)
    assert cfg.pull.last_status == "success"
    assert clock["now"].date().isoformat() == "2026-03-09"
    assert clock["now"].hour == 9 and clock["now"].minute == 30


async def test_trading_only_loop_skips_weekend(monkeypatch):
    """仅交易日 + 周六启用 → 周六日不拉, 周一 09:30 到点拉。"""
    cfg = ExtConfig(
        id="c1", label="t", mode="timeseries", fields=[],
        pull=PullConfig(url="https://x", enabled=True, schedule_time="09:30",
                        schedule_trading_only=True),
    )
    store = _FakeStore(cfg)
    clock = {"now": datetime(2026, 3, 7, 20, 0, tzinfo=CN_TZ)}  # 周六 20:00

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = clock["now"].astimezone(tz) if tz else clock["now"]
            return cls(moment.year, moment.month, moment.day, moment.hour,
                       moment.minute, moment.second, moment.microsecond, moment.tzinfo)

    monkeypatch.setattr(ext_pull, "cn_now", lambda: clock["now"])
    monkeypatch.setattr(ext_pull, "datetime", _FakeDateTime)

    async def fake_fetch_ingest(config, data_dir, **_kw):
        config.pull.enabled = False
        return 7, clock["now"].date().isoformat()

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch_ingest)

    async def fake_sleep(sec):
        clock["now"] += timedelta(seconds=sec + 1)

    monkeypatch.setattr(ext_pull.asyncio, "sleep", fake_sleep)

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._data_dir = "/tmp/nonexistent"

    await asyncio.wait_for(sched._run_daily_at("c1", store, "09:30"), timeout=10)

    # 注意: 周六 20:00 启用且当天已过 09:30, 若无交易日过滤会立即补拉;
    # 过滤后应等到周一才拉
    assert cfg.pull.last_status == "success"
    assert clock["now"].date().isoformat() == "2026-03-09"
    assert clock["now"].hour == 9 and clock["now"].minute == 30


def test_interval_sleep_wakes_when_switched_to_daily_at(tmp_path, monkeypatch) -> None:
    """间隔制睡眠期间切到固定时刻: 必须一分钟内生效, 不能睡满整个间隔。

    回归: 24h 间隔制的旧任务会把新设置的 09:30 调度推迟到第二天。
    """
    import asyncio

    from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
    from app.services.ext_pull import PullScheduler

    store = ExtConfigStore(tmp_path)
    store.upsert(ExtConfig(
        id="demo", label="demo", mode="snapshot",
        fields=[ExtField("symbol")],
        pull=PullConfig(url="https://x.test/d", enabled=True, schedule_minutes=1440),
    ), keep_strategy_cache=True)

    scheduler = PullScheduler()
    scheduler.start(tmp_path)
    scheduler._running = True
    wake = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(_seconds):
        wake["n"] += 1
        # 第一次分片睡眠后模拟用户改成固定时刻
        cfg = store.get("demo")
        cfg.pull.schedule_time = "09:30"
        cfg.pull.schedule_period = "daily"
        store.upsert(cfg, keep_strategy_cache=True)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(scheduler._sleep_interval_reloading("demo", store, 1440 * 60))
    assert wake["n"] == 1  # 只睡了一个分片就返回, 交回外层重排
