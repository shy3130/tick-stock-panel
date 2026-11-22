import json
from datetime import timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_daily import at, bar
from test_price_views import factor, spec
from test_snapshot import write_bundle
from tivelo_contracts.base import fingerprint
from tivelo_contracts.data import BarQuery, DatasetScope
from tivelo_contracts.extension_fields import ExtensionField, ExtensionValue

import tivelo_backtest.legacy.optimization_archive as archive_module
from tivelo_backtest.cli import main
from tivelo_backtest.legacy import minute_optimization
from tivelo_backtest.research.panel_selection import sort_minute_strategy_rows


def _signal_observations(minutes, *, missing=False):
    field = ExtensionField(
        table_id="minute_signals", table_version="1", field_id="pulse", field_version="1",
        value_type="bool", unit="flag", granularity="instrument", time_basis="minute_bar_end",
    )
    digest = fingerprint(field)
    rows = []
    for minute in minutes:
        end = minute.bar_end
        timely = end.minute == 32
        values = {
            "field_sha256": digest, "instrument_id": "A", "effective_at": end,
            "source_id": "signal-source", "valid_until": end + timedelta(minutes=1),
        }
        if not missing or not (end.day == 16 and timely):
            rows.append(ExtensionValue(
                **values, available_at=end, ingested_at=end, bool_value=timely, revision_id="1",
            ))
        late = end + timedelta(seconds=10)
        rows.append(ExtensionValue(
            **values, available_at=late, ingested_at=late, bool_value=not timely, revision_id="2",
        ))
    scope = DatasetScope(
        market_id="cn.equity", asset_class="equity", instrument_ids=("A",),
        start=at(1, 0), end=at(29, 0), extension_field=field,
    )
    return {"dataset_id": "minute.pulse", "field_sha256": digest}, {
        "extension_values": {"minute.pulse": rows}, "scopes": {"minute.pulse": scope},
    }


