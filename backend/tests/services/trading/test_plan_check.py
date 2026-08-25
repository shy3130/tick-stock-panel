"""P4 计划检查 (M11/M12) 核心语义测试。

覆盖: 纯函数 preflight/缺输入 no_action、wait/unknown 不调 Stage2、proceed 才调 Stage2、
usage 累计、cancel、locked/DAG trace、无交易写入、计划兼容 (旧 schema)、
Stage2 输出无行动字段、导出免责声明与敏感字段不泄漏。

不依赖 FastAPI; 使用注入的 fake generate (零真实 provider)。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services import analysis_artifacts
from app.services import strategy_profile as sp
from app.services.ai_structured import AIUsage, CancellationToken, GenerateResponse
from app.services.ai_structured.models import AnalysisTraceNode
from app.services.analysis_context import (
    KlineAnalysisBar,
    KlineAnalysisFrame,
    KlineFeatureRow,
)
from app.services.trading import accounts as accounts_svc
from app.services.trading import plan_check as pc
from app.services.trading import plans as plans_svc
from app.services.trading import store

# ── fixtures ─────────────────────────────────────────────
DATE = "20260806"

_VALID_PROFILE = {
    "strategyId": "trend_a",
    "invalidation": [{"name": "跌破年线", "observable": "收盘价<MA250", "action": "清仓"}],
    "risk": {"positionLimitPct": 30.0, "lossBudgetPct": 10.0, "thesisHorizonMonths": 3},
}


def _analysis_frame(symbol: str = "000001.SZ") -> KlineAnalysisFrame:
    now = datetime.now(UTC)
    bars = [
        KlineAnalysisBar(
            date=(now - timedelta(days=89 - i)).date(),
            open=10.0 + i * 0.01,
            high=10.3 + i * 0.01,
            low=9.8 + i * 0.01,
            close=10.1 + i * 0.01,
            volume=1000 + i,
            closed=True,
        )
        for i in range(90)
    ]
    return KlineAnalysisFrame(
        symbol=symbol,
        market="a_share",
        timeframe="1d",
        data_as_of=now,
        source="canonical_enriched",
        features=[
            KlineFeatureRow(
                date=bar.date,
                bar_type="bullish",
                close_position=0.6,
                range_atr=1.0,
                ema20_pos=0.01,
            )
            for bar in bars
        ],
        adjustment="qfq",
        bars=bars,
        indicators={"ema20": [10.0] * 90, "atr_14": [0.5] * 90},
        warmup_bars=60,
    )


@pytest.fixture(autouse=True)
def _stub_analysis_frame(monkeypatch):
    monkeypatch.setattr(
        pc,
        "_load_plan_analysis_frame",
        lambda repo, symbol: _analysis_frame(symbol),
    )


def _complete_buy_new_entry(eid: str = "e1", symbol: str = "000001.SZ") -> dict:
    """完整 buy_new entry: qty/plannedPrice/strategyId/thesisHorizonMonths + stopLoss。"""
    return {
        "id": eid,
        "symbol": symbol,
        "action": "buy_new",
        "trigger": "突破前高",
        "reason": "趋势确认",
        "qty": 100,
        "plannedPrice": 12.5,
        "strategyId": "trend_a",
        "thesisHorizonMonths": 3,
        "stopLoss": 11.0,
        "exitRule": "",
        "invalidation": "",
    }


def _seed_plan(tmp_path, entries: list[dict]) -> dict:
    return plans_svc.write_plan(tmp_path, DATE, {"entries": entries})


def _seed_profile(tmp_path, strategy_id: str = "trend_a") -> None:
    sp.write_profile(tmp_path, {**_VALID_PROFILE, "strategyId": strategy_id})
    accounts_svc.write_accounts(
        tmp_path,
        {
            "accounts": [
                {
                    "id": "default",
                    "currency": "CNY",
                    "capital": 500_000,
                    "horizonFundMonths": 12,
                    "maxSingleRatio": 0.25,
                    "changes": [],
                }
            ]
        },
    )


def _gen_factory(responses):
    """返回一个 fake generate, 按序消费 responses (str | GenerateResponse)。"""
    it = iter(responses)
    calls = {"n": 0}

    async def generate(messages, **kwargs):
        calls["n"] += 1
        return next(it)

    generate.calls = calls  # type: ignore[attr-defined]
    return generate


def _stage1_ok_json(readiness: str = "sufficient") -> str:
    return json.dumps(
        {
            "trend": "上行",
            "volatility": "中等",
            "liquidity": "充足",
            "readiness": readiness,
            "conflicts": [],
            "notes": [],
        }
    )


def _stage2_ok_json() -> str:
    return json.dumps(
        {
            "checks": [
                {"item": "止损距离", "conclusion": "满足", "reason": "11.0/12.5 距离合理"},
            ],
            "summary": "计划结构完整",
        }
    )


# ── preflight: 空/缺输入 no_action (零 AI) ───────────────
@pytest.mark.asyncio
async def test_missing_plan_entry_no_action_zero_ai(tmp_path):
    """计划或 entry 不存在 → no_action, unknown, 不调用任何 AI。"""
    s1 = _gen_factory(["SHOULD_NOT_CALL"])
    s2 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="ghost",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    r = art.result
    assert r["status"] == "no_action"
    assert r["gate"]["status"] == "unknown"
    assert r["stage1"] is None and r["review"] is None
    assert s1.calls["n"] == 0 and s2.calls["n"] == 0


@pytest.mark.asyncio
async def test_incomplete_entry_no_action_unknown(tmp_path):
    """buy_new 缺 plannedPrice/strategyId/thesisHorizonMonths → unknown no_action, 零 AI。"""
    _seed_plan(
        tmp_path,
        [
            {
                "id": "e1",
                "symbol": "000001.SZ",
                "action": "buy_new",
                "trigger": "t",
                "reason": "r",
                "qty": 100,
            }
        ],
    )
    s1 = _gen_factory(["SHOULD_NOT_CALL"])
    s2 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    r = art.result
    assert r["status"] == "no_action"
    assert r["gate"]["status"] == "unknown"
    assert "plannedPrice" in " ".join(r["gate"]["missing_inputs"])
    assert s1.calls["n"] == 0


@pytest.mark.asyncio
async def test_profile_missing_no_action_unknown(tmp_path):
    """strategyId 存在但 profile 不存在 → unknown no_action, 零 AI。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    s1 = _gen_factory(["SHOULD_NOT_CALL"])
    s2 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    r = art.result
    assert r["status"] == "no_action"
    assert r["gate"]["status"] == "unknown"
    assert s1.calls["n"] == 0


