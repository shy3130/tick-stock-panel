from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.strategy import AIIterateRequest, BuildRequest, build_strategy_stream
from app.custom.assistant import tools as assistant_tools
from app.services import tool_catalog
from app.services.screener import ScreenerService
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.ai_iterator import AIStrategyIterator
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.prompt_builder import build_step1, build_step2

MINUTE_CODE = '''import polars as pl
META = {"id": "custom_minute", "name": "分钟策略", "asset_types": ["stock"],
        "timeframes": ["1m"], "basic_filter": {"enabled": False}, "scoring": {}}
EXECUTION_BACKEND = "minute_filter"
def filter_minute_history(df, params):
    return df.group_by("symbol").agg(close=pl.col("close").last(),
                                   last_datetime=pl.col("datetime").max())
'''


@pytest.fixture
def context(tmp_path):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "custom_minute.py").write_text(MINUTE_CODE, encoding="utf-8")
    engine = StrategyEngine([custom, tmp_path / "strategies" / "ai"])
    return assistant_tools.ToolContext(data_dir=tmp_path, engine=engine, repo=SimpleNamespace())


def test_generator_accepts_minute_entrypoint_and_output_scoring():
    result = AIStrategyGenerator().validate_code(MINUTE_CODE)
    assert result["valid"], result["error"]
    code = MINUTE_CODE.replace('"scoring": {}', '"scoring": {"minute_strength": 1.0}')
    assert AIStrategyGenerator().validate_code(code)["valid"]


@pytest.mark.parametrize(
    "code", [MINUTE_CODE.replace('["1m"]', '["1d"]'),
             MINUTE_CODE.replace("def filter_minute_history", "def filter_history")],
)
def test_generator_rejects_wrong_minute_contract(code):
    assert not AIStrategyGenerator().validate_code(code)["valid"]


def test_minute_request_and_prompts_keep_the_contract(context):
    assert BuildRequest(step=1, execution_backend="minute_filter").execution_backend == "minute_filter"
    assert AIIterateRequest(execution_backend="minute_filter").execution_backend == "minute_filter"
    guide = assistant_tools._create_strategy({"show_guide": True}, context)["guide"]
    assert "filter_minute_history" in guide and '"1m"' in guide
    prompt = build_step1("分钟", "分钟条件", "long", "一分钟上涨", execution_backend="minute_filter")
    assert "filter_minute_history" in prompt
    assert "filter_minute_history" in build_step2(MINUTE_CODE, "调整阈值")


@pytest.mark.asyncio
async def test_minute_stream_generation_saves_and_reloads(monkeypatch, context):
    async def stream(self, prompt):
        yield MINUTE_CODE

    monkeypatch.setattr(AIStrategyGenerator, "stream", stream)
    response = await build_strategy_stream(
        BuildRequest(step=1, strategy_id="ai_minute", execution_backend="minute_filter"),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            repo=SimpleNamespace(store=SimpleNamespace(data_dir=context.data_dir)),
        ))),
    )
    events = []
    async for chunk in response.body_iterator:
        events.extend(json.loads(line) for line in chunk.splitlines() if line.strip())
    result = events[-1]
    assert result["type"] == "result" and result["valid"] is True
    saved = assistant_tools._create_strategy({"name": "分钟策略", "code": result["code"]}, context)
    assert saved["created"] is True
    assert context.engine.get(saved["strategy_id"]).execution_backend == "minute_filter"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    MINUTE_CODE,
    MINUTE_CODE.replace('EXECUTION_BACKEND = "minute_filter"\n', '').replace(
        '"timeframes":', '"execution_backend": "minute_filter", "timeframes":',
    ),
])
async def test_structural_repair_keeps_minute_backend(monkeypatch, code):
    async def repair(self, prompt, guide):
        assert "filter_minute_history" in prompt
        assert "minute_filter" in prompt
        return MINUTE_CODE

    monkeypatch.setattr(AIStrategyGenerator, "_call_llm", repair)
    broken = code.replace("def filter_minute_history", "def missing_entry")
    generator = AIStrategyGenerator()
    validation = generator.validate_code(broken)
    assert generator.needs_structural_repair(validation)
    assert (await generator.repair_code(broken, validation["error"]))["valid"]


