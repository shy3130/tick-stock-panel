"""证据新鲜度判定测试 — 覆盖 fresh / stale / missing / unknown / 聚合 / 确定性。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from app.services.trading.evidence_freshness import (
    ACTION_COLLECT,
    ACTION_INVESTIGATE,
    ACTION_REFRESH,
    ACTION_USE,
    AggregateVerdict,
    EvidenceItem,
    FreshnessVerdict,
    VERDICT_FRESH,
    VERDICT_MISSING,
    VERDICT_STALE,
    VERDICT_UNKNOWN,
    assess_evidence,
    assess_evidences,
)

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


# ── 辅助 ───────────────────────────────────────────────────
def _ev(**kw) -> EvidenceItem:
    """快捷构造 EvidenceItem, 默认 id='e1'。"""
    return EvidenceItem(id=kw.pop("id", "e1"), **kw)


# ── fresh ──────────────────────────────────────────────────
class TestFresh:
    def test_fresh_within_ttl(self):
        """as_of 在 TTL 窗口内 → fresh, required_action=use。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(minutes=30), source="radar", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_FRESH
        assert v.required_action == ACTION_USE
        assert v.usable_for_action is True
        assert v.age_seconds == pytest.approx(1800)
        assert v.as_of is not None
        assert v.source == "radar"

    def test_fresh_at_ttl_boundary(self):
        """age 恰好等于 TTL → 仍然 fresh (≤)。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(seconds=60), source="s1", ttl_seconds=60),
            now=NOW,
        )
        assert v.verdict == VERDICT_FRESH
        assert v.age_seconds == pytest.approx(60)

    def test_fresh_with_required_fields_present(self):
        """必填字段全部存在 → fresh。"""
        v = assess_evidence(
            _ev(
                as_of=NOW - timedelta(minutes=5),
                source="provider",
                ttl_seconds=3600,
                required_fields=["close", "volume"],
                fields={"close": 10.5, "volume": 1000},
            ),
            now=NOW,
        )
        assert v.verdict == VERDICT_FRESH
        assert v.missing_fields == []

    def test_fresh_accepts_iso_string(self):
        """ISO 字符串 as_of 正常解析。"""
        v = assess_evidence(
            _ev(as_of="2026-08-14T11:30:00+00:00", source="s", ttl_seconds=7200),
            now=NOW,
        )
        assert v.verdict == VERDICT_FRESH

    def test_fresh_naive_datetime_treated_as_utc(self):
        """naive datetime 视为 UTC, 能正确判定。"""
        v = assess_evidence(
            _ev(as_of=datetime(2026, 8, 14, 11, 0), source="s", ttl_seconds=7200),
            now=NOW,
        )
        assert v.verdict == VERDICT_FRESH


# ── stale (过期) ───────────────────────────────────────────
class TestStale:
    def test_stale_beyond_ttl(self):
        """age > TTL → stale, required_action=refresh。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(hours=2), source="s1", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_STALE
        assert v.required_action == ACTION_REFRESH
        assert v.usable_for_action is False
        assert v.age_seconds == pytest.approx(7200)

    def test_stale_one_second_over(self):
        """超 TTL 一秒 → stale。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(seconds=61), source="s", ttl_seconds=60),
            now=NOW,
        )
        assert v.verdict == VERDICT_STALE

    def test_stale_reason_contains_age_and_ttl(self):
        """reason 包含 age 和 TTL 数值, 便于排查。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(hours=3), source="s", ttl_seconds=3600),
            now=NOW,
        )
        assert "TTL" in v.reason
        assert "过期" in v.reason


