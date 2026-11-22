import json

import pytest

from tivelo_backtest.cli import main


def test_seal_is_static_idempotent_and_never_overwrites(tmp_path, capsys):
    path = tmp_path / "strategy.py"
    path.write_text(
        "raise RuntimeError('MUST NOT EXECUTE')\ndef build(**kwargs): pass\n", encoding="utf-8"
    )
    arguments = [
        "seal",
        "--root",
        str(tmp_path),
        "--id",
        "demo",
        "--version",
        "1",
        "--entrypoint",
        "strategy:build",
        "--file",
        "strategy.py",
    ]
    assert main(arguments) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == manifest
    path.write_text("def build(**kwargs): pass\n", encoding="utf-8")
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "DEFINITION_CONFLICT"
    assert json.loads((tmp_path / "definition.json").read_text()) == manifest


def test_invalid_request_does_not_expose_input_or_create_output(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text('{"secret":"do-not-print-credential"}', encoding="utf-8")
    assert (
        main(
            [
                "prepare",
                "--request",
                str(request),
                "--definition",
                str(tmp_path),
                "--snapshot",
                str(tmp_path),
                "--output",
                str(tmp_path / "run"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "do-not-print" not in captured.err
    assert json.loads(captured.err)["code"] == "CONTRACT_INVALID"
    assert not (tmp_path / "run").exists()


def test_request_schema_and_missing_archive_are_machine_readable(tmp_path, capsys):
    assert main(["request-schema"]) == 0
    assert "market" in json.loads(capsys.readouterr().out)["required"]
    assert main(["inspect", "--archive", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "ARTIFACT_CORRUPT"


def test_trusted_code_logging_does_not_corrupt_json_stdout(tmp_path, monkeypatch, capsys):
    from tivelo_contracts.execution import DefinitionReference

    import tivelo_backtest.cli as module

    def execute(**kwargs):
        print("strategy progress")
        return DefinitionReference(definition_id="synthetic", version="1", sha256="a" * 64)

    monkeypatch.setattr(module, "execute_run", execute)
    assert (
        main(
            [
                "run",
                "--run",
                str(tmp_path),
                "--snapshot",
                str(tmp_path),
                "--output",
                str(tmp_path / "result"),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["definition_id"] == "synthetic"
    assert captured.err == "strategy progress\n"


@pytest.mark.parametrize("mode", ["plain", "selection", "extension_fields", "polars", "history"])
def test_legacy_robust_cli_replays_a_sealed_strategy(tmp_path, capsys, mode):
    from test_daily import at, bar, security
    from test_snapshot import write_bundle
    from tivelo_contracts.artifacts import ArchiveContents
    from tivelo_contracts.base import canonical_bytes, fingerprint
    from tivelo_contracts.data import DatasetScope
    from tivelo_contracts.extension_fields import ExtensionField, ExtensionValue

    from tivelo_backtest.archives import _record
    from tivelo_backtest.legacy.optimization_archive import LegacyRobustArchive

    snapshot = tmp_path / "snapshot"
    days = (2, 3, 14, 16, 17, 28)
    prices = ("10", "10", "12", "13", "13", "16")
    bars = [bar(day, price) for day, price in zip(days, prices, strict=True)]
    selection = {}
    snapshot_fields = {}
    if mode != "plain":
        symbols = {"A": "600002.SH", "B": "300001.SZ", "C": "600001.SH", "D": "600003.SH"}
        bars.extend(
            bar(day, price, instrument_id=instrument, turnover=turnover)
            for instrument, price, turnover in (
                ("B", "10", "90000"),
                ("C", "14", "120000"),
                ("D", "10", "89000"),
            )
            for day in days
        )
        selection = {
            "field_columns": [],  # The amount filter loads its input automatically.
            "symbols": symbols,
            "selection": {
                "basic_filter": {"boards": ["沪主板"], "amount_min": 90000},
                "scoring": {"close": 0.25, "amount": 0.75},
                "scoring_directions": {"close": "low", "amount": "low"},
            },
        }
    if mode in {"extension_fields", "polars", "history"}:
        references, scopes, observations = {}, {}, {}
        input_columns = [
            ("total_shares", "share", 1_000_000),
            ("float_shares", "share", 200_000),
            ("ext_rank", "rank", 10),
        ]
        if mode in {"polars", "history"}:
            input_columns += [("signal_demo_entry", "flag", 1), ("signal_demo_exit", "flag", 0)]
        for column, unit, level in input_columns:
            descriptor = ExtensionField(
                table_id="inputs", table_version="1", field_id=column, field_version="1",
                granularity="instrument", value_type="int", unit=unit,
            )
            digest = fingerprint(descriptor)
            references[column] = {"dataset_id": column, "field_sha256": digest}
            scopes[column] = DatasetScope(
                market_id="cn.equity", asset_class="equity", instrument_ids=tuple(symbols),
                start=at(1), end=at(29), extension_field=descriptor,
            )
            observations[column] = tuple(
                ExtensionValue(
                    field_sha256=digest, instrument_id=instrument, effective_at=at(1),
                    available_at=at(1), ingested_at=at(1), revision_id="1", source_id="synthetic",
                    int_value=level if column != "ext_rank" else {"A": 10, "B": 1, "C": 30, "D": 1}[instrument],
                )
                for instrument in symbols
            )
        snapshot_fields = {
            "scopes": scopes,
            "extension_values": observations,
            "instruments": tuple(
                security().model_copy(update={
                    "instrument_id": instrument, "display_symbol": symbol,
                    "name": "*ST 样本" if instrument == "D" else "普通证券",
                })
                for instrument, symbol in symbols.items()
            ),
        }
        selection["extension_fields"] = references
        selection["field_cutoff_time"] = "15:00:00+08:00"
        selection["selection"]["basic_filter"].update(
            amount_min=88000, market_cap_min=9_000_000, float_cap_min=1_500_000,
            turnover_min=4, turnover_max=6, exclude_st=True,
        )
        selection["selection"]["scoring"]["ext_rank"] = 1.0
        selection["selection"]["scoring_directions"]["ext_rank"] = "low"
        if mode == "history":
            selection["field_columns"] = ["name"]
    _, facts = write_bundle(snapshot, bars=tuple(bars), **snapshot_fields)
    legacy = tmp_path / "legacy.py"
    source = tmp_path / "trusted"
    strategy = """from app.backtest.matrix import make_signal_matrix, matrix_feature

META = {'id':'demo.matrix','params':[{'id':'enabled','default':True}]}
EXECUTION_BACKEND = 'matrix_native'
print('loaded strategy')

class DailyThreshold:
    def required_warmup_bars(self, params):
        return 1

    def compute_signals(self, market, params):
        entry = (matrix_feature(market, 'close') >= params['minimum']) & params['enabled']
        return make_signal_matrix(market.shape, entry=entry)

MATRIX_STRATEGY = DailyThreshold()
"""
    if mode == "polars":
        strategy = """import polars as pl

META: dict = {'id':'demo.polars','params':{'enabled':True}}
ENTRY_SIGNALS = ['demo_entry']
EXIT_SIGNALS = ['signal_demo_exit']
print('loaded strategy')

def filter(panel, params):
    return (pl.col('close') >= params['minimum']) & pl.lit(params['enabled'])
"""
    elif mode == "history":
        strategy = """import polars as pl

META = {'id':'demo.history','params':{'enabled':True}}
LOOKBACK_DAYS = 75
REQUIRED_FEATURES = ['name']
ENTRY_SIGNALS = ['demo_entry']
EXIT_SIGNALS = ['signal_demo_exit']
print('loaded strategy')

def filter_history(panel, params):
    hits = panel.filter(pl.col('name').is_not_null() & (pl.col('close') >= params['minimum'])
                        & pl.lit(params['enabled'])).select('symbol', 'date')
    return pl.concat([hits.reverse(), hits.head(1)])
"""
    legacy.write_text(strategy, encoding="utf-8")
    command = [
        "seal-legacy-polars" if mode in {"polars", "history"} else "seal-legacy-matrix",
        "--source",
        str(legacy),
        "--version",
        "1",
        "--output",
        str(source),
    ]
    assert main(command) == 0
    sealed = capsys.readouterr()
    assert sealed.err == "" and legacy.read_text() == strategy
    assert main(command) == 0 and capsys.readouterr().out == sealed.out
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "query": {
                    "instrument_ids": list(selection.get("symbols", {"A": "A"})),
                    "timeframe": "1d",
                    "start": "2025-01-01T00:00:00Z",
                    "end": "2025-01-29T00:00:00Z",
                    "as_of": "2025-01-29T00:00:00Z",
                },
                "start": "2025-01-01",
                "end": "2025-01-28",
                "objective": "total_return",
                "params_meta": [
                    {"id": "minimum", "type": "int"},
                    {"id": "alpha", "type": "int"},
                ],
                "param_grid": {"minimum": [13, 10], "alpha": [2, 1]},
                "matcher": {
                    "matching": "open_t+1",
                    "initial_capital": 10000,
                    "max_positions": 1,
                    "commission_pct": 0,
                    "slippage_bps": 0,
                },
                "k_segments": 2,
                "min_trades_total": 2,
                "stage2_top_n": 1,
                **selection,
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "result"
    run = ["legacy-robust", "--definition", str(source), "--snapshot", str(snapshot)]
    if mode in {"extension_fields", "polars", "history"}:
        incomplete = json.loads(request.read_text())
        del incomplete["extension_fields"]["total_shares"]
        missing = tmp_path / "missing.json"
        missing.write_text(json.dumps(incomplete), encoding="utf-8")
        assert main([*run, "--request", str(missing), "--output", str(archive)]) == 1
        assert json.loads(capsys.readouterr().err)["code"] == "DATA_COVERAGE_INSUFFICIENT"
        assert not archive.exists()
    if mode == "history":
        missing_name = json.loads(request.read_text())
        missing_name["field_columns"] = []
        missing.write_text(json.dumps(missing_name), encoding="utf-8")
        assert main([*run, "--request", str(missing), "--output", str(archive)]) == 1
        assert json.loads(capsys.readouterr().err.splitlines()[-1])["code"] == "DATA_COVERAGE_INSUFFICIENT"
        assert not archive.exists()
    assert main([*run, "--request", str(request), "--output", str(archive)]) == 0
    captured = capsys.readouterr()
    manifest = LegacyRobustArchive.model_validate_json(captured.out)
    assert captured.err == "loaded strategy\n"
    (source / "strategy.py").write_text("raise AssertionError('not archived')\n", encoding="utf-8")
    request.unlink()

    inspect = ["inspect", "--kind", "legacy-robust", "--archive", str(archive)]
    assert main(inspect) == 0
    captured = capsys.readouterr()
    view = json.loads(captured.out)
    assert captured.err == "" and view["archive_id"] == manifest.archive_id
    assert view["lock"]["snapshot_id"] == facts.snapshot.snapshot_id
    assert view["lock"]["grid_order"] == ["minimum", "alpha"]
    if mode != "plain":
        assert view["lock"]["scoring_order"] == list(selection["selection"]["scoring"])
        assert view["lock"]["request"]["selection"]["basic_filter"]["boards"] == ["沪主板"]
    else:
        assert "selection" not in view["lock"]["request"] and "scoring_order" not in view["lock"]
    if mode in {"extension_fields", "polars", "history"}:
        assert view["lock"]["request"]["extension_fields"] == selection["extension_fields"]
        assert view["lock"]["request"]["field_cutoff_time"] == selection["field_cutoff_time"]
    assert view["report"]["best_params"] == {"minimum": 10, "alpha": 2}
    assert view["report"]["results"][0]["oos_values"] == [0.2, 0.21]
    assert view["report"]["results"][0]["is_full"]["objective_raw"] == 0.6

    replay = ["legacy-robust-replay", "--archive", str(archive), "--snapshot", str(snapshot)]
    target = tmp_path / "reproduced"
    assert main([*replay, "--output", str(target)]) == 0
    assert LegacyRobustArchive.model_validate_json(capsys.readouterr().out) == manifest
    assert all(
        (target / path.path).read_bytes() == (archive / path.path).read_bytes()
        for path in manifest.contents.files
    )

    report = archive / "report.json"
    report.write_text(json.dumps(dict(view["report"], best_score=500.0)), encoding="utf-8")
    assert main(inspect) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "ARTIFACT_CORRUPT"
    modified = LegacyRobustArchive.create(
        ArchiveContents(
            lock_id=manifest.contents.lock_id,
            files=tuple(_record(archive, path.path) for path in manifest.contents.files),
        )
    )
    (archive / "manifest.json").write_bytes(canonical_bytes(modified))
    blocked = tmp_path / "different"
    assert main([*replay, "--output", str(blocked)]) == 1
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["code"] == "DATA_CONFLICT"
    assert not blocked.exists()


def test_legacy_composite_locks_child_parameters_and_replays_the_graph(tmp_path, capsys):
    from test_daily import bar
    from test_snapshot import write_bundle
    from tivelo_contracts.base import canonical_bytes
    from tivelo_contracts.definitions import DefinitionBundle

    snapshot = tmp_path / "snapshot"
    write_bundle(snapshot, bars=tuple(
        bar(day, price) for day, price in ((2, "10"), (3, "10"), (14, "12"), (16, "13"), (17, "13"), (28, "16"))
    ))
    dependency_root = tmp_path / "children"
    dependency_root.mkdir()
    references = []
    paths = []
    for child_id, parameters_json in (("child.z", "{}"), ("child.a", '{"minimum":13}')):
        source = tmp_path / f"{child_id}.py"
        source.write_text(f"""from app.backtest.matrix import make_signal_matrix
META = {{'id': {child_id!r}, 'params': [{{'id': 'minimum', 'default': 10}}]}}
EXECUTION_BACKEND = 'matrix_native'
class DailyThreshold:
    def required_warmup_bars(self, params):
        return 1
    def compute_signals(self, market, params):
        entry = market.close >= params['minimum']
        return make_signal_matrix(market.shape, entry=entry, exit=~entry)
MATRIX_STRATEGY = DailyThreshold()
""", encoding="utf-8")
        temporary = tmp_path / child_id
        assert main([
            "seal-legacy-matrix", "--source", str(source), "--version", "1", "--output", str(temporary),
        ]) == 0
        reference = DefinitionBundle.model_validate_json(capsys.readouterr().out).reference(parameters_json)
        path = tmp_path / f"{child_id}.json"
        path.write_bytes(canonical_bytes(reference))
        references.append(reference)
        paths.append(path)
        temporary.rename(dependency_root / reference.sha256)

    source = tmp_path / "composite.py"
    source.write_text("""META = {'id':'demo.composite','children':[
    {'strategy_id':'child.z','weight':0.6}, {'strategy_id':'child.a','weight':0.4},
]}
EXECUTION_BACKEND = 'composite'
""", encoding="utf-8")
    definition = tmp_path / "sealed"
    seal = ["seal-legacy-composite", "--source", str(source), "--version", "1", "--output", str(definition)]
    assert main([*seal, "--dependency", str(paths[0])]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DEFINITION_CONFLICT"
    assert not definition.exists()
    assert main([*seal, "--dependency", str(paths[0]), "--dependency", str(paths[1])]) == 0
    capsys.readouterr()

    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "query": {
            "instrument_ids": ["A"], "timeframe": "1d", "start": "2025-01-01T00:00:00Z",
            "end": "2025-01-29T00:00:00Z", "as_of": "2025-01-29T00:00:00Z",
        },
        "start": "2025-01-01", "end": "2025-01-28", "objective": "total_return",
        "params_meta": [{"id": "merge_mode", "type": "select", "options": ["union", "intersect"]}],
        "param_grid": {"merge_mode": ["union", "intersect"]},
        "matcher": {"matching": "open_t+1", "initial_capital": 10000, "max_positions": 1,
                    "commission_pct": 0, "slippage_bps": 0},
        "k_segments": 2, "min_trades_total": 2, "stage2_top_n": 1,
    }), encoding="utf-8")
    archive = tmp_path / "result"
    run = [
        "legacy-robust", "--request", str(request), "--definition", str(definition),
        "--dependencies", str(dependency_root), "--snapshot", str(snapshot), "--output", str(archive),
    ]
    assert main(run) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_COVERAGE_INSUFFICIENT"
    assert not archive.exists()
    settings = json.loads(request.read_text(encoding="utf-8"))
    settings["selection"] = {"basic_filter": {"enabled": False}}
    request.write_text(json.dumps(settings), encoding="utf-8")
    assert main(run) == 0
    capsys.readouterr()
    assert main(["inspect", "--kind", "legacy-robust", "--archive", str(archive)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["lock"]["dependencies"] == [ref.model_dump(mode="json") for ref in reversed(references)]
    assert result["report"]["best_params"] == {"merge_mode": "union"}
    assert result["report"]["results"][0]["oos_values"] == [0.2, 0.21]
    assert result["report"]["results"][0]["is_full"]["objective_raw"] == 0.6

    (dependency_root / references[-1].sha256 / "strategy.py").write_text("raise AssertionError()", encoding="utf-8")
    replay = tmp_path / "replayed"
    assert main([
        "legacy-robust-replay", "--archive", str(archive), "--snapshot", str(snapshot), "--output", str(replay),
    ]) == 0
    assert (replay / "manifest.json").read_bytes() == (archive / "manifest.json").read_bytes()