def test_assistant_executes_latest_minute_partition(monkeypatch, context):
    day = date(2026, 10, 20)
    minute_day = date(2026, 10, 21)
    context.repo.latest_minute_date_global = lambda: minute_day
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: day)
    monkeypatch.setattr(ScreenerService, "_assert_integrity", lambda *a, **kw: None)
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", lambda self, d: pl.DataFrame({
        "symbol": ["600001.SH"], "date": [d], "name": ["测试股"],
    }))
    seen = []

    def history(self, as_of, current):
        seen.append(as_of)
        return pl.DataFrame({"symbol": ["600001.SH"], "close": [10.2],
                             "datetime": [datetime(2026, 10, 21, 10, 0)]})

    monkeypatch.setattr(ScreenerService, "_load_minute_history", history)
    result = assistant_tools._run_strategy({"strategy_id": "custom_minute"}, context)
    assert result["total"] == 1
    assert result["timeframe"] == "1m" and result["as_of"] == str(minute_day)
    assert seen == [minute_day]


def test_assistant_can_create_existing_intraday_signal(context):
    result = assistant_tools._create_signal_strategy({
        "name": "分时上穿均价", "kind": "entry", "timeframe": "intraday", "min_bars": 2,
        "conditions": [{"left": "price", "op": "cross_up", "right": "field:vwap"}],
    }, context)
    assert result["signal"]["timeframe"] == "intraday"
    assert result["signal"]["min_bars"] == 2


@pytest.mark.parametrize("usable", [True, False])
def test_runtime_capabilities_are_visible_to_ai(monkeypatch, usable):
    from app.data_providers import capabilities
    from app.tickflow import policy

    monkeypatch.setattr(policy, "base_tier_name", lambda: "none")
    monkeypatch.setattr(capabilities, "resolve_capability_sources", lambda tier: [
        {"id": "minute", "source": "demo", "usable": usable},
    ])
    rows = tool_catalog.list_data_capabilities()["capabilities"]
    minute = next(row for row in rows if row["id"] == "minute")
    assert minute["source"] == "demo" and minute["usable"] is usable


@pytest.mark.parametrize("bad_code", [
    MINUTE_CODE.replace('["1m"]', '["1d"]'),
    MINUTE_CODE.replace('"minute_filter"', '"polars_expr"').replace(
        "def filter_minute_history", "def filter",
    ),
])
def test_requested_minute_contract_is_repairable(bad_code):
    generator = AIStrategyGenerator(execution_backend="minute_filter")
    validation = generator.validate_code(bad_code)
    assert not validation["valid"]
    assert generator.needs_structural_repair(validation)


@pytest.mark.parametrize("args", [{"timeframe": "1d"}, {"asset_type": "etf"}])
def test_assistant_rejects_unsupported_target_before_reading_data(context, args):
    with pytest.raises(ValueError, match="策略不支持"):
        assistant_tools._run_strategy({"strategy_id": "custom_minute", **args}, context)


def test_assistant_reports_missing_minute_data(monkeypatch, context):
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: date(2026, 10, 21))
    context.repo.latest_minute_date_global = lambda: None
    with pytest.raises(ValueError, match="本地无分钟K"):
        assistant_tools._run_strategy({"strategy_id": "custom_minute"}, context)


def _etf_strategy(context):
    code = '''import polars as pl
META = {"id": "custom_etf", "name": "ETF", "asset_types": ["etf"],
        "timeframes": ["1d"], "basic_filter": {"enabled": False}, "scoring": {}}
def filter(df, params):
    return pl.col("close") > params.get("threshold", 10)
'''
    path = context.data_dir / "strategies" / "custom" / "custom_etf.py"
    path.write_text(code, encoding="utf-8")
    context.engine.reload()
    return context.engine.get("custom_etf")


def test_assistant_selects_etf_and_applies_run_params(monkeypatch, context):
    _etf_strategy(context)
    seen = []
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: date(2026, 10, 21))

    def load(self, as_of):
        seen.append(self.asset_type)
        return pl.DataFrame({"symbol": ["510300.SH"], "close": [3.5], "name": ["ETF"]})

    monkeypatch.setattr(ScreenerService, "_assert_integrity", lambda *a, **kw: None)
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", load)
    result = assistant_tools._run_strategy({
        "strategy_id": "custom_etf", "params": {"threshold": 3},
    }, context)
    assert result["asset_type"] == "etf" and result["timeframe"] == "1d"
    assert result["total"] == 1 and result["rows"][0]["symbol"] == "510300.SH"
    assert seen == ["etf"]
    assert not (context.data_dir / "strategies" / "overrides").exists()


@pytest.mark.asyncio
async def test_backtest_and_final_iteration_select_etf(monkeypatch, context):
    _etf_strategy(context)
    seen = []

    def run(data_dir, *, asset_type, **kwargs):
        seen.append(asset_type)
        return {"stats": {"n_trades": 2}}

    monkeypatch.setattr(tool_catalog, "run_backtest", run)
    payload = await assistant_tools.execute_assistant_tool(
        "run_backtest", {"strategy_id": "custom_etf"}, context,
    )
    assert payload["ok"], payload
    final = await AIStrategyIterator()._run_backtest(
        str(context.data_dir), "custom_etf", engine=context.engine, repo=context.repo,
    )
    assert final["ok"] and final["result"]["stats"] == {"n_trades": 2}
    assert seen == ["etf", "etf"]


