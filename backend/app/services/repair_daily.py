"""修正 / 补全日K数据 — 完全复用盘后管道,只是日期范围由用户传入。

典型场景: 昨天没看盘 / 服务挂了一天,本地日K缺了若干天。

设计原则: 不重复盘后管道的任何逻辑。直接调用 daily_pipeline.run_now(),
通过 override_start_date 参数把"自动算日期"换成"用户指定起点",
其余 (维表 / A股日K / 除权因子 / enriched / 指数 / ETF / 错误兜底) 全部原样复用。
这样修正功能与盘后管道永远保持一致,不会出现遗漏。

落盘是 merge-upsert (按 symbol+date 去重 keep="last"), 新数据天然覆盖旧值,
不会产生重复,也不需要先删分区。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def run_repair_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    start_date: date,
    end_date: date | None = None,
    on_progress: Callable | None = None,
) -> dict:
    """修正 / 补全数据 — 复用盘后管道,日期范围由用户指定。

    通过 run_now(override_start_date=start_date) 把日K/除权/指数的拉取起点
    统一设为 start_date (到今天),其余流程与盘后管道完全一致。

    Args:
        repo:        数据仓库
        capset:      权限集
        start_date:  用户选定的起始日期
        end_date:    保留参数(目前盘后管道固定拉到今天, 此值未使用, 为接口兼容保留)
        on_progress: 进度回调

    Returns:
        run_now() 的完整结果 dict。
    """
    today = date.today()
    if start_date > today:
        return {"error": "起始日期不能晚于今天"}

    logger.info("repair_daily: run pipeline with override_start_date=%s", start_date)

    from app.jobs.daily_pipeline import run_now
    return run_now(repo, capset, on_progress=on_progress, override_start_date=start_date)
