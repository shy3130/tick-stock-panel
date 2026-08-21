"""条件筛选方案 (screener screen) → 策略桥。

把 `user_data/screener_screens.json` 里保存的选股方案注册为
`screen:<hex>` 策略, 供策略监控 (type=strategy 规则) 与策略回测复用。

设计要点 (fail-closed):
- 方案条件即全部语义: basic_filter 关闭, 绝不叠加引擎默认过滤;
- 谓词复用 `screener_query.compile_predicate` + `_materialize` 口径,
  不复制筛选语义;
- 回测面板 (enriched 两条路径) 没有外部 join 列 (财务/龙虎榜/筹码/
  资金流/融资融券/参考数据), 含这些字段的方案在回测前显式拒绝,
  不静默给出空结果。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import polars as pl

from app.services.screener_query import (
    ScreenerSemanticError,
    _materialize,
    compile_predicate,
    get_field_spec,
)
from app.services.screener_screens import list_screens
from app.strategy.engine import StrategyDef, canonical_def_hash

logger = logging.getLogger(__name__)

SCREEN_SOURCE = "screen"
SCREEN_ID_PREFIX = "screen:"

# 回测面板可用列口径: enriched 持久化/运行时列。total_shares/float_shares
# 仅 fresh 路径 (compute_all + instruments join) 有, cache 路径没有 → 保守
# 视为不支持, 即 float/total_market_cap 不支持回测。name 两条路径均可用。
_JOIN_ONLY_COLUMNS = frozenset({"total_shares", "float_shares"})

# FIELD_REGISTRY.source 实际枚举中, 属于外部 join 的取值 (financials /
# reference / tdx_chip / tdx_moneyflow / fstore / unavailable) 一律不支持
# 回测; persist / runtime / persist/runtime / derived 的派生列再按 deps 判。
_PANEL_SOURCES = frozenset({"persist", "runtime", "persist/runtime", "derived"})

_panel_base_columns_cache: frozenset[str] | None = None


def screen_strategy_id(hex_id: str) -> str:
    """方案 id (12 位 hex) → 策略 id `screen:<hex>`。"""
    return f"{SCREEN_ID_PREFIX}{hex_id}"


def _panel_base_columns() -> frozenset[str]:
    """回测面板可用基础列 (懒加载缓存, 避免模块导入期拉起指标管线)。"""
    global _panel_base_columns_cache
    if _panel_base_columns_cache is None:
        from app.indicators.pipeline import ENRICHED_COLUMNS

        _panel_base_columns_cache = frozenset(ENRICHED_COLUMNS) - _JOIN_ONLY_COLUMNS
    return _panel_base_columns_cache


def _field_base_columns(field: str) -> set[str]:
    """字段的谓词求值所需基础列: 有 deps 用 deps, 否则字段自身即列。"""
    spec = get_field_spec(field)
    return set(spec.deps) if spec.deps else {field}



def classify_screen(screen: dict[str, Any]) -> tuple[bool, list[str]]:
    """判断方案是否可注册为回测/监控可算的策略 (保守白名单, 纯 registry 无 IO)。

    规则:
    - 多日序列字段 (source == "sequence") 一律 unsupported — 回测/监控 v1
      面板是单日 enriched, 不提供历史窗口求值;
    - registry source 属外部 join (financials/reference/tdx_chip/
      tdx_moneyflow/fstore/unavailable) → 不支持;
    - 派生字段 deps 不在回测面板列集 (ENRICHED_COLUMNS 去掉
      total_shares/float_shares, 保留 name) → 不支持;
    - order_by 不参与面板求值 (只影响列表排序), 不作为拒绝理由。

    返回 (supported, unsupported_field_names), 名称为字段中文名。
    """
    available = _panel_base_columns()
    unsupported: list[str] = []
    condition_fields: list[str] = []
    for cond in screen.get("conditions") or []:
        if isinstance(cond, dict) and isinstance(cond.get("field"), str):
            condition_fields.append(cond["field"])
    for field in dict.fromkeys(condition_fields):
        spec = get_field_spec(field)
        if spec is None:
            unsupported.append(field)
        elif spec.source == "sequence":
            unsupported.append(spec.label)
        elif spec.source not in _PANEL_SOURCES or not _field_base_columns(field) <= available:
            unsupported.append(spec.label)
    return (not unsupported, unsupported)


class ScreenPanelUnsupportedError(Exception):
    """方案字段在回测面板中不可求值 (fail-closed, 携带缺失列)。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(dict.fromkeys(missing))
        super().__init__("回测面板缺少方案所需字段: " + "、".join(self.missing))


