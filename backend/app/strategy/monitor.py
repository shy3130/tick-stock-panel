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
from typing import Any, Callable

import polars as pl

from app.market_time import cn_today
from app.strategy.custom_signals import _OP_BUILDERS  # type: ignore  # 复用运算符构造器
from app.strategy import config as _strategy_config

logger = logging.getLogger(__name__)

# 信号 / 字段中文名映射 — 与前端 lib/signals.ts 对齐, 用于告警 message / 推送文案。
# signal_* 为内置原子信号, 其余为技术指标/行情字段。
_SIGNAL_CN: dict[str, str] = {
    # 内置信号
    "signal_ma_golden_5_20": "MA5上穿MA20", "signal_ma_dead_5_20": "MA5下穿MA20",
    "signal_ma_golden_20_60": "MA20上穿MA60", "signal_macd_golden": "MACD金叉",
    "signal_macd_dead": "MACD死叉", "signal_ma20_breakout": "突破MA20",
    "signal_ma20_breakdown": "跌破MA20", "signal_n_day_high": "60日新高",
    "signal_n_day_low": "60日新低", "signal_boll_breakout_upper": "突破布林上轨",
    "signal_boll_breakdown_lower": "跌破布林下轨", "signal_volume_surge": "放量",
    "signal_limit_up": "涨停", "signal_limit_down": "跌停",
    "signal_limit_down_recovery": "跌停翘板", "signal_broken_limit_up": "炸板",
    # 行情字段
    "close": "收盘价", "open": "开盘价", "high": "最高价", "low": "最低价",
    "change_pct": "涨跌幅", "change_amount": "涨跌额", "amplitude": "振幅",
    "turnover_rate": "换手率", "volume": "成交量", "amount": "成交额",
    # 均线
    "ma5": "MA5", "ma10": "MA10", "ma20": "MA20", "ma30": "MA30", "ma60": "MA60",
    "ema5": "EMA5", "ema10": "EMA10", "ema20": "EMA20",
    # MACD / BOLL / KDJ / RSI
    "macd_dif": "MACD-DIF", "macd_dea": "MACD-DEA", "macd_hist": "MACD柱",
    "boll_upper": "布林上轨", "boll_lower": "布林下轨",
    "kdj_k": "KDJ-K", "kdj_d": "KDJ-D", "kdj_j": "KDJ-J",
    "rsi_6": "RSI6", "rsi_14": "RSI14", "rsi_24": "RSI24",
    # 量能 / 动量 / 波动
    "vol_ratio_5d": "5日量比", "vol_ratio_20d": "20日量比",
    "vol_ma5": "5日均量", "vol_ma10": "10日均量",
    "high_60d": "60日最高", "low_60d": "60日最低",
    "momentum_5d": "5日动量", "momentum_20d": "20日动量", "momentum_60d": "60日动量",
    "atr_14": "ATR14", "annual_vol_20d": "20日年化波动",
    "consecutive_limit_ups": "连板数", "consecutive_limit_downs": "跌停连板",
}


def _signal_cn_name(name: str) -> str:
    """返回信号/字段的中文名, 找不到原样返回 (与前端 cnSignal 对齐)。"""
    return _SIGNAL_CN.get(name, name)


@dataclass
class StrategyAlert:
    """策略告警"""
    type: str              # "entry" | "exit" | "alert"
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
        # _watching 跨线程锁: on_quote_update 跑在行情轮询线程迭代 _watching,
        # API 线程同时 start/stop 增删会抛 "dict changed size during iteration"。
        # 增删与迭代前的快照都持此锁 (镜像 MonitorRuleEngine.evaluate 的 list 快照)。
        self._watching_lock = threading.Lock()

    def start(self, strategy_id: str, config: dict) -> None:
        """开始监控一个策略

        config: {
            "entry_signals": ["signal_n_day_high", ...],
            "exit_signals": ["signal_ma20_breakdown", ...],
            "alerts": [{"field": "rsi_14", "op": ">", "value": 80, "message": "..."}],
        }
        """
        with self._watching_lock:
            self._watching[strategy_id] = config
        logger.info("strategy monitor started: %s", strategy_id)

    def stop(self, strategy_id: str) -> None:
        with self._watching_lock:
            self._watching.pop(strategy_id, None)
        logger.info("strategy monitor stopped: %s", strategy_id)

    def stop_all(self) -> None:
        with self._watching_lock:
            self._watching.clear()

    @property
    def watching(self) -> dict[str, dict]:
        with self._watching_lock:
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

        # 迭代前持锁快照, 避免行情线程迭代时 API 线程 start/stop 改变字典大小
        with self._watching_lock:
            watching_items = list(self._watching.items())

        for strategy_id, cfg in watching_items:
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
            results.append((
                row.get("symbol", ""),
                row.get("name"),
                row.get("close"),
                row.get("change_pct"),
            ))
        return results


