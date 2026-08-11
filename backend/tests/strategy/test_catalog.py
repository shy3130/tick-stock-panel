from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.api import screener as screener_api
from app.api import strategy as strategy_api
from app.strategy import catalog as strategy_catalog
from app.strategy.engine import StrategyEngine


def _builtin_engine() -> StrategyEngine:
    builtin_dir = Path(strategy_catalog.__file__).parent / "builtin"
    return StrategyEngine(strategy_dirs=[builtin_dir])


def _request(tmp_path, engine: StrategyEngine):
    store = SimpleNamespace(data_dir=tmp_path)
    repo = SimpleNamespace(store=store)
    state = SimpleNamespace(strategy_engine=engine, repo=repo)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_builtin_catalog_is_complete_and_defaults_to_three_core_strategies():
    engine = _builtin_engine()
    metadata = engine.list_strategies()
    ids = {item["id"] for item in metadata}

    assert not engine.load_errors()
    assert ids == strategy_catalog.BUILTIN_STRATEGY_IDS
    assert len(ids) == 22
    assert {
        item["id"]
        for item in metadata
        if strategy_catalog.include_strategy(item, include_experimental=False)
    } == strategy_catalog.CORE_STRATEGY_IDS
    assert strategy_catalog.CORE_STRATEGY_IDS == {
        "bullish_alignment",
        "trend_breakout",
        "pullback_to_support",
    }
    assert len(strategy_catalog.CORE_STRATEGY_IDS) == 3
    assert {
        item["evidence_status"]
        for item in metadata
        if item["id"] in strategy_catalog.CORE_STRATEGY_IDS
    } == {"historical_replay_failed"}


def test_strategy_list_apis_hide_non_core_builtins_but_keep_direct_access(tmp_path):
    engine = _builtin_engine()
    request = _request(tmp_path, engine)

    primary = strategy_api.list_strategies(
        request,
        include_experimental=False,
    )
    compatibility = screener_api.strategies(
        request,
        asset_type="stock",
        timeframe="1d",
        include_experimental=False,
    )
    assert {item["id"] for item in primary["strategies"]} == strategy_catalog.CORE_STRATEGY_IDS
    assert {item["id"] for item in compatibility["presets"]} == strategy_catalog.CORE_STRATEGY_IDS

    expanded = strategy_api.list_strategies(
        request,
        include_experimental=True,
    )
    assert {item["id"] for item in expanded["strategies"]} == strategy_catalog.BUILTIN_STRATEGY_IDS

    hidden = strategy_api.get_strategy("factor_ensemble", request)
    assert hidden["lifecycle"] == "experimental"
    assert hidden["visible_by_default"] is False
    assert hidden["evidence_status"] == "historical_replay_failed"
    for strategy_id in ("oversold_reversal", "limit_up_momentum"):
        demoted = strategy_api.get_strategy(strategy_id, request)
        assert demoted["lifecycle"] == "experimental"
        assert demoted["visible_by_default"] is False
        assert demoted["evidence_status"] == "historical_replay_failed"


def test_user_strategy_remains_visible_by_default(tmp_path):
    custom_dir = tmp_path / "strategies" / "custom"
    custom_dir.mkdir(parents=True)
    (custom_dir / "custom_user.py").write_text(
        '''import polars as pl
META = {
    "id": "custom_user",
    "name": "user strategy",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}
def filter(df, params):
    return pl.lit(True)
''',
        encoding="utf-8",
    )
    engine = StrategyEngine(strategy_dirs=[custom_dir])
    meta = engine.list_strategies()[0]

    assert meta["lifecycle"] == "user"
    assert meta["visible_by_default"] is True
    assert strategy_catalog.include_strategy(meta, include_experimental=False)


class _BatchEngine:
    def __init__(self):
        self.strategy_ids = None

    def list_strategies(self):
        return [
            {
                "id": "core",
                "asset_types": ["stock"],
                "timeframes": ["1d"],
                "visible_by_default": True,
            },
            {
                "id": "hidden",
                "asset_types": ["stock"],
                "timeframes": ["1d"],
                "visible_by_default": False,
            },
        ]

    def run_all(self, _context, *, strategy_ids, **_kwargs):
        self.strategy_ids = strategy_ids
        return {}


class _BatchScreenerService:
    def __init__(self, _repo, asset_type="stock"):
        self.asset_type = asset_type

    def latest_date(self):
        return date(2026, 7, 15)

    def build_strategy_context(self, *_args, **_kwargs):
        return SimpleNamespace(as_of=date(2026, 7, 15))


def test_primary_batch_api_defaults_to_core_and_can_include_hidden(monkeypatch, tmp_path):
    from app.services import screener as screener_service_module

    monkeypatch.setattr(screener_service_module, "ScreenerService", _BatchScreenerService)
    monkeypatch.setattr(strategy_api.strategy_config, "list_overrides", lambda *_args: {})
    engine = _BatchEngine()
    request = _request(tmp_path, engine)

    strategy_api.run_all(
        strategy_api.RunAllRequest(as_of=date(2026, 7, 15)),
        request,
    )
    assert engine.strategy_ids == ["core"]

    strategy_api.run_all(
        strategy_api.RunAllRequest(
            as_of=date(2026, 7, 15),
            include_experimental=True,
        ),
        request,
    )
    assert engine.strategy_ids == ["core", "hidden"]
