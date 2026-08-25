"""M15+M16 analysis artifact store 契约测试。

覆盖：安全 builder、append-only 持久化、失败队列隔离、路径安全、显式重放计划
（不执行 AI / 不写 trade event）、不泄漏 prompt/secret。

按验收要求：本文件不运行（仅校验存在与契约覆盖）。运行用 ``pytest backend/tests/services/test_analysis_artifacts.py``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import analysis_artifacts as aa
from app.services.ai_structured import (
    AIErrorDetails,
    AIUsage,
    AnalysisArtifact,
    StructuredAIResult,
    new_attempt_id,
    new_request_id,
)

_AS_OF = datetime(2026, 8, 5, tzinfo=timezone.utc)


# ── 构造辅助 ──────────────────────────────────────────────
def _result(
    *,
    status: str = "ok",
    purpose: str = "stock_analysis",
    data: object | None = None,
    raw_text: str = "SECRET-PROMPT-BODY",
    error: AIErrorDetails | None = None,
    model: str = "gpt-x",
    profile_id: str = "prof-primary",
    usage: AIUsage | None = None,
    warnings: list[str] | None = None,
) -> StructuredAIResult:
    return StructuredAIResult(
        request_id=new_request_id(),
        attempt_id=new_attempt_id(),
        status=status,  # type: ignore[arg-type]
        purpose=purpose,
        provider="openai_compat",
        profile_id=profile_id,
        model=model,
        data=data,
        raw_text=raw_text,
        usage=usage or AIUsage(prompt_tokens=12, completion_tokens=7, total_tokens=19),
        error=error,
        warnings=warnings or [],
    )


def _failed(
    category: str = "invalid",
    purpose: str = "stock_analysis",
    **kwargs: object,
) -> StructuredAIResult:
    return _result(
        status="failed",
        purpose=purpose,
        data=None,
        error=AIErrorDetails(category=category, message="boom"),  # type: ignore[arg-type]
        **kwargs,
    )


# ── 安全 builder ──────────────────────────────────────────
def test_build_ok_keeps_structured_data_drops_raw_text():
    res = _result(data={"verdict": "buy", "score": 8})
    art = aa.build_artifact(res, symbol="600519.SH", market="a_share", adjustment="qfq", data_as_of=_AS_OF)
    assert art.status == "ok"
    assert art.result == {"verdict": "buy", "score": 8}
    dumped = art.model_dump_json()
    assert "SECRET-PROMPT-BODY" not in dumped
    assert "raw_text" not in dumped
    assert art.symbol == "600519.SH" and art.adjustment == "qfq"


def test_build_drops_non_dict_data():
    art = aa.build_artifact(_result(data=["not", "a", "dict"]))
    assert art.result is None


def test_build_failed_carries_structured_error():
    art = aa.build_artifact(_failed("quota"))
    assert art.status == "failed"
    assert art.error is not None and art.error.category == "quota"
    assert art.result is None


def test_build_does_not_accept_messages_or_credentials():
    # builder 签名不接受 messages/api_key；构造出的 artifact 不含敏感字段
    art = aa.build_artifact(_result())
    dumped = art.model_dump_json().lower()
    for forbidden in ("messages", "api_key", "apikey", "content_hash", "authorization"):
        assert forbidden not in dumped


# ── 持久化：ok / failed / cancelled ───────────────────────
def test_record_ok_writes_attempts_not_failed(tmp_path: Path):
    art = aa.record_result(tmp_path, _result(), symbol="000001.SZ", data_as_of=_AS_OF)
    assert (tmp_path / "user_data" / "ai_attempts" / "attempts" / f"{art.attempt_id}.json").exists()
    assert not list((tmp_path / "user_data" / "ai_attempts" / "failed").glob("*.json"))
    assert len(aa.read_index(tmp_path)) == 1


def test_record_failed_writes_attempts_and_failed_copy(tmp_path: Path):
    art = aa.record_result(tmp_path, _failed("invalid"))
    base = tmp_path / "user_data" / "ai_attempts"
    assert (base / "attempts" / f"{art.attempt_id}.json").exists()
    assert (base / "failed" / f"{art.attempt_id}.json").exists()


def test_record_cancelled_has_no_failed_copy(tmp_path: Path):
    art = aa.record_result(tmp_path, _result(status="cancelled"))
    base = tmp_path / "user_data" / "ai_attempts"
    assert (base / "attempts" / f"{art.attempt_id}.json").exists()
    assert not list((base / "failed").glob("*.json"))


def test_failed_dir_only_contains_failures(tmp_path: Path):
    aa.record_result(tmp_path, _result())               # ok
    aa.record_result(tmp_path, _result(status="cancelled"))
    failed_art = aa.record_result(tmp_path, _failed("syntax"))
    failed_dir = tmp_path / "user_data" / "ai_attempts" / "failed"
    files = sorted(f.name for f in failed_dir.glob("*.json"))
    assert files == [f"{failed_art.attempt_id}.json"]


# ── append-only / 不可覆盖 ────────────────────────────────
def test_index_is_append_only_never_overwrites(tmp_path: Path):
    idx = tmp_path / "user_data" / "ai_attempts" / "index.jsonl"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text('{"seed": true, "attempt_id": "preexisting"}\n', encoding="utf-8")

    a1 = aa.record_result(tmp_path, _result())
    a2 = aa.record_result(tmp_path, _failed("missing"))

    lines = [l for l in idx.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3  # 种子行 + 2 条新行，绝不覆盖
    import json as _json
    first = _json.loads(lines[0])
    assert first["seed"] is True  # 历史行原样保留
    assert {a1.attempt_id, a2.attempt_id} == {_json.loads(l)["attempt_id"] for l in lines[1:]}


def test_record_refuses_to_overwrite_history(tmp_path: Path):
    art = aa.record_result(tmp_path, _result())
    snapshot = aa.read(tmp_path, art.attempt_id)
    assert snapshot is not None
    with pytest.raises(aa.ArtifactExistsError):
        aa.record(tmp_path, art)  # 同 attempt_id 再次写入必须拒绝
    # 原文件未被改写
    again = aa.read(tmp_path, art.attempt_id)
    assert again is not None and again.created_at == snapshot.created_at


def test_no_tmp_file_left_after_record(tmp_path: Path):
    aa.record_result(tmp_path, _result())
    attempts_dir = tmp_path / "user_data" / "ai_attempts" / "attempts"
    assert not list(attempts_dir.glob("*.tmp"))


# ── 读 / 列 / 检索 ────────────────────────────────────────
def test_read_returns_artifact_and_unknown_is_none(tmp_path: Path):
    art = aa.record_result(tmp_path, _result(), symbol="600519.SH")
    got = aa.read(tmp_path, art.attempt_id)
    assert got is not None and got.attempt_id == art.attempt_id and got.symbol == "600519.SH"
    assert aa.read(tmp_path, "att_does_not_exist") is None


def test_list_failed_filters_by_category_and_purpose(tmp_path: Path):
    aa.record_result(tmp_path, _failed("invalid", purpose="stock_analysis"))
    aa.record_result(tmp_path, _failed("quota", purpose="stock_analysis"))
    aa.record_result(tmp_path, _failed("syntax", purpose="strategy_review"))
    aa.record_result(tmp_path, _result())  # ok 不进失败队列

    assert len(aa.list_failed(tmp_path)) == 3
    assert len(aa.list_failed(tmp_path, category="quota")) == 1
    assert len(aa.list_failed(tmp_path, purpose="strategy_review")) == 1
    assert {a.error.category for a in aa.list_failed(tmp_path, category="invalid")} == {"invalid"}


def test_list_artifacts_status_filter(tmp_path: Path):
    aa.record_result(tmp_path, _result())
    aa.record_result(tmp_path, _failed("invalid"))
    aa.record_result(tmp_path, _result(status="cancelled"))
    assert len(aa.list_artifacts(tmp_path)) == 3
    assert len(aa.list_artifacts(tmp_path, status="ok")) == 1
    assert len(aa.list_artifacts(tmp_path, status="failed")) == 1


def test_read_index_is_safe_projection(tmp_path: Path):
    aa.record_result(tmp_path, _result(data={"secret_in_output": "no"}))
    lines = aa.read_index(tmp_path)
    assert len(lines) == 1
    line = lines[0]
    assert line["status"] == "ok"
    assert "result" not in line and "raw_text" not in line and "messages" not in line
    assert line["has_result"] is True
    assert line["usage"]["total_tokens"] == 19


# ── 落盘内容不泄漏 secret ─────────────────────────────────
def test_persisted_files_contain_no_prompt_or_secret(tmp_path: Path):
    res = _result(data={"summary": "ok"}, raw_text="ULTRA-SECRET-RAW")
    art = aa.record_result(tmp_path, res)
    raw = (tmp_path / "user_data" / "ai_attempts" / "attempts" / f"{art.attempt_id}.json").read_text("utf-8")
    low = raw.lower()
    assert "ultra-secret-raw" not in low
    for forbidden in ("raw_text", "messages", "api_key", "apikey", "authorization", "prompt_body"):
        assert forbidden not in low


# ── 显式重放计划 ─────────────────────────────────────────
def test_replay_plan_content_failure_is_replayable_with_fresh_attempt(tmp_path: Path):
    failed = aa.record_result(tmp_path, _failed("invalid", model="gpt-old"), data_as_of=_AS_OF)
    plan = aa.replay_plan(failed)
    assert plan is not None
    assert plan.replayable is True
    assert plan.new_attempt_id != failed.attempt_id            # 新 attempt
    assert plan.parent_attempt_id == failed.attempt_id          # 关联原 attempt（旧结果不覆盖）
    assert plan.must_refresh_data is True                       # 必须刷新数据
    assert plan.data_as_of == _AS_OF.isoformat()                # 标记旧值已过期
    assert "gpt-old" in plan.profile_change_hint                 # 提示 model 可能已变化
    assert "不执行" in plan.note and "交易事件" in plan.note


def test_replay_plan_quota_not_replayable(tmp_path: Path):
    plan = aa.replay_plan(aa.record_result(tmp_path, _failed("quota")))
    assert plan is not None and plan.replayable is False
    assert "quota" in (plan.reason or "").lower()


def test_replay_plan_provider_auth_not_replayable(tmp_path: Path):
    # auth 无独立分类，归入 provider
    plan = aa.replay_plan(aa.record_result(tmp_path, _failed("provider")))
    assert plan is not None and plan.replayable is False
    assert "provider" in (plan.reason or "").lower() or "auth" in (plan.reason or "").lower()


def test_replay_plan_non_failed_returns_none(tmp_path: Path):
    ok = aa.record_result(tmp_path, _result())
    cancelled = aa.record_result(tmp_path, _result(status="cancelled"))
    assert aa.replay_plan(ok) is None
    assert aa.replay_plan(cancelled) is None


def test_replay_plan_failed_without_error_returns_none():
    # failed 但缺少 error 分类 —— 无法判定，不返回计划
    art = aa.build_artifact(_result(status="failed"))
    assert art.error is None
    assert aa.replay_plan(art) is None
    assert aa.is_replayable(art) is False


def test_is_replayable_predicate_matches_plan(tmp_path: Path):
    invalid = aa.record_result(tmp_path, _failed("invalid"))
    quota = aa.record_result(tmp_path, _failed("quota"))
    assert aa.is_replayable(invalid) is True
    assert aa.is_replayable(quota) is False
    assert aa.is_replayable(aa.record_result(tmp_path, _result())) is False  # ok


# ── 路径安全 ──────────────────────────────────────────────
def test_record_rejects_path_traversal_attempt_id(tmp_path: Path):
    evil = AnalysisArtifact(
        id="art_evil",
        attempt_id="../evil",
        request_id="req-1",
        purpose="stock_analysis",
        status="ok",
    )
    with pytest.raises(aa.ArtifactError):
        aa.record(tmp_path, evil)
    # 没有任何文件逃逸到 ai_attempts 之外
    assert not (tmp_path / "evil.json").exists()


def test_read_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(aa.ArtifactError):
        aa.read(tmp_path, "..%2fevil")
    with pytest.raises(aa.ArtifactError):
        aa.read(tmp_path, "att_x/../../etc")


def test_safe_id_rejects_unsafe_characters(tmp_path: Path):
    for bad in ("", "a/b", "a\\b", "..", "a b", "a:b", "a\x00b"):
        with pytest.raises(aa.ArtifactError):
            aa.record(
                tmp_path,
                AnalysisArtifact(
                    id="art_x", attempt_id=bad, request_id="r", purpose="p", status="ok"
                ),
            )


# ── schema 校验 ──────────────────────────────────────────
def test_validate_roundtrips_good_artifact(tmp_path: Path):
    art = aa.record_result(tmp_path, _result(data={"k": 1}))
    roundtrip = aa.validate(art)
    assert roundtrip.attempt_id == art.attempt_id
    assert roundtrip.result == {"k": 1}


def test_read_revalidates_schema(tmp_path: Path):
    art = aa.record_result(tmp_path, _result())
    # 篡改落盘文件为非法 status —— 读回时 model_validate 应拒绝
    p = tmp_path / "user_data" / "ai_attempts" / "attempts" / f"{art.attempt_id}.json"
    import json as _json

    obj = _json.loads(p.read_text("utf-8"))
    obj["status"] = "bogus"
    p.write_text(_json.dumps(obj), encoding="utf-8")
    with pytest.raises(Exception):
        aa.read(tmp_path, art.attempt_id)


# ── 结构性保证：重放路径不执行 AI / 不写 trade event ───────
def test_module_has_no_provider_or_trade_dependency():
    src = Path(aa.__file__).read_text("utf-8")
    for forbidden in (
        "generate_ai_text",
        "trade_events",
        "append_event",
        "append_audit",
        "decision_audit",
        "import httpx",
        "import requests",
        "import aiohttp",
        "from app.services.trading",
        "from app.services.ai_provider",
    ):
        assert forbidden not in src, f"analysis_artifacts 不应依赖 {forbidden!r}（重放不得执行 AI / 写交易事件）"
