from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine


def _strategy_code(strategy_id: str, *, body: str = "return pl.lit(True)") -> str:
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    {body}
'''


def test_duplicate_strategy_id_reports_both_paths(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.py").write_text(_strategy_code("duplicate"), encoding="utf-8")
    (second / "b.py").write_text(_strategy_code("duplicate"), encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[first, second])

    assert not engine.has("duplicate")
    errors = engine.load_errors()
    assert len(errors) == 2
    assert {item["file"] for item in errors} == {str(first / "a.py"), str(second / "b.py")}
    assert all("duplicate strategy id" in item["error"] for item in errors)


def test_failed_reload_keeps_previous_registry(tmp_path):
    path = tmp_path / "stable.py"
    path.write_text(_strategy_code("stable"), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    previous = engine.get("stable")

    path.write_text("this is not valid python", encoding="utf-8")

    with pytest.raises(ValueError, match="strategy reload failed"):
        engine.reload()

    assert engine.get("stable") is previous
    assert engine.load_errors()


def test_context_rejects_unsupported_timeframe(tmp_path):
    path = tmp_path / "daily.py"
    path.write_text(_strategy_code("daily"), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[tmp_path])

    with pytest.raises(ValueError, match="does not support timeframe"):
        engine.run(
            "daily",
            StrategyDataContext(
                asset_type="stock",
                timeframe="5m",
                as_of=date(2026, 1, 2),
                current=pl.DataFrame({"symbol": ["000001.SZ"]}),
            ),
        )


def test_run_all_respects_explicit_empty_strategy_ids(tmp_path):
    path = tmp_path / "daily.py"
    path.write_text(_strategy_code("daily"), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 1, 2),
        current=pl.DataFrame({"symbol": ["000001.SZ"]}),
    )

    assert engine.run_all(context, strategy_ids=[]) == {}


def test_builtin_custom_and_ai_files_share_one_registry_and_run_path(tmp_path):
    strategy_ids = {
        "builtin": "builtin_plugin",
        "custom": "custom_plugin",
        "ai": "ai_plugin",
    }
    dirs = []
    for source, strategy_id in strategy_ids.items():
        directory = tmp_path / "strategies" / source
        directory.mkdir(parents=True)
        (directory / f"{strategy_id}.py").write_text(
            _strategy_code(strategy_id),
            encoding="utf-8",
        )
        dirs.append(directory)

    engine = StrategyEngine(strategy_dirs=dirs)
    sources = {meta["id"]: meta["source"] for meta in engine.list_strategies()}
    assert sources == {strategy_id: source for source, strategy_id in strategy_ids.items()}

    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 1, 2),
        current=pl.DataFrame({"symbol": ["000001.SZ"]}),
    )
    overrides = {
        strategy_id: {"basic_filter": {"enabled": False}}
        for strategy_id in strategy_ids.values()
    }
    results = engine.run_all(context, overrides_map=overrides)
    assert set(results) == set(strategy_ids.values())
    assert all(result.total == 1 for result in results.values())
