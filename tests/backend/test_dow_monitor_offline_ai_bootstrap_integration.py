from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.dow_monitor_half_hour_ai_calendar import HalfHourWindowCalendar
from app.services.dow_monitor_half_hour_ai_models import analysis_id_for
from app.services.dow_monitor_half_hour_ai_prompt import HalfHourAiPromptService
from app.services.dow_monitor_half_hour_ai_repository import (
    DowMonitorHalfHourAiRepository,
)
from app.services.dow_monitor_half_hour_ai_snapshot import HalfHourAiSnapshotBuilder
from app.services.dow_monitor_minute_result_history import (
    DowEngineStableStateBuilder,
    DowMonitorMinuteResultHistoryBuilder,
)
from app.services.dow_monitor_minute_result_materializer import (
    DowMonitorMinuteResultMaterializer,
)
from app.services.dow_monitor_minute_result_models import DowMonitorMinuteResult
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)
from app.services.dow_monitor_minute_result_source import DowMonitorMinuteResultSource
from app.services.dow_monitor_models import MonitoredSymbol
from app.services.dow_monitor_offline_bootstrap import DowMonitorOfflineBootstrap
from app.services.dow_monitor_store import DowMonitorStore
from app.workers.dow_monitor_half_hour_ai import DowMonitorHalfHourAiWorker


BEIJING = ZoneInfo("Asia/Shanghai")
SESSION_OPEN = datetime(2026, 7, 31, 21, 30, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 7, 31, 22, 0, tzinfo=BEIJING)
CREATED_AT = datetime(2026, 7, 31, 22, 17, tzinfo=BEIJING)
POLL_TIME = datetime(2026, 7, 31, 22, 17, 5, tzinfo=BEIJING)
TRADE_DATE = date(2026, 7, 31)
SYMBOL = "RNG.US"


