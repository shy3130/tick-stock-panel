"""模拟盘「绑定即评估」— 把账户绑定的策略自动接入实时评估循环。

背景: 模拟盘自动跟单吃的是监控引擎的事件流, 但事件源(策略评估)此前只由
监控页的用户动作(建 type=strategy 规则)开启 — 「绑定」成了订阅了一个
不会打开的事件源。本模块在绑定变化时向 MonitorRuleEngine 注入/移除
**内存态合成规则**(不落盘、不进监控页), 使绑定即生效; 监控页回归本职
(告警推送), 与模拟盘互不依赖。

性能与去重红线:
- 评估引擎仍只有一个, 每策略每轮只评估一次、事件扇出给全部订阅账户;
- 某策略已有启用中的真监控规则时不注入合成规则(双规则会重复推事件);
- 合成事件只喂模拟盘下单, 不进 SSE 告警/系统通知/webhook/留痕
  (quote_service 用 is_paper_watch_event 分流)。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PAPER_WATCH_RULE_PREFIX = "paper_watch:"
# side=auto 跟单需要全部四类事件; 引擎默认只有池事件, 必须显式放开。
_NOTIFY_EVENTS = ("buy_signal", "sell_signal", "pool_entry", "pool_exit")


def is_paper_watch_event(ev: dict) -> bool:
    """事件是否来自模拟盘合成规则 (路由分流用: 只喂模拟盘, 不进告警流)。"""
    return str(ev.get("rule_id") or "").startswith(PAPER_WATCH_RULE_PREFIX)


def synthetic_rule_id(strategy_id: str) -> str:
    return f"{PAPER_WATCH_RULE_PREFIX}{strategy_id}"


def _make_rule(strategy_id: str) -> dict:
    return {
        "id": synthetic_rule_id(strategy_id),
        "name": f"模拟盘自动评估 · {strategy_id}",
        "type": "strategy",
        "strategy_id": strategy_id,
        "asset_type": "stock",
        # scope 默认 "symbols" 且空列表会把候选过滤成空, 必须显式全市场
        "scope": "all",
        "enabled": True,
        "notify_events": list(_NOTIFY_EVENTS),
    }


def sync_from_state(app_state: Any, data_dir: Path) -> dict[str, Any] | None:
    """API 层便捷入口: 从 app.state 取两个引擎执行 sync。

    辅助钩子, 不得影响调用方主流程: 引擎未就绪返回 None;
    引擎形态异常/内部错误只告警不抛出 (单元测试直连 sync() 覆盖真实逻辑)。
    """
    monitor_engine = getattr(app_state, "monitor_engine", None)
    strategy_engine = getattr(app_state, "strategy_engine", None)
    if monitor_engine is None or strategy_engine is None:
        return None
    try:
        return sync(data_dir, monitor_engine, strategy_engine)
    except Exception as e:  # noqa: BLE001
        logger.warning("paper watch sync failed (ignored): %s", e)
        return None


def watched_strategy_ids(data_dir: Path) -> set[str]:
    """全部账户「启用的 strategy 型自动跟单规则」绑定的策略 id 集合。"""
    from app.strategy import paper, paper_auto

    ids: set[str] = set()
    for acc_id in paper.list_account_ids(data_dir):
        for rule in paper_auto.load_auto_rules(data_dir, acc_id, enabled_only=True):
            if rule.get("match_kind") == "strategy" and rule.get("match_id"):
                ids.add(str(rule["match_id"]))
    return ids


def sync(data_dir: Path, monitor_engine: Any, strategy_engine: Any) -> dict[str, Any]:
    """把「未被真监控规则覆盖的绑定策略」增量注入/移出评估循环 (幂等)。

    必须在每次 set_rules (全量覆盖会清掉合成规则) 之后重新调用 —
    接线点见 main 启动 / monitor_rules._sync_engine / paper 规则 CRUD / 策略删除。
    """
    covered = {
        str(r.get("strategy_id"))
        for r in monitor_engine.rules.values()
        if r.get("enabled", True)
        and r.get("type") == "strategy"
        and not str(r.get("id") or "").startswith(PAPER_WATCH_RULE_PREFIX)
    }
    want: set[str] = set()
    for sid in watched_strategy_ids(data_dir):
        if sid in covered:
            continue
        try:
            strategy_engine.get(sid)
        except ValueError:
            continue  # 策略已删除/失效, 下次 sync 前引擎侧本就会跳过它
        want.add(sid)

    current = {
        str(r.get("strategy_id"))
        for rid, r in monitor_engine.rules.items()
        if str(rid).startswith(PAPER_WATCH_RULE_PREFIX)
    }
    added = sorted(want - current)
    removed = sorted(current - want)
    for sid in added:
        monitor_engine.add_rule(_make_rule(sid))
    for sid in removed:
        monitor_engine.remove_rule(synthetic_rule_id(sid))
    if added or removed:
        logger.info(
            "paper watch synced: +%s -%s (watching %d strategies)",
            added, removed, len(want),
        )
    return {"added": added, "removed": removed, "watching": sorted(want)}
