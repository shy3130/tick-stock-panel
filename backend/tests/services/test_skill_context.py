import json

import pytest

from app.services import skill_context


def test_load_by_scenario():
    text = skill_context.load_skill_context("trade_journal")

    assert "以下为本地方法论" in text
    assert "交易流水复盘" in text


def test_reject_path_escape(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "index.json").write_text(json.dumps([{"path": "../x.md", "scenarios": ["x"]}]), encoding="utf-8")
    monkeypatch.setattr(skill_context, "ROOT", root)

    with pytest.raises(ValueError):
        skill_context.load_skill_context("x")


def test_total_truncation(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "a.md").write_text("a" * 100, encoding="utf-8")
    (root / "index.json").write_text(json.dumps([{"path": "a.md", "scenarios": ["x"], "max_chars": 100}]), encoding="utf-8")
    monkeypatch.setattr(skill_context, "ROOT", root)

    text = skill_context.load_skill_context("x", max_chars=10)

    assert text.endswith("a" * 10)


def test_unknown_scenario_empty():
    assert skill_context.load_skill_context("missing") == ""


def test_safe_loader_warns_and_returns_empty(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "index.json").write_text(json.dumps([{"path": "../x.md", "scenarios": ["x"]}]), encoding="utf-8")
    monkeypatch.setattr(skill_context, "ROOT", root)
    warnings = []

    assert skill_context.load_skill_context_safe("x", warnings=warnings) == ""
    assert warnings == ["方法论库加载失败: x"]


def test_a_share_filtering_terms():
    text = skill_context.load_skill_context("trade_journal") + skill_context.load_skill_context("market_recap")
    banned = ["crypto", "DeFi", "options", "券商实盘连接"]

    assert not any(term in text for term in banned)


def test_index_covers_planned_topics():
    ids = {item["id"] for item in skill_context._load_index()}

    assert {
        "market-recap",
        "sector-rotation",
        "trade-journal",
        "shadow-account",
        "alpha-zoo",
        "factor-research",
        "risk-analysis",
        "backtest-diagnose",
        "technical-basic",
        "candlestick",
        "multi-factor",
        "market-microstructure",
    } <= ids