# ================================================================
# 通用监控规则引擎 MonitorRuleEngine
# ================================================================

_SIGNAL_PREFIXES = ("signal_", "csg_")


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
        # ETF 版历史窗口加载器 (asset_type=etf 的规则用)。为 None 时 ETF filter_history 策略跳过。
        self._history_loader_etf: Callable[[_dt.date, int], "pl.DataFrame"] | None = None
        # 本轮 evaluate() 产出的策略选股结果: strategy_id → {rows, total, as_of}
        # 供策略页实时回显复用 (/api/screener/cached 端点直接读取此内存结果), 避免重跑
        self._latest_strategy_results: dict[str, dict] = {}

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

    def set_history_loader_etf(self, fn) -> None:
        """注入 ETF 版历史窗口加载器 (asset_type=etf 的 strategy 型规则用)。

        签名同 set_history_loader; 复用 ScreenerService(asset_type='etf')._load_enriched_history。
        为 None 时 ETF filter_history 策略退回到跳过逻辑。
        """
        self._history_loader_etf = fn

    def _history_loader_for(self, rule: dict):
        """按规则的 asset_type 选历史加载器。etf → ETF 加载器, 否则股票加载器。"""
        if rule.get("asset_type") == "etf":
            return self._history_loader_etf
        return self._history_loader

    def set_name_map(self, name_map: dict[str, str]) -> None:
        """注入 symbol → 股票名 映射, 用于在告警事件里回填 name 字段。

        enriched DataFrame 在 pipeline 计算后不含 name 列 (见 indicators/pipeline.py),
        触发时从 instruments 表预构建此映射, 保证 AlertEvent.name 有值。
        """
        self._name_map = name_map or {}

    # ── 规则管理 ───────────────────────────────────────
    def set_rules(self, rules: list[dict]) -> None:
        """批量设置规则 (覆盖)。用于启动时 reload。

        先构建完整 dict 再原子替换 self._rules, 避免评估线程 (行情轮询)
        读到装载了一半的规则表。
        """
        new_rules: dict[str, dict] = {}
        for r in rules:
            if r.get("enabled") is not False:
                new_rules[r["id"]] = r
        self._rules = new_rules
        logger.info("MonitorRuleEngine: 装载 %d 条规则", len(self._rules))

    def add_rule(self, rule: dict) -> None:
        if rule.get("enabled") is not False:
            self._rules[rule["id"]] = rule
        else:
            self._rules.pop(rule["id"], None)

    def remove_rule(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)
        # 清理对应的 cooldown 记录 (list 快照: 评估线程可能并发写 _last_fire)
        self._last_fire = {k: v for k, v in list(self._last_fire.items()) if k[0] != rule_id}

    def clear(self) -> None:
        self._rules.clear()
        self._last_fire.clear()

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

    def has_rule_type(self, rtype: str) -> bool:
        """是否存在指定类型的 (已启用) 规则。供 quote_service 判断是否需要注入特殊数据。"""
        if not self._rules:
            return False
        # list() 快照: API 线程可能并发增删规则, 直接迭代 dict 会抛 RuntimeError
        return any(
            r.get("enabled", True) and r.get("type") == rtype
            for r in list(self._rules.values())
        )

    # ── 评估 ───────────────────────────────────────────
    def has_asset_rules(self, asset_type: str) -> bool:
        """是否存在指定资产类型的 (已启用) 规则。供 quote_service 判断是否需要 ETF 评估轮。"""
        if not self._rules:
            return False
        return any(
            r.get("enabled", True) and r.get("asset_type", "stock") == asset_type
            for r in list(self._rules.values())
        )

    def evaluate(self, df: pl.DataFrame, asset_type: str = "stock",
                 reset_strategy_results: bool = True) -> list[dict]:
        """行情更新后评估规则。

        按 asset_type 只评估匹配资产类型的规则; ETF 规则应传 ETF enriched 快照。
        股票/ETF 分两轮评估时, 仅股票轮重置 _latest_strategy_results (它供股票策略页
        /cached 回显; ETF 策略页走实时单跑, 不依赖它)。

        Args:
            df: 实时 enriched 数据 (含 signal_/csg_/指标列)
            asset_type: 只评估该资产类型的规则 (默认 stock, 向后兼容)
            reset_strategy_results: 是否重置策略结果缓存 (多轮评估时仅首轮 True)
        Returns:
            触发的 AlertEvent dict 列表 (含 ts/rule_id/source/type/symbol/...)
        """
        if not self._rules or df.is_empty():
            return []

        now = time.time()
        events: list[dict] = []
        # 每轮重置: 只保留本次 evaluate 产出的策略结果
        if reset_strategy_results:
            self._latest_strategy_results = {}

        # list() 快照: 本方法跑在行情轮询线程, API 线程同时 add/remove 规则
        # 会触发 "dictionary changed size during iteration", 整轮告警丢失
        for rule_id, rule in list(self._rules.items()):
            if rule.get("asset_type", "stock") != asset_type:
                continue
            try:
                events.extend(self._evaluate_rule(df, rule, now))
            except Exception as e:
                logger.warning("规则评估失败 %s: %s", rule_id, e)

        return events

    def _evaluate_rule(self, df: pl.DataFrame, rule: dict, now: float) -> list[dict]:
        """评估单条规则,返回触发的 events。"""
        # 1. 按 scope 过滤作用域
        scoped = self._apply_scope(df, rule)
        if scoped.is_empty():
            return []

        # 2. 根据 type 构建命中集
        #    元组格式: (event_type, symbol, name, price, pct, signals)
        hit_rows: list[tuple[str, str, Any, Any, Any, list[str]]] = []

        rtype = rule.get("type", "signal")
        if rtype == "strategy":
            # 策略类型: 跑策略选股 → 对比上期选股池 → 产出 new_entry/dropped 事件
            hit_rows = self._match_strategy(scoped, rule)
        elif rtype == "ladder":
            # 连板梯队封单监控: 独立处理 (需带预警封单值, 走专属 message)
            return self._evaluate_ladder(scoped, rule, now)
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
                    rule, ev_type=ev_type, sym=sym, name=resolved_name,
                    pct=pct, price=price,
                    conditions=list(rule.get("conditions", [])) if rule.get("type") != "strategy" else None,
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
            if self._alert_handler:
                try:
                    self._alert_handler(ev)
                except Exception as e:
                    logger.warning("alert handler failed: %s", e)

        return events

    @staticmethod
    def _apply_scope(df: pl.DataFrame, rule: dict) -> pl.DataFrame:
        """按 scope 过滤 DataFrame。"""
        scope = rule.get("scope", "symbols")
        if scope == "all":
            return df
        if scope == "symbols":
            syms = rule.get("symbols", [])
            if not syms:
                return df.head(0)
            return df.filter(pl.col("symbol").is_in(syms))
        if scope == "sector":
            # sector 过滤需 df 含板块列 (后续接入 ext_data JOIN)。在 JOIN 落地前
            # fail-closed 返回空 —— 绝不退化为「全市场」误触发 (旧行为 return df 会让
            # 一条板块规则对全市场每只命中都告警)。新建 sector 规则已在 validate 拦截,
            # 此处兜底任何历史遗留的 sector 规则。
            logger.warning("scope=sector 规则 %s 暂不支持(板块 JOIN 未实现), 本轮跳过",
                           rule.get("id"))
            return df.head(0)
        return df

    def _match_strategy(
        self, df: pl.DataFrame, rule: dict,
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
        at = rule.get("asset_type", "stock")
        pool_key = (sid, at)
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
            "as_of": cn_today(),
            "overrides": overrides,
        }
        if s.filter_history_fn:
            history_loader = self._history_loader_for(rule)
            if history_loader is None:
                logger.debug("策略 %s 需要历史数据但未注入 history_loader (asset_type=%s), 跳过实时监控",
                             sid, rule.get("asset_type", "stock"))
                return []
            try:
                today = cn_today()
                lookback = max(1, getattr(s, "lookback_days", 30))
                hist_df = history_loader(today, lookback)
                if hist_df is None or hist_df.is_empty():
                    logger.debug("策略 %s 历史数据为空, 跳过本轮实时监控", sid)
                    return []
                # 历史窗口可能与今日已落盘数据重叠: 排掉 hist_df 中 date==today 的行,
                # 今日行情始终以实时 df 为准 (盘中逐轮更新, 最接近收盘真相)。
                # 否则 today 行重复会污染 filter_history 的 .over("symbol") 窗口判定。
                if "date" in hist_df.columns:
                    hist_df = hist_df.filter(pl.col("date") != today)
                # 拼接历史窗口 + 今日实时行情 (filter_history 用 .over("symbol") 窗口, 多日天然可用)
                run_kwargs["precomputed_history"] = pl.concat(
                    [hist_df, df], how="diagonal_relaxed"
                )
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
        # 策略结果缓存仅用于股票策略页 /cached 回显; ETF 策略页走实时单跑, 不写入。
        if at == "stock":
            try:
                import math
                self._latest_strategy_results[sid] = {
                    "total": result.total,
                    "as_of": str(cn_today()),
                    "rows": [
                        {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                         for k, v in row.items()}
                        for row in result.rows
                    ],
                }
            except Exception:  # noqa: BLE001
                pass

        current_pool: set[str] = {r["symbol"] for r in result.rows}
        prev_pool = self._strategy_pools.get(pool_key)

        # 首次运行: 仅记录当前选股池, 不产生事件
        if prev_pool is None:
            self._strategy_pools[pool_key] = current_pool
            return []

        new_entries = current_pool - prev_pool
        dropped = prev_pool - current_pool

        # 无变更
        if not new_entries and not dropped:
            return []

        # 更新存储
        self._strategy_pools[pool_key] = current_pool

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
        df: pl.DataFrame, rule: dict,
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
                c["field"] for c in conditions
                if c.get("op") == "truth" and row.get(c["field"])
            ]
            results.append((sym, name, price, pct, hit_sigs))
        return results

    def _evaluate_ladder(self, scoped: pl.DataFrame, rule: dict, now: float) -> list[dict]:
        """评估连板梯队封单监控规则。

        封单量从注入的临时列 _sealed_vol (手) 读取 (由 quote_service 评估前注入)。
        命中条件: 封单比较值 <= threshold (且封单 > 0, 排除无 depth 数据的股票)。
        涨停(direction=up) → 炸板预警; 跌停(direction=down) → 翘板预警。
        """
        if "_sealed_vol" not in scoped.columns:
            return []  # 无封单数据 (depth 未拉取), 安全降级

        metric = rule.get("metric", "sealed_vol")
        threshold = rule.get("threshold", 0)
        direction = rule.get("direction", "up")
        cooldown = rule.get("cooldown_seconds", 600)
        severity = rule.get("severity", "warn")

        # 比较值: sealed_vol 直接用 (手), sealed_amount = 手 × 100股 × close
        if metric == "sealed_amount":
            cmp_expr = pl.col("_sealed_vol") * 100 * pl.col("close")
            unit = "元"
        else:
            cmp_expr = pl.col("_sealed_vol")
            unit = "手"

        # 命中: 封单 > 0 (有数据) 且 比较值 <= 阈值
        hit = scoped.filter(
            pl.col("_sealed_vol").is_not_null()
            & (pl.col("_sealed_vol") > 0)
            & (cmp_expr <= threshold)
        )
        if hit.is_empty():
            return []

        warn_label = "炸板预警" if direction == "up" else "翘板预警"
        events: list[dict] = []
        for row in hit.iter_rows(named=True):
            sym = row.get("symbol", "")
            key = (rule["id"], sym)
            last = self._last_fire.get(key)
            if last is not None and (now - last) < cooldown:
                continue
            self._last_fire[key] = now

            name = row.get("name") or self._name_map.get(sym) or sym
            price = row.get("close")
            pct = row.get("change_pct")
            sealed_vol = row.get("_sealed_vol")
            # 预警封单值 (展示用)
            sealed_value = sealed_vol * 100 * (price or 0) if metric == "sealed_amount" else sealed_vol

            # message 体现预警封单量 + 阈值
            if metric == "sealed_amount":
                sv_text = f"{sealed_value / 1e4:.0f}万{unit}"
                th_text = f"{threshold / 1e4:.0f}万{unit}"
            else:
                sv_text = f"{sealed_value:,.0f} {unit}"
                th_text = f"{threshold:,.0f} {unit}"
            message = f"{warn_label} · 封单 {sv_text} ≤ {th_text}"

            events.append({
                "ts": int(now * 1000),
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "source": "ladder",
                "type": warn_label,
                "symbol": sym,
                "name": name,
                "message": message,
                "price": price,
                "change_pct": pct,
                "signals": [],
                "severity": severity,
                "conditions": [],
                "logic": "and",
                "sealed_value": sealed_value,   # 预警封单量/额 (飞书+记录展示)
                "sealed_metric": metric,
            })
        return events

    def _default_message(self, rule: dict, ev_type: str = "", sym: str = "",
                          name: str = "", pct: Any = None, price: Any = None,
                          conditions: list[dict] | None = None) -> str:
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