@pytest.mark.parametrize("mode,price_view,signal_mode", [
    ("position", "raw", None), ("full", "raw", None), ("position", "qfq", None),
    ("position", "hfq", None), ("position", "raw", "definitions"),
    ("position", "raw", "facts"), ("position", "raw", "missing_fact"),
])
def test_minute_optimizer_archives_and_replays_trades(tmp_path, capsys, monkeypatch, mode, price_view, signal_mode):
    assert main(["legacy-robust-schema"]) == 0
    assert "minute" in json.loads(capsys.readouterr().out)["properties"]
    snapshot = tmp_path / "snapshot"
    prices = [(1, "19.6"), (2, "10"), (3, "10.5"), (14, "10.8"), (16, "11.3"), (17, "11.8"), (28, "12")]
    daily = [bar(day, close) for day, close in prices]
    minutes = []
    for day, close in [(2, "10"), (16, "11.3")]:
        for label, price in [(30, "9"), (31, close)]:
            stamp = at(day).replace(minute=label)
            end = stamp + timedelta(minutes=1)
            minutes.append(bar(
                day, price, timeframe="1m", bar_start=stamp, bar_end=end,
                available_at=end, ingested_at=end, session_id="fixture.minute",
            ))
    signal_reference, signal_fixture = (None, {})
    if signal_mode in {"facts", "missing_fact"}:
        signal_reference, signal_fixture = _signal_observations(minutes, missing=signal_mode == "missing_fact")
    write_bundle(
        snapshot, bars=(*daily, *minutes),
        factors=tuple(factor(day, value, instrument_id="A") for day, value in [(1, "1"), (2, "2"), (28, "4")]),
        **signal_fixture,
    )
    source = tmp_path / "minute.py"
    script = """import polars as pl
META = {'id': 'demo.minute', 'timeframes': ['1m'], 'daily_history_bars': 2}
EXECUTION_BACKEND = 'minute_filter'
def filter_minute_history(frame, params, *, daily):
    assert daily['date'].max() < frame['datetime'].dt.date().min()
    return frame.filter(pl.col('close') >= params['minimum']).select(
        'symbol', 'close', pl.col('datetime').alias('last_datetime'))
"""
    if signal_mode:
        script = script.replace("EXECUTION_BACKEND", "REQUIRED_FEATURES = ['csgi_pulse']\nEXECUTION_BACKEND", 1)
        script = script.replace("return frame.filter", "return frame.filter(pl.col('csgi_pulse')).filter", 1)
    source.write_text(script, encoding="utf-8")
    sealed = tmp_path / "sealed"
    assert main(["seal-legacy-minute", "--source", str(source), "--version", "1", "--output", str(sealed)]) == 0
    capsys.readouterr()

    calls = []

    def bind(specification):
        calls.append(specification.adapter_id)
        return SimpleNamespace(
            specification=specification,
            load_legacy_minute_limits=lambda: lambda symbol, day, name: 0.1,
        )

    monkeypatch.setattr(archive_module, "discover_market_adapter", bind)
    query = {
        "instrument_ids": ["A"], "timeframe": "1d", "start": at(1, 0).isoformat(),
        "end": at(29, 0).isoformat(), "as_of": at(29).isoformat(),
    }
    request = {
        "query": query,
        "minute": {
            "query": {**query, "timeframe": "1m"}, "minute_label": "end", "mode": mode,
            "market": {"adapter_id": "fixture.cn.minute", "distribution": "tivelo-backtest"},
        },
        "symbols": {"A": "600000.SH"}, "start": "2025-01-01", "end": "2025-01-28",
        "params_meta": [{"id": "minimum", "type": "int"}], "param_grid": {"minimum": [10, 20]},
        "selection": {"basic_filter": {"enabled": False}},
        "matcher": {
            "initial_capital": 10000, "max_hold_days": 1, "max_positions": 1, "commission_pct": 0, "slippage_bps": 0,
        },
        "objective": "total_return", "k_segments": 2, "min_trades_total": 2, "stage2_top_n": 1,
    }
    if price_view != "raw":
        request["price_view"] = spec(price_view, anchor=28 if price_view == "qfq" else 1).model_dump(mode="json")
    if signal_mode == "definitions":
        request["minute"]["signal_definitions"] = [{
            "id": "pulse", "name": "Price pulse", "kind": "entry", "timeframe": "intraday",
            "conditions": [{"left": "price", "op": ">=", "right": "10"}],
        }]
    if signal_reference is not None:
        request["minute"]["signal_fields"] = {"csgi_pulse": signal_reference}
    req_path, result = tmp_path / "request.json", tmp_path / "result"
    req_path.write_text(json.dumps(request), encoding="utf-8")
    status = main([
        "legacy-robust", "--request", str(req_path), "--definition", str(sealed),
        "--snapshot", str(snapshot), "--output", str(result),
    ])
    if signal_mode == "missing_fact":
        assert status == 1
        assert json.loads(capsys.readouterr().err)["code"] == "DATA_COVERAGE_INSUFFICIENT"
        assert not result.exists()
        return
    assert status == 0
    receipt = json.loads(capsys.readouterr().out)
    assert main(["inspect", "--kind", "legacy-robust", "--archive", str(result)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["archive_id"] == receipt["archive_id"]
    assert document["lock"]["semantics"] == "legacy.minute.robust.v1"
    assert document["lock"]["request"]["minute"]["market"]["adapter_id"] == "fixture.cn.minute"
    for name in ("signal_definitions", "signal_fields"):
        assert document["lock"]["request"]["minute"].get(name) == request["minute"].get(name)
    report = document["report"]
    assert report["best_params"] == {"minimum": 10}
    assert report["best_row_detail"]["trades_total"] == 2
    assert report["results"][1]["error"]
    best = report["best_backtest"]
    assert [row["entry_date"] for row in best["trades"]] == ["2025-01-02 09:32", "2025-01-16 09:32"]
    assert [row["entry_price"] for row in best["trades"]] == [10.0, 11.3]
    assert best["minute_replay"]["replayed_days"] == 2
    assert "2025-01-03" in best["minute_replay"]["skipped_days"]
    assert best["equity_curve"]

    (sealed / "strategy.py").write_text("raise AssertionError('read the archived source')", encoding="utf-8")
    req_path.unlink()
    replay = tmp_path / "replay"
    assert main([
        "legacy-robust-replay", "--archive", str(result), "--snapshot", str(snapshot), "--output", str(replay),
    ]) == 0
    assert calls == ["fixture.cn.minute"] * 2
    for file in result.rglob("*"):
        if file.is_file():
            assert (replay / file.relative_to(result)).read_bytes() == file.read_bytes()


def test_minute_scores_remain_deterministic_when_a_result_limit_cuts_a_tie():
    candidates = pl.DataFrame({"symbol": ["600002.SH", "600001.SH", "600003.SH"], "score": [100.0, 100.0, 50.0]})
    expected = ["600001.SH", "600002.SH", "600003.SH"]
    assert sort_minute_strategy_rows(candidates, None, True)["symbol"].to_list() == expected
    assert sort_minute_strategy_rows(candidates.reverse(), None, True).head(1)["symbol"].item() == expected[0]


def test_cached_minute_frame_is_isolated_between_trials(monkeypatch):
    calls = []

    def read(reader, query, **kwargs):
        calls.append(query.start)
        return pl.DataFrame({"close": [10.0]})

    monkeypatch.setattr(minute_optimization, "read_legacy_minute_frame", read)
    query = BarQuery(instrument_ids=("A",), timeframe="1m", start=at(1), end=at(5), as_of=at(5))
    load_day = minute_optimization._minute_loader(None, query, None, "end")
    day = at(2).date()
    load_day(day).replace_column(0, pl.Series("close", [99.0]))
    assert load_day(day)["close"].item() == 10.0
    assert len(calls) == 1
