from __future__ import annotations

import json

import pytest

from research.common.universe import (
    stable_symbol_sample,
    symbols_sha256,
    universe_manifest,
    write_universe_manifest,
)


def test_stable_symbol_sample_is_independent_of_input_order_and_duplicates():
    first = stable_symbol_sample(["C", "A", "B", "A"], 2, 7)
    second = stable_symbol_sample(["B", "C", "A"], 2, 7)
    assert first == second
    assert symbols_sha256(first) == symbols_sha256(second)


def test_stable_symbol_sample_rejects_negative_size():
    with pytest.raises(ValueError, match="non-negative"):
        stable_symbol_sample(["A"], -1, 7)


def test_manifest_records_full_audit_contract_and_writes_atomically(tmp_path):
    manifest = universe_manifest(
        ["A", "B"],
        seed=9,
        requested_size=2,
        start="2025-01-01",
        end="2025-12-31",
    )
    path = tmp_path / "universe.json"
    write_universe_manifest(path, manifest)
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored == manifest
    assert restored["symbols"] == ["A", "B"]
    assert restored["symbols_sha256"] == symbols_sha256(["A", "B"])
