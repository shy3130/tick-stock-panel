"""扩展数据定时拉取的时间基准测试 — 窗口与落盘日期都按北京时间。

背景: `app/market_time.py` 开篇写明「服务器/容器本地时区不可靠 (python:slim
镜像默认 UTC), 交易时段判断、实时行情落盘日期等必须显式使用北京时间, 否则
Docker 部署时轮询窗口与真实交易时段完全错开」。

拉取窗口正是照着 A 股交易时段设的, 落盘分区正是行情日期, 两处却都用了裸的
`datetime.now()` / `date.today()`。UTC 容器里 09:30-15:00 的窗口实际落在北京
17:30-23:00, 每天都在收盘之后; 北京时间 08:00 之前落盘写的是前一天的分区。

判据不依赖跑测试的机器在哪个时区: 固定一个北京时刻, 断言窗口按它判断。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.market_time import CN_TZ
from app.services import ext_pull


@pytest.fixture
def at_beijing(monkeypatch: pytest.MonkeyPatch):
    """把北京时间钉在给定时刻。"""

    def freeze(moment: datetime):
        monkeypatch.setattr(ext_pull, "cn_now", lambda: moment)
        monkeypatch.setattr(ext_pull, "cn_today", lambda: moment.date())

    return freeze


def test_window_follows_beijing_time_not_the_machine_clock(at_beijing) -> None:
    # 北京 10:00, 正在早盘。
    at_beijing(datetime(2026, 3, 2, 10, 0, tzinfo=CN_TZ))
    assert ext_pull._in_time_window("09:30", "15:00") is True

    # 北京 17:30 — UTC 容器上「本地 09:30」正是这个时刻, 修复前会误判为在窗口内。
    at_beijing(datetime(2026, 3, 2, 17, 30, tzinfo=CN_TZ))
    assert ext_pull._in_time_window("09:30", "15:00") is False


def test_window_across_midnight_still_works(at_beijing) -> None:
    at_beijing(datetime(2026, 3, 2, 23, 0, tzinfo=CN_TZ))
    assert ext_pull._in_time_window("22:00", "02:00") is True
    at_beijing(datetime(2026, 3, 2, 1, 0, tzinfo=CN_TZ))
    assert ext_pull._in_time_window("22:00", "02:00") is True
    at_beijing(datetime(2026, 3, 2, 12, 0, tzinfo=CN_TZ))
    assert ext_pull._in_time_window("22:00", "02:00") is False


def test_no_window_configured_is_still_unrestricted(at_beijing) -> None:
    at_beijing(datetime(2026, 3, 2, 3, 0, tzinfo=CN_TZ))
    assert ext_pull._in_time_window(None, None) is True
    assert ext_pull._in_time_window("09:30", None) is True
    assert ext_pull._in_time_window(None, "15:00") is True


async def test_default_landing_date_is_the_beijing_date(at_beijing, monkeypatch) -> None:
    """不传 target_date 时按北京日期落盘。

    北京 00:30 对应 UTC 前一天 16:30, 裸 date.today() 在 UTC 容器上会把当天的
    数据写进前一天的分区。
    """
    at_beijing(datetime(2026, 3, 2, 0, 30, tzinfo=CN_TZ))
    seen: dict[str, date] = {}

    async def fake_fetch(config, target_date, data_dir=None):
        seen["fetched"] = target_date
        return [{"symbol": "000001.SZ", "v": 1}]

    def fake_write(rows, config, data_dir, snapshot_date, **kwargs):
        seen["written"] = snapshot_date
        return len(rows)

    monkeypatch.setattr(ext_pull, "fetch_rows_for_date", fake_fetch)
    monkeypatch.setattr(ext_pull, "rows_to_parquet", fake_write)

    written, day_str = await ext_pull.fetch_and_ingest(object(), "/tmp")

    assert written == 1
    assert seen["fetched"] == date(2026, 3, 2)
    assert seen["written"] == date(2026, 3, 2)
    assert day_str == "2026-03-02"


async def test_an_explicit_target_date_still_wins(at_beijing, monkeypatch) -> None:
    # 历史回补路径不受影响。
    at_beijing(datetime(2026, 3, 2, 10, 0, tzinfo=CN_TZ))
    seen: dict[str, date] = {}

    async def fake_fetch(config, target_date, data_dir=None):
        seen["fetched"] = target_date
        return [{"symbol": "000001.SZ", "v": 1}]

    monkeypatch.setattr(ext_pull, "fetch_rows_for_date", fake_fetch)
    monkeypatch.setattr(ext_pull, "rows_to_parquet", lambda *a, **k: 1)

    _, day_str = await ext_pull.fetch_and_ingest(object(), "/tmp", date(2026, 1, 5))

    assert seen["fetched"] == date(2026, 1, 5)
    assert day_str == "2026-01-05"
