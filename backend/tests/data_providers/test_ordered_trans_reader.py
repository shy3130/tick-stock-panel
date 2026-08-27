import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from app.data_providers.fquant.ordered_trans import (
    OrderedTransIntegrityError,
    PublishedOrderedTransMinuteReader,
    build_generation_staging,
    canonical_json_bytes,
    publish_staged_generation,
    read_current_bytes,
)


def _write_csv(root: Path, symbol: str, day: date) -> Path:
    path = root / day.strftime("%Y%m%d") / f"{symbol[-2:].lower()}{symbol[:6]}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["time,price,vol,num,amount,buyorsell"]
    minute_values = list(range(570, 690)) + list(range(780, 900))
    for index, minute in enumerate(minute_values):
        hh, mm = divmod(minute, 60)
        rows.append(f"{hh:02d}:{mm:02d},{10 + index / 100:.2f},1,1,10,1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _published(tmp_path: Path) -> tuple[Path, date]:
    raw = tmp_path / "raw"
    root = tmp_path / "published"
    day = date(2026, 8, 26)
    for symbol in ("600519.SH", "000001.SZ"):
        _write_csv(raw, symbol, day)
    built = build_generation_staging(
        snapshot_root=root,
        raw_root=raw,
        symbols=["600519.SH", "000001.SZ"],
        days=[day],
    )
    outcome = publish_staged_generation(root, built, read_current_bytes(root))
    assert outcome.status == "published"
    return root, day


def test_reader_pins_manifest_and_enforces_contract(tmp_path: Path) -> None:
    root, day = _published(tmp_path)
    reader = PublishedOrderedTransMinuteReader(root)
    assert reader.market_days(day, day) == [day]
    assert reader.session("600519.SH", day).open_time == time(9, 30)
    bars = reader.minute_bars("600519.SH", day)
    assert len(bars) == 240
    assert bars[0].ts == datetime(2026, 8, 26, 9, 31)
    assert bars[-1].ts == datetime(2026, 8, 26, 15, 0)
    assert reader.sealed_cutoff() == datetime(2026, 8, 26, 15, 0)
    assert reader.catalog_manifest()["coverage"] == {
        "000001.SZ": ["2026-08-26"],
        "600519.SH": ["2026-08-26"],
    }
    assert "entries" not in reader.catalog_manifest()
    assert len(reader.manifest_sha256()) == 64
    reader.close()
    with pytest.raises(OrderedTransIntegrityError):
        reader.minute_bars("600519.SH", day)
    reader.close()


def test_reader_rejects_artifact_hash_change(tmp_path: Path) -> None:
    root, day = _published(tmp_path)
    reader = PublishedOrderedTransMinuteReader(root)
    generation = reader.generation()
    artifact = root / generation / "bars" / f"date={day.isoformat()}" / "600519.SH.parquet"
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(OrderedTransIntegrityError):
        reader.minute_bars("600519.SH", day)


def test_reader_rejects_coverage_escape(tmp_path: Path) -> None:
    root, _day = _published(tmp_path)
    generation = next(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    manifest = root / generation / "manifest.json"
    value = json.loads(manifest.read_bytes())
    value["entries"][0]["artifact"]["relative_path"] = "../../outside.parquet"
    manifest.write_bytes(canonical_json_bytes(value))
    with pytest.raises(OrderedTransIntegrityError):
        PublishedOrderedTransMinuteReader(root)


def test_reader_rejects_symlink_current_pointer(tmp_path: Path) -> None:
    root, _day = _published(tmp_path)
    current = root / "current.json"
    target = root / "current-real.json"
    current.rename(target)
    current.symlink_to(target)
    with pytest.raises(OrderedTransIntegrityError):
        PublishedOrderedTransMinuteReader(root)
