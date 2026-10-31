"""策略决策编排。仅写扩展绑定/检查点，通过 paper 公开函数创建普通订单。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from datetime import datetime, time
from pathlib import Path

from app.market_time import CN_TZ, cn_now, in_continuous_session
from app.services.fs_utils import atomic_write_text
from app.strategy import paper, paper_auto

from .snapshot import DIFFERENCES, check_revision, digest, make_snapshot

logger = logging.getLogger(__name__)
PREFIX = "strategy-exec:"


class ExecutionService:
    def __init__(self, data_dir: Path, engine, screener, quotes=None):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "user_data" / "extensions" / "paper_execution"
        self.engine = engine
        self.screener = screener
        self.quotes = quotes
        # 与 worker/API 串行；重计算不持 PAPER_LOCK，只有检查与下单持核心锁。
        self.lock = threading.RLock()
        self.stopping = threading.Event()

    def _path(self, account: str) -> Path:
        return self.root / f"{paper.validate_account_id(account)}.json"

    def _load(self, account: str) -> dict | None:
        path = self._path(account)
        if not path.exists():
            return None
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("account") != account
            or not isinstance(state.get("snapshot"), dict)
        ):
            raise ValueError("跟单状态版本或内容不可用，已停止执行；请检查扩展数据")
        if digest(state["snapshot"]) != state.get("revision"):
            raise ValueError("跟单参数快照校验失败，已停止执行")
        # 缺失的可选运行字段不重建旧信号；控制字段缺失默认暂停。
        state.setdefault("enabled", False)
        state.setdefault("status", "paused")
        state.setdefault("message", "请核对参数后恢复跟单")
        for key, default in (("events", []), ("risk", {}), ("exits", {}), ("last_bar", None)):
            state.setdefault(key, default)
        if not isinstance(state["enabled"], bool):
            raise ValueError("跟单启用状态不可用，已停止执行")
        return state

    def _save(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path(state["account"]),
            json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2),
        )

    def get(self, account: str) -> dict | None:
        with self.lock:
            state = self._load(account)
            return {**state, "differences": DIFFERENCES} if state else None

    def list_bindings(self) -> list[dict]:
        with self.lock:
            result = []
            for path in sorted(self.root.glob("*.json")):
                try:
                    state = self._load(path.stem)
                    result.append(
                        {
                            "account": state["account"],
                            "name": state["snapshot"]["name"],
                            "enabled": state["enabled"],
                            "status": state["status"],
                            "message": state["message"],
                        }
                    )
                except (ValueError, OSError, KeyError, TypeError):
                    result.append(
                        {
                            "account": path.stem,
                            "name": "策略快照不可读",
                            "enabled": False,
                            "status": "blocked",
                            "message": "请检查扩展数据后重新绑定",
                        }
                    )
            return result

    def preview(self, source: dict, account: str = "default") -> dict:
        with self.lock:
            if source.get("candidate_id"):
                from app.backtest.candidates import CandidateStore

                candidate = next(
                    (
                        c
                        for c in CandidateStore(self.data_dir).list()
                        if c["id"] == source["candidate_id"]
                    ),
                    None,
                )
                if not candidate or candidate["kind"] != "strategy":
                    raise ValueError("策略回测候选不存在")
                source = {"config": candidate["config"], "label": candidate["name"]}
            return make_snapshot(
                self.data_dir, self.engine, source, paper.get_account(self.data_dir, account)
            )

    def bind(
        self, account: str, source: dict, revision: str, *, disable_legacy=False, now=None
    ) -> dict:
        with self.lock:
            preview = self.preview(source, account)
            if revision != preview["revision"]:
                raise ValueError("策略或依赖在预览后发生变化，请重新预览")
            now = (now or cn_now()).astimezone(CN_TZ)
            with paper.PAPER_LOCK:
                old = self._load(account)
                acc = paper.get_account(self.data_dir, account)
                if not acc or acc.get("status") != "active":
                    raise ValueError("请先创建或解冻模拟账户")
                positions = paper.load_positions(self.data_dir, account)
                orders = paper.load_orders(self.data_dir, account)
                if any(p["qty"] > 0 for p in positions.values()) or any(
                    o["status"] == "pending" for o in orders
                ):
                    raise ValueError("为避免混用策略，请使用没有持仓和待成交委托的独立账户")
                if any(not str(o.get("source", "")).startswith(PREFIX) for o in orders):
                    raise ValueError("账户存在其他来源的历史委托，请新建独立账户绑定")
                legacy = paper_auto.load_auto_rules(self.data_dir, account, enabled_only=True)
                if legacy and not disable_legacy:
                    raise ValueError("存在启用的旧跟单规则，请确认停用后再绑定")
                snapshot = preview["snapshot"]
                # 已有账务不修改；仅允许首次空账户同步成本参数。
                if orders and any(acc[k] != v for k, v in snapshot["fees"].items()):
                    raise ValueError("账户已有成交记录且费用与新快照不同，请新建账户")
                if not math.isclose(acc["initial_cash"], snapshot["initial_capital"], abs_tol=0.01):
                    raise ValueError(
                        f"账户初始资金与回测配置不一致（需 {snapshot['initial_capital']:g}），请使用同本金账户"
                    )
                state = {
                    "version": 1,
                    "account": account,
                    "snapshot": snapshot,
                    "revision": preview["revision"],
                    "enabled": True,
                    "status": "waiting",
                    "message": "等待当日收盘数据确认",
                    "bound_at": now.isoformat(),
                    "last_bar": old.get("last_bar") if old else None,
                    "events": old.get("events", []) if old else [],
                    "risk": {},
                    "exits": {},
                }
                disabled = []
                try:
                    for rule in legacy:
                        paper_auto.set_enabled(self.data_dir, rule["id"], False, account)
                        disabled.append(rule)
                    if not orders:
                        paper.update_settings(self.data_dir, account, **snapshot["fees"])
                    self._save(state)
                except Exception:
                    for rule in disabled:
                        paper_auto.set_enabled(self.data_dir, rule["id"], True, account)
                    if not orders:
                        paper.update_settings(
                            self.data_dir, account, **{k: acc[k] for k in snapshot["fees"]}
                        )
                    raise
                return self.get(account)

    def set_enabled(self, account: str, enabled: bool) -> dict:
        with self.lock:
            state = self._load(account)
            if not state:
                raise ValueError("账户尚未绑定策略快照")
            if enabled:
                check_revision(self.data_dir, self.engine, state["snapshot"])
                self._check_account(state)
            state.update(
                enabled=enabled,
                status="waiting" if enabled else "paused",
                message="等待下一次评估" if enabled else "跟单已暂停；已有委托仍由模拟仓处理",
            )
            self._save(state)
            return self.get(account)

    def _check_account(self, state: dict) -> tuple[dict, dict, list]:
        account = state["account"]
        acc = paper.get_account(self.data_dir, account)
        if not acc or acc.get("status") != "active":
            raise ValueError("模拟账户不存在或已冻结")
        if paper_auto.load_auto_rules(self.data_dir, account, enabled_only=True):
            raise ValueError("检测到启用的旧跟单规则，请停用后恢复快照跟单")
        if any(acc[k] != v for k, v in state["snapshot"]["fees"].items()):
            raise ValueError("模拟账户费用已改变，请恢复为快照费率后重新启用")
        orders = paper.load_orders(self.data_dir, account)
        if any(not str(o.get("source", "")).startswith(PREFIX) for o in orders):
            raise ValueError("检测到其他来源委托，已暂停；请使用独立策略账户")
        return acc, paper.load_positions(self.data_dir, account), orders

    def tick(self, now: datetime | None = None) -> None:
        now = (now or cn_now()).astimezone(CN_TZ)
        if self.stopping.is_set():
            return
        with self.lock:
            # 只在有绑定时读行情；同快照同日策略执行一次，账户间扇出。
            results: dict = {}
            for path in sorted(self.root.glob("*.json")):
                if self.stopping.is_set():
                    break
                try:
                    state = self._load(path.stem)
                    if state and state["enabled"]:
                        self._tick_account(state, now, results)
                except Exception:
                    logger.exception("paper execution failed for account %s", path.stem)

    def _tick_account(self, state: dict, now: datetime, results: dict) -> None:
        try:
            check_revision(self.data_dir, self.engine, state["snapshot"])
            with paper.PAPER_LOCK:
                self._check_account(state)
            if now.weekday() < 5 and now.time() < time(9, 25) and state["exits"]:
                with paper.PAPER_LOCK:
                    _, positions, orders = self._check_account(state)
                    self._sell_intents(
                        state,
                        positions,
                        orders,
                        {s: i.get("price") for s, i in state["exits"].items()},
                        attempt_day=now.date().isoformat(),
                    )
            self._risk_tick(state, now)
            day = now.date()
            if now.time() < time(15, 5) or now.weekday() >= 5:
                state.update(status="waiting", message="等待交易日收盘确认；盘中仅检查保护性风控")
            elif state.get("last_bar") == day.isoformat():
                state.update(status="running", message="当日信号已处理，等待下一交易日")
            elif self.screener.latest_date() != day:
                state.update(status="waiting", message="日线数据未更新到当日，不回放历史信号")
            elif (
                not self.quotes
                or not self.quotes.status().get("final_sync_done")
                or self.quotes.status().get("final_sync_failed")
            ):
                state.update(status="waiting", message="收盘快照尚未确认，请检查行情源与数据同步")
            else:
                key = (state["revision"], day)
                if key not in results:
                    snapshot = state["snapshot"]
                    context = self.screener.build_strategy_context(
                        self.engine,
                        day,
                        [snapshot["strategy_id"]],
                        timeframe="1d",
                        params_map={snapshot["strategy_id"]: snapshot["params"]},
                        overrides_map={snapshot["strategy_id"]: snapshot["overrides"]},
                    )
                    if context.current is None or context.current.is_empty():
                        raise ValueError("当日日线数据为空")
                    if "raw_close" not in context.current.columns:
                        raise ValueError("缺少不复权价格，不能下单")
                    if context.current["date"].min() != day or context.current["date"].max() != day:
                        raise ValueError("策略输入日期不一致，已停止下单")
                    if (
                        self.engine.get(snapshot["strategy_id"]).execution_backend
                        != "matrix_native"
                    ):
                        required = (
                            snapshot["overrides"]["entry_signals"]
                            + snapshot["overrides"]["exit_signals"]
                        )
                        missing = [
                            s
                            for s in required
                            if (s if s.startswith(("signal_", "csg_")) else f"signal_{s}")
                            not in context.current.columns
                        ]
                        if missing:
                            raise ValueError("买卖信号依赖尚不可用: " + ", ".join(missing))
                    result = self.engine.run(
                        snapshot["strategy_id"],
                        context,
                        pool=snapshot["symbols"],
                        params=snapshot["params"],
                        overrides=snapshot["overrides"],
                    )
                    results[key] = (result, context.current)
                result, frame = results[key]
                check_revision(self.data_dir, self.engine, state["snapshot"])
                self._daily_orders(state, result, frame, day.isoformat())
                state.update(
                    last_bar=day.isoformat(), status="running", message="当日逐股信号已处理"
                )
        except Exception as exc:  # noqa: BLE001 — 金融计算无法确定时停用该绑定
            logger.warning(
                "paper execution paused for %s: %s", state["account"], type(exc).__name__
            )
            state.update(
                enabled=False,
                status="blocked",
                message=str(exc)
                if isinstance(exc, ValueError)
                else "策略跟单计算或存储失败，请检查数据与后端日志后恢复",
            )
        state["checked_at"] = now.isoformat()
        self._save(state)

    def _event(
        self, state: dict, symbol: str, side: str, reason: str, message: str, order=None
    ) -> None:
        state["events"] = [
            {
                "at": cn_now().isoformat(),
                "symbol": symbol,
                "side": side,
                "reason": reason,
                "message": message,
                "order_id": order["id"] if order else None,
            },
            *state.get("events", []),
        ][:100]

    def _submit(
        self,
        state: dict,
        orders: list,
        symbol: str,
        side: str,
        reason: str,
        bar: str,
        price: float,
        *,
        qty: int,
        order_type="next_open",
    ) -> dict | None:
        token = hashlib.sha256(
            f"{state['revision']}|{bar}|{symbol}|{side}|{reason}".encode()
        ).hexdigest()[:24]
        source = f"{PREFIX}{token}:{reason}"
        if any(o.get("source") == source for o in orders):
            return None
        if any(
            o["symbol"] == symbol and o["status"] == "pending" and o["side"] == side for o in orders
        ):
            return None
        if self.stopping.is_set():
            return None
        order, error = paper.create_order(
            self.data_dir,
            symbol,
            side,
            account_id=state["account"],
            qty=qty,
            ref_price=price,
            asset_type="stock",
            order_type=order_type,
            source=source,
        )
        self._event(state, symbol, side, reason, error or "已提交委托", order)
        if order:
            orders.append(order)
        return order

    def _daily_orders(self, state: dict, result, frame, day: str) -> None:
        snapshot = state["snapshot"]
        prices = {
            r["symbol"]: r["raw_close"] for r in frame.select("symbol", "raw_close").to_dicts()
        }
        if result.as_of.isoformat() != day:
            raise ValueError("策略结果日期不一致")
        entries = {r["symbol"] for r in result.entry_signal_hits}
        exits = {r["symbol"] for r in result.exit_signal_hits}
        # 不使用池进出事件；无显式买点的策略由引擎给出候选入场命中。
        with paper.PAPER_LOCK:
            positions_before = paper.load_positions(self.data_dir, state["account"])
        hold_days = (
            self._holding_days(positions_before, day)
            if snapshot["overrides"]["max_hold_days"]
            else {}
        )
        with paper.PAPER_LOCK:
            acc, positions, orders = self._check_account(state)
            for symbol, pos in positions.items():
                if pos["qty"] <= 0:
                    continue
                if symbol in exits:
                    state["exits"].setdefault(
                        symbol, {"reason": "signal", "bar": day, "price": prices.get(symbol)}
                    )
                elif (
                    snapshot["overrides"]["max_hold_days"]
                    and hold_days.get(symbol, 0) >= snapshot["overrides"]["max_hold_days"]
                ):
                    state["exits"].setdefault(
                        symbol, {"reason": "max_hold", "bar": day, "price": prices.get(symbol)}
                    )
            self._save(state)
            self._sell_intents(state, positions, orders, prices, attempt_day=day)
            held = {s for s, p in positions.items() if p["qty"] > 0}
            pending = {
                o["symbol"] for o in orders if o["side"] == "buy" and o["status"] == "pending"
            }
            blocked = (
                held
                | pending
                | exits
                | {
                    o["symbol"]
                    for o in orders
                    if o["side"] == "sell" and str(o.get("created_at", "")).startswith(day)
                }
            )
            selected = []
            for row in result.rows:
                symbol = row["symbol"]
                if symbol not in entries or symbol in blocked or symbol in selected:
                    continue
                score = result.scores.get(symbol)
                if any(snapshot["overrides"][k] is not None for k in ("score_min", "score_max")):
                    if score is None or not math.isfinite(score):
                        raise ValueError("买入评分无法计算")
                    if (
                        snapshot["overrides"]["score_min"] is not None
                        and score < snapshot["overrides"]["score_min"]
                    ):
                        continue
                    if (
                        snapshot["overrides"]["score_max"] is not None
                        and score > snapshot["overrides"]["score_max"]
                    ):
                        continue
                if _price(prices.get(symbol)) is None:
                    raise ValueError("入场标的缺少有效不复权价格")
                selected.append(symbol)
            # 核心回测以分数降序分配席位；无评分时稳定沿用引擎顺序。
            if any(not math.isfinite(result.scores.get(s, 0.0)) for s in selected):
                raise ValueError("入场评分无效，不能确定买入顺序")
            selected.sort(key=lambda s: result.scores.get(s, 0.0), reverse=True)
            slots = snapshot["max_positions"] - len(held | pending)
            selected = selected[: max(0, slots)]
            if not selected:
                return
            if any(_price(prices.get(s)) is None for s in held):
                raise ValueError("持仓缺少当日价格，不能可靠计算敞口")
            mv = sum(positions[s]["qty"] * prices[s] for s in held)
            equity = acc["cash"] + mv
            reserved = sum(
                self._reserved(o, acc)
                for o in orders
                if o["status"] == "pending" and o["side"] == "buy"
            )
            available = max(0.0, acc["cash"] - reserved)
            capacity = max(0.0, equity * snapshot["max_exposure_pct"] - mv - reserved)
            target = equity * snapshot["max_exposure_pct"] / snapshot["max_positions"]
            budget = min(available, capacity, target * len(selected))
            weights = (
                [max(0.0, result.scores.get(s, 0.0)) for s in selected]
                if snapshot["position_sizing"] == "score_weight"
                else [1.0] * len(selected)
            )
            if not all(math.isfinite(w) for w in weights):
                raise ValueError("买入评分无法计算")
            if not sum(weights):
                weights = [1.0] * len(selected)
            total_weight = sum(weights)
            for symbol, weight in zip(selected, weights, strict=True):
                allocation = min(budget * weight / total_weight, target, available, capacity)
                price = prices[symbol]
                # 留出真实佣金和滑点，避免满额委托被资金预检整体拒绝。
                qty = paper.qty_from_amount(
                    max(0.0, allocation - paper.MIN_COMMISSION)
                    / (1 + acc["commission_pct"] + acc["slippage_bps"] / 10000),
                    price,
                )
                if qty <= 0:
                    self._event(state, symbol, "buy", "signal", "预算不足一手，未下单")
                    continue
                order = self._submit(state, orders, symbol, "buy", "signal", day, price, qty=qty)
                if order:
                    cost = self._reserved(order, acc)
                    available -= cost
                    capacity -= cost

    @staticmethod
    def _reserved(order: dict, acc: dict) -> float:
        price = _price(order.get("ref_price"))
        if price is None:
            raise ValueError("待成交买单缺少参考价，不能可靠计算资金占用")
        return order["qty"] * price * (1 + acc["slippage_bps"] / 10000) + paper.buy_fee(
            order["qty"], price, acc["commission_pct"]
        )

    def _holding_days(self, positions: dict, day: str) -> dict[str, int]:
        # 批量读取，不持账务锁；按每只股票实际交易日计数，周末不以自然日代替。
        starts = {
            s: min(lot["date"] for lot in p["lots"] if lot["qty"] > 0)
            for s, p in positions.items()
            if p["qty"] > 0
        }
        if not starts:
            return {}
        frame = self.screener.repo.get_daily_batch(
            list(starts),
            datetime.fromisoformat(min(starts.values())).date(),
            datetime.fromisoformat(day).date(),
            columns=["date", "symbol"],
        )
        if frame.is_empty():
            raise ValueError("缺少持仓交易日数据，无法检查最大持有天数")
        result = {}
        for symbol, start in starts.items():
            days = frame.filter(frame["symbol"] == symbol)["date"].unique().to_list()
            if not days:
                raise ValueError("缺少持仓交易日数据，无法检查最大持有天数")
            result[symbol] = sum(start < d.isoformat() <= day for d in days)
        return result

    def _sell_intents(
        self,
        state: dict,
        positions: dict,
        orders: list,
        prices: dict,
        *,
        attempt_day: str,
        market=False,
    ) -> None:
        for symbol, intent in list(state["exits"].items()):
            if positions.get(symbol, {}).get("qty", 0) <= 0:
                del state["exits"][symbol]
                continue
            price = _price(prices.get(symbol))
            if price is None:
                continue
            capacity = paper.sell_capacity(self.data_dir, symbol, state["account"])
            qty = (
                int(max(0, capacity[0] - capacity[1]) // paper.LOT_SIZE) * paper.LOT_SIZE
                if capacity
                else 0
            )
            if qty:
                self._submit(
                    state,
                    orders,
                    symbol,
                    "sell",
                    intent["reason"],
                    attempt_day,
                    price,
                    qty=qty,
                    order_type="market"
                    if market and intent["reason"] not in {"signal", "max_hold"}
                    else "next_open",
                )

    def _risk_tick(self, state: dict, now: datetime) -> None:
        if not in_continuous_session(now) or not self.quotes:
            return
        risk = state["snapshot"]["overrides"]
        if not state["exits"] and not any(
            risk.get(k) is not None
            for k in ("stop_loss", "take_profit", "trailing_stop", "trailing_take_profit_activate")
        ):
            return
        status = self.quotes.status()
        age = status.get("quote_age_ms")
        if age is None or not 0 <= age <= 90_000:
            state["risk_message"] = "实时快照过期，保护性风控等待新鲜行情"
            return
        frame, day = self.quotes.get_enriched_today()
        if day != now.date() or frame.is_empty() or "raw_close" not in frame.columns:
            return
        if "quote_ts" not in frame.columns:
            state["risk_message"] = "实时快照缺少时间戳，保护性风控等待可确认行情"
            return
        stamp = now.timestamp() * 1000
        prices = {
            r["symbol"]: r["raw_close"]
            for r in frame.select("symbol", "raw_close", "quote_ts").to_dicts()
            if isinstance(r["quote_ts"], (int, float)) and 0 <= stamp - r["quote_ts"] <= 90_000
        }
        with paper.PAPER_LOCK:
            _, positions, orders = self._check_account(state)
            state["risk_message"] = (
                "实时风控运行中"
                if all(s in prices for s, p in positions.items() if p["qty"] > 0)
                else "部分持仓行情过期，等待新鲜快照"
            )
            fills = paper.load_fills(self.data_dir, state["account"])
            for symbol, pos in positions.items():
                if pos["qty"] <= 0:
                    state["risk"].pop(symbol, None)
                    continue
                price = _price(prices.get(symbol))
                if price is None:
                    continue
                buys = [
                    (i, f)
                    for i, f in enumerate(fills)
                    if f.get("symbol") == symbol
                    and f.get("side") == "buy"
                    and f.get("kind", "fill") == "fill"
                ]
                if not buys:
                    raise ValueError("持仓缺少入场成交，无法计算风控基准")
                buy_index, buy = buys[-1]
                entry = float(buy["price"])
                factor = math.prod(
                    float(f["factor"])
                    for f in fills[buy_index + 1 :]
                    if f.get("symbol") == symbol and f.get("kind") == "corp_action"
                )
                entry /= factor
                previous = state["risk"].get(symbol, {})
                same = previous.get("entry_order") == buy["order_id"]
                peak = (
                    previous.get("peak", entry) * previous.get("factor", factor) / factor
                    if same
                    else entry
                )
                peak = max(peak, price)
                state["risk"][symbol] = {
                    "entry_order": buy["order_id"],
                    "peak": peak,
                    "factor": factor,
                }
                # T+1 与回测一样：入场日不触发保护性卖出，但记录后续观测峰值。
                if buy["date"] >= day.isoformat():
                    continue
                reason = risk_reason(state["snapshot"]["overrides"], entry, peak, price)
                if reason:
                    state["exits"].setdefault(
                        symbol, {"reason": reason, "bar": day.isoformat(), "price": price}
                    )
            self._save(state)
            self._sell_intents(
                state, positions, orders, prices, attempt_day=day.isoformat(), market=True
            )


def _price(value) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def risk_reason(risk: dict, entry: float, peak: float, price: float) -> str | None:
    """实时观测点的风控阈值；不假定已知两次快照之间的高低点顺序。"""
    lines = []
    if risk.get("stop_loss") is not None:
        lines.append((entry * (1 - abs(risk["stop_loss"])), "stop_loss"))
    if risk.get("trailing_stop") is not None:
        lines.append((peak * (1 - abs(risk["trailing_stop"])), "trailing_stop"))
    activate = risk.get("trailing_take_profit_activate")
    drawdown = risk.get("trailing_take_profit_drawdown")
    if activate is not None and drawdown is not None and peak / entry - 1 >= activate:
        lines.append((peak * (1 - drawdown), "trailing_take_profit"))
    if lines:
        threshold, reason = max(lines, key=lambda v: v[0])
        if price <= threshold:
            return reason
    if risk.get("take_profit") is not None and price >= entry * (1 + abs(risk["take_profit"])):
        return "take_profit"
    return None
