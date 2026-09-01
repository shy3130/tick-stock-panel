"""独立持仓分析 Pi Agent 的确定性事实层。

保守落地决策：
- Pi worker 只触发一个窄只读工具；持仓、成本和资金流不回传给模型，判定与
  Markdown 均在 Python 中生成，兼顾复现性与券商事实隐私。
- 规范没有给指数调仓日历，因此 14:55–15:00 只在调用方显式确认调仓日时排除，
  不根据日期猜测；桶额覆盖率低于 0.95 视为不完整，以覆盖实测 0.83–0.91 缺桶区间。
- L1 只接受既有交易计划的结构化字段，不从自由文本推导无条件动作。
- 可选 Agent Reach 只查询主判定票的公开信息；结果固定为 C 级未核验展示，
  不进入纪律线、情景概率、自进化样本或任何交易动作。
"""
from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.json_safe import finite_float_or_none
from app.services.agent_reach_research import (
    AgentReachChannel,
    AgentReachResearchAdapter,
    PublicResearchBundle,
    PublicResearchSubject,
    get_agent_reach_research_adapter,
)
from app.services.trading import fhold_client
from app.services.trading import plans as plans_service

logger = logging.getLogger(__name__)


_CN_TZ = ZoneInfo("Asia/Shanghai")
_SYMBOL_RE = re.compile(r"^[0-9A-Z]{1,8}\.(SH|SZ|BJ|HK|ETF)$")

# §3.1 / §3.3 的已明示机械阈值。它们只决定数据可信度或风险标签，不会执行交易。
_PRICE_ANOMALY_RATIO = 0.15
_DAILY_LOSS_WARNING_CNY = -3_000.0
_MONEYFLOW_STALE_MINUTES = 15
_MONEYFLOW_WINDOW_MINUTES = 30
_ACCELERATED_OUTFLOW_CNY = -50_000_000.0
_MISSING_BUCKET_RATIO = 0.95
_DUPLICATE_BUCKET_RATIO = 1.20