@pytest.mark.asyncio
@pytest.mark.parametrize("earliest", [None, date(2026, 10, 21)])
async def test_minute_backtest_rejects_missing_coverage_before_worker(monkeypatch, context, earliest):
    context.repo.earliest_minute_date = lambda: earliest
    worker = []
    monkeypatch.setattr("app.backtest.worker.make_worker_task", lambda *a: worker.append(a))
    result = await assistant_tools.execute_assistant_tool("run_backtest", {
        "strategy_id": "custom_minute", "start": "2026-10-20", "end": "2026-10-21",
    }, context)
    assert not result["ok"] and "无法覆盖回测起始日" in result["error"]
    assert not worker


@pytest.mark.asyncio
async def test_minute_backtest_preserves_params_and_worker_contract(monkeypatch, context):
    context.repo.earliest_minute_date = lambda: date(2026, 10, 20)
    seen = []

    def task(kind, data_dir, cfg):
        seen.append((kind, cfg))
        return cfg

    monkeypatch.setattr("app.backtest.worker.make_worker_task", task)
    monkeypatch.setattr("app.backtest.worker.run_worker_task", lambda task: {"stats": {"n_trades": 3}})
    result = await assistant_tools.execute_assistant_tool("run_backtest", {
        "strategy_id": "custom_minute", "start": "2026-10-20", "end": "2026-10-21",
        "params": {"volume_window": 20},
    }, context)
    assert result["ok"], result
    kind, cfg = seen[0]
    assert kind == "backtest" and cfg.strategy_id == "custom_minute"
    assert cfg.asset_type == "stock" and cfg.params == {"volume_window": 20}
    assert cfg.start == date(2026, 10, 20) and cfg.end == date(2026, 10, 21)
    assert result["result"]["stats"]["n_trades"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("step", [1, 2])
async def test_stream_repairs_wrong_backend_and_preserves_old_minute_edits(monkeypatch, step, context):
    calls = []

    async def stream(self, prompt):
        yield MINUTE_CODE.replace('"minute_filter"', '"polars_expr"').replace(
            "def filter_minute_history", "def filter",
        )

    async def repair(self, prompt, guide):
        calls.append(prompt)
        return MINUTE_CODE

    monkeypatch.setattr(AIStrategyGenerator, "stream", stream)
    monkeypatch.setattr(AIStrategyGenerator, "_call_llm", repair)
    kwargs = {"execution_backend": "minute_filter"} if step == 1 else {"current_code": MINUTE_CODE}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=context.data_dir)),
    )))
    response = await build_strategy_stream(BuildRequest(step=step, **kwargs), request)
    events = [json.loads(line) async for line in response.body_iterator]
    assert len(calls) == 1 and 'timeframes=["1m"]' in calls[0]
    assert events[-1]["valid"] and '"minute_filter"' in events[-1]["code"]


def test_minute_custom_output_scoring_runs_with_real_engine(context):
    code = MINUTE_CODE.replace('"scoring": {}', '"scoring": {"minute_strength": 1.0}').replace(
        'last_datetime=pl.col("datetime").max()',
        'last_datetime=pl.col("datetime").max(), minute_strength=pl.col("close").last()',
    )
    (context.data_dir / "strategies" / "custom" / "custom_minute.py").write_text(code, encoding="utf-8")
    context.engine.reload()
    frame = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "close": [10., 20.],
                          "datetime": [datetime(2026, 10, 21, 10)] * 2})
    result = context.engine.run("custom_minute", StrategyDataContext(
        asset_type="stock", timeframe="1m", as_of=date(2026, 10, 21), history=frame,
    ))
    assert result.total == 2
    assert [row["symbol"] for row in result.rows] == ["600002.SH", "600001.SH"]
    assert result.rows[0]["score"] > result.rows[1]["score"]


def test_api_and_ai_share_minute_backtest_coverage_error(context):
    from fastapi import HTTPException

    from app.api.backtest import _guard_minute_strategy_backtest

    context.repo.earliest_minute_date = lambda: date(2026, 10, 21)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=context.engine, repo=context.repo,
    )))
    with pytest.raises(HTTPException) as error:
        _guard_minute_strategy_backtest(request, "custom_minute", date(2026, 10, 20), "stock")
    assert error.value.status_code == 400
    assert "无法覆盖回测起始日" in error.value.detail
