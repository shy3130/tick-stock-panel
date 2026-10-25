"""自动策略实验室调度单入口 (P3): 周度挖掘 + 每日守护一次挂载。

替代 daily_pipeline 直接调用两个调度函数的旧接线; 原入口
(run_weekly_mining / run_daily_guardian) 保持独立可用, 便于单独触发与测试。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_scheduled_lab(app_state: Any) -> dict[str, Any]:
    """收盘管道成功后依次触发周度挖掘与每日守护复验 (各自独立防重)。"""
    from app.services.guardian_schedule import run_daily_guardian
    from app.services.mining_schedule import run_weekly_mining

    results: dict[str, Any] = {"mining": None, "guardian": None}
    try:
        if not getattr(app_state, "mining_lock_acquired", True):
            # 降级模式 (挖掘锁被其他进程占用): 跳过周度挖掘而不是报错,
            # 守护复验不受影响
            logger.warning("scheduled lab mining skipped: mining lock unavailable")
            results["mining"] = {"skipped": "mining lock unavailable"}
        else:
            results["mining"] = run_weekly_mining(app_state)
    except Exception:
        logger.exception("scheduled lab mining enqueue failed")
    try:
        results["guardian"] = run_daily_guardian(app_state)
    except Exception:
        logger.exception("scheduled lab guardian enqueue failed")
    return results