class MoneyflowState(StrEnum):
    AVAILABLE = "available"
    INCOMPLETE = "incomplete"
    FROZEN = "frozen"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class PositionAnalysisL2Rule(BaseModel):
    """用户待确认的 L2 双条件，不是可执行订单。"""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    price_line: float = Field(gt=0)
    price_direction: Literal["below_or_equal", "above_or_equal"]
    moneyflow_direction: Literal["negative", "positive"]
    moneyflow_threshold: float = Field(gt=0)
    action_summary: str = Field(min_length=1, max_length=80)

    @field_validator("symbol")
    @classmethod
    def _canonical_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _SYMBOL_RE.fullmatch(normalized):
            raise ValueError("symbol 必须是带市场后缀的规范代码")
        return normalized

    @field_validator("action_summary")
    @classmethod
    def _single_line_action(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("action_summary 不能为空")
        return normalized


class TechnicalLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    source_date: str | None = None


class MoneyflowAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: MoneyflowState
    cutoff: str | None = None
    recent_net: float | None = None
    cumulative_net: float | None = None
    classification_evidence_grade: Literal["D"] | None = None
    total_amount: float | None = None
    quote_amount: float | None = None
    coverage_ratio: float | None = None
    classification: str | None = None
    note: str


class PositionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = None
    quantity: float | None = None
    cost_price: float | None = None
    position_ratio: float | None = None
    current_price: float | None = None
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    previous_close: float | None = None
    previous_close_date: str | None = None
    holding_pnl: float | None = None
    today_pnl: float | None = None
    change_pct: float | None = None
    quote_source: str | None = None
    quote_as_of: str | None = None
    technical: TechnicalLevels
    moneyflow: MoneyflowAssessment


class DisciplineLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    level: Literal["L1"] = "L1"
    source: str
    action: str
    trigger_price: float
    direction: Literal["below_or_equal", "above_or_equal"]
    current_price: float | None = None
    distance_pct: float | None = None
    triggered: bool = False


class L2Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    price_leg: bool
    moneyflow_leg: bool
    moneyflow_basis: str
    action_summary: str


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["A 弱势", "B 修复", "C 强攻"]
    probability: float | None = None
    trigger_action: str
    note: str


class PositionAnalysisResult(BaseModel):
    """可推送、可审计的持仓分析输出。所有金额单位均为人民币。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    analysis_id: str = Field(default_factory=lambda: f"pa-{uuid4().hex[:16]}")
    status: Literal["ok", "degraded", "invalid", "unavailable"]
    trade_date: str
    generated_at: str
    holdings_captured_at: str | None = None
    rows: list[PositionRow] = Field(default_factory=list)
    total_today_pnl: float | None = None
    warnings: list[str] = Field(default_factory=list)
    disciplines: list[DisciplineLine] = Field(default_factory=list)
    key_changes: list[str] = Field(default_factory=list)
    l2_items: list[L2Assessment] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    public_research: PublicResearchBundle = Field(default_factory=PublicResearchBundle)
    provenance: dict[str, str | None] = Field(default_factory=dict)
    markdown: str = ""


@dataclass(frozen=True)
class _DailyHoldingsSnapshot:
    trade_date: date
    captured_at: datetime
    available: bool
    positions: tuple[dict[str, Any], ...]


class PositionAnalysisService:
    """持仓盯盘判定服务。

    取 fhold 的操作按上海交易日缓存：同一进程中首轮读取后，盘中后续轮次
    绝不重新拉持仓。进程中途重启只能在首轮重新建立该日快照，这是没有额外
    持仓持久化事实源时的保守恢复边界。
    """

    def __init__(
        self,
        *,
        holdings_fetcher: Callable[[], dict[str, Any]] = fhold_client.fetch_holdings,
        provider_getter: Callable[[], Any] | None = None,
        research_adapter: AgentReachResearchAdapter | None = None,
    ) -> None:
        self._holdings_fetcher = holdings_fetcher
        self._provider_getter = provider_getter or _active_provider
        self._research_adapter = research_adapter
        self._snapshot_lock = threading.Lock()
        self._daily_snapshot: _DailyHoldingsSnapshot | None = None

    def analyze(
        self,
        app_state: Any,
        *,
        now: datetime | None = None,
        l2_rules: tuple[PositionAnalysisL2Rule, ...] = (),
        index_rebalance_tail_window: bool = False,
        public_research_enabled: bool = False,
        public_research_channels: tuple[AgentReachChannel, ...] = (
            AgentReachChannel.TWITTER,
        ),
    ) -> PositionAnalysisResult:
        observed_at = _as_cn_time(now)
        trade_date = observed_at.date()
        snapshot = self._holdings_for_day(trade_date, observed_at)
        generated_at = observed_at.isoformat(timespec="seconds")
        public_research = _not_run_public_research(
            public_research_enabled,
            public_research_channels,
            "holdings_unavailable",
        )
        if not snapshot.available:
            return self._with_markdown(
                PositionAnalysisResult(
                    status="unavailable",
                    trade_date=trade_date.isoformat(),
                    generated_at=generated_at,
                    warnings=["持仓快照不可用：本轮不推断仓位、盈亏或动作。"],
                    public_research=public_research,
                    provenance={
                        "holdings": "fhold-cli:unavailable",
                        "public_research": _research_provenance(public_research),
                    },
                )
            )

        warnings: list[str] = []
        positions = _usable_positions(snapshot.positions, warnings)
        if not positions:
            public_research = _not_run_public_research(
                public_research_enabled,
                public_research_channels,
                "no_positions",
            )
            return self._with_markdown(
                PositionAnalysisResult(
                    status="ok",
                    trade_date=trade_date.isoformat(),
                    generated_at=generated_at,
                    holdings_captured_at=snapshot.captured_at.isoformat(timespec="seconds"),
                    warnings=warnings,
                    public_research=public_research,
                    provenance={
                        "holdings": "fhold-cli:daily_snapshot",
                        "public_research": _research_provenance(public_research),
                    },
                )
            )

        symbols = [str(item["symbol"]) for item in positions]
        quotes, quote_warning = self._quotes(app_state)
        if quote_warning:
            warnings.append(quote_warning)
        provider, daily_rows, daily_warning = self._daily_rows(symbols, trade_date)
        if daily_warning:
            warnings.append(daily_warning)
        technical, technical_warning = self._technical_levels(app_state, symbols, trade_date)
        if technical_warning:
            warnings.append(technical_warning)

        ratio_denominator = sum(
            value
            for item in positions
            if (value := finite_float_or_none(item.get("marketValue"))) is not None and value > 0
        )
        rows: list[PositionRow] = []
        quote_amounts: dict[str, float | None] = {}
        for item in positions:
            symbol = str(item["symbol"])
            quote = quotes.get(symbol, {})
            current_price = finite_float_or_none(quote.get("last_price", quote.get("close")))
            previous_close = daily_rows.get(symbol, {}).get("close")
            quantity = finite_float_or_none(item.get("qty"))
            cost_price = finite_float_or_none(item.get("costPrice"))
            holding_pnl = _pnl(quantity, current_price, cost_price)
            today_pnl = _pnl(quantity, current_price, previous_close)
            change_pct = _ratio_change(current_price, previous_close)
            quote_amounts[symbol] = finite_float_or_none(quote.get("amount"))
            market_value = finite_float_or_none(item.get("marketValue"))
            ratio = market_value / ratio_denominator if market_value is not None and ratio_denominator else None
            rows.append(
                PositionRow(
                    symbol=symbol,
                    name=_as_text(item.get("name")),
                    quantity=quantity,
                    cost_price=cost_price,
                    position_ratio=ratio,
                    current_price=current_price,
                    open_price=finite_float_or_none(quote.get("open")),
                    high_price=finite_float_or_none(quote.get("high")),
                    low_price=finite_float_or_none(quote.get("low")),
                    previous_close=previous_close,
                    previous_close_date=_as_text(daily_rows.get(symbol, {}).get("date")),
                    holding_pnl=holding_pnl,
                    today_pnl=today_pnl,
                    change_pct=change_pct,
                    quote_source=_as_text(quote.get("source")),
                    technical=technical.get(symbol, TechnicalLevels()),
                    quote_as_of=_as_text(quote.get("_as_of")),
                    moneyflow=MoneyflowAssessment(
                        state=MoneyflowState.UNAVAILABLE,
                        note="尚未读取分钟大单资金流",
                    ),
                )
            )

        anomalous = [row.symbol for row in rows if row.change_pct is not None and abs(row.change_pct) > _PRICE_ANOMALY_RATIO]
        if anomalous:
            warnings.append(
                "价格数据异常（涨跌幅绝对值超过 15%）：" + "、".join(anomalous) + "；本轮判定作废。"
            )
            rows = [
                row.model_copy(
                    update={
                        "moneyflow": MoneyflowAssessment(
                            state=MoneyflowState.INVALID,
                            note="价格异常时不使用资金面进行判定",
                        )
                    }
                )
                for row in rows
            ]
            public_research = _not_run_public_research(
                public_research_enabled,
                public_research_channels,
                "invalid_price_round",
            )
            return self._with_markdown(
                PositionAnalysisResult(
                    status="invalid",
                    trade_date=trade_date.isoformat(),
                    generated_at=generated_at,
                    holdings_captured_at=snapshot.captured_at.isoformat(timespec="seconds"),
                    rows=rows,
                    total_today_pnl=_sum_or_none(row.today_pnl for row in rows),
                    warnings=warnings,
                    public_research=public_research,
                    provenance=_provenance(provider, rows, public_research),
                )
            )

        moneyflows, moneyflow_warning = self._moneyflows(
            provider,
            symbols,
            trade_date,
            observed_at,
            quote_amounts,
            exclude_tail_window=index_rebalance_tail_window,
        )
        if moneyflow_warning:
            warnings.append(moneyflow_warning)
        rows = [row.model_copy(update={"moneyflow": moneyflows[row.symbol]}) for row in rows]

        total_today_pnl = _sum_or_none(row.today_pnl for row in rows)
        if total_today_pnl is not None and total_today_pnl < _DAILY_LOSS_WARNING_CNY:
            warnings.append(f"今日合计亏损 {total_today_pnl:,.0f} 元，超过 -3,000 元警示线。")

        disciplines = self._discipline_lines(app_state, trade_date, rows, warnings)
        l2_items = _evaluate_l2(l2_rules, {row.symbol: row for row in rows})
        key_changes = _key_changes(rows)
        data_dir = getattr(getattr(getattr(app_state, "repo", None), "store", None), "data_dir", None)
        from app.services.position_analysis_learning import (
            DEFAULT_SCENARIO_PRIORS,
            active_scenario_priors,
        )

        active_priors = active_scenario_priors(data_dir)
        scenario_priors = active_priors or DEFAULT_SCENARIO_PRIORS
        degraded = any(
            row.current_price is None
            or row.previous_close is None
            or row.moneyflow.state is not MoneyflowState.AVAILABLE
            for row in rows
        )
        public_research = self._fetch_public_research(
            rows,
            enabled=public_research_enabled,
            channels=public_research_channels,
        )
        result = PositionAnalysisResult(
            status="degraded" if degraded else "ok",
            trade_date=trade_date.isoformat(),
            generated_at=generated_at,
            holdings_captured_at=snapshot.captured_at.isoformat(timespec="seconds"),
            rows=rows,
            total_today_pnl=total_today_pnl,
            warnings=warnings,
            disciplines=disciplines,
            key_changes=key_changes,
            l2_items=l2_items,
            scenarios=_scenarios(
                rows,
                disciplines,
                scenario_priors,
                calibrated=active_priors is not None,
            ),
            public_research=public_research,
            provenance=_provenance(provider, rows, public_research),
        )
        return self._with_markdown(result)

    def _fetch_public_research(
        self,
        rows: list[PositionRow],
        *,
        enabled: bool,
        channels: tuple[AgentReachChannel, ...],
    ) -> PublicResearchBundle:
        if not enabled:
            return PublicResearchBundle()
        if not rows:
            return _not_run_public_research(True, channels, "no_rows")
        primary = sorted(
            rows,
            key=lambda row: (
                -(row.position_ratio if row.position_ratio is not None else -1.0),
                row.symbol,
            ),
        )[0]
        adapter = self._research_adapter
        if adapter is None:
            adapter = AgentReachResearchAdapter()
            self._research_adapter = adapter
        try:
            return adapter.fetch(
                PublicResearchSubject(symbol=primary.symbol, name=primary.name),
                channels,
                scope="primary_position_only",
            )
        except Exception as exc:  # noqa: BLE001 - optional external context never blocks P0/P1.
            logger.warning("Agent Reach 公开信息读取失败 (%s)", type(exc).__name__)
            return _not_run_public_research(True, channels, "adapter_error")

    def public_research_health(self) -> dict[str, dict[str, str | None]]:
        adapter = self._research_adapter
        if adapter is None:
            adapter = AgentReachResearchAdapter()
            self._research_adapter = adapter
        return adapter.health()

    def _holdings_for_day(self, trade_date: date, now: datetime) -> _DailyHoldingsSnapshot:
        with self._snapshot_lock:
            if (
                self._daily_snapshot is not None
                and self._daily_snapshot.trade_date == trade_date
                and self._daily_snapshot.available
            ):
                return self._daily_snapshot
            try:
                payload = self._holdings_fetcher()
            except Exception:  # noqa: BLE001 - fhold already fail-soft; cache failure for the day.
                payload = {"available": False, "positions": []}
            available = bool(payload.get("available")) if isinstance(payload, dict) else False
            raw_positions = payload.get("positions") if isinstance(payload, dict) else []
            positions = tuple(item for item in raw_positions or [] if isinstance(item, dict))
            self._daily_snapshot = _DailyHoldingsSnapshot(
                trade_date=trade_date,
                captured_at=now,
                available=available,
                positions=positions,
            )
            return self._daily_snapshot

    def _quotes(self, app_state: Any) -> tuple[dict[str, dict[str, Any]], str | None]:
        service = getattr(app_state, "quote_service", None)
        if service is None or not callable(getattr(service, "get_quotes_compat", None)):
            return {}, "实时行情服务不可用；价格相关判定降级。"
        try:
            status = service.status() if callable(getattr(service, "status", None)) else None
            if isinstance(status, dict) and status.get("has_recent_data") is False:
                return {}, "实时行情缓存不新鲜；价格相关判定降级。"
            as_of = _as_text(status.get("source_as_of")) if isinstance(status, dict) else None
            return {
                str(row.get("symbol") or "").upper(): {**row, "_as_of": as_of}
                for row in _rows(service.get_quotes_compat())
                if isinstance(row, dict) and row.get("symbol")
            }, None
        except Exception:  # noqa: BLE001 - quote service is an optional read boundary.
            return {}, "实时行情读取失败；价格相关判定降级。"

    def _daily_rows(
        self, symbols: list[str], trade_date: date
    ) -> tuple[Any | None, dict[str, dict[str, Any]], str | None]:
        try:
            provider = self._provider_getter()
        except Exception:  # noqa: BLE001
            return None, {}, "日 K provider 不可用；昨收与涨跌幅不推断。"
        capabilities = getattr(provider, "capabilities", None)
        if capabilities is not None and getattr(capabilities, "daily", False) is not True:
            return provider, {}, "日 K capability 不可用；昨收与涨跌幅不推断。"
        getter = getattr(provider, "get_daily", None)
        if not callable(getter):
            return provider, {}, "provider 未提供日 K；昨收与涨跌幅不推断。"
        try:
            start = datetime.combine(trade_date - timedelta(days=45), time.min)
            end = datetime.combine(trade_date, time.max)
            raw_rows: list[dict[str, Any]] = []
            for asset_type in ("stock", "hk", "etf"):
                selected = [symbol for symbol in symbols if _asset_type(symbol) == asset_type]
                if selected:
                    raw_rows.extend(_rows(getter(selected, start, end, asset_type)))
        except Exception:  # noqa: BLE001
            return provider, {}, "日 K读取失败；昨收与涨跌幅不推断。"
        latest: dict[str, tuple[date, float]] = {}
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            row_date = _as_date(row.get("date") or row.get("trade_date"))
            close = finite_float_or_none(row.get("close"))
            if symbol not in symbols or row_date is None or row_date >= trade_date or close is None:
                continue
            old = latest.get(symbol)
            if old is None or row_date > old[0]:
                latest[symbol] = (row_date, close)
        return provider, {
            symbol: {"date": day.isoformat(), "close": close}
            for symbol, (day, close) in latest.items()
        }, None

    def _technical_levels(
        self, app_state: Any, symbols: list[str], trade_date: date
    ) -> tuple[dict[str, TechnicalLevels], str | None]:
        repo = getattr(app_state, "repo", None)
        getter = getattr(repo, "get_enriched_range", None)
        if not callable(getter):
            return {}, "技术指标仓储不可用；MA5/10/20 标注为 n/a。"
        try:
            frame = getter(
                trade_date - timedelta(days=45),
                trade_date - timedelta(days=1),
                symbols=symbols,
                columns=["ma5", "ma10", "ma20"],
            )
        except Exception:  # noqa: BLE001
            return {}, "技术指标读取失败；MA5/10/20 标注为 n/a。"
        latest: dict[str, tuple[date, dict[str, Any]]] = {}
        for row in _rows(frame):
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            row_date = _as_date(row.get("date"))
            if symbol not in symbols or row_date is None or row_date >= trade_date:
                continue
            old = latest.get(symbol)
            if old is None or row_date > old[0]:
                latest[symbol] = (row_date, row)
        return {
            symbol: TechnicalLevels(
                ma5=finite_float_or_none(row.get("ma5")),
                ma10=finite_float_or_none(row.get("ma10")),
                ma20=finite_float_or_none(row.get("ma20")),
                source_date=row_date.isoformat(),
            )
            for symbol, (row_date, row) in latest.items()
        }, None

    def _moneyflows(
        self,
        provider: Any | None,
        symbols: list[str],
        trade_date: date,
        now: datetime,
        quote_amounts: dict[str, float | None],
        *,
        exclude_tail_window: bool,
    ) -> tuple[dict[str, MoneyflowAssessment], str | None]:
        unavailable = {
            symbol: MoneyflowAssessment(
                state=MoneyflowState.UNAVAILABLE,
                note="分钟大单资金流 capability 不可用；仅按价格判定。",
            )
            for symbol in symbols
        }
        if provider is None:
            return unavailable, None
        status_getter = getattr(provider, "get_moneyflow_status", None)
        moneyflow_getter = getattr(provider, "get_moneyflow_stock", None)
        if not callable(status_getter) or not callable(moneyflow_getter):
            return unavailable, None
        try:
            status = status_getter()
            capability = status.get("moneyflow_minute_stock", {}) if isinstance(status, dict) else {}
            if not isinstance(capability, dict) or capability.get("available") is not True:
                return unavailable, None
        except Exception:  # noqa: BLE001
            return unavailable, None

        output: dict[str, MoneyflowAssessment] = {}
        for symbol in symbols:
            try:
                raw = _rows(moneyflow_getter(symbol, trade_date, trade_date, "minute"))
            except Exception:  # noqa: BLE001
                output[symbol] = unavailable[symbol]
                continue
            output[symbol] = _assess_moneyflow(
                raw,
                now,
                quote_amounts.get(symbol),
                exclude_tail_window=exclude_tail_window,
            )

        duplicated = [
            symbol
            for symbol, item in output.items()
            if item.coverage_ratio is not None and item.coverage_ratio > _DUPLICATE_BUCKET_RATIO
        ]
        if duplicated:
            return {
                symbol: MoneyflowAssessment(
                    state=MoneyflowState.INVALID,
                    cutoff=item.cutoff,
                    quote_amount=item.quote_amount,
                    coverage_ratio=item.coverage_ratio,
                    note="桶成交额与行情成交额比值超过 1.20，疑似重复计数；全票大单读数作废，仅按价格判定。",
                )
                for symbol, item in output.items()
            }, "分钟大单资金流疑似重复计数（" + "、".join(duplicated) + "）；全票资金面已作废。"
        return output, None

    def _discipline_lines(
        self,
        app_state: Any,
        trade_date: date,
        rows: list[PositionRow],
        warnings: list[str],
    ) -> list[DisciplineLine]:
        repo = getattr(app_state, "repo", None)
        data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
        if data_dir is None:
            warnings.append("交易计划存储不可用；未加载 L1 既定线。")
            return []
        plan_path = (
            Path(data_dir)
            / "user_data"
            / "trading"
            / "plans"
            / f"{trade_date.strftime('%Y%m%d')}.json"
        )
        if not plan_path.is_file():
            return []
        try:
            plan = plans_service.read_plan(data_dir, trade_date.strftime("%Y%m%d"))
        except Exception:  # noqa: BLE001
            warnings.append("交易计划读取失败；未加载 L1 既定线。")
            return []
        row_by_symbol = {row.symbol: row for row in rows}
        lines: list[DisciplineLine] = []
        for entry in (plan or {}).get("entries", []):
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol") or "").upper()
            row = row_by_symbol.get(symbol)
            if row is None:
                continue
            action = str(entry.get("action") or "")
            stop_loss = finite_float_or_none(entry.get("stopLoss"))
            planned_price = finite_float_or_none(entry.get("plannedPrice"))
            if stop_loss is not None:
                lines.append(_line(symbol, "stopLoss", "止损", stop_loss, "below_or_equal", row.current_price))
            elif action == "sl" and planned_price is not None:
                lines.append(_line(symbol, "plannedPrice", "止损", planned_price, "below_or_equal", row.current_price))
            elif action in {"tp", "trim"} and planned_price is not None:
                lines.append(_line(symbol, "plannedPrice", "减仓", planned_price, "above_or_equal", row.current_price))
            elif entry.get("trigger"):
                # trigger 是自由文本，不可安全解析成机械价格线；宁可不触发也不猜测。
                warnings.append(f"{symbol} 的计划 trigger 非结构化，未作为 L1 既定线执行。")
        return lines

    @staticmethod
    def _with_markdown(result: PositionAnalysisResult) -> PositionAnalysisResult:
        return result.model_copy(update={"markdown": render_markdown(result)})


def build_pi_agent_definition(
    l2_rules: tuple[PositionAnalysisL2Rule, ...],
    *,
    index_rebalance_tail_window: bool,
    public_research_enabled: bool,
    public_research_channels: tuple[AgentReachChannel, ...],
):
    """构造只暴露一次分析工具的 Pi definition。

    terminal_before_reply=True 是刻意设计：Pi 只可请求工具，Python 在把任何持仓
    或成本事实写回 worker 前终止子进程；工具事件也不先于判定文本对外展示。
    """
    from app.services.agent_runtime import PiAgentDefinition

    def dispatch(name: str, app_state: Any, args: dict[str, Any]) -> dict[str, Any]:
        if name != "analyze_position_snapshot" or args:
            raise ValueError("持仓分析工具参数无效")
        service = _service_for(app_state)
        return service.analyze(
            app_state,
            l2_rules=l2_rules,
            index_rebalance_tail_window=index_rebalance_tail_window,
            public_research_enabled=public_research_enabled,
            public_research_channels=public_research_channels,
        ).model_dump(mode="json")

    def terminal_text(name: str, result: dict[str, Any]) -> str | None:
        if name != "analyze_position_snapshot":
            return None
        markdown = result.get("markdown") if isinstance(result, dict) else None
        return markdown if isinstance(markdown, str) else None

    return PiAgentDefinition(
        tools=(
            {
                "name": "analyze_position_snapshot",
                "description": "读取当日缓存持仓、本地行情和资金流；按调用方开关可附加 Agent Reach C 级公开信息，生成一次纪律化持仓判定。无参数。",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                "read_only": True,
            },
        ),
        system_prompt=(
            "你是持仓分析 Pi Agent。必须且只能调用一次 analyze_position_snapshot。"
            "不得预测、荐股、下单或修改任何持仓/交易事实。工具结果不会回传给你；"
            "Python 会直接交付经过数据门控的纪律化判定。"
        ),
        final_prompt="不得输出自然语言；最终输出由 Python 的确定性模板生成。",
        dispatch_tool=dispatch,
        terminal_text=terminal_text,
        terminal_before_reply=True,
        emit_tool_events=False,
    )


async def run_position_analysis_stream(
    app_state: Any,
    *,
    profile_id: str | None,
    l2_rules: tuple[PositionAnalysisL2Rule, ...],
    index_rebalance_tail_window: bool,
    public_research_enabled: bool,
    public_research_channels: tuple[AgentReachChannel, ...],
):
    """运行独立 Pi Agent；不受通用 Agent 的 python/pi 全局开关影响。"""
    from app.services.agent_runtime import run_scoped_pi_agent_stream

    definition = build_pi_agent_definition(
        l2_rules,
        index_rebalance_tail_window=index_rebalance_tail_window,
        public_research_enabled=public_research_enabled,
        public_research_channels=public_research_channels,
    )
    messages = [{"role": "user", "content": "执行一次持仓纪律分析。"}]
    async for line in run_scoped_pi_agent_stream(
        messages, app_state, profile_id, definition
    ):
        yield line


def render_markdown(result: PositionAnalysisResult) -> str:
    """按 §5 固定顺序渲染；此函数不调用模型，也不发送 meow。"""
    header = f"**{result.generated_at[11:16]}轮 · {result.trade_date}持仓盯盘**"
    if result.warnings:
        header += "[ ⚠️警示]"
    lines = [header, "", "| 票 | 现价 | 涨幅 | 持仓盈亏 | 今日 | 大单 |", "|---|---:|---:|---:|---:|---|"]
    if not result.rows:
        lines.append("| — | — | — | — | — | — |")
    for row in result.rows:
        cells = [
            row.name or row.symbol,
            _money(row.current_price, decimals=2),
            _percent(row.change_pct),
            _money(row.holding_pnl),
            _money(row.today_pnl),
            _moneyflow_cell(row.moneyflow),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", f"今日合计: {_money(result.total_today_pnl)}元", "", "纪律:"])
    if result.status == "invalid":
        lines.append("- 本轮价格异常，所有纪律判定作废。")
    elif not result.disciplines:
        lines.append("- 无可核验的结构化 L1 既定线。")
    else:
        for item in result.disciplines:
            direction = "跌破" if item.direction == "below_or_equal" else "收复"
            trigger = "已触发，需人工执行" if item.triggered else "未触发"
            lines.append(
                f"- L1: {item.symbol} {direction}线 {_money(item.trigger_price, decimals=2)}，"
                f"现价 {_money(item.current_price, decimals=2)}，距离 {_percent(item.distance_pct / 100 if item.distance_pct is not None else None)}；{trigger}（{item.action}）。"
            )
    if result.scenarios:
        lines.extend(["", "情景框架（主观概率，非预测）:", "| 情景 | 概率 | 触发位 → 动作 |", "|---|---:|---|"])
        for scenario in result.scenarios:
            probability = _percent(scenario.probability) if scenario.probability is not None else "未配置"
            lines.append(f"| {scenario.name} | {probability} | {scenario.trigger_action} |")
        lines.append(f"- {result.scenarios[0].note}")
    if result.warnings:
        lines.extend(["", "数据警示:"])
        lines.extend(f"- {item}" for item in result.warnings)
    research = result.public_research
    if research.status != "disabled":
        lines.extend(
            [
                "",
                "外部公开信息（C级未核验，仅作情绪附注，不参与纪律判定）:",
            ]
        )
        if research.evidence:
            for item in research.evidence:
                author = f" @{_markdown_escape(item.author)}" if item.author else ""
                published = f" · {item.published_at}" if item.published_at else ""
                lines.append(
                    f"- [UNVERIFIED] {item.platform}{author}{published}: "
                    f"{_markdown_escape(item.excerpt)} ([来源]({item.url}))"
                )
        else:
            reason = "、".join(research.warnings) if research.warnings else "backend_unavailable"
            lines.append(f"- unavailable（{_markdown_escape(reason)}）")
    if result.key_changes:
        lines.extend(["", "关键变化:"])
        lines.extend(f"- {item}" for item in result.key_changes)
    if result.l2_items:
        lines.extend(["", "待用户裁决项:"])
        for item in result.l2_items:
            price_mark = "✓" if item.price_leg else "✗"
            money_mark = "✓" if item.moneyflow_leg else "✗"
            row = next((candidate for candidate in result.rows if candidate.symbol == item.symbol), None)
            current = _money(row.current_price, decimals=2) if row else "n/a"
            lines.append(
                f"- L2 双条件状态：价格腿 {price_mark}（现价 {current} vs 线）、"
                f"资金腿 {money_mark}（{item.moneyflow_basis}）→ 待用户裁决（执行含义 = {item.action_summary}）"
            )
    lines.extend(["", "数据截至: " + result.generated_at, "非投资建议；L1 仅输出触发信号，不自动执行交易。"])
    return "\n".join(lines)


def _active_provider() -> Any:
    from app.data_providers.registry import get_active_provider_name, get_provider

    return get_provider(get_active_provider_name("daily"))


def _asset_type(symbol: str) -> Literal["stock", "hk", "etf"]:
    if symbol.endswith(".HK"):
        return "hk"
    if symbol.endswith(".ETF"):
        return "etf"
    return "stock"


def _service_for(app_state: Any) -> PositionAnalysisService:
    service = getattr(app_state, "position_analysis_service", None)
    if not isinstance(service, PositionAnalysisService):
        service = PositionAnalysisService(
            research_adapter=get_agent_reach_research_adapter(app_state),
        )
        app_state.position_analysis_service = service
    return service

def public_research_health(app_state: Any) -> dict[str, Any]:
    """显式健康检查；默认开关仍为关闭，不触发任何研究查询。"""
    return {
        "default_enabled": False,
        "supported_channels": [channel.value for channel in AgentReachChannel],
        "health": _service_for(app_state).public_research_health(),
    }


def _usable_positions(raw: tuple[dict[str, Any], ...], warnings: list[str]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for item in raw:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            warnings.append(f"无法映射为规范代码的持仓 {item.get('code') or '未知'} 未进入行情判定。")
            continue
        positions.append({**item, "symbol": symbol})
    return positions


def _assess_moneyflow(
    raw_rows: list[dict[str, Any]],
    now: datetime,
    quote_amount: float | None,
    *,
    exclude_tail_window: bool = False,
) -> MoneyflowAssessment:
    now = _as_cn_time(now)
    buckets: list[tuple[datetime, float, float | None]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        bucket_time = _as_bucket_time(row.get("bucket_time"), now.date())
        super_large = finite_float_or_none(row.get("super_large_net"))
        large = finite_float_or_none(row.get("large_net"))
        if bucket_time is None or super_large is None or large is None:
            continue
        buckets.append((bucket_time, super_large + large, finite_float_or_none(row.get("total_amount"))))
    if not buckets:
        return MoneyflowAssessment(
            state=MoneyflowState.UNAVAILABLE,
            note="分钟大单字段缺失；禁止推断散户方向，仅按价格判定。",
        )
    buckets.sort(key=lambda item: item[0])
    latest = buckets[-1][0]
    cutoff = latest.strftime("%H:%M")

    cumulative_net = sum(item[1] for item in buckets)
    window_start = now - timedelta(minutes=_MONEYFLOW_WINDOW_MINUTES)
    # 仅在调用方由官方调仓日事实确认后排除 14:55-15:00；未知日期不猜。
    recent = [
        item
        for item in buckets
        if item[0] > window_start
        and not (
            exclude_tail_window
            and time(14, 55) <= item[0].timetz().replace(tzinfo=None) <= time(15, 0)
        )
    ]
    recent_net = sum(item[1] for item in recent) if recent else None
    total_amount = sum(item[2] for item in buckets if item[2] is not None)
    coverage_ratio = total_amount / quote_amount if quote_amount and quote_amount > 0 else None
    has_gap = _has_bucket_gap([item[0] for item in buckets])
    state = MoneyflowState.AVAILABLE
    note = "主力口径=超大单+大单；最近 30 分钟净额与当日累计净额分列。"
    if now - latest > timedelta(minutes=_MONEYFLOW_STALE_MINUTES):
        state = MoneyflowState.FROZEN
        recent_net = None
        note = f"大单数据冻结至 {cutoff}；资金归因降级为纯价格。"
    elif coverage_ratio is not None and coverage_ratio < _MISSING_BUCKET_RATIO:
        state = MoneyflowState.INCOMPLETE
        note = (
            f"桶成交额/行情成交额={coverage_ratio:.2f}，存在少采；主力口径仅作保守参考，"
            "资金归因为盲区推断。"
        )
    elif has_gap:
        state = MoneyflowState.INCOMPLETE
        note = "检测到分钟桶断档；价格结论可用，资金归因为盲区推断。"

    classification: str | None = None
    if recent_net is not None and recent_net <= _ACCELERATED_OUTFLOW_CNY:
        classification = "accelerated_distribution"
    peak_cumulative = max(_prefix_sums(item[1] for item in buckets), default=0.0)
    if peak_cumulative >= 100_000_000 and cumulative_net > 0 and cumulative_net <= peak_cumulative * 0.5:
        classification = "positive_inflow_receding"
    return MoneyflowAssessment(
        state=state,
        cutoff=cutoff,
        recent_net=recent_net,
        cumulative_net=cumulative_net,
        total_amount=total_amount if total_amount else None,
        quote_amount=quote_amount,
        coverage_ratio=coverage_ratio,
        classification=classification,
        classification_evidence_grade="D" if classification else None,
        note=note,
    )


def _evaluate_l2(
    rules: tuple[PositionAnalysisL2Rule, ...], rows: dict[str, PositionRow]
) -> list[L2Assessment]:
    items: list[L2Assessment] = []
    for rule in rules:
        row = rows.get(rule.symbol)
        current = row.current_price if row else None
        price_leg = (
            current <= rule.price_line
            if current is not None and rule.price_direction == "below_or_equal"
            else current >= rule.price_line
            if current is not None
            else False
        )
        money = row.moneyflow if row else None
        recent_net = money.recent_net if money and money.state in {MoneyflowState.AVAILABLE, MoneyflowState.INCOMPLETE} else None
        money_leg = (
            recent_net <= -rule.moneyflow_threshold
            if recent_net is not None and rule.moneyflow_direction == "negative"
            else recent_net >= rule.moneyflow_threshold
            if recent_net is not None
            else False
        )
        if money is None or recent_net is None:
            basis = "大单 n/a，资金腿不成立"
        else:
            basis = f"{money.note}；最近30分钟={_money(recent_net)}元"
        items.append(
            L2Assessment(
                symbol=rule.symbol,
                price_leg=price_leg,
                moneyflow_leg=money_leg,
                moneyflow_basis=basis,
                action_summary=rule.action_summary,
            )
        )
    return items


def _key_changes(rows: list[PositionRow]) -> list[str]:
    changes: list[str] = []
    for row in rows:
        money = row.moneyflow
        if money.state is MoneyflowState.FROZEN:
            changes.append(f"{row.symbol} 大单数据冻结至 {money.cutoff}，资金归因已降级为纯价格。")
        elif money.state is MoneyflowState.INVALID:
            changes.append(f"{row.symbol} 大单金额口径疑似重复，已作废。")
        elif money.classification == "accelerated_distribution":
            changes.append(
                f"[INFERENCE] {row.symbol} 最近30分钟主力净流出 {_money(money.recent_net)}元；"
                "该结论未把全天累计净额当作时段净额。"
            )
        elif money.classification == "positive_inflow_receding" and _tail_pressure(row):
            changes.append(
                f"[INFERENCE] {row.symbol} 主力净流入从盘中峰值回落且尾盘承压："
                "利好兑现换手，保留为中性复合分类，不做二元资金归因。"
            )
        elif money.state is MoneyflowState.INCOMPLETE:
            changes.append(f"{row.symbol} 分钟资金流不完整，资金归因为盲区推断。")
    return changes


def _scenarios(
    rows: list[PositionRow],
    lines: list[DisciplineLine],
    priors: dict[str, float],
    *,
    calibrated: bool,
) -> list[Scenario]:
    primary = max((row for row in rows if row.position_ratio is not None), key=lambda row: row.position_ratio, default=None)
    if primary is None:
        return []
    down = next(
        (line for line in lines if line.symbol == primary.symbol and line.direction == "below_or_equal"), None
    )
    up = next(
        (line for line in lines if line.symbol == primary.symbol and line.direction == "above_or_equal"), None
    )
    weak = (
        f"跌破 {_money(down.trigger_price, decimals=2)} → {down.action}(L1)"
        if down
        else "无可核验的 L1 下行线；仅观察"
    )
    repair = (
        f"收复 {_money(up.trigger_price, decimals=2)} → {up.action}(L1)"
        if up
        else "无可核验的 L1 修复线；仅观察"
    )
    note = (
        "[INFERENCE] 概率来自已验证且人工批准的本地反馈校准 profile；"
        "仍是主观情景权重，非预测。"
        if calibrated
        else "[INFERENCE] 概率使用规范示例的保守初始权重；不是历史规律或股价预测。"
    )
    return [
        Scenario(
            name="A 弱势",
            probability=priors["weak"],
            trigger_action=weak,
            note=note,
        ),
        Scenario(
            name="B 修复",
            probability=priors["repair"],
            trigger_action=repair,
            note=note,
        ),
        Scenario(
            name="C 强攻",
            probability=priors["strong_attack"],
            trigger_action="需最近30分钟主力净额转正，且分钟资金流口径有效",
            note=note,
        ),
    ]


def _line(
    symbol: str,
    source: str,
    action: str,
    trigger_price: float,
    direction: Literal["below_or_equal", "above_or_equal"],
    current_price: float | None,
) -> DisciplineLine:
    triggered = (
        current_price <= trigger_price
        if current_price is not None and direction == "below_or_equal"
        else current_price >= trigger_price
        if current_price is not None
        else False
    )
    distance = ((current_price / trigger_price) - 1) * 100 if current_price is not None else None
    return DisciplineLine(
        symbol=symbol,
        source=source,
        action=action,
        trigger_price=trigger_price,
        direction=direction,
        current_price=current_price,
        distance_pct=distance,
        triggered=triggered,
    )


def _provenance(
    provider: Any | None,
    rows: list[PositionRow],
    public_research: PublicResearchBundle,
) -> dict[str, str | None]:
    sources = {row.quote_source for row in rows if row.quote_source}
    return {
        "holdings": "fhold-cli:daily_snapshot",
        "daily": str(getattr(provider, "name", None)) if provider is not None else None,
        "quote": ",".join(sorted(sources)) if sources else None,
        "moneyflow": str(getattr(provider, "name", None)) if provider is not None else None,
        "sector_moneyflow": "not_integrated_optional",
        "public_research": _research_provenance(public_research),
        "news_sentiment": _research_provenance(public_research),
    }


def _not_run_public_research(
    enabled: bool,
    channels: tuple[AgentReachChannel, ...],
    reason: str,
) -> PublicResearchBundle:
    if not enabled:
        return PublicResearchBundle()
    return PublicResearchBundle(
        status="unavailable",
        channels_requested=list(dict.fromkeys(channel.value for channel in channels)),
        warnings=[f"agent_reach:not_run:{reason}"],
    )


def _research_provenance(bundle: PublicResearchBundle) -> str:
    channels = ",".join(bundle.channels_used or bundle.channels_requested) or "none"
    return f"agent-reach:{bundle.status}:{channels}:grade_c"


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, pl.DataFrame):
        return value.to_dicts()
    to_dicts = getattr(value, "to_dicts", None)
    if callable(to_dicts):
        return to_dicts()
    return value if isinstance(value, list) else []


def _as_cn_time(value: datetime | None) -> datetime:
    now = value or datetime.now(_CN_TZ)
    return now.replace(tzinfo=_CN_TZ) if now.tzinfo is None else now.astimezone(_CN_TZ)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_bucket_time(value: Any, default_date: date) -> datetime | None:
    if isinstance(value, datetime):
        return _as_cn_time(value)
    if isinstance(value, time):
        return datetime.combine(default_date, value, tzinfo=_CN_TZ)
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed_time = time.fromisoformat(raw)
        except ValueError:
            return None
        return datetime.combine(default_date, parsed_time, tzinfo=_CN_TZ)
    return _as_cn_time(parsed)



def _pnl(quantity: float | None, current: float | None, baseline: float | None) -> float | None:
    if quantity is None or current is None or baseline is None:
        return None
    return quantity * (current - baseline)

def _ratio_change(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return round(current / baseline - 1, 12)


def _sum_or_none(values) -> float | None:
    kept = [value for value in values if value is not None and math.isfinite(value)]
    return sum(kept) if kept else None


def _has_bucket_gap(times: list[datetime]) -> bool:
    if len(times) < 3:
        return False
    deltas = [later - earlier for earlier, later in pairwise(times) if later > earlier]
    if not deltas:
        return False
    baseline = min(deltas)
    return any(delta > baseline * 2 for delta in deltas)


def _prefix_sums(values) -> list[float]:
    total = 0.0
    output: list[float] = []
    for value in values:
        total += value
        output.append(total)
    return output


def _tail_pressure(row: PositionRow) -> bool:
    return (
        row.moneyflow.cutoff is not None
        and row.moneyflow.cutoff >= "14:55"
        and row.current_price is not None
        and row.high_price is not None
        and row.high_price > 0
        and row.current_price <= row.high_price * 0.99
    )


def _as_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _markdown_escape(value: str) -> str:
    text = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in ("\\", "`", "*", "_", "[", "]", "(", ")"):
        text = text.replace(character, f"\\{character}")
    return text


def _money(value: float | None, *, decimals: int = 0) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:,.{decimals}f}"


def _percent(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100:+.2f}%"


def _moneyflow_cell(item: MoneyflowAssessment) -> str:
    if item.state in {MoneyflowState.FROZEN, MoneyflowState.UNAVAILABLE, MoneyflowState.INVALID}:
        return "n/a"
    return _money(item.recent_net)