@pytest.mark.asyncio
async def test_profile_invalid_no_action_unknown(tmp_path):
    """profile 存在但结构非法 → unknown no_action, 零 AI。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    sp.write_profile(
        tmp_path,
        {
            **_VALID_PROFILE,
            "risk": {"positionLimitPct": 0, "lossBudgetPct": 10.0, "thesisHorizonMonths": 3},
        },
    )
    s1 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
    )
    r = art.result
    assert r["status"] == "no_action"
    assert r["gate"]["status"] == "unknown"
    assert s1.calls["n"] == 0


# ── wait/unknown 不调 Stage2 ─────────────────────────────
@pytest.mark.asyncio
async def test_stage1_insufficient_gate_wait_no_stage2(tmp_path):
    """Stage1 readiness=insufficient → gate=wait, 不调 Stage2。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory([_stage1_ok_json("insufficient")])
    s2 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    r = art.result
    assert r["status"] == "no_action"
    assert r["gate"]["status"] == "wait"
    assert s1.calls["n"] == 1
    assert s2.calls["n"] == 0


# ── proceed 才调 Stage2 ──────────────────────────────────
@pytest.mark.asyncio
async def test_proceed_calls_stage2_review_ready(tmp_path):
    """Stage1 sufficient + 门禁全通过 → proceed → 调 Stage2 → review_ready。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory(
        [
            GenerateResponse(
                text=_stage1_ok_json("sufficient"),
                usage=AIUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            )
        ]
    )
    s2 = _gen_factory(
        [
            GenerateResponse(
                text=_stage2_ok_json(),
                usage=AIUsage(prompt_tokens=20, completion_tokens=4, total_tokens=24),
            )
        ]
    )
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    r = art.result
    assert r["status"] == "review_ready"
    assert r["gate"]["status"] == "proceed"
    assert r["stage1"]["readiness"] == "sufficient"
    assert r["review"]["checks"][0]["item"] == "止损距离"
    assert s1.calls["n"] == 1
    assert s2.calls["n"] == 1


# ── usage 累计 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_usage_accumulated_across_stages(tmp_path):
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory(
        [
            GenerateResponse(
                text=_stage1_ok_json("sufficient"),
                usage=AIUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            )
        ]
    )
    s2 = _gen_factory(
        [
            GenerateResponse(
                text=_stage2_ok_json(),
                usage=AIUsage(
                    prompt_tokens=20, cached_prompt_tokens=5, completion_tokens=4, total_tokens=24
                ),
            )
        ]
    )
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    assert art.usage.total_tokens == 36
    assert art.usage.cached_prompt_tokens == 5


# ── cancel ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cancel_before_stage1(tmp_path):
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    token = CancellationToken()
    token.cancel()
    s1 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        cancel_token=token,
        stage1_generate=s1,
    )
    assert s1.calls["n"] == 0
    assert art.status == "cancelled"
    assert art.result["status"] == "no_action"
    assert art.result["gate"]["status"] == "unknown"
    assert art.result["gate"]["reasons"] == ["Stage1 被取消"]


@pytest.mark.asyncio
async def test_cancel_during_preflight_event_is_audited(tmp_path):
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory(["SHOULD_NOT_CALL"])

    async def cancel_on_preflight(kind, _payload):
        if kind == "preflight_completed":
            raise asyncio.CancelledError

    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        on_event=cancel_on_preflight,
        stage1_generate=s1,
    )

    assert s1.calls["n"] == 0
    assert art.status == "cancelled"
    assert analysis_artifacts.read(tmp_path, art.attempt_id) is not None


@pytest.mark.asyncio
async def test_stage1_failure_is_not_reported_as_data_insufficient(tmp_path):
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s2 = _gen_factory(["SHOULD_NOT_CALL"])

    async def fail_stage1(_messages, **_kwargs):
        raise RuntimeError("provider unavailable")

    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=fail_stage1,
        stage2_generate=s2,
    )

    assert art.status == "failed"
    assert art.result["gate"]["status"] == "unknown"
    assert art.result["gate"]["reasons"] == ["Stage1 调用或输出失败"]
    assert "数据不足以判断" not in json.dumps(art.result, ensure_ascii=False)
    assert s2.calls["n"] == 0


# ── locked / DAG trace ───────────────────────────────────
@pytest.mark.asyncio
async def test_trace_nodes_locked_and_form_dag(tmp_path):
    """程序节点 locked; trace 是 DAG; final 节点回溯到 locked。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory([_stage1_ok_json("sufficient")])
    s2 = _gen_factory([_stage2_ok_json()])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    trace = art.trace
    ids = {n.id for n in trace}
    assert "plan_entry" in ids and "program_gate" in ids
    # 程序节点必须 locked
    for n in trace:
        if n.kind in ("program_rule", "fact"):
            assert n.locked, f"{n.id} 应 locked"
    # DAG: 无环
    assert not pc._has_cycle(trace)
    # final 节点回溯 locked
    locked_ids = {n.id for n in trace if n.locked}
    depended = {dep for n in trace for dep in n.depends_on}
    finals = [n for n in trace if n.id not in depended]
    for f in finals:
        assert pc._reaches_locked(f, trace, locked_ids), f"{f.id} 未回溯 locked"


