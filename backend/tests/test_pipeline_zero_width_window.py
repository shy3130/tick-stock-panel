"""#574: free/none 档收盘后的日K批量窗口不得为零宽 (start == end)。

当天分区已存在(15:00 实时行情写过)时, 旧实现取 start_date = latest_daily = today,
右端点同样是 today; 批量接口按 [start, end) 语义取数, 零宽窗口返回 200 空 payload,
0 行入库却仍判 succeeded, 当天官方收盘价永远拉不进来(只能等次日 gap fill)。

本测试只钉窗口构造: 非零宽且跨到次日 00:00。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.services import instrument_sync, kline_sync

TODAY = date(2026, 3, 2)


class _StopError(Exception):
    """日K窗口已经记下, 后面的管道阶段与本 bug 无关。"""


def test_closed_day_batch_window_ends_after_today(monkeypatch, tmp_path) -> None:
    captured: list[tuple[date, date]] = []

    def fake_batch(universe, repo, capset, start_date=None, end_date=None,
                   on_chunk_done=None, failed_out=None):
        captured.append((start_date.date(), end_date.date()))
        raise _StopError()

    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: TODAY, raising=False)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe",
                        lambda capset, repo=None: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", fake_batch)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        # 当天分区已存在(收盘前 15:00 实时行情写盘中快照)
        latest_daily_date=lambda: TODAY,
    )
    capset = SimpleNamespace(has=lambda key: False)  # free/none 档: 无 QUOTE_POOL

    with pytest.raises(_StopError):
        daily_pipeline.run_now(repo, capset)  # type: ignore[arg-type]

    assert captured, "应发起日K范围拉取"
    start, end = captured[0]
    assert start == TODAY
    assert end == TODAY + timedelta(days=1), (
        f"右端点须为次日 00:00, 实际 [{start} ~ {end}]: "
        "零宽窗口会让批量接口返回 0 行却仍判成功 (#574)")
