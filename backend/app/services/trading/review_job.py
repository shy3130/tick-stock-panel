"""盘后状态驱动 AI 归因 (L0/L1/L2 触发)。

把「何时调 AI 归因」从「用户手动点」升级为「状态驱动」:

- L0 无新红旗且无新平仓 → 返回零字段, 零 AI 调用 (no_change 语义, 省 token)。
- L1 有候选 → 只对候选 trades 跑 ``autopsy.run_autopsy``; 已归因且事件数未变 → skip。
- L2 用户手动全量 → 仍走现有单条 ``POST /trades/{id}/autopsy``, 本模块不实现。

候选 = 最近 1 个自然日内出现新红旗的 trades (``scan_all`` 按 ts 过滤, 跳过 "global" 分组)
     + 最近 1 日新平仓的 trades (closedAt 在近 1 日)。

去重: 已落盘归因 (``read_autopsy`` 非 None) 且其记录的 eventCount == 当前事件数 →
      skip 计数 +1, 不重复消耗 AI。eventCount 由本模块在归因成功后补写进归因文件
      (additive, 向后兼容); 无 eventCount 的旧记录视为需重跑。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.errors import BLOCKED_BY_DEPENDENCY
from app.services.ai_provider import ai_configured
from app.services.trading import store
from app.services.trading.autopsy import read_autopsy, run_autopsy
from app.services.trading.models import STATUS_CLOSED
from app.services.trading.red_flags import scan_all

logger = logging.getLogger(__name__)

# 候选时间窗 (天): 仅归因近 1 日出现新红旗 / 新平仓的笔
CANDIDATE_WINDOW_DAYS = 1

# now_str() 输出 "%Y-%m-%d %H:%M"; 兼容 ISO / 带秒 / 纯日期
_TS_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def _parse_ts(value: Any) -> datetime | None:
    """宽松解析时间戳 (now_str / ISO / 带秒)。失败返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:  # 带时区的 ISO 串
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _autopsy_file(data_dir: Path, trade_id: str) -> Path:
    """归因记录路径 (与 autopsy._safe_id / _autopsies_dir 同口径)。"""
    safe = trade_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return store.trading_dir(data_dir) / "autopsies" / f"{safe}.json"


def _collect_candidates(
    data_dir: Path,
    now: datetime | None = None,
    window_days: int = CANDIDATE_WINDOW_DAYS,
) -> set[str]:
    """收集候选 trade_id: 近 window_days 内有新红旗或新平仓的笔 (纯代码, 无 AI)。

    - 新红旗: ``scan_all`` 结果按 flag.ts 过滤; P6.1 在 scan_all 增加 "global" 分组,
      属全局级提示而非单笔, 故跳过。
    - 新平仓: ``list_trades`` 中 status=已平仓 且 closedAt 在窗口内。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=window_days)
    candidates: set[str] = set()

    # 1. 新红旗 (scan_all 按读取时实时计算, 纯代码)
    for trade_id, flags in scan_all(data_dir).items():
        if trade_id == "global":  # 全局级红旗 (门禁膨胀等), 非单笔候选
            continue
        for flag in flags:
            ts = _parse_ts(flag.get("ts"))
            if ts is not None and ts >= cutoff:
                candidates.add(trade_id)
                break

    # 2. 新平仓
    for trade in store.list_trades(data_dir):
        if trade.get("status") != STATUS_CLOSED:
            continue
        closed_at = _parse_ts(trade.get("closedAt"))
        if closed_at is not None and closed_at >= cutoff:
            tid = trade.get("tradeId")
            if tid:
                candidates.add(tid)

    return candidates


async def run_state_driven_autopsy(data_dir: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """盘后状态驱动 AI 归因。

    ``now`` 可注入 (默认 ``datetime.now()``), 用于测试与确定性调度; 其语义与
    ``_collect_candidates`` 的时间窗完全一致 —— 近 1 日内的新红旗 / 新平仓才入候选。

    返回 schema::

        {
          "level": "L0" | "L1",
          "candidates": int,
          "autopsied": int,
          "skipped": int,
          "errors": [{"tradeId": str, "error": str}],   # L1 才有
          "code"?: str,                                  # AI 未配置时 "blocked_by_dependency"
          "detail"?: str,
        }

    - L0 无候选 → 零 AI 调用, 返回 ``{"level":"L0","candidates":0,"autopsied":0,"skipped":0}``。
    - L1 + AI 未配置 → 返回 ``blocked_by_dependency`` 语义 (code/detail), 不抛异常, autopsied=0。
    - L1 正常: 逐候选归因; 已归因且事件数未变 skip; 单笔失败记 errors 继续 (fail-soft)。
    """
    data_dir = Path(data_dir)
    candidates = _collect_candidates(data_dir, now=now or datetime.now())

    if not candidates:
        logger.info("trading auto-review L0: no candidates, zero AI calls")
        return {"level": "L0", "candidates": 0, "autopsied": 0, "skipped": 0}

    # L1: AI 未配置 → blocked_by_dependency, 不中断调度
    if not ai_configured():
        logger.info(
            "trading auto-review L1: %d candidates but AI not configured", len(candidates)
        )
        return {
            "level": "L1",
            "candidates": len(candidates),
            "autopsied": 0,
            "skipped": 0,
            "errors": [],
            "code": BLOCKED_BY_DEPENDENCY,
            "detail": "AI 未配置,无法执行归因分析",
        }

    autopsied = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    for trade_id in sorted(candidates):
        try:
            existing = read_autopsy(data_dir, trade_id)
            current_count = len(store.read_events(data_dir, trade_id))
            # 去重: 已归因且事件数未变 → skip
            if existing is not None and existing.get("eventCount") == current_count:
                skipped += 1
                continue
            result = await run_autopsy(data_dir, trade_id)
            # 补写事件数供下次去重 (run_autopsy 已落盘, 此处仅追加 additive 字段)。
            # 归因目录可能尚未创建 (AI 调用被 mock 的测试场景), 确保目录存在再写。
            result["eventCount"] = current_count
            ap = _autopsy_file(data_dir, trade_id)
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            autopsied += 1
        except Exception as e:  # noqa: BLE001 — fail-soft, 单笔失败不影响其他
            errors.append({"tradeId": trade_id, "error": str(e)})
            logger.warning("trading auto-review autopsy failed for %s: %s", trade_id, e)

    logger.info(
        "trading auto-review L1: candidates=%d autopsied=%d skipped=%d errors=%d",
        len(candidates), autopsied, skipped, len(errors),
    )
    return {
        "level": "L1",
        "candidates": len(candidates),
        "autopsied": autopsied,
        "skipped": skipped,
        "errors": errors,
    }