def test_dag_validator_rejects_cycle():
    a = AnalysisTraceNode(id="a", kind="fact", label="a", status="pass", locked=True)
    b = AnalysisTraceNode(
        id="b", kind="model_assessment", label="b", status="pass", depends_on=["a"]
    )
    a.depends_on = ["b"]  # 制造环 a→b→a
    assert pc._has_cycle([a, b])


def test_dag_validator_rejects_final_without_locked():
    a = AnalysisTraceNode(id="a", kind="model_assessment", label="a", status="pass")
    # a 无依赖, 也无 locked 可达 → 违规
    assert pc._validate_trace_dag([a])


# ── 无交易写入 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_trade_event_written(tmp_path):
    """计划检查绝不写入 trade_events.jsonl / trades。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory([_stage1_ok_json("sufficient")])
    s2 = _gen_factory([_stage2_ok_json()])
    await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    events_path = store.events_path(tmp_path)
    assert not events_path.exists() or events_path.read_text().strip() == ""
    assert not (tmp_path / "user_data" / "trading" / "trades").exists() or not list(
        (tmp_path / "user_data" / "trading" / "trades").iterdir()
    )


# ── 计划兼容 (旧 schema 可读写) ─────────────────────────
def test_old_plan_entry_without_additive_fields_still_validates(tmp_path):
    """旧计划条目 (无 P4 additive 字段) 仍可写读。"""
    plan = plans_svc.write_plan(
        tmp_path,
        DATE,
        {"entries": [{"id": "old1", "symbol": "A.SH", "action": "watch", "trigger": "t"}]},
    )
    e = plan["entries"][0]
    # 旧字段保留
    assert e["action"] == "watch"
    # additive 字段缺省为 None/空
    assert e["strategyId"] is None
    assert e["plannedPrice"] is None
    assert e["stopLoss"] is None
    assert e["exitRule"] == ""
    assert e["thesisHorizonMonths"] is None
    assert e["invalidation"] == ""
    # 回读一致
    back = plans_svc.read_plan(tmp_path, DATE)
    assert back["entries"][0]["id"] == "old1"


def test_new_plan_entry_with_additive_fields_roundtrip(tmp_path):
    plan = plans_svc.write_plan(tmp_path, DATE, {"entries": [_complete_buy_new_entry()]})
    e = plan["entries"][0]
    assert e["strategyId"] == "trend_a"
    assert e["plannedPrice"] == 12.5
    assert e["stopLoss"] == 11.0
    assert e["thesisHorizonMonths"] == 3


# ── Stage2 输出无行动字段 ─────────────────────────────────
def test_stage2_output_model_has_no_action_fields():
    """Stage2PlanReview schema 不含 order/side/action/价格字段。"""
    fields = set(pc.Stage2PlanReview.model_fields.keys())
    forbidden = {
        "order",
        "side",
        "action",
        "buy",
        "sell",
        "price",
        "quantity",
        "recommendedPrice",
        "targetPrice",
        "signal",
    }
    assert not (fields & forbidden)


def test_stage2_forbids_extra_action_keys_in_checks():
    """模型若输出 checks 项里带 side/order 应被 schema forbid 拒绝。"""
    from pydantic import ValidationError

    bad = {"checks": [{"item": "x", "conclusion": "满足", "reason": "r", "side": "buy"}]}
    with pytest.raises(ValidationError):
        pc.Stage2PlanReview.model_validate(bad)


@pytest.mark.asyncio
async def test_stage2_rejects_forbidden_action_keys_in_data(tmp_path):
    """_assert_stage2_no_action_fields 拒绝含禁止键的 Stage2 原始数据。"""
    bad_data = {"checks": [], "summary": "", "side": "buy"}
    with pytest.raises(ValueError, match="禁止"):
        pc._assert_stage2_no_action_fields(bad_data)


# ── 导出免责声明与敏感字段不泄漏 ─────────────────────────
@pytest.mark.asyncio
async def test_markdown_export_contains_disclaimer(tmp_path):
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory([_stage1_ok_json("sufficient")])
    s2 = _gen_factory([_stage2_ok_json()])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    md = pc.artifact_to_markdown(art)
    assert "免责声明" in md
    assert "不构成任何买卖建议" in md
    assert "交易计划检查报告" in md


@pytest.mark.asyncio
async def test_artifact_does_not_leak_prompt_or_raw(tmp_path):
    """artifact 序列化不含 messages/prompt/raw_text。"""
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    s1 = _gen_factory([_stage1_ok_json("sufficient")])
    s2 = _gen_factory([_stage2_ok_json()])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    dumped = art.model_dump_json()
    assert "messages" not in dumped
    assert "DATA_BEGIN" not in dumped  # prompt 定界符不泄漏


@pytest.mark.asyncio
async def test_no_action_artifact_persisted(tmp_path):
    """artifact 落 analysis_artifacts.record (no_action 也落盘)。"""
    _seed_plan(
        tmp_path,
        [{"id": "e1", "symbol": "000001.SZ", "action": "buy_new", "trigger": "t", "reason": "r"}],
    )  # 不完整
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=_gen_factory([]),
        stage2_generate=_gen_factory([]),
    )
    back = analysis_artifacts.read(tmp_path, art.attempt_id)
    assert back is not None
    assert back.purpose == pc.PURPOSE
    assert back.result["status"] == "no_action"
    assert back.program_rules_version == pc.PROGRAM_RULES_VERSION


# ── Stage2 prompt DATA_BEGIN/DATA_END 定界 ───────────────
def test_stage2_prompt_uses_data_delimiters():
    entry = _complete_buy_new_entry()
    msgs = pc._build_stage2_messages(entry, _VALID_PROFILE)
    user = msgs[1]["content"]
    assert "DATA_BEGIN" in user and "DATA_END" in user
    assert "数据" in user and "指令" in user  # 声明为数据非指令


def test_stage2_schema_rejects_program_owned_fields():
    with pytest.raises(ValidationError):
        pc.Stage2PlanReview.model_validate(
            {
                "checks": [],
                "summary": "仅检查计划",
                "symbol": "000001.SZ",
                "gate_status": "proceed",
            }
        )


# ── 模型不可升级程序门禁 ─────────────────────────────────
@pytest.mark.asyncio
async def test_gate_is_program_only_never_upgraded_by_model(tmp_path):
    """程序门禁纯程序计算; Stage1 即使 '乐观' 也不能把 insufficient 升级为 proceed。

    构造 Stage1 返回 insufficient (数据不足) → 即使模型 '乐观', gate 仍 wait。
    """
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    # Stage1 故意返回 insufficient
    s1 = _gen_factory([_stage1_ok_json("insufficient")])
    s2 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
        stage2_generate=s2,
    )
    assert art.result["gate"]["status"] == "wait"
    assert s2.calls["n"] == 0


@pytest.mark.asyncio
async def test_watch_plan_uses_no_mechanical_trade_gates(tmp_path):
    _seed_plan(
        tmp_path,
        [
            {
                "id": "watch-1",
                "symbol": "000001.SZ",
                "action": "watch",
                "trigger": "观察放量突破",
                "reason": "研究候选",
            }
        ],
    )
    s1 = _gen_factory([_stage1_ok_json("sufficient")])
    s2 = _gen_factory([_stage2_ok_json()])

    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="watch-1",
        stage1_generate=s1,
        stage2_generate=s2,
    )

    assert art.result["gate"]["status"] == "proceed"
    assert art.result["status"] == "review_ready"
    assert s2.calls["n"] == 1


@pytest.mark.asyncio
async def test_missing_kline_context_no_action_zero_ai(monkeypatch, tmp_path):
    _seed_plan(tmp_path, [_complete_buy_new_entry()])
    _seed_profile(tmp_path)
    monkeypatch.setattr(pc, "_load_plan_analysis_frame", lambda repo, symbol: None)
    s1 = _gen_factory(["SHOULD_NOT_CALL"])
    art = await pc.run_plan_check(
        repo=None,
        data_dir=tmp_path,
        date=DATE,
        entry_id="e1",
        stage1_generate=s1,
    )
    assert art.result["status"] == "no_action"
    assert art.result["gate"]["status"] == "unknown"
    assert "kline_analysis_frame" in art.result["gate"]["missing_inputs"]
    assert s1.calls["n"] == 0


def test_stage1_prompt_contains_kline_facts_not_just_symbol():
    frame = _analysis_frame()
    messages, meta = pc._build_stage1_messages(
        frame,
        _complete_buy_new_entry(),
        _VALID_PROFILE,
        max_tokens=8000,
    )
    text = "\n".join(message["content"] for message in messages)
    assert "canonical_enriched" in text
    assert "data_as_of" in text
    assert "FEATURES" in text and "cp=" in text
    assert meta["estimated_tokens"] <= 8000