# ── missing (缺字段 / 缺 source / 缺 as_of) ────────────────
class TestMissing:
    def test_source_missing(self):
        """source 缺失 → missing, required_action=collect。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(minutes=5), ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_MISSING
        assert v.required_action == ACTION_COLLECT
        assert v.usable_for_action is False
        assert "来源" in v.reason

    def test_source_empty_string(self):
        """source 为空字符串 → missing。"""
        v = assess_evidence(
            _ev(as_of=NOW, source="   ", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_MISSING

    def test_as_of_missing(self):
        """as_of 缺失 → missing。"""
        v = assess_evidence(
            _ev(source="s", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_MISSING
        assert "as_of" in v.reason
        assert v.age_seconds is None

    def test_required_field_missing(self):
        """必填字段缺失 → missing, 报告缺失字段。"""
        v = assess_evidence(
            _ev(
                as_of=NOW - timedelta(minutes=5),
                source="s",
                ttl_seconds=3600,
                required_fields=["close", "volume", "open"],
                fields={"close": 10.0, "volume": None},  # volume=None, open 缺失
            ),
            now=NOW,
        )
        assert v.verdict == VERDICT_MISSING
        assert sorted(v.missing_fields) == ["open", "volume"]
        assert v.required_action == ACTION_COLLECT

    def test_fields_none_with_required(self):
        """fields=None 但 required_fields 非空 → 全部 missing。"""
        v = assess_evidence(
            _ev(
                as_of=NOW,
                source="s",
                ttl_seconds=3600,
                required_fields=["a", "b"],
                fields=None,
            ),
            now=NOW,
        )
        assert v.verdict == VERDICT_MISSING
        assert sorted(v.missing_fields) == ["a", "b"]

    def test_zero_value_counts_as_present(self):
        """字段值为 0 或空字符串 → 视为存在 (仅 None / 不存在算缺失)。"""
        v = assess_evidence(
            _ev(
                as_of=NOW,
                source="s",
                ttl_seconds=3600,
                required_fields=["x"],
                fields={"x": 0},
            ),
            now=NOW,
        )
        assert v.verdict == VERDICT_FRESH

    def test_source_takes_priority_over_as_of(self):
        """source 和 as_of 同时缺失 → missing (先报 source)。"""
        v = assess_evidence(_ev(ttl_seconds=3600), now=NOW)
        assert v.verdict == VERDICT_MISSING
        assert "来源" in v.reason


# ── unknown (不可判定) ─────────────────────────────────────
class TestUnknown:
    def test_future_as_of(self):
        """as_of 晚于 now → unknown (未来时间, 可疑)。"""
        v = assess_evidence(
            _ev(as_of=NOW + timedelta(hours=1), source="s", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_UNKNOWN
        assert v.required_action == ACTION_INVESTIGATE
        assert v.usable_for_action is False
        assert v.as_of is not None

    def test_unparseable_as_of(self):
        """as_of 格式无法解析 → unknown。"""
        v = assess_evidence(
            _ev(as_of="not-a-date", source="s", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_UNKNOWN
        assert "不可解析" in v.reason

    def test_ttl_missing(self):
        """TTL 缺失 → unknown (无新鲜度规则)。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(minutes=5), source="s"),
            now=NOW,
        )
        assert v.verdict == VERDICT_UNKNOWN
        assert "TTL" in v.reason

    def test_ttl_zero_or_negative(self):
        """TTL ≤ 0 → unknown (无效规则)。"""
        for bad_ttl in (0, -1, -0.01):
            v = assess_evidence(
                _ev(as_of=NOW - timedelta(minutes=5), source="s", ttl_seconds=bad_ttl),
                now=NOW,
            )
            assert v.verdict == VERDICT_UNKNOWN, f"ttl={bad_ttl}"

    def test_ttl_bool_rejected(self):
        """TTL 为 bool → unknown (True 会被 float() 接受但语义错误)。"""
        v = assess_evidence(
            _ev(as_of=NOW, source="s", ttl_seconds=True),
            now=NOW,
        )
        assert v.verdict == VERDICT_UNKNOWN

    def test_as_of_empty_string(self):
        """as_of 为空字符串 → missing (走 as_of 缺失分支)。"""
        v = assess_evidence(
            _ev(as_of="", source="s", ttl_seconds=3600),
            now=NOW,
        )
        assert v.verdict == VERDICT_MISSING


