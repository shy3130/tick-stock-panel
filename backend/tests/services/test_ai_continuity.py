"""M25 跨日连续性 (ai_continuity) 测试。

覆盖: parent 选择 (兼容/不兼容)、连续性评估 (fresh/incremental/full_reanalysis 各失效原因)、
parent 链构建、安全禁词校验、与 plan_check 集成 (enable_continuity 写入 parent_attempt_id)。

不依赖 FastAPI; 使用 tmp_path 真实 artifact store (不修改 data/)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services import ai_continuity as cont
from app.services import analysis_artifacts as aa
from app.services.ai_structured import (
    AIUsage,
    AnalysisArtifact,
)
from app.services.analysis_context import (
    KlineAnalysisBar,
    KlineAnalysisFrame,
    KlineFeatureRow,
)

# ── 构造辅助 ──────────────────────────────────────────────
_NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
_PURPOSE = "trading_plan_check"
_SCHEMA = "v1"
_PRV = "tickflow-gates-v1"
_PROMPT = "plan-check-v1"


def _bars(n: int = 90, end: datetime = _NOW) -> list[KlineAnalysisBar]:
    """生成 n 根日 K 线, 最后一根日期 = end.date()。"""
    return [
        KlineAnalysisBar(
            date=(end - timedelta(days=n - 1 - i)).date(),
            open=10.0 + i * 0.01,
            high=10.3 + i * 0.01,
            low=9.8 + i * 0.01,
            close=10.1 + i * 0.01,
            volume=1000 + i,
            closed=True,
        )
        for i in range(n)
    ]


def _frame(
    *,
    symbol: str = "000001.SZ",
    market: str = "a_share",
    data_as_of: datetime = _NOW,
    adjustment: str = "qfq",
    bars: list[KlineAnalysisBar] | None = None,
) -> KlineAnalysisFrame:
    bl = bars or _bars(end=data_as_of)
    return KlineAnalysisFrame(
        symbol=symbol,
        market=market,
        timeframe="1d",
        data_as_of=data_as_of,
        source="canonical_enriched",
        adjustment=adjustment,  # type: ignore[arg-type]
        bars=bl,
        features=[
            KlineFeatureRow(date=b.date, close_position=0.6, range_atr=1.0)
            for b in bl
        ],
        indicators={},
        warmup_bars=60,
    )


def _artifact(
    data_dir: Path,
    *,
    attempt_id: str | None = None,
    purpose: str = _PURPOSE,
    status: str = "ok",
    symbol: str = "000001.SZ",
    schema_version: str = _SCHEMA,
    program_rules_version: str | None = _PRV,
    prompt_version: str = _PROMPT,
    profile_id: str = "prof-a",
    market: str = "a_share",
    adjustment: str = "qfq",
    data_as_of: datetime = _NOW,
    parent_attempt_id: str | None = None,
    result: dict | None = None,
) -> AnalysisArtifact:
    """直接构造并持久化 artifact (绕过 build_artifact, 精确控制字段)。"""
    aid = attempt_id or f"att_{_counter()}"
    art = AnalysisArtifact(
        id=aid,
        attempt_id=aid,
        request_id=f"req_{_counter()}",
        purpose=purpose,
        status=status,  # type: ignore[arg-type]
        schema_version=schema_version,
        prompt_version=prompt_version,
        program_rules_version=program_rules_version,
        created_at=data_as_of - timedelta(hours=1),
        data_as_of=data_as_of,
        symbol=symbol,
        market=market,
        adjustment=adjustment,  # type: ignore[arg-type]
        profile_id=profile_id,
        model="test-model",
        result=result or {"status": "review_ready"},
        usage=AIUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        parent_attempt_id=parent_attempt_id,
    )
    return aa.record(data_dir, art)


_counter_val = 0


def _counter() -> str:
    global _counter_val
    _counter_val += 1
    return f"{_counter_val:08d}"


# ════════════════════════════════════════════════════════════
# parent 选择
# ════════════════════════════════════════════════════════════
class TestSelectParent:
    def test_no_artifacts_returns_none(self, tmp_path):
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is None

    def test_selects_latest_ok_matching(self, tmp_path):
        old = _artifact(tmp_path, data_as_of=_NOW - timedelta(days=5))
        new = _artifact(tmp_path, data_as_of=_NOW - timedelta(days=1))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is not None
        assert parent.attempt_id == new.attempt_id

    def test_skips_failed_status(self, tmp_path):
        _artifact(tmp_path, status="failed", data_as_of=_NOW - timedelta(days=1))
        ok = _artifact(tmp_path, status="ok", data_as_of=_NOW - timedelta(days=5))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is not None
        assert parent.attempt_id == ok.attempt_id

    def test_skips_cancelled_status(self, tmp_path):
        _artifact(tmp_path, status="cancelled", data_as_of=_NOW - timedelta(days=1))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is None

    def test_filters_wrong_symbol(self, tmp_path):
        _artifact(tmp_path, symbol="600519.SH", data_as_of=_NOW - timedelta(days=1))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is None

    def test_filters_wrong_schema_version(self, tmp_path):
        _artifact(tmp_path, schema_version="v2", data_as_of=_NOW - timedelta(days=1))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is None

    def test_filters_wrong_program_rules_version(self, tmp_path):
        _artifact(
            tmp_path,
            program_rules_version="other-v2",
            data_as_of=_NOW - timedelta(days=1),
        )
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is None

    def test_filters_wrong_purpose(self, tmp_path):
        _artifact(tmp_path, purpose="stock_analysis", data_as_of=_NOW - timedelta(days=1))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is None

    def test_profile_mismatch_does_not_filter(self, tmp_path):
        """profile_id 不一致仍可选为 parent (在 assess 中判定 full_reanalysis)。"""
        _artifact(tmp_path, profile_id="prof-other", data_as_of=_NOW - timedelta(days=1))
        parent = cont.select_parent(
            tmp_path,
            symbol="000001.SZ",
            purpose=_PURPOSE,
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert parent is not None
        assert parent.profile_id == "prof-other"


# ════════════════════════════════════════════════════════════
# 连续性评估
# ════════════════════════════════════════════════════════════
class TestAssessContinuity:
    def test_no_parent_is_fresh(self, tmp_path):
        frame = _frame()
        verdict = cont.assess_continuity(None, frame)
        assert verdict.mode == cont.ContinuityMode.FRESH
        assert verdict.parent_attempt_id is None
        assert "首次" in verdict.reason or "无兼容" in verdict.reason

    def test_incremental_when_compatible(self, tmp_path):
        parent_as_of = _NOW - timedelta(days=3)
        parent = _artifact(tmp_path, data_as_of=parent_as_of)
        frame = _frame(data_as_of=_NOW)
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a")
        assert verdict.mode == cont.ContinuityMode.INCREMENTAL
        assert verdict.parent_attempt_id == parent.attempt_id
        assert verdict.bars_delta == 3
        assert len(verdict.new_bar_dates) == 3

    def test_full_reanalysis_on_profile_change(self, tmp_path):
        parent = _artifact(tmp_path, profile_id="prof-a", data_as_of=_NOW - timedelta(days=1))
        frame = _frame()
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-b")
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "profile" in verdict.reason.lower() or "profile_id" in verdict.reason
        assert verdict.compatibility["profile_match"] is False

    def test_full_reanalysis_on_prompt_version_change(self, tmp_path):
        parent = _artifact(tmp_path, prompt_version="plan-check-v1", data_as_of=_NOW - timedelta(days=1))
        frame = _frame()
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a", prompt_version="plan-check-v2")
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "prompt" in verdict.reason.lower()

    def test_full_reanalysis_on_market_change(self, tmp_path):
        parent = _artifact(tmp_path, market="a_share", data_as_of=_NOW - timedelta(days=1))
        frame = _frame(market="hk_stock")
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a")
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "market" in verdict.reason.lower()

    def test_full_reanalysis_on_adjustment_change(self, tmp_path):
        parent = _artifact(tmp_path, adjustment="qfq", data_as_of=_NOW - timedelta(days=1))
        frame = _frame(adjustment="hfq")
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a")
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "adjustment" in verdict.reason.lower()

    def test_full_reanalysis_when_parent_data_as_of_none(self, tmp_path):
        parent = _artifact(tmp_path, data_as_of=_NOW - timedelta(days=1))
        parent = parent.model_copy(update={"data_as_of": None})
        frame = _frame()
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a")
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "data_as_of" in verdict.reason or "锚点" in verdict.reason

    def test_full_reanalysis_on_anchor_not_in_window(self, tmp_path):
        """parent 锚点不在当前窗口 (数据滚动过远)。"""
        parent_as_of = _NOW - timedelta(days=200)
        parent = _artifact(tmp_path, data_as_of=parent_as_of)
        frame = _frame(data_as_of=_NOW)  # 90 bars, oldest = _NOW - 89 days
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a")
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "跨度" in verdict.reason or "锚点" in verdict.reason or "窗口" in verdict.reason

    def test_full_reanalysis_on_gap_exceeds_threshold(self, tmp_path):
        parent_as_of = _NOW - timedelta(days=65)
        parent = _artifact(tmp_path, data_as_of=parent_as_of)
        # 构造 frame 使 parent 锚点在窗口内, 但新增 bar > max_gap_bars
        bars = _bars(n=90, end=_NOW)
        # 注入 parent 锚点日期到 bars 中 (确保 anchor_seen=True)
        anchor_date = parent_as_of.date()
        if not any(b.date == anchor_date for b in bars):
            bars = list(bars) + [KlineAnalysisBar(date=anchor_date, open=1, high=1, low=1, close=1, volume=1, closed=True)]
            bars.sort(key=lambda b: b.date)
        frame = _frame(data_as_of=_NOW, bars=bars)
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a", max_gap_bars=60)
        assert verdict.mode == cont.ContinuityMode.FULL_REANALYSIS
        assert "阈值" in verdict.reason or "超过" in verdict.reason
        assert verdict.bars_delta > 60

    def test_incremental_bars_delta_zero_when_same_day(self, tmp_path):
        """parent 和当前帧同一天 → bars_delta=0, 仍 incremental。"""
        parent = _artifact(tmp_path, data_as_of=_NOW)
        frame = _frame(data_as_of=_NOW)
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a")
        assert verdict.mode == cont.ContinuityMode.INCREMENTAL
        assert verdict.bars_delta == 0

    def test_custom_max_gap_bars_allows_larger_delta(self, tmp_path):
        parent_as_of = _NOW - timedelta(days=65)
        parent = _artifact(tmp_path, data_as_of=parent_as_of)
        bars = _bars(n=90, end=_NOW)
        anchor_date = parent_as_of.date()
        if not any(b.date == anchor_date for b in bars):
            bars = list(bars) + [KlineAnalysisBar(date=anchor_date, open=1, high=1, low=1, close=1, volume=1, closed=True)]
            bars.sort(key=lambda b: b.date)
        frame = _frame(data_as_of=_NOW, bars=bars)
        verdict = cont.assess_continuity(parent, frame, profile_id="prof-a", max_gap_bars=100)
        assert verdict.mode == cont.ContinuityMode.INCREMENTAL


# ════════════════════════════════════════════════════════════
# 安全: 禁词校验
# ════════════════════════════════════════════════════════════
class TestForbiddenKeys:
    def test_clean_meta_passes(self):
        verdict = cont.ContinuityVerdict(
            mode=cont.ContinuityMode.INCREMENTAL,
            reason="ok",
        )
        meta = cont.build_continuity_meta(verdict)
        cont.assert_no_forbidden_keys(meta)  # should not raise

    @pytest.mark.parametrize(
        "bad_key",
        ["order", "side", "price", "direction", "action", "buy", "sell", "quantity", "stopLoss"],
    )
    def test_forbidden_key_raises(self, bad_key):
        with pytest.raises(ValueError, match="forbidden key"):
            cont.assert_no_forbidden_keys({bad_key: "evil"})

    def test_nested_forbidden_key_raises(self):
        with pytest.raises(ValueError, match="forbidden key"):
            cont.assert_no_forbidden_keys({"compatibility": {"order": True}})

    def test_continuity_meta_never_contains_forbidden_keys(self):
        """任何 verdict 产出的 meta 都不含禁词。"""
        for mode in cont.ContinuityMode:
            verdict = cont.ContinuityVerdict(
                mode=mode,
                parent_attempt_id="att_x" if mode != cont.ContinuityMode.FRESH else None,
                reason="test",
                compatibility={"profile_match": True, "prompt_match": True},
            )
            meta = cont.build_continuity_meta(verdict)
            # 递归检查
            cont.assert_no_forbidden_keys(meta)


# ════════════════════════════════════════════════════════════
# parent 链构建
# ════════════════════════════════════════════════════════════
class TestParentChain:
    def test_chain_single_node_no_parent(self, tmp_path):
        art = _artifact(tmp_path, data_as_of=_NOW)
        chain = cont.build_parent_chain(tmp_path, art.attempt_id)
        assert len(chain) == 1
        assert chain[0]["attempt_id"] == art.attempt_id
        assert chain[0]["parent_attempt_id"] is None

    def test_chain_walks_parent_linkage(self, tmp_path):
        grandparent = _artifact(tmp_path, data_as_of=_NOW - timedelta(days=10))
        parent = _artifact(
            tmp_path,
            data_as_of=_NOW - timedelta(days=5),
            parent_attempt_id=grandparent.attempt_id,
            result={
                "status": "review_ready",
                "continuity": {"mode": "incremental", "reason": "ok", "bars_delta": 5},
            },
        )
        child = _artifact(
            tmp_path,
            data_as_of=_NOW,
            parent_attempt_id=parent.attempt_id,
            result={
                "status": "review_ready",
                "continuity": {"mode": "incremental", "reason": "ok", "bars_delta": 5},
            },
        )
        chain = cont.build_parent_chain(tmp_path, child.attempt_id)
        assert len(chain) == 3
        assert chain[0]["attempt_id"] == child.attempt_id
        assert chain[1]["attempt_id"] == parent.attempt_id
        assert chain[2]["attempt_id"] == grandparent.attempt_id
        assert chain[2]["parent_attempt_id"] is None

    def test_chain_truncates_on_missing_parent(self, tmp_path):
        """parent_attempt_id 指向不存在的 artifact → 安全截断。"""
        art = _artifact(tmp_path, data_as_of=_NOW, parent_attempt_id="att_nonexistent")
        chain = cont.build_parent_chain(tmp_path, art.attempt_id)
        assert len(chain) == 1  # 只有自己, parent 不存在

    def test_chain_truncates_on_cycle(self, tmp_path):
        """环检测: a→b→a → 安全截断, 不无限循环。"""
        a = AnalysisArtifact(
            id="att_cycle_a",
            attempt_id="att_cycle_a",
            request_id="req_a",
            purpose=_PURPOSE,
            status="ok",
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
            data_as_of=_NOW,
            symbol="000001.SZ",
            parent_attempt_id="att_cycle_b",
            usage=AIUsage(),
        )
        b = AnalysisArtifact(
            id="att_cycle_b",
            attempt_id="att_cycle_b",
            request_id="req_b",
            purpose=_PURPOSE,
            status="ok",
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
            data_as_of=_NOW,
            symbol="000001.SZ",
            parent_attempt_id="att_cycle_a",
            usage=AIUsage(),
        )
        aa.record(tmp_path, a)
        aa.record(tmp_path, b)
        chain = cont.build_parent_chain(tmp_path, "att_cycle_a")
        assert len(chain) == 2  # a, b, then cycle detected

    def test_chain_includes_usage(self, tmp_path):
        art = _artifact(tmp_path, data_as_of=_NOW)
        chain = cont.build_parent_chain(tmp_path, art.attempt_id)
        assert "usage" in chain[0]
        assert chain[0]["usage"]["prompt_tokens"] == 100


# ════════════════════════════════════════════════════════════
# find_latest_artifact (analysis_artifacts 新增泛用函数)
# ════════════════════════════════════════════════════════════
class TestFindLatestArtifact:
    def test_returns_none_when_empty(self, tmp_path):
        assert aa.find_latest_artifact(tmp_path) is None

    def test_returns_latest_by_created_at(self, tmp_path):
        old = _artifact(tmp_path, data_as_of=_NOW - timedelta(days=10))
        new = _artifact(tmp_path, data_as_of=_NOW - timedelta(days=1))
        result = aa.find_latest_artifact(tmp_path, purpose=_PURPOSE)
        assert result is not None
        assert result.attempt_id == new.attempt_id

    def test_filters_by_symbol(self, tmp_path):
        _artifact(tmp_path, symbol="600519.SH", data_as_of=_NOW - timedelta(days=1))
        target = _artifact(tmp_path, symbol="000001.SZ", data_as_of=_NOW - timedelta(days=5))
        result = aa.find_latest_artifact(tmp_path, symbol="000001.SZ")
        assert result is not None
        assert result.attempt_id == target.attempt_id

    def test_filters_by_multiple_criteria(self, tmp_path):
        _artifact(
            tmp_path,
            symbol="000001.SZ",
            schema_version="v2",
            data_as_of=_NOW - timedelta(days=1),
        )
        target = _artifact(
            tmp_path,
            symbol="000001.SZ",
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
            data_as_of=_NOW - timedelta(days=5),
        )
        result = aa.find_latest_artifact(
            tmp_path,
            symbol="000001.SZ",
            schema_version=_SCHEMA,
            program_rules_version=_PRV,
        )
        assert result is not None
        assert result.attempt_id == target.attempt_id


# ════════════════════════════════════════════════════════════
# plan_check 集成: enable_continuity 写入 parent_attempt_id
# ════════════════════════════════════════════════════════════
class TestPlanCheckContinuityIntegration:
    """验证 enable_continuity=True 时 artifact 带 parent_attempt_id 和 continuity meta。"""

    DATE = "20260810"

    def _seed_plan(self, tmp_path, entries):
        from app.services.trading import plans as plans_svc
        return plans_svc.write_plan(tmp_path, self.DATE, {"entries": entries})

    def _seed_profile(self, tmp_path):
        from app.services.trading import accounts as accounts_svc
        from app.services import strategy_profile as sp
        profile = {
            "strategyId": "trend_a",
            "invalidation": [{"name": "跌破年线", "observable": "收盘价<MA250", "action": "清仓"}],
            "risk": {"positionLimitPct": 30.0, "lossBudgetPct": 10.0, "thesisHorizonMonths": 3},
        }
        sp.write_profile(tmp_path, profile)
        accounts_svc.write_accounts(tmp_path, {
            "accounts": [{
                "id": "default", "currency": "CNY", "capital": 500_000,
                "horizonFundMonths": 12, "maxSingleRatio": 0.25, "changes": [],
            }]
        })

    def _complete_entry(self, eid="e1", symbol="000001.SZ"):
        return {
            "id": eid, "symbol": symbol, "action": "buy_new", "trigger": "突破前高",
            "reason": "趋势确认", "qty": 100, "plannedPrice": 12.5,
            "strategyId": "trend_a", "thesisHorizonMonths": 3,
            "stopLoss": 11.0, "exitRule": "", "invalidation": "",
        }

    def _stub_frame(self, monkeypatch, symbol="000001.SZ"):
        from app.services.trading import plan_check as pc
        monkeypatch.setattr(pc, "_load_plan_analysis_frame", lambda repo, sym: _frame(symbol=sym))

    @pytest.mark.asyncio
    async def test_continuity_disabled_no_parent(self, tmp_path, monkeypatch):
        """enable_continuity=False → artifact.parent_attempt_id=None, result 无 continuity。"""
        from app.services.trading import plan_check as pc
        from app.services.ai_structured import GenerateResponse

        self._seed_plan(tmp_path, [self._complete_entry()])
        self._seed_profile(tmp_path)
        self._stub_frame(monkeypatch)

        def _gen(messages, **kw):
            return GenerateResponse(data=json.dumps({
                "trend": "上升", "volatility": "中等", "liquidity": "充足",
                "readiness": "sufficient", "conflicts": [], "notes": [],
            }))

        def _gen2(messages, **kw):
            return GenerateResponse(data=json.dumps({"checks": [], "summary": "ok"}))

        art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen, stage2_generate=_gen2,
            enable_continuity=False,
        )
        assert art.parent_attempt_id is None
        assert art.result is not None
        assert "continuity" not in (art.result or {})

    @pytest.mark.asyncio
    async def test_continuity_enabled_fresh_first_run(self, tmp_path, monkeypatch):
        """enable_continuity=True, 无 parent → mode=fresh, parent_attempt_id=None。"""
        from app.services.trading import plan_check as pc
        from app.services.ai_structured import GenerateResponse

        self._seed_plan(tmp_path, [self._complete_entry()])
        self._seed_profile(tmp_path)
        self._stub_frame(monkeypatch)

        def _gen(messages, **kw):
            return GenerateResponse(data=json.dumps({
                "trend": "上升", "volatility": "中等", "liquidity": "充足",
                "readiness": "sufficient", "conflicts": [], "notes": [],
            }))

        def _gen2(messages, **kw):
            return GenerateResponse(data=json.dumps({"checks": [], "summary": "ok"}))

        art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen, stage2_generate=_gen2,
            enable_continuity=True,
        )
        assert art.parent_attempt_id is None
        assert art.result is not None
        cont_meta = (art.result or {}).get("continuity")
        assert cont_meta is not None
        assert cont_meta["mode"] == "fresh"

    @pytest.mark.asyncio
    async def test_continuity_enabled_incremental_with_parent(self, tmp_path, monkeypatch):
        """enable_continuity=True, 有兼容 parent → mode=incremental, parent_attempt_id 链接。"""
        from app.services.trading import plan_check as pc
        from app.services.ai_structured import GenerateResponse

        # 先跑一次 (不带 continuity) 来种 parent artifact
        self._seed_plan(tmp_path, [self._complete_entry()])
        self._seed_profile(tmp_path)
        self._stub_frame(monkeypatch)

        def _gen_s1(messages, **kw):
            return GenerateResponse(data=json.dumps({
                "trend": "上升", "volatility": "中等", "liquidity": "充足",
                "readiness": "sufficient", "conflicts": [], "notes": [],
            }))

        def _gen_s2(messages, **kw):
            return GenerateResponse(data=json.dumps({"checks": [], "summary": "ok"}))

        parent_art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen_s1, stage2_generate=_gen_s2,
            enable_continuity=False,
        )
        assert parent_art.status == "ok"

        # 第二次跑, 带 continuity → 应链接到 parent
        child_art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen_s1, stage2_generate=_gen_s2,
            enable_continuity=True,
        )
        assert child_art.parent_attempt_id == parent_art.attempt_id
        cont_meta = (child_art.result or {}).get("continuity")
        assert cont_meta is not None
        # 同一天同帧 → incremental (bars_delta=0)
        assert cont_meta["mode"] == "incremental"

        # parent 未被覆盖 (append-only)
        back = aa.read(tmp_path, parent_art.attempt_id)
        assert back is not None
        assert back.parent_attempt_id is None  # parent 没有被修改

    @pytest.mark.asyncio
    async def test_continuity_append_only_on_cancelled(self, tmp_path, monkeypatch):
        """enable_continuity=True, 取消后 artifact 仍 append-only (新 attempt, 不覆盖 parent)。"""
        from app.services.trading import plan_check as pc
        from app.services.ai_structured import CancellationToken, GenerateResponse

        self._seed_plan(tmp_path, [self._complete_entry()])
        self._seed_profile(tmp_path)
        self._stub_frame(monkeypatch)

        def _gen_s1(messages, **kw):
            return GenerateResponse(data=json.dumps({
                "trend": "上升", "volatility": "中等", "liquidity": "充足",
                "readiness": "sufficient", "conflicts": [], "notes": [],
            }))

        def _gen_s2(messages, **kw):
            return GenerateResponse(data=json.dumps({"checks": [], "summary": "ok"}))

        parent_art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen_s1, stage2_generate=_gen_s2,
            enable_continuity=False,
        )

        token = CancellationToken()
        token.cancel()
        child_art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen_s1, stage2_generate=_gen_s2,
            cancel_token=token,
            enable_continuity=True,
        )
        # 取消 → 新 artifact, 不覆盖 parent
        assert child_art.attempt_id != parent_art.attempt_id
        back = aa.read(tmp_path, parent_art.attempt_id)
        assert back is not None
        assert back.status == "ok"  # parent 未变

    @pytest.mark.asyncio
    async def test_continuity_disabled_preserves_existing_behavior(self, tmp_path, monkeypatch):
        """enable_continuity 默认 False → 与现有行为完全一致 (无 continuity 字段)。"""
        from app.services.trading import plan_check as pc
        from app.services.ai_structured import GenerateResponse

        self._seed_plan(tmp_path, [self._complete_entry()])
        self._seed_profile(tmp_path)
        self._stub_frame(monkeypatch)

        def _gen_s1(messages, **kw):
            return GenerateResponse(data=json.dumps({
                "trend": "上升", "volatility": "中等", "liquidity": "充足",
                "readiness": "sufficient", "conflicts": [], "notes": [],
            }))

        def _gen_s2(messages, **kw):
            return GenerateResponse(data=json.dumps({"checks": [], "summary": "ok"}))

        art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen_s1, stage2_generate=_gen_s2,
        )
        assert art.parent_attempt_id is None
        assert "continuity" not in (art.result or {})
        assert art.status == "ok"

    @pytest.mark.asyncio
    async def test_continuity_result_has_no_forbidden_keys(self, tmp_path, monkeypatch):
        """continuity meta 递归校验无交易行动字段。"""
        from app.services.trading import plan_check as pc
        from app.services.ai_structured import GenerateResponse

        self._seed_plan(tmp_path, [self._complete_entry()])
        self._seed_profile(tmp_path)
        self._stub_frame(monkeypatch)

        def _gen_s1(messages, **kw):
            return GenerateResponse(data=json.dumps({
                "trend": "上升", "volatility": "中等", "liquidity": "充足",
                "readiness": "sufficient", "conflicts": [], "notes": [],
            }))

        def _gen_s2(messages, **kw):
            return GenerateResponse(data=json.dumps({"checks": [], "summary": "ok"}))

        art = await pc.run_plan_check(
            repo=None, data_dir=tmp_path,
            date=self.DATE, entry_id="e1",
            stage1_generate=_gen_s1, stage2_generate=_gen_s2,
            enable_continuity=True,
        )
        cont_meta = (art.result or {}).get("continuity")
        assert cont_meta is not None
        cont.assert_no_forbidden_keys(cont_meta)  # should not raise