def screen_record_of(strategy: StrategyDef) -> dict[str, Any] | None:
    """取 build_screen_strategy 附着在策略定义上的原始方案记录 (非 screen 策略为 None)。"""
    return getattr(strategy, "screen_record", None)


def build_screen_strategy(screen: dict[str, Any]) -> StrategyDef:
    """把方案记录构建为 `screen:<hex>` 策略定义。

    谓词非法 (字段/操作符/取值不合法) 时向上抛 ScreenerSemanticError /
    pydantic 校验错误, 由调用方 (sync) 记 warning 跳过。
    order_by 不参与策略语义 (排序只影响条件页列表展示), 这里刻意不
    传入 compile_predicate — 否则 unsortable 的排序字段 (industry 等)
    会让一个 classify 判定可注册的方案在 sync 时被整份跳过。
    """
    # classify 只看条件字段; 含外部 join / sequence 字段的方案不可注册
    # 为策略 (面板不可求值), fail-closed 在源头拒绝而不是运行时 500。
    supported, _unsupported = classify_screen(screen)
    if not supported:
        unsupported_labels = "、".join(_unsupported)
        raise ScreenerSemanticError(
            "conditions",
            f"方案包含策略面板不可求值的字段: {unsupported_labels}",
        )
    expression, applied, _order = compile_predicate(
        screen.get("conditions") or [],
        None,
        group_logic=screen.get("group_logic") or "and",
    )
    # 条件字段 (order 不参与面板求值, 只影响列表排序)
    condition_fields = list(dict.fromkeys(
        cond["field"] for cond in applied if isinstance(cond.get("field"), str)
    ))
    required_base: set[str] = set()
    for f in condition_fields:
        required_base |= _field_base_columns(f)
    predicate_json = {
        "conditions": applied,
        "group_logic": screen.get("group_logic") or "and",
    }
    strategy_id = screen_strategy_id(str(screen["id"]))
    n_conditions = len(applied)

    def filter_fn(panel: pl.DataFrame, params: dict) -> pl.Expr:
        # fail-closed: 基础列缺失 → 显式报错, 不给空结果。
        missing = required_base - set(panel.columns)
        if missing:
            raise ScreenPanelUnsupportedError(sorted(missing))
        # 复用筛选端 _materialize 口径补齐派生公开列 (above_ma20 等)。
        df = _materialize(panel, set(condition_fields))
        mask = df.select(expression.alias("_screen_hit"))["_screen_hit"]
        return pl.lit(mask.fill_null(False).cast(pl.Boolean))

    strategy = StrategyDef(
        meta={
            "id": strategy_id,
            "name": screen["name"],
            "description": f"条件筛选方案: {n_conditions} 个条件",
            "category": "我的方案",
            "params": [],
            "asset_types": ["stock"],
        },
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=filter_fn,
        filter_history_fn=None,
        lookback_days=1,
        source=SCREEN_SOURCE,
        file_path=None,
        # 同谓词同 hash, 谓词变更 hash 变更 (F13 变更提示口径)。
        def_hash=canonical_def_hash(predicate_json),
    )
    # 附着原始方案记录, 供回测端 run() 预检复用 classify_screen。
    strategy.screen_record = screen  # type: ignore[attr-defined]
    return strategy


def sync_screen_strategies(engine: Any, data_dir: Path | str) -> int:
    """把存储中的全部方案同步注册进策略引擎 (幂等)。

    先移除引擎里所有现存 `screen:` 前缀条目再重建; 单个方案构建失败
    (谓词非法或 classify 判不可注册) 记日志跳过, 不阻塞其余。返回注册数。
    非 StrategyEngine 替身 (无 _strategies dict, 如测试 stub) 直接跳过。
    """
    strategies = getattr(engine, "_strategies", None)
    if not isinstance(strategies, dict):
        return 0
    for sid in [k for k in strategies if k.startswith(SCREEN_ID_PREFIX)]:
        strategies.pop(sid, None)
    registered = 0
    for screen in list_screens(data_dir):
        try:
            strategy = build_screen_strategy(screen)
        except ScreenerSemanticError as e:
            # classify 拒绝 (外部 join / sequence 字段): 这是数据能力边界
            # 而非程序错误, 用 info 级别 — 前端列表已用 strategy_supported
            # 门控「回测/监控」按钮, 这里只是后端同一口径的兜底。
            logger.info(
                "screen strategy not registrable (skip): id=%s name=%s reason=%s",
                screen.get("id"), screen.get("name"), e,
            )
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "screen strategy build failed (skip): id=%s name=%s error=%s",
                screen.get("id"), screen.get("name"), e,
            )
            continue
        strategies[strategy.meta["id"]] = strategy
        registered += 1
    return registered
