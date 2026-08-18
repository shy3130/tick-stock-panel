"""agent_research_tools 定向单元测试。

确定性策略: 不读真实行情/回测数据 — QueryService 与 Strategy/FactorBacktest
Service 全部 monkeypatch 到进程内 fake, 后台 daemon thread 经 fake 瞬时完成,
再通过 get_pool_backtest(wait_seconds) 等待终态。
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.backtest import factor as factor_module
from app.backtest import strategy as strategy_module
from app.services import agent_research_tools as art
from app.services import screener_query as screener_query_module
from app.services.agent_research_tools import (
    MAX_BACKTEST_DAYS,
    PREVIEW_LIMIT,
    get_pool_backtest,
    screen_stock_pool,
    start_pool_backtest,
)

AS_OF = "2026-07-10"
POOL_CONDITIONS = [{"field": "change_pct", "op": ">", "value": 0.05}]


@pytest.fixture(autouse=True)
def _reset_job_table():
    """进程内任务表是模块级单例: 测试前后都清空, 避免跨用例串扰。"""
    art._JOBS.clear()
    yield
    art._JOBS.clear()


class _FakeRepo:
    def __init__(self, data_dir: Path):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.cache_generation = 7
        self.enriched_read_ceiling = date(2026, 7, 9)


@pytest.fixture()
def state(tmp_path: Path):
    return SimpleNamespace(
        repo=_FakeRepo(tmp_path),
        backtest_engine=object(),
        strategy_engine=object(),
    )


class _FakeQueryService:
    """typed 筛选边界替身: 只返回合法 rows/total/as_of, 不触 DuckDB。"""

    def __init__(self, repo):  # noqa: ANN001 — 与真实 QueryService 同签名
        self.repo = repo

    def query(self, req):  # noqa: ANN001
        rows = [
            {"symbol": f"6000{i:02d}.SH", "name": f"股{i}", "close": 10.0 + i, "change_pct": 0.06}
            for i in range(12)
        ]
        return {"rows": rows, "total": 12, "as_of": AS_OF, "applied": [], "elapsed_ms": 1.0}


@pytest.fixture()
def screener_fake(monkeypatch):
    monkeypatch.setattr(screener_query_module, "QueryService", _FakeQueryService)


def _screen_pool(state) -> dict:
    return screen_stock_pool(
        state,
        {"conditions": POOL_CONDITIONS, "as_of": AS_OF, "limit": 500},
    )


def _wait_terminal(state, job_id: str, timeout: float = 10.0) -> dict:
    return get_pool_backtest(state, {"job_id": job_id, "wait_seconds": timeout})


# ── screen: typed 筛选 + 持久化 + 不外泄全量 symbols ──────────────
def test_screen_persists_pool_and_response_hides_symbols(state, screener_fake, monkeypatch):
    resp = _screen_pool(state)

    assert resp["status"] == "success"
    assert resp["count"] == 12 and resp["total"] == 12 and resp["as_of"] == AS_OF
    pool_id = resp["pool_id"]
    assert len(pool_id) == 16 and all(c in "0123456789abcdef" for c in pool_id)
    assert len(resp["preview"]) <= PREVIEW_LIMIT == 10

    # 全量 symbols 只落盘, 不进响应(第 11/12 只只允许出现在 artifact 里)。
    serialized = json.dumps(resp, ensure_ascii=False)
    assert "600010.SH" not in serialized and "600011.SH" not in serialized
    assert "600009.SH" in serialized  # preview 内的第 10 只仍然可见

    artifact = Path(state.repo.store.data_dir) / "user_data" / "agent_pools" / f"{pool_id}.json"
    assert artifact.is_file()
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    assert pool["pool_id"] == pool_id
    assert pool["as_of"] == AS_OF
    assert pool["symbols"] == [f"6000{i:02d}.SH" for i in range(12)]
    assert pool["conditions"] == POOL_CONDITIONS
    assert pool["order_by"] == {"field": "change_pct", "direction": "desc"}
    # 数据水位证据: created_at + repo 水印属性。
    assert pool["data_watermark"]["cache_generation"] == 7
    assert pool["data_watermark"]["enriched_read_ceiling"] == "2026-07-09"
    assert pool["data_watermark"]["created_at"]

    # 内容寻址且不可变：相同规范内容复用既有 artifact，不做覆写。
    monkeypatch.setattr(
        art,
        "_atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pool artifact must not be overwritten")),
    )
    again = _screen_pool(state)
    assert again["pool_id"] == pool_id


def test_screen_rejects_sql_like_and_extra_input(state, screener_fake):
    with pytest.raises(ValueError):  # 未知字段/伪 SQL field 被封闭校验拒绝
        screen_stock_pool(
            state,
            {"conditions": [{"field": "change_pct; DROP TABLE x", "op": ">", "value": 1}]},
        )
    with pytest.raises(ValidationError):  # 顶层多余参数 extra="forbid"
        screen_stock_pool(state, {"conditions": POOL_CONDITIONS, "sql": "1=1"})
    with pytest.raises(ValidationError):  # limit 超上限
        screen_stock_pool(state, {"conditions": POOL_CONDITIONS, "limit": 501})


# ── pool_id 白名单 ───────────────────────────────────────────
@pytest.mark.parametrize("bad", ["../secrets.json", "../../etc/passwd", "ZZZZZZZZZZZZZZZZ", "abc", ""])
def test_malicious_pool_id_rejected(state, bad):
    with pytest.raises(ValueError, match="invalid pool_id"):
        start_pool_backtest(
            state,
            {
                "pool_id": bad,
                "target": "strategy",
                "strategy_id": "s1",
                "start": AS_OF,
                "end": "2026-09-30",
            },
        )


def test_unknown_pool_id_rejected(state):
    with pytest.raises(ValueError, match="unknown pool_id"):
        start_pool_backtest(
            state,
            {
                "pool_id": "0123456789abcdef",
                "target": "factor",
                "factor_name": "momentum_20d",
                "start": AS_OF,
                "end": "2026-09-30",
            },
        )

def test_tampered_pool_checksum_rejected(state, screener_fake):
    pool_id = _screen_pool(state)["pool_id"]
    artifact = Path(state.repo.store.data_dir) / "user_data" / "agent_pools" / f"{pool_id}.json"
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    pool["symbols"] = ["000001.SZ"]
    artifact.write_text(json.dumps(pool), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum"):
        start_pool_backtest(
            state,
            {
                "pool_id": pool_id,
                "target": "strategy",
                "strategy_id": "s1",
                "start": AS_OF,
                "end": "2026-07-11",
            },
        )


# ── 时间约束 ────────────────────────────────────────────────
def test_start_before_pool_as_of_rejected(state, screener_fake):
    pool_id = _screen_pool(state)["pool_id"]
    with pytest.raises(ValueError, match="不得早于"):
        start_pool_backtest(
            state,
            {
                "pool_id": pool_id,
                "target": "strategy",
                "strategy_id": "s1",
                "start": "2026-07-01",
                "end": "2026-09-30",
            },
        )


def test_start_end_ordering_and_max_days_rejected(state, screener_fake):
    pool_id = _screen_pool(state)["pool_id"]
    base = {"pool_id": pool_id, "target": "strategy", "strategy_id": "s1", "start": AS_OF}
    with pytest.raises(ValueError, match="不得早于 start"):
        start_pool_backtest(state, {**base, "end": "2026-07-01"})
    with pytest.raises(ValueError, match="186"):
        start_pool_backtest(state, {**base, "end": "2027-01-12"})  # 187 个自然日

def test_pool_day_close_entry_is_rejected(state, screener_fake):
    pool_id = _screen_pool(state)["pool_id"]
    with pytest.raises(ValueError, match="open_t\\+1"):
        start_pool_backtest(
            state,
            {
                "pool_id": pool_id,
                "target": "strategy",
                "strategy_id": "s1",
                "start": AS_OF,
                "end": "2026-07-11",
                "matching": "close_t",
            },
        )


# ── target 必要字段与互斥参数 ─────────────────────────────────
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"target": "strategy"}, "strategy_id"),  # strategy 缺 strategy_id
        ({"target": "strategy", "strategy_id": "s1", "factor_name": "rsi_14"}, "factor_name"),
        ({"target": "strategy", "strategy_id": "s1", "n_groups": 5}, "n_groups"),
        ({"target": "factor"}, "factor_name"),  # factor 缺 factor_name
        ({"target": "factor", "factor_name": "rsi_14", "strategy_id": "s1"}, "strategy_id"),
        ({"target": "factor", "factor_name": "rsi_14", "max_positions": 5}, "max_positions"),
        ({"target": "factor", "factor_name": "rsi_14", "matching": "close_t"}, "matching"),
    ],
)
def test_target_field_validation(state, extra, message):
    with pytest.raises(ValidationError, match=message):
        start_pool_backtest(state, {"pool_id": "0" * 16, "start": AS_OF, "end": "2026-09-30", **extra})


def test_start_extra_keys_rejected(state, screener_fake):
    pool_id = _screen_pool(state)["pool_id"]
    with pytest.raises(ValidationError):
        start_pool_backtest(
            state,
            {
                "pool_id": pool_id,
                "target": "strategy",
                "strategy_id": "s1",
                "start": AS_OF,
                "end": "2026-09-30",
                "initial_capital": 10_000_000,  # 未开放的安全参数子集之外
            },
        )


# ── 后台任务: fake 回测边界 ──────────────────────────────────
@pytest.fixture()
def fake_strategy_service(monkeypatch):
    seen: list = []

    class _Svc:
        def __init__(self, engine, strategy_engine):
            self.engine = engine
            self.strategy_engine = strategy_engine

        def run(self, config):  # noqa: ANN001
            seen.append(config)
            return SimpleNamespace(
                run_id="runstg0001",
                config={
                    "strategy_id": config.strategy_id,
                    "symbols": list(config.symbols),
                    "start": str(config.start),
                    "end": str(config.end),
                    "max_positions": config.max_positions,
                },
                stats={
                    "total_return": 0.4213,
                    "annual_return": 1.2,
                    "max_drawdown": -0.18,
                    "sharpe": 1.7,
                    "n_trades": 33,
                    "equity_curve": [{"date": "d", "v": 1.0}] * 500,  # 大对象: 不得外泄
                    "trades": [{"x": 1}] * 500,
                },
                strategy_info={"id": "s1", "name": "fake"},
                error=None,
            )

    monkeypatch.setattr(strategy_module, "StrategyBacktestService", _Svc)
    return seen


@pytest.fixture()
def fake_factor_service(monkeypatch):
    seen: list = []

    class _Svc:
        def __init__(self, engine):
            self.engine = engine

        def run(self, config):  # noqa: ANN001
            seen.append(config)
            return SimpleNamespace(
                run_id="runfac0002",
                config={
                    "factor_name": config.factor_name,
                    "symbols": list(config.symbols),
                    "start": str(config.start),
                    "end": str(config.end),
                },
                ic_mean=0.031,
                ic_std=0.11,
                ir=0.28,
                ic_win_rate=0.61,
                group_stats=[{"group": i} for i in range(5)],
                group_nav=[{"date": "d"}] * 300,
                long_short_stats={"total_return": 0.2, "annual_return": 0.4, "sharpe": 0.9},
                n_symbols=12,
                n_dates=120,
                error=None,
            )

    monkeypatch.setattr(factor_module, "FactorBacktestService", _Svc)
    return seen


def test_strategy_job_success_writes_pool_evidence_run_card(
    state, screener_fake, fake_strategy_service
):
    pool_id = _screen_pool(state)["pool_id"]
    started = start_pool_backtest(
        state,
        {
            "pool_id": pool_id,
            "target": "strategy",
            "strategy_id": "s1",
            "start": AS_OF,
            "end": "2026-12-31",  # 174 天 ≤ 186
            "matching": "open_t+1",
            "max_positions": 5,
        },
    )
    job_id = started["job_id"]
    assert started["status"] in {"pending", "running", "success"}
    assert started["pool_id"] == pool_id

    resp = _wait_terminal(state, job_id)
    assert resp["status"] == "success", resp
    assert resp["run_id"] == "runstg0001"

    # 回测 service 收到的是池内全量 symbols 与封闭参数。
    (config,) = fake_strategy_service
    assert config.symbols == [f"6000{i:02d}.SH" for i in range(12)]
    assert config.start == date(2026, 7, 10) and config.end == date(2026, 12, 31)
    assert config.matching == "open_t+1" and config.max_positions == 5

    # 响应只有摘要: 无净值/交易明细大列表、无全量 symbols, 且带研究用途警告。
    assert "equity_curve" not in resp
    assert "trades" not in resp
    assert "600011.SH" not in json.dumps(resp, ensure_ascii=False)
    assert resp["disclaimer"] == art.RESEARCH_ONLY_DISCLAIMER
    assert resp["stats"]["total_return"] == 0.4213
    assert set(resp["stats"]) <= {*art._STRATEGY_STATS_KEYS, "disclaimer"}
    assert resp["run_card_ref"]["run_id"] == "runstg0001"
    assert resp["run_card_ref"]["kind"] == "pool_backtest_strategy"

    # 不可变研究卡: config 携带 pool 证据 + 免责声明, stats 只放摘要。
    card_path = Path(state.repo.store.data_dir) / "research" / "run_cards" / "runstg0001.json"
    assert card_path.is_file()
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["kind"] == "pool_backtest_strategy"
    assert card["config"]["pool_id"] == pool_id
    assert card["config"]["pool_as_of"] == AS_OF
    assert card["config"]["pool_hash"] == pool_id
    assert card["config"]["pool_symbols_count"] == 12
    assert card["config"]["data_watermark"]["cache_generation"] == 7
    assert card["config"]["disclaimer"] == art.RESEARCH_ONLY_DISCLAIMER
    assert "symbols" not in card["config"]  # 全量池以 pool artifact 为准
    assert card["strategy_hash"]  # strategy_def 参与哈希(ResearchStore 不落盘定义)
    assert card["stats"]["total_return"] == 0.4213
    assert card["stats"]["disclaimer"] == art.RESEARCH_ONLY_DISCLAIMER
    assert "equity_curve" not in card["stats"]


def test_factor_job_success_writes_run_card(state, screener_fake, fake_factor_service):
    pool_id = _screen_pool(state)["pool_id"]
    started = start_pool_backtest(
        state,
        {
            "pool_id": pool_id,
            "target": "factor",
            "factor_name": "momentum_20d",
            "start": AS_OF,
            "end": "2027-01-11",  # 186 个自然日边界: 恰好允许
            "n_groups": 5,
            "rebalance": "monthly",
        },
    )
    resp = _wait_terminal(state, started["job_id"])
    assert resp["status"] == "success", resp
    assert resp["run_id"] == "runfac0002"
    assert resp["stats"]["ic_mean"] == 0.031
    assert resp["stats"]["n_groups"] == 5
    assert resp["stats"]["long_short_sharpe"] == 0.9
    assert "group_nav" not in json.dumps(resp, ensure_ascii=False)
    assert resp["disclaimer"] == art.RESEARCH_ONLY_DISCLAIMER

    (config,) = fake_factor_service
    assert config.factor_name == "momentum_20d"
    assert config.end == date(2027, 1, 11)

    card_path = Path(state.repo.store.data_dir) / "research" / "run_cards" / "runfac0002.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["kind"] == "pool_backtest_factor"
    assert card["config"]["pool_id"] == pool_id
    assert card["config"]["pool_as_of"] == AS_OF
    assert card["config"]["pool_symbols_count"] == 12
    assert card["config"]["requested"] == {
        "pool_id": pool_id,
        "target": "factor",
        "start": AS_OF,
        "end": "2027-01-11",
        "factor_name": "momentum_20d",
        "n_groups": 5,
        "rebalance": "monthly",
    }
    assert card["strategy_hash"] == ""  # 因子无策略定义(ResearchStore 约定)
    assert card["stats"]["disclaimer"] == art.RESEARCH_ONLY_DISCLAIMER


def test_result_error_marks_job_error_without_run_card(state, screener_fake, monkeypatch):
    class _FailingSvc:
        def __init__(self, engine, strategy_engine):
            pass

        def run(self, config):  # noqa: ANN001
            # 回测 service 以 result.error 报告失败(数据缺失等), 不抛异常。
            return SimpleNamespace(
                run_id="runbad0003",
                config={},
                stats={},
                strategy_info=None,
                error="无数据，请检查日期范围或先运行盘后管道",
            )

    monkeypatch.setattr(strategy_module, "StrategyBacktestService", _FailingSvc)

    pool_id = _screen_pool(state)["pool_id"]
    started = start_pool_backtest(
        state,
        {
            "pool_id": pool_id,
            "target": "strategy",
            "strategy_id": "s1",
            "start": AS_OF,
            "end": "2026-09-30",
        },
    )
    resp = _wait_terminal(state, started["job_id"])
    assert resp["status"] == "error"
    assert "无数据" in resp["error"]
    assert resp.get("run_card_ref") is None
    cards_dir = Path(state.repo.store.data_dir) / "research" / "run_cards"
    assert not cards_dir.exists() or not any(cards_dir.glob("*.json"))


def test_same_config_deduplicates_job(state, screener_fake, fake_strategy_service):
    pool_id = _screen_pool(state)["pool_id"]
    payload = {
        "pool_id": pool_id,
        "target": "strategy",
        "strategy_id": "s1",
        "start": AS_OF,
        "end": "2026-09-30",
    }
    first = start_pool_backtest(state, payload)
    second = start_pool_backtest(state, payload)
    assert second["job_id"] == first["job_id"]
    assert second.get("deduplicated") is True
    _wait_terminal(state, first["job_id"])
    assert len(fake_strategy_service) == 1  # 后台只执行一次


# ── get: 未知/非法 job_id 与 wait 边界 ────────────────────────
def test_unknown_and_invalid_job_id(state):
    with pytest.raises(ValueError, match="unknown job_id"):
        get_pool_backtest(state, {"job_id": "pb-deadbeefcafe"})
    with pytest.raises(ValueError, match="invalid job_id"):
        get_pool_backtest(state, {"job_id": "../run_cards/x"})
    with pytest.raises(ValidationError):
        get_pool_backtest(state, {"job_id": "pb-deadbeefcafe", "wait_seconds": 31})


def test_pending_progress_shape_and_thread_safety(state, screener_fake, monkeypatch):
    """慢回测下 pending 返回进度摘要; 并发 get 不破坏任务表。"""
    release = threading.Event()

    class _SlowSvc:
        def __init__(self, engine, strategy_engine):
            pass

        def run(self, config):  # noqa: ANN001
            release.wait(5)
            return SimpleNamespace(
                run_id="runslow004",
                config={},
                stats={"total_return": 0.1},
                strategy_info=None,
                error=None,
            )

    monkeypatch.setattr(strategy_module, "StrategyBacktestService", _SlowSvc)
    pool_id = _screen_pool(state)["pool_id"]
    started = start_pool_backtest(
        state,
        {"pool_id": pool_id, "target": "strategy", "strategy_id": "s1", "start": AS_OF, "end": "2026-09-30"},
    )
    pending = get_pool_backtest(state, {"job_id": started["job_id"], "wait_seconds": 0.1})
    assert pending["status"] in {"pending", "running"}
    assert pending["progress"]["start"] == AS_OF
    assert pending["progress"]["end"] == "2026-09-30"
    assert "600011.SH" not in json.dumps(pending, ensure_ascii=False)

    results: list = []

    def _reader():
        results.append(get_pool_backtest(state, {"job_id": started["job_id"]}))

    threads = [threading.Thread(target=_reader) for _ in range(4)]
    for t in threads:
        t.start()
    release.set()
    for t in threads:
        t.join(5)
    final = _wait_terminal(state, started["job_id"])
    assert final["status"] == "success"
    assert final["run_id"] == "runslow004"
    assert all(r["job_id"] == started["job_id"] for r in results)



def test_thread_start_failure_releases_job_slot(state, screener_fake, monkeypatch):
    pool_id = _screen_pool(state)["pool_id"]
    monkeypatch.setattr(
        art.threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread limit")),
    )

    with pytest.raises(ValueError, match="无法启动"):
        start_pool_backtest(
            state,
            {
                "pool_id": pool_id,
                "target": "strategy",
                "strategy_id": "s1",
                "start": AS_OF,
                "end": "2026-07-11",
            },
        )
    assert art._JOBS == {}


def test_active_job_capacity_never_evicts_running_jobs(state, screener_fake):
    pool_id = _screen_pool(state)["pool_id"]
    for index in range(art._MAX_JOBS):
        art._JOBS[f"pb-{index:012x}"] = {
            "status": "running",
            "finished_ts": None,
            "created_ts": float(index),
        }

    with pytest.raises(ValueError, match="已达上限"):
        start_pool_backtest(
            state,
            {
                "pool_id": pool_id,
                "target": "strategy",
                "strategy_id": "s1",
                "start": AS_OF,
                "end": "2026-07-11",
            },
        )
    assert len(art._JOBS) == art._MAX_JOBS
    assert all(record["status"] == "running" for record in art._JOBS.values())

def test_max_days_constant():
    assert MAX_BACKTEST_DAYS == 186
