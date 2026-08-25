"""自定义信号 AI 草稿 API 聚焦契约测试（不触发真实 provider）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import signals
from app.services.ai_signal_draft import CustomSignalDraftError, CustomSignalDraftRequest


def _request(data_dir: Path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)))
        )
    )


@pytest.mark.asyncio
async def test_ai_draft_api_returns_only_draft_rationale_meta_and_does_not_write(
    monkeypatch, tmp_path
):
    before = sorted(tmp_path.rglob("*"))

    async def fake_generate(text, profile_id):
        return {
            "draft": {
                "id": "close_high",
                "name": "收盘强势",
                "kind": "entry",
                "conditions": [{"left": "close", "op": ">", "right": "10"}],
            },
            "rationale": "literal",
            "ai_meta": {"profile_id": profile_id, "provider": "test", "usage": {"total_tokens": 1}},
        }

    monkeypatch.setattr(signals, "generate_custom_signal_draft", fake_generate)
    result = await signals.ai_draft_custom_signal(
        CustomSignalDraftRequest(text="收盘大于10", profile_id="p1"), _request(tmp_path)
    )
    assert set(result) == {"draft", "rationale", "ai_meta"}
    assert result["draft"]["id"] == "close_high"
    assert result["ai_meta"]["profile_id"] == "p1"
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.asyncio
async def test_ai_draft_api_maps_provider_unavailable_to_503(monkeypatch, tmp_path):
    async def unavailable(text, profile_id):
        raise CustomSignalDraftError("provider_unavailable")

    monkeypatch.setattr(signals, "generate_custom_signal_draft", unavailable)
    with pytest.raises(HTTPException) as exc:
        await signals.ai_draft_custom_signal(
            CustomSignalDraftRequest(text="测试"), _request(tmp_path)
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_ai_draft_api_suffixes_conflicting_id_only_in_response(monkeypatch, tmp_path):
    async def fake_generate(text, profile_id):
        return {
            "draft": {
                "id": "same",
                "name": "x",
                "kind": "both",
                "conditions": [{"left": "close", "op": ">", "right": "0"}],
            },
            "rationale": None,
            "ai_meta": {},
        }

    monkeypatch.setattr(signals, "generate_custom_signal_draft", fake_generate)
    monkeypatch.setattr(signals.custom_signals, "load_all", lambda _: [{"id": "same"}])
    result = await signals.ai_draft_custom_signal(
        CustomSignalDraftRequest(text="测试"), _request(tmp_path)
    )
    assert result["draft"]["id"] == "same_1"
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_ai_draft_api_ignores_non_object_files_but_keeps_valid_id_conflicts(
    monkeypatch, tmp_path
):
    async def fake_generate(text, profile_id):
        return {
            "draft": {
                "id": "same",
                "name": "x",
                "kind": "both",
                "conditions": [{"left": "close", "op": ">", "right": "0"}],
            },
            "rationale": None,
            "ai_meta": {},
        }

    monkeypatch.setattr(signals, "generate_custom_signal_draft", fake_generate)
    monkeypatch.setattr(
        signals.custom_signals,
        "load_all",
        lambda _: ["broken", {"id": "same"}],
    )

    result = await signals.ai_draft_custom_signal(
        CustomSignalDraftRequest(text="测试"),
        _request(tmp_path),
    )

    assert result["draft"]["id"] == "same_1"


@pytest.mark.asyncio
async def test_ai_draft_api_fails_closed_when_conflict_store_is_unreadable(monkeypatch, tmp_path):
    async def fake_generate(text, profile_id):
        return {
            "draft": {
                "id": "same",
                "name": "x",
                "kind": "both",
                "conditions": [{"left": "close", "op": ">", "right": "0"}],
            },
            "rationale": None,
            "ai_meta": {},
        }

    def fail_load(_):
        raise PermissionError("denied")

    monkeypatch.setattr(signals, "generate_custom_signal_draft", fake_generate)
    monkeypatch.setattr(signals.custom_signals, "load_all", fail_load)

    with pytest.raises(HTTPException) as exc:
        await signals.ai_draft_custom_signal(
            CustomSignalDraftRequest(text="测试"),
            _request(tmp_path),
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "custom_signal_store_unavailable"