# ── 聚合多证据 ─────────────────────────────────────────────
class TestAggregate:
    def test_all_fresh(self):
        """全部 fresh → 聚合 fresh, usable=True。"""
        items = [
            _ev(id="a", as_of=NOW - timedelta(minutes=5), source="s", ttl_seconds=3600),
            _ev(id="b", as_of=NOW - timedelta(minutes=10), source="s", ttl_seconds=3600),
        ]
        agg = assess_evidences(items, now=NOW)
        assert agg.verdict == VERDICT_FRESH
        assert agg.required_action == ACTION_USE
        assert agg.usable_for_action is True
        assert agg.total == 2
        assert agg.by_verdict == {VERDICT_FRESH: 2}
        assert agg.worst_items == []

    def test_mixed_fresh_and_stale(self):
        """混合 fresh + stale → 聚合 stale (最保守)。"""
        items = [
            _ev(id="fresh", as_of=NOW - timedelta(minutes=5), source="s", ttl_seconds=3600),
            _ev(id="stale", as_of=NOW - timedelta(hours=2), source="s", ttl_seconds=3600),
        ]
        agg = assess_evidences(items, now=NOW)
        assert agg.verdict == VERDICT_STALE
        assert agg.usable_for_action is False
        assert agg.worst_items == ["stale"]
        assert agg.by_verdict == {VERDICT_FRESH: 1, VERDICT_STALE: 1}

    def test_missing_dominates_stale(self):
        """missing 严重度高于 stale → 聚合 missing。"""
        items = [
            _ev(id="stale", as_of=NOW - timedelta(hours=2), source="s", ttl_seconds=3600),
            _ev(id="miss", source=None, as_of=NOW, ttl_seconds=3600),
        ]
        agg = assess_evidences(items, now=NOW)
        assert agg.verdict == VERDICT_MISSING
        assert agg.worst_items == ["miss"]

    def test_unknown_dominates_stale(self):
        """unknown 严重度高于 stale → 聚合 unknown。"""
        items = [
            _ev(id="stale", as_of=NOW - timedelta(hours=2), source="s", ttl_seconds=3600),
            _ev(id="unk", as_of=NOW, source="s"),  # no TTL
        ]
        agg = assess_evidences(items, now=NOW)
        assert agg.verdict == VERDICT_UNKNOWN

    def test_empty_list(self):
        """空证据列表 → unknown (fail-closed)。"""
        agg = assess_evidences([], now=NOW)
        assert agg.verdict == VERDICT_UNKNOWN
        assert agg.usable_for_action is False
        assert agg.total == 0

    def test_single_missing_blocks_all(self):
        """一条 missing 阻断全部 → usable=False。"""
        items = [
            _ev(id="ok", as_of=NOW - timedelta(minutes=1), source="s", ttl_seconds=3600),
            _ev(id="bad", source=None),
        ]
        agg = assess_evidences(items, now=NOW)
        assert agg.usable_for_action is False
        assert agg.verdict == VERDICT_MISSING

    def test_details_preserved(self):
        """聚合结果保留个体明细。"""
        items = [_ev(id="x", as_of=NOW, source="s", ttl_seconds=3600)]
        agg = assess_evidences(items, now=NOW)
        assert len(agg.details) == 1
        assert isinstance(agg.details[0], FreshnessVerdict)
        assert agg.details[0].id == "x"


