"""弱转强事件因子：仅使用注入的 sealed reader，缺证据即 fail-closed。"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Protocol, TypedDict, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

WEAK_TO_STRONG_PROTOCOL_ID = "weak_to_strong_v1"
WEAK_TO_STRONG_SCHEMA_VERSION = 2
WEAK_TO_STRONG_DISCLAIMER = "研究评估输出：仅结构化证据、删失与能力状态；不含交易指令、买卖方向、价格目标或投资建议"
MAX_SYMBOLS_PER_REQUEST = 100
VOLUME_BASELINE_DAYS = 5
VOLUME_SURGE_RATIO = 2.0
DEFAULT_RESEARCH_COST_BPS = 20.0
REQUIRED_CAPABILITIES: tuple[str, ...] = ("immutable_run_manifest", "canonical_daily_sealed_reader", "timestamped_minute_reader", "auction_evidence_reader", "sortable_tick_reader", "historical_order_book_reader", "pit_regime_records", "pit_st_records", "pit_float_shares_records")
_CENSORING_BY_CAPABILITY = {"immutable_run_manifest": "missing_run_manifest", "canonical_daily_sealed_reader": "missing_sealed_daily", "timestamped_minute_reader": "missing_timestamped_minute", "auction_evidence_reader": "censored_preopen", "sortable_tick_reader": "missing_sortable_tick", "historical_order_book_reader": "missing_order_book_evidence", "pit_regime_records": "missing_pit_regime", "pit_st_records": "missing_pit_st", "pit_float_shares_records": "missing_pit_float_shares"}
BANNED_EVIDENCE_KEY_TERMS = ("buy", "sell", "go_long", "go_short", "long", "short", "entry", "exit", "position", "stop_loss", "stop_profit", "take_profit", "target_price", "trade_signal", "order_action", "买", "卖", "加仓", "减仓", "建仓", "开仓", "平仓", "清仓", "止损", "止盈", "目标价", "下单", "挂单")
EvidenceValue = Union[str, int, float, bool, None]
EventLabel = Literal["one_word_limit", "sealed_limit", "broken_resealed", "broken_not_resealed", "gap_up_no_touch", "no_gap_up", "no_prior_day_limit_up", "bar_touched"]
Partition = Literal["is", "oos", "unspecified"]
_SYMBOL_RE = re.compile(r"^(?:(SH|SZ|BJ)\.?)?(\d{6})(?:\.(SH|SZ|BJ))?$", re.IGNORECASE)
_MARKET_BY_CODE_PREFIX = (("SH", ("600", "601", "603", "605", "688", "689")), ("SZ", ("000", "001", "002", "003", "300", "301")), ("BJ", ("43", "83", "87", "88", "92")))

def _market_for_code(code: str) -> str | None:
    for market, prefixes in _MARKET_BY_CODE_PREFIX:
        if code.startswith(prefixes): return market
    return None

def canonicalize_symbol(raw: str) -> str:
    if not isinstance(raw, str): raise ValueError("symbol must be a string")
    m = _SYMBOL_RE.match(raw)
    if m is None: raise ValueError(f"unrecognized symbol format: {raw!r}")
    prefix, code, suffix = (m.group(1) or "").upper() or None, m.group(2), (m.group(3) or "").upper() or None
    market = _market_for_code(code)
    if market is None:
        if prefix or suffix: raise ValueError(f"cannot verify market for code {code}")
    elif (prefix and prefix != market) or (suffix and suffix != market): raise ValueError(f"exchange qualifier does not match {code}")
    return code

def validate_evidence_keys(keys: Iterable[str]) -> None:
    for key in keys:
        for term in BANNED_EVIDENCE_KEY_TERMS:
            if term in key.lower(): raise ValueError(f"evidence key {key!r} contains banned term {term!r}")

class RunManifest(TypedDict):
    generation: str
    sha256: str
class DailyBar(TypedDict):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
class MinuteBar(TypedDict):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
class AuctionEvidence(TypedDict):
    open_price: float
    matched_volume: float
class Tick(TypedDict):
    timestamp: datetime
    seq: int
    price: float
    volume: float
class OrderBook(TypedDict):
    timestamp: datetime
    bid1_price: float | None
    bid1_volume: float
    ask1_price: float | None
    ask1_volume: float
class PITRecord(TypedDict):
    effective_at: datetime
    available_at: datetime
    limit_up_pct: float | None
    limit_down_pct: float | None
    is_st: bool | None
    float_shares: float | None

class WeakToStrongReader(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def run_manifest(self) -> RunManifest: ...
    def daily_bars(self, symbol: str, start: date, end: date) -> Sequence[DailyBar]: ...
    def suspended_dates(self, symbol: str, start: date, end: date) -> Sequence[date]: ...
    def minute_bars(self, symbol: str, trade_date: date) -> Sequence[MinuteBar]: ...
    def auction_snapshot(self, symbol: str, trade_date: date) -> AuctionEvidence | None: ...
    def ticks(self, symbol: str, trade_date: date) -> Sequence[Tick]: ...
    def order_book_snapshots(self, symbol: str, trade_date: date) -> Sequence[OrderBook]: ...
    def pit_snapshot(self, symbol: str, as_of: date) -> PITRecord | None: ...

ReaderFactory = Callable[[], WeakToStrongReader | None]
_READER_FACTORIES: list[ReaderFactory] = []
def register_reader_factory(factory: ReaderFactory) -> None: _READER_FACTORIES.append(factory)
def resolve_weak_to_strong_reader() -> WeakToStrongReader | None:
    for factory in tuple(_READER_FACTORIES):
        try:
            reader = factory()
            if reader is not None and set(REQUIRED_CAPABILITIES).issubset(reader.capabilities()): return reader
        except Exception: continue
    return None

class WeakToStrongEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS_PER_REQUEST)
    signal_date: date
    oos_start: date | None = None
    cost_bps: float = Field(default=DEFAULT_RESEARCH_COST_BPS, ge=0, le=500)
    @field_validator("symbols")
    @classmethod
    def _canonicalize_symbols(cls, value: list[str]) -> list[str]:
        canonical = [canonicalize_symbol(v) for v in value]
        if len(set(canonical)) != len(canonical): raise ValueError("duplicate canonical symbol")
        return canonical
    @field_validator("signal_date")
    @classmethod
    def _reject_future_date(cls, value: date) -> date:
        if value > date.today(): raise ValueError(f"signal_date {value.isoformat()} is in the future")
        return value

class ManifestStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["available", "unavailable"]
    missing_capabilities: list[str]
    generation: str | None = None
    sha256: str | None = None
class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["auction_open", "first_touch", "board_break", "reseal", "day_close"]
    time: datetime
class ForwardDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partition: Partition
    status: Literal["available", "censored"]
    next_trade_date: date | None = None
    gross_bps: float | None = None
    cost_bps: float
    net_bps: float | None = None
class WeakToStrongSymbolEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    status: Literal["available", "unavailable", "censored"]
    status_reason: Literal["reader_missing", "evaluated", "downgraded_bar_touched", "suspended", "pit_incomplete", "insufficient_evidence", "data_reader_error"]
    missing_capabilities: list[str] = Field(default_factory=list)
    core_status: Literal["unavailable", "bar_touched", "sealed_limit"]
    reachability_status: Literal["unavailable", "bar_touched", "verified"]
    event_label: EventLabel | None = None
    oos_partition: Partition
    censoring: list[str]
    timeline: list[TimelineEvent] = Field(default_factory=list)
    evidence: dict[str, EvidenceValue] = Field(default_factory=dict)
    forward: ForwardDiagnostic
    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str: return canonicalize_symbol(value)
    @field_validator("evidence")
    @classmethod
    def _safe_keys(cls, value: dict[str, EvidenceValue]) -> dict[str, EvidenceValue]:
        validate_evidence_keys(value.keys()); return value
class PartitionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partition: Partition
    symbols: int
    forward_available: int
    forward_censored: int
    mean_forward_return_gross_bps: float | None = None
    mean_forward_return_net_bps: float | None = None
class WeakToStrongRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_symbols: int
    cost_bps: float
    oos_start: date | None
    by_status: dict[str, int]
    by_event_label: dict[str, int]
    censoring_counts: dict[str, int]
    is_forward: PartitionSummary
    oos_forward: PartitionSummary
    unspecified_forward: PartitionSummary
class WeakToStrongEvaluateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_id: str
    schema_version: int
    signal_date: date
    observed_at: datetime
    manifest: ManifestStatus
    evaluations: list[WeakToStrongSymbolEvaluation]
    summary: WeakToStrongRunSummary
    disclaimer: str

def _price(value: float) -> float: return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def _limit(prev: float, pct: float, up: bool = True) -> float: return _price(prev * (1 + pct if up else 1 - pct))
def _partition(day: date, boundary: date | None) -> Partition: return "unspecified" if boundary is None else ("oos" if day >= boundary else "is")
def _pit_ok(pit: PITRecord | None, cutoff: datetime) -> bool: return bool(pit and pit.get("limit_up_pct") is not None and pit.get("is_st") is not None and pit["effective_at"] <= cutoff and pit["available_at"] <= cutoff)

def _forward(bars: Sequence[DailyBar], signal_date: date, partition: Partition, cost: float, censoring: list[str]) -> ForwardDiagnostic:
    current = next((b for b in bars if b["trade_date"] == signal_date), None); nxt = next((b for b in bars if b["trade_date"] > signal_date), None)
    if current is None or nxt is None or current["close"] <= 0:
        if "forward_insufficient" not in censoring: censoring.append("forward_insufficient")
        return ForwardDiagnostic(partition=partition, status="censored", cost_bps=cost)
    gross = round((nxt["close"] / current["close"] - 1) * 10000, 4)
    return ForwardDiagnostic(partition=partition, status="available", next_trade_date=nxt["trade_date"], gross_bps=gross, cost_bps=cost, net_bps=round(gross - cost, 4))

def _empty(symbol: str, partition: Partition, cost: float, reason: Literal["reader_missing", "evaluated", "downgraded_bar_touched", "suspended", "pit_incomplete", "insufficient_evidence", "data_reader_error"], censoring: list[str]) -> WeakToStrongSymbolEvaluation:
    return WeakToStrongSymbolEvaluation(symbol=symbol, status="unavailable", status_reason=reason, core_status="unavailable", reachability_status="unavailable", oos_partition=partition, censoring=censoring, forward=ForwardDiagnostic(partition=partition, status="censored", cost_bps=cost))

def _finalize(symbol: str, partition: Partition, cost: float, status: Literal["available", "unavailable", "censored"], reason: Literal["reader_missing", "evaluated", "downgraded_bar_touched", "suspended", "pit_incomplete", "insufficient_evidence", "data_reader_error"], core: Literal["unavailable", "bar_touched", "sealed_limit"], reachability: Literal["unavailable", "bar_touched", "verified"], label: EventLabel | None, censoring: list[str], timeline: list[TimelineEvent], evidence: dict[str, EvidenceValue], forward: ForwardDiagnostic) -> WeakToStrongSymbolEvaluation:
    return WeakToStrongSymbolEvaluation(symbol=symbol, status=status, status_reason=reason, core_status=core, reachability_status=reachability, event_label=label, oos_partition=partition, censoring=censoring, timeline=timeline, evidence=evidence, forward=forward)

def _evaluate_symbol(reader: WeakToStrongReader, symbol: str, signal_date: date, cost: float, boundary: date | None) -> WeakToStrongSymbolEvaluation:
    partition = _partition(signal_date, boundary); start, end = signal_date - timedelta(days=45), signal_date + timedelta(days=15)
    try:
        bars = sorted(reader.daily_bars(symbol, start, end), key=lambda b: b["trade_date"]); suspended = set(reader.suspended_dates(symbol, start, end))
        if signal_date in suspended: return WeakToStrongSymbolEvaluation(symbol=symbol, status="censored", status_reason="suspended", core_status="unavailable", reachability_status="unavailable", oos_partition=partition, censoring=["suspended"], forward=ForwardDiagnostic(partition=partition, status="censored", cost_bps=cost))
        by_day = {b["trade_date"]: b for b in bars}; event_bar = by_day.get(signal_date)
        if event_bar is None: return _empty(symbol, partition, cost, "insufficient_evidence", ["missing_event_day_bars"])
        prior = [b for b in bars if b["trade_date"] < signal_date]
        if len(prior) < 2: return _empty(symbol, partition, cost, "insufficient_evidence", ["missing_prior_day_bars"])
        prior_bar, prior_prev = prior[-1], prior[-2]; pit = reader.pit_snapshot(symbol, signal_date); prior_pit = reader.pit_snapshot(symbol, prior_bar["trade_date"])
        if not _pit_ok(pit, datetime.combine(signal_date, time(23, 59, 59))) or not _pit_ok(prior_pit, datetime.combine(prior_bar["trade_date"], time(23, 59, 59))): return _empty(symbol, partition, cost, "pit_incomplete", ["missing_pit_regime", "missing_pit_st"])
        minutes = sorted((m for m in reader.minute_bars(symbol, signal_date) if m["timestamp"].date() == signal_date), key=lambda m: m["timestamp"])
        if not minutes: return _empty(symbol, partition, cost, "insufficient_evidence", ["missing_timestamped_minute"])
        auction = reader.auction_snapshot(symbol, signal_date); ticks = sorted((t for t in reader.ticks(symbol, signal_date) if t["timestamp"].date() == signal_date), key=lambda t: (t["timestamp"], t["seq"])); books = sorted((b for b in reader.order_book_snapshots(symbol, signal_date) if b["timestamp"].date() == signal_date), key=lambda b: b["timestamp"])
    except Exception: return _empty(symbol, partition, cost, "data_reader_error", ["data_reader_error"])
    censoring: list[str] = []
    if pit.get("float_shares") is None: censoring.append("missing_pit_float_shares")
    prior_limit = _limit(prior_prev["close"], float(prior_pit["limit_up_pct"])); limit_up = _limit(prior_bar["close"], float(pit["limit_up_pct"])); limit_down = _limit(prior_bar["close"], float(pit.get("limit_down_pct") or pit["limit_up_pct"]), False)
    baseline = prior[:-1][-VOLUME_BASELINE_DAYS:]; avg_volume = sum(b["volume"] for b in baseline) / len(baseline) if baseline else None; ratio = round(prior_bar["volume"] / avg_volume, 4) if avg_volume else None
    evidence: dict[str, EvidenceValue] = {"pit_is_st": bool(pit["is_st"]), "pit_float_shares": pit.get("float_shares"), "prior_trade_date": prior_bar["trade_date"].isoformat(), "prior_close": prior_bar["close"], "prior_limit_up_price": prior_limit, "prior_day_limit_up": prior_bar["close"] >= prior_limit - 1e-6, "prior_day_volume": prior_bar["volume"], "prior_day_volume_baseline_avg": avg_volume, "prior_day_volume_ratio": ratio, "prior_day_volume_surge": ratio >= VOLUME_SURGE_RATIO if ratio is not None else None, "open_price": event_bar["open"], "gap_bps": round((event_bar["open"] / prior_bar["close"] - 1) * 10000, 4), "limit_up_price": limit_up, "limit_down_price": limit_down, "minute_count": len(minutes), "auction_open_price": auction["open_price"] if auction else None, "auction_matched_volume": auction["matched_volume"] if auction else None}
    timeline: list[TimelineEvent] = []; day_close = datetime.combine(signal_date, time(15, 0))
    if auction is not None: timeline.append(TimelineEvent(event="auction_open", time=datetime.combine(signal_date, time(9, 25))))
    if not evidence["prior_day_limit_up"]: timeline.append(TimelineEvent(event="day_close", time=day_close)); return _finalize(symbol, partition, cost, "available", "evaluated", "bar_touched", "unavailable", "no_prior_day_limit_up", censoring, timeline, evidence, _forward(bars, signal_date, partition, cost, censoring))
    if event_bar["open"] <= prior_bar["close"] + 1e-6: timeline.append(TimelineEvent(event="day_close", time=day_close)); return _finalize(symbol, partition, cost, "available", "evaluated", "bar_touched", "unavailable", "no_gap_up", censoring, timeline, evidence, _forward(bars, signal_date, partition, cost, censoring))
    if event_bar["high"] < limit_up - 1e-6: timeline.append(TimelineEvent(event="day_close", time=day_close)); return _finalize(symbol, partition, cost, "available", "evaluated", "bar_touched", "unavailable", "gap_up_no_touch", censoring, timeline, evidence, _forward(bars, signal_date, partition, cost, censoring))
    first_idx = next((i for i, t in enumerate(ticks) if t["price"] >= limit_up - 1e-6), None)
    if first_idx is None:
        censoring.append("missing_tick_data"); timeline.append(TimelineEvent(event="day_close", time=day_close)); return _finalize(symbol, partition, cost, "censored", "downgraded_bar_touched", "bar_touched", "unavailable", "bar_touched", censoring, timeline, evidence, _forward(bars, signal_date, partition, cost, censoring))
    first = ticks[first_idx]; timeline.append(TimelineEvent(event="first_touch", time=first["timestamp"])); break_tick = next((t for t in ticks[first_idx + 1:] if t["price"] < limit_up - .005), None); reseal_tick = next((t for t in ticks[ticks.index(break_tick) + 1:] if t["price"] >= limit_up - 1e-6), None) if break_tick else None
    if break_tick: timeline.append(TimelineEvent(event="board_break", time=break_tick["timestamp"]))
    if reseal_tick: timeline.append(TimelineEvent(event="reseal", time=reseal_tick["timestamp"]))
    evidence.update({"first_touch_time": first["timestamp"].strftime("%H:%M:%S"), "board_break_time": break_tick["timestamp"].strftime("%H:%M:%S") if break_tick else None, "reseal_time": reseal_tick["timestamp"].strftime("%H:%M:%S") if reseal_tick else None, "board_break_count": sum(1 for a, b in zip(ticks[first_idx:], ticks[first_idx + 1:]) if a["price"] >= limit_up - 1e-6 and b["price"] < limit_up - 1e-6), "closed_at_limit": abs(event_bar["close"] - limit_up) <= 1e-6, "tick_prints_at_limit": sum(t["price"] >= limit_up - 1e-6 for t in ticks), "tick_prints_below_limit": sum(t["price"] < limit_up - 1e-6 for t in ticks[first_idx:])})
    touch_books = [b for b in books if first["timestamp"] <= b["timestamp"] <= first["timestamp"] + timedelta(minutes=5)]; seal = any(b.get("bid1_price") is not None and b["bid1_price"] >= limit_up - 1e-6 and b.get("bid1_volume", 0) > 0 and (b.get("ask1_price") is None or b["ask1_price"] > limit_up + 1e-6) for b in books if first["timestamp"] <= b["timestamp"] <= day_close); queue = next((b["bid1_volume"] for b in touch_books if b.get("bid1_price") is not None and b["bid1_price"] >= limit_up - 1e-6), None)
    evidence.update({"book_observed_at_touch": bool(touch_books), "queue_volume_at_limit": queue}); reachability = "verified" if queue is not None else ("bar_touched" if books else "unavailable"); one_word = all(abs(event_bar[k] - limit_up) <= 1e-6 for k in ("open", "high", "low", "close")) and all(m["low"] >= limit_up - 1e-6 for m in minutes); auction_at_limit = bool(auction and auction["open_price"] >= limit_up - 1e-6)
    if one_word and not auction_at_limit: censoring.append("censored_preopen"); label, status, reason, core = "bar_touched", "censored", "downgraded_bar_touched", "bar_touched"
    elif one_word and seal: label, status, reason, core = "one_word_limit", "available", "evaluated", "sealed_limit"
    elif abs(event_bar["close"] - limit_up) <= 1e-6 and seal: label, status, reason, core = ("broken_resealed" if break_tick else "sealed_limit"), "available", "evaluated", "sealed_limit"
    elif abs(event_bar["close"] - limit_up) <= 1e-6: label, status, reason, core = "bar_touched", "censored", "downgraded_bar_touched", "bar_touched"; censoring.append("missing_order_book_evidence")
    else: label, status, reason, core = "broken_not_resealed", "available", "evaluated", "bar_touched"
    timeline.append(TimelineEvent(event="day_close", time=day_close)); return _finalize(symbol, partition, cost, status, reason, core, reachability, label, censoring, timeline, evidence, _forward(bars, signal_date, partition, cost, censoring))

def _partition_summary(evals: Sequence[WeakToStrongSymbolEvaluation], part: Partition) -> PartitionSummary:
    rows = [e.forward for e in evals if e.oos_partition == part]; good = [r for r in rows if r.status == "available"]; gross = [r.gross_bps for r in good if r.gross_bps is not None]; net = [r.net_bps for r in good if r.net_bps is not None]
    return PartitionSummary(partition=part, symbols=len(rows), forward_available=len(good), forward_censored=len(rows)-len(good), mean_forward_return_gross_bps=round(sum(gross)/len(gross), 4) if gross else None, mean_forward_return_net_bps=round(sum(net)/len(net), 4) if net else None)

def _summary(evals: Sequence[WeakToStrongSymbolEvaluation], cost: float, boundary: date | None) -> WeakToStrongRunSummary:
    statuses: dict[str,int] = {}; labels: dict[str,int] = {}; cens: dict[str,int] = {}
    for e in evals:
        statuses[e.status] = statuses.get(e.status, 0)+1; key = e.event_label or "none"; labels[key] = labels.get(key, 0)+1
        for c in e.censoring: cens[c] = cens.get(c, 0)+1
    return WeakToStrongRunSummary(total_symbols=len(evals), cost_bps=cost, oos_start=boundary, by_status=dict(sorted(statuses.items())), by_event_label=dict(sorted(labels.items())), censoring_counts=dict(sorted(cens.items())), is_forward=_partition_summary(evals, "is"), oos_forward=_partition_summary(evals, "oos"), unspecified_forward=_partition_summary(evals, "unspecified"))

def evaluate_weak_to_strong_v1(request: WeakToStrongEvaluateRequest) -> WeakToStrongEvaluateResponse:
    reader = resolve_weak_to_strong_reader(); part = _partition(request.signal_date, request.oos_start)
    if reader is None:
        missing = list(REQUIRED_CAPABILITIES); evaluations = [_empty(s, part, request.cost_bps, "reader_missing", [_CENSORING_BY_CAPABILITY[c] for c in missing]) for s in request.symbols]; manifest = ManifestStatus(status="unavailable", missing_capabilities=missing)
    else:
        try:
            info = reader.run_manifest(); manifest = ManifestStatus(status="available", missing_capabilities=[], generation=info["generation"], sha256=info["sha256"])
        except Exception:
            evaluations = [_empty(s, part, request.cost_bps, "insufficient_evidence", ["missing_run_manifest"]) for s in request.symbols]; manifest = ManifestStatus(status="unavailable", missing_capabilities=["immutable_run_manifest"])
        else: evaluations = [_evaluate_symbol(reader, s, request.signal_date, request.cost_bps, request.oos_start) for s in request.symbols]
    return WeakToStrongEvaluateResponse(protocol_id=WEAK_TO_STRONG_PROTOCOL_ID, schema_version=WEAK_TO_STRONG_SCHEMA_VERSION, signal_date=request.signal_date, observed_at=datetime.now(UTC), manifest=manifest, evaluations=evaluations, summary=_summary(evaluations, request.cost_bps, request.oos_start), disclaimer=WEAK_TO_STRONG_DISCLAIMER)
