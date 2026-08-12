from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.strategy import delete_strategy
from app.services import preferences
from app.strategy import monitor_rules
from app.strategy.engine import StrategyEngine


def _strategy_code(strategy_id: str) -> str:
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}}
def filter(df, params):
    return pl.lit(True)
'''


class _MonitorEngine:
    def __init__(self) -> None:
        self.invalidations = 0
        self.rules: list[dict] = []

    def invalidate_strategy_state(self) -> None:
        self.invalidations += 1

    def set_rules(self, rules: list[dict]) -> None:
        self.rules = rules


def _request(data_dir: Path, engine: StrategyEngine, monitor: _MonitorEngine | None = None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    state = SimpleNamespace(repo=repo, strategy_engine=engine, monitor_engine=monitor)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_delete_strategy_is_not_blocked_by_another_broken_file(monkeypatch, tmp_path):
    custom_dir = tmp_path / "strategies" / "custom"
    custom_dir.mkdir(parents=True)
    strategy_path = custom_dir / "target.py"
    strategy_path.write_text(_strategy_code("target"), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[custom_dir])

    # 模拟用户目录中另有一份损坏策略。旧删除逻辑的全量 reload 会因此回滚并返回 500。
    broken_path = custom_dir / "broken.py"
    broken_path.write_text("this is not valid python", encoding="utf-8")
    with pytest.raises(ValueError, match="strategy reload failed"):
        engine.reload()
    assert engine.has("target")

    override_path = tmp_path / "user_data" / "strategy_overrides" / "target.json"
    override_path.parent.mkdir(parents=True)
    override_path.write_text("{}", encoding="utf-8")
    cache_path = tmp_path / "user_data" / "strategy_cache.json"
    cache_path.write_text("{}", encoding="utf-8")

    rule = monitor_rules.normalize({
        "id": "mr_target",
        "name": "策略监控 · target",
        "type": "strategy",
        "strategy_id": "target",
        "scope": "all",
        "conditions": [],
    })
    monitor_rules.save_one(tmp_path, rule)

    preference_updates: list[dict] = []
    monkeypatch.setattr(preferences, "get_strategy_monitor_ids", lambda: ["target", "other"])
    monkeypatch.setattr(
        preferences,
        "set_realtime_monitor_config",
        lambda config: preference_updates.append(config) or config,
    )
    monitor = _MonitorEngine()

    result = delete_strategy("target", _request(tmp_path, engine, monitor))

    assert result == {"ok": True, "warnings": []}
    assert not strategy_path.exists()
    assert broken_path.exists()
    assert not engine.has("target")
    assert not override_path.exists()
    assert not cache_path.exists()
    assert preference_updates == [{"strategy_monitor_ids": ["other"]}]
    saved_rule = monitor_rules.load_one(tmp_path, "mr_target")
    assert saved_rule is not None and saved_rule["enabled"] is False
    assert monitor.invalidations == 1
    assert monitor.rules and monitor.rules[0]["enabled"] is False


def test_delete_strategy_reports_read_only_volume_without_unregistering(monkeypatch, tmp_path):
    custom_dir = tmp_path / "strategies" / "custom"
    custom_dir.mkdir(parents=True)
    strategy_path = custom_dir / "target.py"
    strategy_path.write_text(_strategy_code("target"), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[custom_dir])
    request = _request(tmp_path, engine)

    original_unlink = Path.unlink

    def blocked_unlink(path: Path, *args, **kwargs):
        if path == strategy_path:
            raise PermissionError(30, "Read-only file system", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)

    with pytest.raises(HTTPException) as exc_info:
        delete_strategy("target", request)

    assert exc_info.value.status_code == 409
    assert "Docker" in exc_info.value.detail
    assert "只读挂载" in exc_info.value.detail
    assert strategy_path.exists()
    assert engine.has("target")