def _aware_beijing(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return (
        parsed.replace(tzinfo=BEIJING)
        if parsed.tzinfo is None or parsed.utcoffset() is None
        else parsed.astimezone(BEIJING)
    )


def _raw_offline_rows(observation_count: int) -> dict[str, list[dict]]:
    quotes: list[dict] = []
    depth: list[dict] = []
    trades: list[dict] = []
    candles: list[dict] = []
    capital: list[dict] = []
    minute_bars: list[dict] = []

    for offset in range(observation_count):
        bar_time = SESSION_OPEN + timedelta(minutes=offset)
        observed_at = bar_time + timedelta(minutes=1, seconds=-1)
        open_price = 50.0 + offset * 0.1
        close_price = open_price + 0.05
        volume = 1_000.0 + offset * 10
        minute_bars.append(
            {
                "symbol": SYMBOL,
                "market": "us",
                "period": "min_1",
                "bar_time": bar_time.isoformat(),
                "open": open_price,
                "high": close_price + 0.1,
                "low": open_price - 0.1,
                "close": close_price,
                "volume": volume,
                "turnover": close_price * volume,
                "updated_at": observed_at.isoformat(),
            }
        )
        quotes.append(
            {
                "symbol": SYMBOL,
                "market": "us",
                "snapshot_time": observed_at.isoformat(),
                "last_done": close_price,
                "prev_close": 49.5,
                "high": close_price + 0.2,
                "low": 49.8,
                "updated_at": observed_at.isoformat(),
            }
        )
        depth.append(
            {
                "symbol": SYMBOL,
                "market": "us",
                "snapshot_time": observed_at.isoformat(),
                "bid_volume": 600,
                "ask_volume": 400,
                "payload": json.dumps(
                    {
                        "bids": [
                            {"volume": value} for value in (150, 140, 130, 100, 80)
                        ],
                        "asks": [{"volume": value} for value in (110, 90, 80, 70, 50)],
                    }
                ),
                "updated_at": observed_at.isoformat(),
            }
        )
        trades.append(
            {
                "symbol": SYMBOL,
                "market": "us",
                "trade_time": (bar_time + timedelta(seconds=30)).isoformat(),
                "price": close_price,
                "volume": 100,
                "direction": "BUY",
                "updated_at": observed_at.isoformat(),
            }
        )
        capital.append(
            {
                "symbol": SYMBOL,
                "market": "us",
                "snapshot_time": observed_at.isoformat(),
                "total_in": 600_000 + offset * 1_000,
                "total_out": 400_000 + offset * 500,
                "updated_at": observed_at.isoformat(),
            }
        )

    candles.extend(minute_bars)
    for period, width in (("min_5", 5), ("min_15", 15), ("min_30", 30)):
        for offset in range(0, observation_count, width):
            bucket = minute_bars[offset : offset + width]
            if len(bucket) != width:
                continue
            bar_time = SESSION_OPEN + timedelta(minutes=offset)
            candles.append(
                {
                    "symbol": SYMBOL,
                    "market": "us",
                    "period": period,
                    "bar_time": bar_time.isoformat(),
                    "open": bucket[0]["open"],
                    "high": max(row["high"] for row in bucket),
                    "low": min(row["low"] for row in bucket),
                    "close": bucket[-1]["close"],
                    "volume": sum(row["volume"] for row in bucket),
                    "turnover": sum(row["turnover"] for row in bucket),
                    "updated_at": (bar_time + timedelta(minutes=width)).isoformat(),
                }
            )

    return {
        "lb_realtime_quotes": quotes,
        "lb_realtime_depth": depth,
        "lb_realtime_trades": trades,
        "lb_realtime_candlesticks": candles,
        "lb_realtime_capital": capital,
    }


class _MemoryClickHouse:
    """External ClickHouse boundary used by the real source and repositories."""

    def __init__(self, raw_rows: dict[str, list[dict]]) -> None:
        self._raw_rows = raw_rows
        self._canonical_rows: list[DowMonitorMinuteResult] = []
        self._analyses: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.raw_query_tables: list[str] = []
        self.raw_query_sql: list[str] = []

    @property
    def canonical_rows(self) -> tuple[DowMonitorMinuteResult, ...]:
        with self._lock:
            return tuple(self._canonical_rows)

    @property
    def analysis_ids(self) -> set[str]:
        with self._lock:
            return set(self._analyses)

    def query(self, sql: str) -> list[dict]:
        with self._lock:
            for table, rows in self._raw_rows.items():
                if f"longbridge.{table}" in sql:
                    self.raw_query_tables.append(table)
                    self.raw_query_sql.append(sql)
                    return [dict(row) for row in rows]

            if "lb_dow_monitor_minute_results" in sql:
                if "GROUP BY market, symbol" in sql:
                    return [
                        {
                            "market": row.market,
                            "symbol": row.symbol,
                            "decision_minute": row.decision_minute,
                        }
                        for row in self._canonical_rows
                    ]
                return [
                    row.model_dump(mode="python")
                    for row in sorted(
                        self._canonical_rows,
                        key=lambda item: item.decision_minute,
                    )
                ]

            if "lb_dow_monitor_half_hour_ai_analyses" in sql:
                analysis_id = self._quoted_clause(sql, "analysis_id")
                if analysis_id is not None:
                    document = self._analyses.get(analysis_id)
                    return [dict(document)] if document is not None else []
                if "SELECT status" in sql:
                    market = self._quoted_clause(sql, "market")
                    symbol = self._quoted_clause(sql, "symbol")
                    window_match = re.search(
                        r"window_end\s*=\s*parseDateTime64BestEffort\(\s*'([^']+)'",
                        sql,
                    )
                    window_end = window_match.group(1) if window_match else None
                    matches = [
                        document
                        for document in self._analyses.values()
                        if document["market"] == market
                        and document["symbol"] == symbol
                        and document["window_end"] == window_end
                    ]
                    return [{"status": matches[-1]["status"]}] if matches else []
            return []

    def execute(self, sql: str, payload: bytes | None = None) -> bytes:
        with self._lock:
            if "INSERT INTO longbridge.lb_dow_monitor_minute_results" in sql:
                for line in (payload or b"").decode("utf-8").splitlines():
                    self._canonical_rows.append(
                        self._canonical_from_document(json.loads(line))
                    )
            elif "INSERT INTO longbridge.lb_dow_monitor_half_hour_ai_analyses" in sql:
                document = json.loads((payload or b"{}").decode("utf-8"))
                self._analyses[document["analysis_id"]] = document
            return b""

    @staticmethod
    def _quoted_clause(sql: str, field: str) -> str | None:
        match = re.search(rf"\b{field}\s*=\s*'([^']*)'", sql)
        return match.group(1) if match else None

    @staticmethod
    def _canonical_from_document(document: dict) -> DowMonitorMinuteResult:
        values = dict(document)
        for field in (
            "decision_minute",
            "source_bar_time",
            "formal_signal_time",
            "updated_at",
        ):
            values[field] = _aware_beijing(values.get(field))
        values["backfill"] = bool(values["backfill"])
        values["missing_fields"] = tuple(values["missing_fields"])
        values["source_timestamps"] = {
            key: _aware_beijing(timestamp)
            for key, timestamp in json.loads(values["source_timestamps"]).items()
        }
        values["result_payload"] = json.loads(values["result_payload"])
        return DowMonitorMinuteResult.model_validate(values)


class _EngineBar:
    def __init__(self, values: dict) -> None:
        self._values = values

    def model_dump(self, mode: str = "python") -> dict:
        assert mode == "python"
        return dict(self._values)


class _Fake19912Client:
    """External stable-state engine boundary; the production adapter stays real."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def evaluate(self, symbol, timeframe, bars, completion, as_of):
        assert completion == "FINAL"
        assert bars
        assert all(datetime.fromisoformat(row["timestamp"]) <= as_of for row in bars)
        self.calls.append((symbol, timeframe, as_of))
        return SimpleNamespace(
            snapshot=SimpleNamespace(
                bar_completion="FINAL",
                provisional=False,
                price_to_line_pct=0.5,
                line_role="SUPPORT",
                volume_ratio_20=1.2,
            ),
            bars=tuple(_EngineBar(row) for row in bars),
        )


class _FakeModel:
    """External LLM boundary; prompt construction and validation stay real."""

    def __init__(self) -> None:
        self.calls = 0
        self.snapshots: list[dict] = []

    async def __call__(self, messages, **_kwargs) -> str:
        self.calls += 1
        self.snapshots.append(json.loads(messages[-1]["content"]))
        return json.dumps(
            {
                "title": "Offline checkpoint",
                "summary": "Canonical evidence is available through the cutoff.",
                "conclusion": "Observe the bounded checkpoint evidence.",
                "evidence": [
                    {
                        "metric_key": "latest_price",
                        "meaning": "Latest canonical price at the cutoff.",
                    }
                ],
                "risks": ["Offline evidence can still be incomplete."],
                "scenarios": [],
                "data_quality": ["Bounded canonical minute results were used."],
                "report": {
                    "headline": {
                        "title": "离线分钟结构已补足",
                        "trend_bias": "TRANSITION",
                        "opportunity_change": "UNCHANGED",
                        "summary": "阶段路径已恢复，方向仍待下一阶段确认。",
                    },
                    "stage_path": [
                        {
                            "period": "开盘至检查点",
                            "description": "价格沿分钟路径震荡修复。",
                            "metric_keys": ["stage.low", "stage.close"],
                        }
                    ],
                    "hidden_changes": ["离线分钟数据补足了阶段内部路径"],
                    "comparison_with_previous": "首次报告，暂无上一阶段可比。",
                    "day_overview": "当日累计结构仍处于方向确认阶段。",
                    "channel": {
                        "direction": "TRANSITION",
                        "maturity": "FORMING",
                        "explanation": "通道尚未成熟，等待后续高低点确认。",
                        "evidence_metric_keys": ["stage.change_pct"],
                    },
                    "patterns": [
                        {
                            "name": "无成熟形态",
                            "status": "NONE",
                            "explanation": "当前阶段没有可确认的突破或反转。",
                            "evidence_metric_keys": ["stage.change_pct"],
                            "invalidation_metric_keys": [],
                        }
                    ],
                    "volume_capital_interpretation": "量价证据已恢复，但资金推动仍需确认。",
                    "holding_advice": {
                        "state": "HOLD_OBSERVE",
                        "advice": "持仓者继续观察结构确认。",
                        "conditions": ["通道方向明确"],
                    },
                    "watching_advice": {
                        "state": "WAIT_CONFIRMATION",
                        "advice": "未参与者等待突破确认。",
                        "conditions": ["量价同步突破"],
                    },
                    "next_stage_conditions": {
                        "strengthen": ["高点和低点同步抬升"],
                        "risk": ["量价出现背离"],
                        "invalidation": ["跌破阶段低点"],
                    },
                    "confidence": "MEDIUM",
                },
            }
        )


@dataclass
class _System:
    worker: DowMonitorHalfHourAiWorker
    clickhouse: _MemoryClickHouse
    analysis_repository: DowMonitorHalfHourAiRepository
    model: _FakeModel
    engine_client: _Fake19912Client


def _build_system(
    tmp_path,
    *,
    observation_count: int,
) -> _System:
    symbol = MonitoredSymbol(
        symbol=SYMBOL,
        market="us",
        enabled=True,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    symbols_path = tmp_path / "user_data" / "dow_monitor_symbols.json"
    symbols_path.parent.mkdir(parents=True)
    symbols_path.write_text(
        json.dumps([symbol.model_dump(mode="json")]),
        encoding="utf-8",
    )
    store = DowMonitorStore(tmp_path)
    assert store.list_symbols() == [symbol]

    clickhouse = _MemoryClickHouse(_raw_offline_rows(observation_count))
    minute_repository = DowMonitorMinuteResultRepository(
        query_fn=clickhouse.query,
        execute_fn=clickhouse.execute,
    )
    analysis_repository = DowMonitorHalfHourAiRepository(
        query_fn=clickhouse.query,
        execute_fn=clickhouse.execute,
    )
    engine_client = _Fake19912Client()
    materializer = DowMonitorMinuteResultMaterializer(
        source=DowMonitorMinuteResultSource(query_fn=clickhouse.query),
        repository=minute_repository,
        history_builder=DowMonitorMinuteResultHistoryBuilder(
            DowEngineStableStateBuilder(engine_client)
        ),
        notifications_fn=lambda: store.list_notifications(limit=1_000_000),
    )
    model = _FakeModel()
    worker = DowMonitorHalfHourAiWorker(
        monitor_store=store,
        minute_repository=minute_repository,
        analysis_repository=analysis_repository,
        calendar=HalfHourWindowCalendar(),
        snapshot_builder=HalfHourAiSnapshotBuilder(),
        prompt_service=HalfHourAiPromptService(model),
        offline_bootstrap=DowMonitorOfflineBootstrap(materializer),
    )
    return _System(
        worker=worker,
        clickhouse=clickhouse,
        analysis_repository=analysis_repository,
        model=model,
        engine_client=engine_client,
    )


def _assert_canonical_semantics(
    rows: tuple[DowMonitorMinuteResult, ...],
    *,
    expected_count: int,
) -> None:
    assert len(rows) == expected_count
    assert len(rows) <= 500
    assert {(row.market, row.symbol) for row in rows} == {("us", SYMBOL)}
    assert all(SESSION_OPEN < row.decision_minute <= WINDOW_END for row in rows)
    assert max(row.decision_minute for row in rows) <= WINDOW_END
    assert (
        max(timestamp for row in rows for timestamp in row.source_timestamps.values())
        <= WINDOW_END
    )
    assert all(row.backfill for row in rows)


def _analysis_at(
    repository: DowMonitorHalfHourAiRepository,
    window_end: datetime,
):
    return repository.get_by_id(analysis_id_for("us", SYMBOL, TRADE_DATE, window_end))


# Catches a bootstrap that bypasses canonical persistence/reload, widens the
# cutoff, replays an older startup checkpoint, or calls the model more than once.
@pytest.mark.asyncio
async def test_raw_offline_evidence_materializes_canonical_rows_before_ai_result(
    tmp_path,
) -> None:
    system = _build_system(tmp_path, observation_count=30)
    assert system.clickhouse.canonical_rows == ()
    assert system.clickhouse.analysis_ids == set()

    completed_count = await system.worker.run_due_jobs(now=POLL_TIME)

    canonical_rows = system.clickhouse.canonical_rows
    _assert_canonical_semantics(canonical_rows, expected_count=30)
    assert max(row.decision_minute for row in canonical_rows) == WINDOW_END
    assert set(system.clickhouse.raw_query_tables) == {
        "lb_realtime_quotes",
        "lb_realtime_depth",
        "lb_realtime_trades",
        "lb_realtime_candlesticks",
        "lb_realtime_capital",
    }
    assert all(WINDOW_END.isoformat() in sql for sql in system.clickhouse.raw_query_sql)
    assert system.engine_client.calls
    assert all(as_of <= WINDOW_END for _, _, as_of in system.engine_client.calls)

    assert completed_count == 1
    analysis = _analysis_at(system.analysis_repository, WINDOW_END)
    assert analysis is not None
    assert analysis.status == "completed"
    assert analysis.window_end == WINDOW_END
    assert analysis.data_cutoff == WINDOW_END
    assert analysis.input_snapshot["observation_count"] == 30
    assert system.model.calls == 1
    assert system.model.snapshots[0]["data_cutoff"] == WINDOW_END.isoformat()
    assert _analysis_at(system.analysis_repository, SESSION_OPEN) is None


# Catches a worker that treats a successful materializer call as sufficient
# evidence and invokes the LLM even though the real canonical row count is low.
@pytest.mark.asyncio
async def test_insufficient_raw_evidence_persists_status_without_model_call(
    tmp_path,
) -> None:
    system = _build_system(tmp_path, observation_count=2)
    assert system.clickhouse.canonical_rows == ()
    assert system.clickhouse.analysis_ids == set()

    completed_count = await system.worker.run_due_jobs(now=POLL_TIME)

    canonical_rows = system.clickhouse.canonical_rows
    _assert_canonical_semantics(canonical_rows, expected_count=2)

    assert completed_count == 0
    analysis = _analysis_at(system.analysis_repository, WINDOW_END)
    assert analysis is not None
    assert analysis.status == "insufficient_data"
    assert analysis.data_cutoff == WINDOW_END
    assert analysis.input_snapshot["observation_count"] == 2
    assert analysis.error_code == "INSUFFICIENT_DATA"
    assert system.model.calls == 0
    assert _analysis_at(system.analysis_repository, SESSION_OPEN) is None
