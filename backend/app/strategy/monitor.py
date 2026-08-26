"""策略实时监控 — 订阅行情更新，检查策略买卖信号和提醒条件。

职责: 接收实时行情 DataFrame → 检查监控中策略的信号/提醒 → 推送告警。
不知道: 策略加载逻辑、AI、API、配置持久化、回测。
依赖: 外部调用 on_quote_update() 传入实时数据。

本模块含两个评估器:
  1. StrategyMonitorService — 旧的策略监控 (type=strategy),第二步迁移到 MonitorRuleEngine
  2. MonitorRuleEngine — 通用规则引擎,覆盖 signal/price/market/strategy 四类,
     支持 scope (symbols/all/sector) + 多条件 AND/OR + cooldown 去重
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import polars as pl

from app.strategy.custom_signals import _OP_BUILDERS  # type: ignore  # 复用运算符构造器
from app.strategy import config as _strategy_config

logger = logging.getLogger(__name__)

# 信号 / 字段中文名映射 — 与前端 lib/signals.ts 对齐, 用于告警 message / 推送文案。
# signal_* 为内置原子信号, 其余为技术指标/行情字段。
_SIGNAL_CN: dict[str, str] = {
    # 内置信号
    "signal_ma_golden_5_20": "MA5上穿MA20",
    "signal_ma_dead_5_20": "MA5下穿MA20",
    "signal_ma_golden_20_60": "MA20上穿MA60",
    "signal_macd_golden": "MACD金叉",
    "signal_macd_dead": "MACD死叉",
    "signal_ma20_breakout": "突破MA20",
    "signal_ma20_breakdown": "跌破MA20",
    "signal_n_day_high": "60日新高",
    "signal_n_day_low": "60日新低",
    "signal_boll_breakout_upper": "突破布林上轨",
    "signal_boll_breakdown_lower": "跌破布林下轨",
    "signal_volume_surge": "放量",
    "signal_limit_up": "涨停",
    "signal_limit_down": "跌停",
    "signal_limit_down_recovery": "跌停翘板",
    "signal_broken_limit_up": "炸板",
    # 行情字段
    "close": "收盘价",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "change_pct": "涨跌幅",
    "change_amount": "涨跌额",
    "amplitude": "振幅",
    "turnover_rate": "换手率",
    "volume": "成交量",
    "amount": "成交额",
    # 均线
    "ma5": "MA5",
    "ma10": "MA10",
    "ma20": "MA20",
    "ma30": "MA30",
    "ma60": "MA60",
    "ema5": "EMA5",
    "ema10": "EMA10",
    "ema20": "EMA20",
    # MACD / BOLL / KDJ / RSI
    "macd_dif": "MACD-DIF",
    "macd_dea": "MACD-DEA",
    "macd_hist": "MACD柱",
    "boll_upper": "布林上轨",
    "boll_lower": "布林下轨",
    "kdj_k": "KDJ-K",
    "kdj_d": "KDJ-D",
    "kdj_j": "KDJ-J",
    "rsi_6": "RSI6",
    "rsi_14": "RSI14",
    "rsi_24": "RSI24",
    # 量能 / 动量 / 波动
    "vol_ratio_5d": "5日量比",
    "vol_ratio_20d": "20日量比",
    "vol_ma5": "5日均量",
    "vol_ma10": "10日均量",
    "high_60d": "60日最高",
    "low_60d": "60日最低",
    "momentum_5d": "5日动量",
    "momentum_20d": "20日动量",
    "momentum_60d": "60日动量",
    "atr_14": "ATR14",
    "annual_vol_20d": "20日年化波动",
    "consecutive_limit_ups": "连板数",
    "consecutive_limit_downs": "跌停连板",
}


def _signal_cn_name(name: str) -> str:
    """返回信号/字段的中文名, 找不到原样返回 (与前端 cnSignal 对齐)。"""
    return _SIGNAL_CN.get(name, name)


@dataclass
class StrategyAlert:
    """策略告警"""

    type: str  # "entry" | "exit" | "alert"
    strategy_id: str
    symbol: str
    name: str | None
    message: str
    price: float | None = None
    change_pct: float | None = None
    signals: list[str] = field(default_factory=list)


class StrategyMonitorService:
    """策略实时监控服务"""

    def __init__(self, alert_handler: Callable[[StrategyAlert], None] | None = None):
        """
        Args:
            alert_handler: 告警回调 (如推 SSE)
        """
        self._alert_handler = alert_handler
        # strategy_id → 监控配置
        self._watching: dict[str, dict] = {}

    def start(self, strategy_id: str, config: dict) -> None:
        """开始监控一个策略

        config: {
            "entry_signals": ["signal_n_day_high", ...],
            "exit_signals": ["signal_ma20_breakdown", ...],
            "alerts": [{"field": "rsi_14", "op": ">", "value": 80, "message": "..."}],
        }
        """
        self._watching[strategy_id] = config
        logger.info("strategy monitor started: %s", strategy_id)

    def stop(self, strategy_id: str) -> None:
        self._watching.pop(strategy_id, None)
        logger.info("strategy monitor stopped: %s", strategy_id)

    def stop_all(self) -> None:
        self._watching.clear()

    @property
    def watching(self) -> dict[str, dict]:
        return dict(self._watching)

    def on_quote_update(self, df: pl.DataFrame) -> list[StrategyAlert]:
        """行情更新后调用。向量化检查所有监控策略。

        Args:
            df: 实时 enriched 数据 (~5500行)
        Returns:
            触发的告警列表
        """
        if not self._watching or df.is_empty():
            return []

        all_alerts: list[StrategyAlert] = []

        for strategy_id, cfg in self._watching.items():
            # 买入信号
            entry_sigs = cfg.get("entry_signals", [])
            if entry_sigs:
                for sym, name, price, pct, hit_sigs in self._check_signals(df, entry_sigs):
                    alert = StrategyAlert(
                        type="entry",
                        strategy_id=strategy_id,
                        symbol=sym,
                        name=name,
                        message=f"买入信号触发",
                        price=price,
                        change_pct=pct,
                        signals=hit_sigs,
                    )
                    all_alerts.append(alert)
                    self._emit(alert)

            # 卖出信号
            exit_sigs = cfg.get("exit_signals", [])
            if exit_sigs:
                for sym, name, price, pct, hit_sigs in self._check_signals(df, exit_sigs):
                    alert = StrategyAlert(
                        type="exit",
                        strategy_id=strategy_id,
                        symbol=sym,
                        name=name,
                        message=f"卖出信号触发",
                        price=price,
                        change_pct=pct,
                        signals=hit_sigs,
                    )
                    all_alerts.append(alert)
                    self._emit(alert)

            # 提醒条件
            for alert_cfg in cfg.get("alerts", []):
                for sym, name, price, pct in self._check_alert(df, alert_cfg):
                    alert = StrategyAlert(
                        type="alert",
                        strategy_id=strategy_id,
                        symbol=sym,
                        name=name,
                        message=alert_cfg.get("message", "提醒"),
                        price=price,
                        change_pct=pct,
                    )
                    all_alerts.append(alert)
                    self._emit(alert)

        return all_alerts

    def _emit(self, alert: StrategyAlert) -> None:
        if self._alert_handler:
            try:
                self._alert_handler(alert)
            except Exception as e:
                logger.warning("alert handler failed: %s", e)

    @staticmethod
    def _check_signals(
        df: pl.DataFrame,
        signals: list[str],
    ) -> list[tuple[str, str | None, float | None, float | None, list[str]]]:
        """检查信号列，返回 [(symbol, name, price, change_pct, [hit_signals])]。
        支持内置 signal_ 与自定义 csg_ 前缀。"""
        cols = set(df.columns)
        resolved: list[tuple[str, str]] = []  # (原值, 列名)
        for s in signals:
            col = s if (s.startswith("signal_") or s.startswith("csg_")) else f"signal_{s}"
            if col in cols:
                resolved.append((s, col))
        if not resolved:
            return []

        mask = pl.any_horizontal(pl.col(c).fill_null(False) for _, c in resolved)
        hit_df = df.filter(mask)

        results = []
        for row in hit_df.iter_rows(named=True):
            sym = row.get("symbol", "")
            name = row.get("name")
            price = row.get("close")
            pct = row.get("change_pct")
            hit_sigs = [orig for orig, col in resolved if row.get(col)]
            results.append((sym, name, price, pct, hit_sigs))
        return results

    @staticmethod
    def _check_alert(
        df: pl.DataFrame,
        alert: dict,
    ) -> list[tuple[str, str | None, float | None, float | None]]:
        """检查阈值型提醒条件"""
        field = alert.get("field", "")
        if field not in df.columns:
            return []

        if "op" in alert:
            # 阈值比较
            op = alert["op"]
            value = alert["value"]
            col = pl.col(field)
            ops = {
                ">": col > value,
                ">=": col >= value,
                "<": col < value,
                "<=": col <= value,
            }
            expr = ops.get(op)
            if expr is None:
                return []
        else:
            # 信号列 (布尔)
            expr = pl.col(field).fill_null(False)

        hit_df = df.filter(expr)
        results = []
        for row in hit_df.iter_rows(named=True):
            results.append(
                (
                    row.get("symbol", ""),
                    row.get("name"),
                    row.get("close"),
                    row.get("change_pct"),
                )
            )
        return results


# ================================================================
# 通用监控规则引擎 MonitorRuleEngine
# ================================================================

_SIGNAL_PREFIXES = ("signal_", "csg_")

# ── 自选分组作用域: group_id → 成员集合解析 (进程内缓存) ────
# 缓存按 watchlist 数据版本号失效: 版本不变时零磁盘 IO; 自选页任何增删
# 分组/成员的操作都会 bump 版本号, 下一轮评估立即拿到新成员 (无需等 TTL)。
_group_cache_lock = threading.Lock()
_group_cache: dict[str, Any] = {}
# 已告警过的「分组已删除」(rule_id, group_id), 防止每轮评估刷日志
_warned_missing_groups: set[tuple[str, str]] = set()


def _watchlist_groups_snapshot(data_dir: Path | str | None) -> dict[str, frozenset[str]]:
    """返回 {group_id: 成员symbol集}；未注入数据目录时严格失败关闭。"""
    if data_dir is None:
        return {}
    from app.services import watchlist

    rev_before = watchlist.revision(data_dir)
    cache_key = str(data_dir) if data_dir is not None else ""
    with _group_cache_lock:
        cached = _group_cache.get(cache_key)
        if cached is not None and cached.get("_rev") == rev_before:
            return cached["groups"]
    groups: dict[str, set[str]] = {g["id"]: set() for g in watchlist.list_groups(data_dir)}
    for row in watchlist.list_symbols(data_dir):
        for gid in row.get("group_ids") or []:
            members = groups.get(gid)
            if members is not None:
                members.add(str(row["symbol"]))
    frozen = {gid: frozenset(syms) for gid, syms in groups.items()}
    if watchlist.revision(data_dir) == rev_before:
        with _group_cache_lock:
            _group_cache[cache_key] = {"_rev": rev_before, "groups": frozen}
    return frozen


def _group_members_or_none(data_dir: Path | str | None, rule: dict) -> frozenset[str] | None:
    """解析规则绑定的分组成员; 分组已删除返回 None, 解析异常返回 None 并记日志。"""
    group_id = str(rule.get("group_id") or "")
    try:
        groups = _watchlist_groups_snapshot(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自选分组数据读取失败, 规则 %s 本轮跳过: %s", rule.get("id"), exc)
        return None
    members = groups.get(group_id)
    if members is None:
        key = (str(rule.get("id") or ""), group_id)
        if key not in _warned_missing_groups:
            _warned_missing_groups.add(key)
            logger.warning(
                "监控规则 %s 绑定的自选分组 %s 已删除, 本轮跳过 (fail-closed, 恢复分组后自动生效)",
                rule.get("id"),
                group_id,
            )
    return members


def _is_signal_field(field: str) -> bool:
    return any(field.startswith(p) for p in _SIGNAL_PREFIXES)


def _build_condition_mask(df: pl.DataFrame, conditions: list[dict], logic: str) -> pl.DataFrame:
    """根据 conditions + logic 构建过滤后的命中 DataFrame。

    conditions: [{"field","op","value"?}] — op=truth 为布尔信号, 否则阈值比较
    logic: "and" | "or"
    返回命中行 (含 symbol/name/close/change_pct + 各信号列)
    """
    cols = set(df.columns)
    parts: list[pl.Expr] = []
    for c in conditions:
        field = c["field"]
        if field not in cols:
            return df.head(0)  # 字段缺失,无法判定 → 空结果
        op = c["op"]
        if op == "truth":
            parts.append(pl.col(field).fill_null(False))
        elif op in _OP_BUILDERS:
            parts.append(_OP_BUILDERS[op](pl.col(field), c["value"]))
        else:
            return df.head(0)
    if not parts:
        return df.head(0)
    if logic == "or":
        mask = pl.any_horizontal(parts)
    else:
        mask = pl.all_horizontal(parts)
    return df.filter(mask)


def _push_rule_webhook(rule: dict, ev: dict) -> None:
    """规则命中时按需推送到外部 webhook (失败仅记日志,不阻塞告警落盘)。
    仅当 rule.webhook_enabled 且 webhook_url 非空时触发;POST 同步发,超时 3s,
    trust_env=False (与 webhook_adapter 一致, 走直连不走代理)。
    任何异常一律吞成 warning —— 监控命中路径不可因推送失败而中断。
    """
    if not rule.get("webhook_enabled"):
        return
    url = (rule.get("webhook_url") or "").strip()
    if not url:
        return
    payload = {
        "ts": ev.get("ts"),
        "rule_id": ev.get("rule_id"),
        "rule_name": ev.get("rule_name"),
        "symbol": ev.get("symbol"),
        "severity": ev.get("severity", "info"),
        "message": ev.get("message"),
    }
    try:
        import httpx

        httpx.post(url, json=payload, timeout=3.0, trust_env=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("监控规则 %s webhook 推送失败: %s", rule.get("id"), e)


class MonitorRuleEngine:
    """通用监控规则引擎 — 接收实时行情 DataFrame,评估所有规则,返回 AlertEvent。

    与 StrategyMonitorService 的区别:
      - 规则来自 monitor_rules 存储 (用户可配), 而非写死的 strategy config
      - 支持 scope (symbols/all/sector) 过滤作用域
      - 支持 conditions + logic (AND/OR) 任意组合
      - ★ cooldown 去重: 同一 (rule_id, symbol) 在冷却期内不重复触发
    """

    def __init__(self, alert_handler: Callable[[dict], None] | None = None):
        self._alert_handler = alert_handler
        self._rules: dict[str, dict] = {}  # rule_id → rule
        # (rule_id, symbol) → 上次触发时间戳(秒)。用于 cooldown 去重。
        self._last_fire: dict[tuple[str, str], float] = {}
        self._strategy_engine = None  # 延迟注入, type=strategy 规则用它跑选股
        # symbol → 股票名 (enriched DataFrame 已 drop name 列, 触发时从此映射回填)
        self._name_map: dict[str, str] = {}
        # 策略选股池状态: strategy_id → 上期选股符号集合 (用于 diff 变更)
        self._strategy_pools: dict[str, set[str]] = {}
        # 数据目录 (用于加载策略 overrides)
        self._data_dir = None
        # 历史窗口加载器: (target_date, lookback_days) → 多日 enriched DataFrame。
        # 用于声明 filter_history 的策略 (如反包), 实时监控时拼历史窗口 + 今日行情跑选股。
        # 为 None 时, filter_history 策略仍会被跳过 (保持旧行为, 不破坏无历史场景)。
        self._history_loader: Callable[[_dt.date, int], "pl.DataFrame"] | None = None
        # 本轮 evaluate() 产出的策略选股结果: strategy_id → {rows, total, as_of}
        # 供策略页实时回显复用 (/api/screener/cached 端点直接读取此内存结果), 避免重跑
        self._latest_strategy_results: dict[str, dict] = {}
        # 交易所异动监测 loader: () → (stock_returns, index_returns, names, provenance)。
        # 由 main 装配 (只读 repository + quote_service 本地数据); 为 None 时
        # type=abnormal 规则跳过 (保持旧行为, 不破坏无装配场景)。
        self._abnormal_loader: Callable[..., Any] | None = None
        # type=abnormal 边缘触发状态: (rule_id, symbol, window) → 是否越线。
        # 方向变化也会把同一窗口重置为未越线，避免旧方向状态阻止后续 re-arm。
        self._abnormal_state: dict[tuple[str, str, str], bool] = {}
        # 本轮 evaluate() 共享的异动总览行 (多个 abnormal 规则只算一次)
        self._abnormal_rows_cache: list[dict] | None = None

    def set_abnormal_loader(self, fn) -> None:
        """注入异动数据 loader: () → abnormal_moves.load_inputs(...) 四元组。"""
        self._abnormal_loader = fn
        self._abnormal_state.clear()

    @property
    def has_abnormal_rules(self) -> bool:
        """是否存在启用的 type=abnormal 规则 (quote_service 据此决定是否定时评估)。"""
        return any(r.get("type") == "abnormal" for r in self._rules.values())

    def set_strategy_engine(self, engine) -> None:
        """注入 StrategyEngine, type=strategy 规则据此跑选股。"""
        self._strategy_engine = engine

    def set_data_dir(self, data_dir) -> None:
        """注入数据目录, 用于加载策略的用户覆盖配置。"""
        self._data_dir = data_dir

    def set_history_loader(self, fn) -> None:
        """注入历史窗口加载器, 用于声明 filter_history 的策略跑实时监控。

        loader 签名: (target_date, lookback_days) → 多日 enriched DataFrame。
        复用 ScreenerService._load_enriched_history (三级缓存, 命中 ~0ms)。
        为 None 时 filter_history 策略退回到跳过逻辑 (不破坏无历史场景)。
        """
        self._history_loader = fn

    def set_name_map(self, name_map: dict[str, str]) -> None:
        """注入 symbol → 股票名 映射, 用于在告警事件里回填 name 字段。

        enriched DataFrame 在 pipeline 计算后不含 name 列 (见 indicators/pipeline.py),
        触发时从 instruments 表预构建此映射, 保证 AlertEvent.name 有值。
        """
        self._name_map = name_map or {}

    # ── 规则管理 ───────────────────────────────────────
    def set_rules(self, rules: list[dict]) -> None:
        """批量设置规则（覆盖）；API 重载后异动状态必须重新 prime。"""
        self._rules = {}
        for rule in rules:
            if rule.get("enabled") is not False:
                self._rules[rule["id"]] = rule
        self._abnormal_state.clear()
        self._abnormal_rows_cache = None
        logger.info("MonitorRuleEngine: 装载 %d 条规则", len(self._rules))

    def add_rule(self, rule: dict) -> None:
        if rule.get("enabled") is not False:
            self._rules[rule["id"]] = rule
        else:
            self._rules.pop(rule["id"], None)
        self._clear_abnormal_state(rule["id"])

    def remove_rule(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)
        # 清理对应的 cooldown 记录
        self._last_fire = {k: v for k, v in self._last_fire.items() if k[0] != rule_id}
        self._clear_abnormal_state(rule_id)

    def _clear_abnormal_state(self, rule_id: str) -> None:
        """规则更新/删除时清掉其全部异动边缘状态 (下次观测重新 prime)。"""
        self._abnormal_state = {k: v for k, v in self._abnormal_state.items() if k[0] != rule_id}

    def clear(self) -> None:
        self._rules.clear()
        self._last_fire.clear()
        self._abnormal_state.clear()

    @property
    def rules(self) -> dict[str, dict]:
        return dict(self._rules)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def latest_strategy_results(self) -> dict[str, dict]:
        """返回本轮 evaluate() 产出的策略选股结果 (strategy_id → {rows, total, as_of})。

        供策略页实时回显复用: /api/screener/cached 端点直接读取此内存结果,
        避免对被监控的策略重跑第二遍。无 type=strategy 规则时返回空 dict。
        """
        return self._latest_strategy_results

    # ── 评估 ───────────────────────────────────────────
    def evaluate(self, df: pl.DataFrame) -> list[dict]:
        """评估依赖实时 enriched DataFrame 的普通规则。

        异动规则由 :meth:`evaluate_abnormal` 独立节流执行，避免每轮行情评估重复
        加载 90 日历史，也避免独立异动轮询重跑策略/普通规则。
        """
        regular_rules = [
            rule for rule in self._rules.values() if rule.get("type", "signal") != "abnormal"
        ]
        if not regular_rules or df.is_empty():
            return []

        now = time.time()
        events: list[dict] = []
        self._latest_strategy_results = {}
        for rule in regular_rules:
            try:
                events.extend(self._evaluate_rule(df, rule, now))
            except Exception as e:
                logger.warning("规则评估失败 %s: %s", rule.get("id"), e)
        return events

    def evaluate_abnormal(self) -> list[dict]:
        """只评估交易所异动规则；由 QuoteService 约 30 秒调用一次。"""
        abnormal_rules = [rule for rule in self._rules.values() if rule.get("type") == "abnormal"]
        if not abnormal_rules:
            return []

        now = time.time()
        events: list[dict] = []
        self._abnormal_rows_cache = None
        for rule in abnormal_rules:
            try:
                events.extend(self._evaluate_abnormal(rule, now))
            except Exception as e:
                logger.warning("异动规则评估失败 %s: %s", rule.get("id"), e)
        return events

    def _abnormal_overview_rows(self) -> list[dict] | None:
        """本轮共享的异动检测行; loader 未装配或失败返回 None (规则整体跳过)。"""
        if self._abnormal_rows_cache is not None:
            return self._abnormal_rows_cache
        if self._abnormal_loader is None:
            return None
        try:
            from app.services.abnormal_moves import build_rows

            stock_returns, index_returns, names, _prov = self._abnormal_loader()
            rows = build_rows(stock_returns, index_returns, names)
        except Exception as e:  # noqa: BLE001
            logger.warning("异动规则数据加载失败, 本轮跳过: %s", e)
            rows = None
        self._abnormal_rows_cache = rows
        return rows

    def _evaluate_abnormal(self, rule: dict, now: float) -> list[dict]:
        """type=abnormal 规则评估: 总览行 → scope/方向/窗口/倍率过滤 → 边缘触发。

        阈值倍率: rule.threshold_pct (50~150, 100=交易所阈值), 行 ratio 已按交易所
        阈值归一, 故命中条件为 ratio * 100 >= threshold_pct。
        """
        all_rows = self._abnormal_overview_rows()
        if all_rows is None:
            return []

        direction = rule.get("direction", "both")
        window = rule.get("abnormal_window", "any")
        threshold_pct = float(rule.get("threshold_pct", 100))

        # scope 过滤: symbols/all/watchlist_group (与普通规则同口径, fail-closed)
        scope = rule.get("scope", "symbols")
        if scope == "symbols":
            syms = set(rule.get("symbols", []))
            rows = [r for r in all_rows if r["symbol"] in syms]
        elif scope == "watchlist_group":
            members = _group_members_or_none(self._data_dir, rule)
            rows = [r for r in all_rows if r["symbol"] in (members or frozenset())]
        elif scope == "all":
            rows = list(all_rows)
        else:
            logger.warning(
                "异动规则 %s scope=%r 不支持, fail-closed 跳过",
                rule.get("id"),
                scope,
            )
            return []

        if window != "any":
            rows = [r for r in rows if r["window"] == window]

        # 不先丢弃反方向观测；反方向必须把同一 symbol/window 的边缘状态
        # 重置为 False，之后重新进入目标方向才能产生 false→true 告警。
        # 边缘触发状态机 (每轮对全部观测 key 记录 above 状态):
        #   越线 = ratio*100 >= threshold_pct (含等号)
        #   首次观测 (prev None) → prime, 不告警 (无论 above 与否)
        #   prev False → above  → false→true 边缘, cooldown 后告警
        #   prev True  → above  → 持续越线, 不重复告警
        #   above → False      → re-arm, 下次越线可再告警
        events: list[dict] = []
        cooldown = rule.get("cooldown_seconds", 3600)
        severity = rule.get("severity", "info")
        for r in sorted(rows, key=lambda x: (x["symbol"], x["window"])):
            key = (rule["id"], r["symbol"], r["window"])
            direction_matches = direction == "both" or r["direction"] == direction
            above = direction_matches and r["ratio"] * 100 >= threshold_pct
            prev = self._abnormal_state.get(key)
            self._abnormal_state[key] = above
            if prev is None or prev is True or not above:
                continue
            # prev is False and above → false→true 边缘, 走 cooldown 后告警
            sym = r["symbol"]
            ck = (rule["id"], sym)
            last = self._last_fire.get(ck)
            if last is not None and (now - last) < cooldown:
                continue
            self._last_fire[ck] = now
            sign = "+" if r["deviation_pct"] >= 0 else ""
            name = r.get("name") or self._name_map.get(sym, "")
            message = (
                f"{name or sym} {r['window']}偏离{sign}{r['deviation_pct']:.1f}% "
                f"(阈值{r['threshold_pct']:.0f}%, 基准{r['benchmark_symbol']}) · "
                f"交易所规则近似监测"
            )
            ev = {
                "ts": int(now * 1000),
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "source": "abnormal",
                "type": f"abnormal_{r['direction']}_{r['window']}",
                "symbol": sym,
                "name": name,
                "message": rule.get("message", "") or message,
                "price": None,
                "change_pct": None,
                "signals": [f"deviate_{r['window']}"],
                "severity": severity,
                "conditions": [],
                "logic": "and",
            }
            events.append(ev)
            _push_rule_webhook(rule, ev)
            if self._alert_handler:
                try:
                    self._alert_handler(ev)
                except Exception as e:  # noqa: BLE001
                    logger.warning("alert handler failed: %s", e)
        return events

    def _evaluate_rule(self, df: pl.DataFrame, rule: dict, now: float) -> list[dict]:
        """评估单条规则,返回触发的 events。"""
        rtype = rule.get("type", "signal")
        # 交易所异动规则: 不依赖实时 DataFrame (历史偏离 + 指数实时修正), 走独立路径
        if rtype == "abnormal":
            return self._evaluate_abnormal(rule, now)

        # 1. 按 scope 过滤作用域
        scoped = self._apply_scope(df, rule)
        if scoped.is_empty():
            return []

        # 2. 根据 type 构建命中集
        #    元组格式: (event_type, symbol, name, price, pct, signals)
        hit_rows: list[tuple[str, str, Any, Any, Any, list[str]]] = []

        if rtype == "strategy":
            # 策略类型: 跑策略选股 → 对比上期选股池 → 产出 new_entry/dropped 事件
            hit_rows = self._match_strategy(scoped, rule)
        else:
            # signal / price / market: 通用条件匹配
            for sym, name, price, pct, hit_sigs in self._match_conditions(scoped, rule):
                hit_rows.append((rtype, sym, name, price, pct, hit_sigs))

        if not hit_rows:
            return []

        # 3. cooldown 去重 + 生成 events
        cooldown = rule.get("cooldown_seconds", 3600)
        severity = rule.get("severity", "info")
        source = rtype

        events: list[dict] = []
        for ev_type, sym, name, price, pct, hit_sigs in hit_rows:
            # cooldown 键: 批量事件用特殊键, 单只事件用 (rule_id, symbol)
            is_batch = sym == "_batch"
            if is_batch:
                key = (rule["id"], f"_{ev_type}_batch")
            else:
                key = (rule["id"], sym)
            last = self._last_fire.get(key)
            if last is not None and (now - last) < cooldown:
                continue  # 冷却期内, 跳过
            self._last_fire[key] = now

            # 批量事件: name 存放预构建的消息文本
            if is_batch:
                resolved_name = ""
                message = name  # name 字段即批量消息
            else:
                resolved_name = name if name else self._name_map.get(sym)
                message = rule.get("message", "") or self._default_message(
                    rule,
                    ev_type=ev_type,
                    sym=sym,
                    name=resolved_name,
                    pct=pct,
                    price=price,
                    conditions=list(rule.get("conditions", []))
                    if rule.get("type") != "strategy"
                    else None,
                )

            ev = {
                "ts": int(now * 1000),
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "source": source,
                "type": ev_type,
                "symbol": "" if is_batch else sym,
                "name": resolved_name,
                "message": message,
                "price": price,
                "change_pct": pct,
                "signals": hit_sigs,
                "severity": severity,
                # 触发条件快照 (signal/price/market 类型): 用于触发记录展示
                # 「命中了什么条件」。strategy 类型靠策略选股池 diff, 不写条件。
                "conditions": list(rule.get("conditions", [])) if rtype != "strategy" else [],
                "logic": rule.get("logic", "and") if rtype != "strategy" else "and",
            }
            events.append(ev)
            _push_rule_webhook(rule, ev)
            if self._alert_handler:
                try:
                    self._alert_handler(ev)
                except Exception as e:
                    logger.warning("alert handler failed: %s", e)

        return events

    def _apply_scope(self, df: pl.DataFrame, rule: dict) -> pl.DataFrame:
        """按 scope 过滤作用域 DataFrame (fail-closed)。

        安全策略 — 任何不支持的 scope 都返回空, 绝不退化为全市场:
          - scope=all             → 全市场 (显式允许)
          - scope=symbols         → 仅指定股票; symbols 为空则空
          - scope=watchlist_group → 动态读取自选分组成员; 每轮评估从
            self._data_dir 实时读取 (带版本号缓存); data_dir 缺失/组不存在/
            已删除/为空/读取失败 → 返回空, 绝不退化为全市场
          - scope=sector          → 未提供精确板块过滤, 返回空
          - 未知 scope            → 返回空
        历史已保存的 sector 规则不允许保存(validate 拒绝), 但存量文件可能仍含
        sector; 此处保证它不会被当作全市场执行。
        """
        scope = rule.get("scope", "symbols")
        if scope == "all":
            return df
        if scope == "symbols":
            syms = rule.get("symbols", [])
            if not syms:
                return df.head(0)
            return df.filter(pl.col("symbol").is_in(syms))
        if scope == "watchlist_group":
            members = _group_members_or_none(self._data_dir, rule)
            if not members:
                return df.head(0)
            return df.filter(pl.col("symbol").is_in(list(members)))
        # sector 或任何未知 scope: fail-closed, 返回空 (不当全市场执行)
        logger.warning(
            "MonitorRuleEngine._apply_scope: 规则 %s scope=%r 不支持精确过滤, "
            "fail-closed 返回空 (不退化为全市场)",
            rule.get("id"),
            scope,
        )
        return df.head(0)

    def _match_strategy(
        self,
        df: pl.DataFrame,
        rule: dict,
    ) -> list[tuple[str, str, Any, Any, Any, list[str]]]:
        """策略类型评估: 跑策略选股 → 对比上期选股池 → 产出变更事件。

        返回 [(event_type, symbol, name, price, pct, signals)]
        event_type: "new_entry" (新入选) | "dropped" (已移出)
        单只变更逐只返回; 同一策略 >5 只合并为一条批量事件 (symbol="_batch")
        """
        if self._strategy_engine is None:
            return []
        sid = rule.get("strategy_id")
        if not sid:
            return []
        try:
            s = self._strategy_engine.get(sid)
        except Exception:
            return []
        if s is None:
            return []

        # 运行策略选股: 复用当前 enriched DataFrame 跳过数据加载
        overrides = {}
        if self._data_dir:
            try:
                overrides = _strategy_config.load_override(self._data_dir, sid)
            except Exception:
                pass

        # 声明 filter_history 的策略 (如反包) 需要多日历史窗口才能判定形态。
        # 旧实现因"实时监控不支持 history loader"直接跳过 → 反包等策略盘中永不触发。
        # 现接入 history_loader, 拼历史窗口 + 今日实时行情, 经 precomputed_history 喂给引擎。
        # loader 为 None (未装配) 时退回跳过, 保持旧行为, 不破坏无历史场景。
        run_kwargs: dict = {
            "as_of": _dt.date.today(),
            "overrides": overrides,
        }
        if s.filter_history_fn:
            if self._history_loader is None:
                logger.debug("策略 %s 需要历史数据但未注入 history_loader, 跳过实时监控", sid)
                return []
            try:
                today = _dt.date.today()
                lookback = max(1, getattr(s, "lookback_days", 30))
                hist_df = self._history_loader(today, lookback)
                if hist_df is None or hist_df.is_empty():
                    logger.debug("策略 %s 历史数据为空, 跳过本轮实时监控", sid)
                    return []
                # 历史窗口可能与今日已落盘数据重叠: 排掉 hist_df 中 date==today 的行,
                # 今日行情始终以实时 df 为准 (盘中逐轮更新, 最接近收盘真相)。
                # 否则 today 行重复会污染 filter_history 的 .over("symbol") 窗口判定。
                if "date" in hist_df.columns:
                    hist_df = hist_df.filter(pl.col("date") != today)
                # 拼接历史窗口 + 今日实时行情 (filter_history 用 .over("symbol") 窗口, 多日天然可用)
                run_kwargs["precomputed_history"] = pl.concat([hist_df, df], how="diagonal_relaxed")
            except Exception as e:
                logger.warning("策略 %s 加载历史窗口失败, 跳过: %s", sid, e)
                return []
        else:
            # 普通策略: 复用当前 enriched DataFrame 跳过数据加载
            run_kwargs["precomputed"] = df

        try:
            result = self._strategy_engine.run(sid, **run_kwargs)
        except Exception as e:
            logger.warning("策略 %s 选股执行失败: %s", sid, e)
            return []

        # 记录本轮完整选股结果 (供策略页实时回显: /cached 端点直接读取, 不落盘)。
        # 与下面的 diff 事件无关 — 无论是否产生 new_entry/dropped, 结果都该可用于回显。
        try:
            import math

            self._latest_strategy_results[sid] = {
                "total": result.total,
                "as_of": str(_dt.date.today()),
                "rows": [
                    {
                        k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                        for k, v in row.items()
                    }
                    for row in result.rows
                ],
            }
        except Exception:  # noqa: BLE001
            pass

        current_pool: set[str] = {r["symbol"] for r in result.rows}
        prev_pool = self._strategy_pools.get(sid)

        # 首次运行: 仅记录当前选股池, 不产生事件
        if prev_pool is None:
            self._strategy_pools[sid] = current_pool
            return []

        new_entries = current_pool - prev_pool
        dropped = prev_pool - current_pool

        # 无变更
        if not new_entries and not dropped:
            return []

        # 更新存储
        self._strategy_pools[sid] = current_pool

        sname = s.meta.get("name", "") or s.meta.get("id", sid)

        # 构建查找表 (新入选股票可在 result.rows 中找到; 移出股票需从 df 找)
        row_map: dict[str, dict] = {r["symbol"]: r for r in result.rows}
        dropped_map: dict[str, dict] = {}
        if dropped:
            try:
                _dd = df.filter(pl.col("symbol").is_in(list(dropped)))
                for row in _dd.iter_rows(named=True):
                    dropped_map[row["symbol"]] = row
            except Exception:
                pass

        results: list[tuple[str, str, Any, Any, Any, list[str]]] = []

        # ── 新入选 ──
        new_list = sorted(new_entries)
        if len(new_list) > 5:
            names: list[str] = []
            for sym in new_list:
                row = row_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                names.append(str(name))
            message = f"策略「{sname}」进入 {len(new_entries)} 只：{'、'.join(names)}"
            results.append(("new_entry", "_batch", message, None, None, []))
        else:
            for sym in new_list:
                row = row_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                price = row.get("close")
                pct = row.get("change_pct")
                results.append(("new_entry", sym, name, price, pct, []))

        # ── 已移出 ──
        dropped_list = sorted(dropped)
        if len(dropped_list) > 5:
            names = []
            for sym in dropped_list:
                row = dropped_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                names.append(str(name))
            message = f"策略「{sname}」移出 {len(dropped)} 只：{'、'.join(names)}"
            results.append(("dropped", "_batch", message, None, None, []))
        else:
            for sym in dropped_list:
                row = dropped_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                price = row.get("close")
                pct = row.get("change_pct")
                results.append(("dropped", sym, name, price, pct, []))

        return results

    @staticmethod
    def _match_conditions(
        df: pl.DataFrame,
        rule: dict,
    ) -> list[tuple[str, Any, Any, Any, list[str]]]:
        """按 conditions + logic 匹配,返回命中行 [(symbol,name,price,pct,signals)]。"""
        conditions = rule.get("conditions", [])
        logic = rule.get("logic", "and")
        if not conditions:
            return []
        hit_df = _build_condition_mask(df, conditions, logic)
        results = []
        for row in hit_df.iter_rows(named=True):
            sym = row.get("symbol", "")
            name = row.get("name")
            price = row.get("close")
            pct = row.get("change_pct")
            # 收集命中的信号列名 (仅 op=truth 且为真的)
            hit_sigs = [
                c["field"] for c in conditions if c.get("op") == "truth" and row.get(c["field"])
            ]
            results.append((sym, name, price, pct, hit_sigs))
        return results

    def _default_message(
        self,
        rule: dict,
        ev_type: str = "",
        sym: str = "",
        name: str = "",
        pct: Any = None,
        price: Any = None,
        conditions: list[dict] | None = None,
    ) -> str:
        """生成默认 message。

        - strategy: 按变更方向生成 (进入/移出 + 涨跌幅)
        - signal/price/market: 条件摘要 + 现价 + 涨跌幅 (避免笼统的「信号触发」)
        """
        rtype = rule.get("type", "signal")
        if rtype == "strategy":
            # 从 StrategyEngine 取策略名; 失败则退化为 rule_name 里截取的部分
            sname = ""
            sid = rule.get("strategy_id")
            if sid and self._strategy_engine is not None:
                try:
                    s = self._strategy_engine.get(sid)
                    sname = s.meta.get("name", "") or s.meta.get("id", "")
                except Exception:  # noqa: BLE001
                    sname = ""
            if not sname:
                rn = rule.get("name", "")
                sname = rn.split(" · ", 1)[1] if " · " in rn else (rn or "策略")

            if ev_type == "new_entry":
                pct_text = ""
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    pct_text = f" {sign}{pct * 100:.1f}%"
                return f"策略「{sname}」进入 {name}{pct_text}"
            elif ev_type == "dropped":
                pct_text = ""
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    pct_text = f" {sign}{pct * 100:.1f}%"
                return f"策略「{sname}」移出 {name}{pct_text}"
            return f"策略「{sname}」变更"

        # signal / price / market: 条件摘要 + 现价 + 涨跌幅
        # 条件摘要: 把 conditions (truth/比较) 拼成可读串, 如 "MA20金叉 且 量比>2"
        cond_text = self._format_conditions_text(rule, conditions)
        price_text = f"现价 {price}" if price is not None else ""
        pct_text = ""
        if pct is not None:
            sign = "+" if pct >= 0 else ""
            pct_text = f"{sign}{pct * 100:.1f}%"
        tail = " · ".join(s for s in (price_text, pct_text) if s)
        if cond_text and tail:
            return f"{cond_text} · {tail}"
        return cond_text or tail or "监控触发"

    @staticmethod
    def _format_conditions_text(rule: dict, conditions: list[dict] | None) -> str:
        """把 rule.conditions 拼成可读文本 (用于 message / 推送)。

        op=truth: 直接用信号中文名 (如 "MA20金叉")
        op=比较: 字段中文名 + 操作符 + 值 (如 "涨跌幅≥5")
        logic: and → "且", or → "或"
        """
        conds = conditions if conditions is not None else list(rule.get("conditions", []))
        if not conds:
            return ""
        logic_word = "且" if rule.get("logic", "and") == "and" else "或"
        parts: list[str] = []
        for c in conds:
            field = c.get("field", "")
            op = c.get("op", "truth")
            value = c.get("value")
            label = _signal_cn_name(field) or field
            if op == "truth":
                parts.append(label)
            else:
                op_map = {"gte": "≥", "lte": "≤", "gt": ">", "lt": "<", "eq": "="}
                parts.append(f"{label}{op_map.get(op, op)}{value}")
        return f" {logic_word} ".join(parts)