# ── 确定性 ─────────────────────────────────────────────────
class TestDeterminism:
    def test_same_input_same_verdict(self):
        """相同输入 + 相同 now → 完全相同的 verdict (含 age)。"""
        item = _ev(as_of=NOW - timedelta(minutes=10), source="src", ttl_seconds=3600)
        v1 = assess_evidence(item, now=NOW)
        v2 = assess_evidence(item, now=NOW)
        assert v1 == v2
        assert v1.verdict == v2.verdict
        assert v1.age_seconds == v2.age_seconds

    def test_deterministic_across_many_items(self):
        """多次调用聚合, 结果一致。"""
        items = [
            _ev(id=f"e{i}", as_of=NOW - timedelta(minutes=i), source="s", ttl_seconds=3600)
            for i in range(5)
        ]
        a1 = assess_evidences(items, now=NOW)
        a2 = assess_evidences(items, now=NOW)
        assert a1.verdict == a2.verdict
        assert a1.by_verdict == a2.by_verdict

    def test_dict_input_equivalent_to_dataclass(self):
        """dict 输入与等价 dataclass 输入产出相同 verdict。"""
        dc = EvidenceItem(id="d", as_of=NOW - timedelta(minutes=5), source="s", ttl_seconds=3600)
        d = {"id": "d", "as_of": NOW - timedelta(minutes=5), "source": "s", "ttl_seconds": 3600}
        assert assess_evidence(dc, now=NOW) == assess_evidence(d, now=NOW)

    def test_camelcase_dict_keys_supported(self):
        """camelCase 键 (asOf / ttlSeconds / requiredFields) 也被识别。"""
        d = {"id": "c", "asOf": "2026-08-14T11:00:00+00:00", "source": "s", "ttlSeconds": 7200}
        v = assess_evidence(d, now=NOW)
        assert v.verdict == VERDICT_FRESH


# ── 序列化 / 不可变 / provenance ──────────────────────────
class TestSerializationAndProvenance:
    def test_verdict_immutable(self):
        """FreshnessVerdict 不可变 (frozen)。"""
        v = assess_evidence(
            _ev(as_of=NOW, source="s", ttl_seconds=3600), now=NOW
        )
        with pytest.raises(FrozenInstanceError):
            v.verdict = "tampered"  # type: ignore[misc]

    def test_to_dict_roundtrip(self):
        """to_dict 序列化后包含全部 provenance 字段。"""
        v = assess_evidence(
            _ev(as_of=NOW - timedelta(minutes=5), source="radar", ttl_seconds=3600),
            now=NOW,
        )
        d = v.to_dict()
        for key in ("id", "verdict", "reason", "age_seconds", "required_action",
                     "source", "as_of", "ttl_seconds", "missing_fields"):
            assert key in d
        assert d["source"] == "radar"

    def test_aggregate_to_dict_serializes_details(self):
        """AggregateVerdict.to_dict 递归序列化 details。"""
        agg = assess_evidences(
            [_ev(id="a", as_of=NOW, source="s", ttl_seconds=3600)],
            now=NOW,
        )
        d = agg.to_dict()
        assert isinstance(d["details"], list)
        assert d["details"][0]["id"] == "a"

    def test_as_of_normalized_to_utc_iso(self):
        """as_of 输出为 UTC ISO 字符串。"""
        v = assess_evidence(
            _ev(as_of=datetime(2026, 8, 14, 4, 0), source="s", ttl_seconds=36000),
            now=NOW,
        )
        assert v.as_of is not None
        assert v.as_of.endswith("+00:00")
        # naive 04:00 → UTC 04:00
        assert "T04:00:00" in v.as_of


# ── fail-closed 不变量 ────────────────────────────────────
class TestFailClosed:
    """非 fresh verdict → required_action 永不为 use。"""

    @pytest.mark.parametrize(
        ("as_of", "source", "ttl", "expected_verdict"),
        [
            (NOW - timedelta(hours=2), "s", 3600, VERDICT_STALE),
            (None, "s", 3600, VERDICT_MISSING),
            (NOW, None, 3600, VERDICT_MISSING),
            (NOW + timedelta(hours=1), "s", 3600, VERDICT_UNKNOWN),
            (NOW, "s", None, VERDICT_UNKNOWN),
            ("bad", "s", 3600, VERDICT_UNKNOWN),
        ],
    )
    def test_non_fresh_never_use(self, as_of, source, ttl, expected_verdict):
        item = _ev(as_of=as_of, source=source, ttl_seconds=ttl)
        v = assess_evidence(item, now=NOW)
        assert v.verdict == expected_verdict
        assert v.required_action != ACTION_USE
        assert v.usable_for_action is False
