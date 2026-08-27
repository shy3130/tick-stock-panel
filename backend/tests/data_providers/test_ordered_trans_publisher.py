from datetime import date
from pathlib import Path

import pytest

from app.data_providers.fquant.ordered_trans import (
    MaterializationSkipped,
    OrderedTransIntegrityError,
    aggregate_bars,
    build_generation_staging,
    materialize_symbol_day,
    publish_staged_generation,
    snapshot_source,
    read_current_bytes,
)


def _write_csv(root: Path, symbol: str, day: date, *, venue: bool = False, drop: int | None = None) -> Path:
    path = root / day.strftime("%Y%m%d") / f"{symbol[-2:].lower()}{symbol[:6]}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "time,price,vol,num,amount,buyorsell" + (",venue" if venue else "")
    rows = [header]
    minute_values = list(range(570, 690)) + list(range(780, 900))
    for index, minute in enumerate(minute_values):
        if index == drop:
            continue
        hh, mm = divmod(minute, 60)
        rows.append(f"{hh:02d}:{mm:02d},{10 + index / 100:.2f},1,1,10,1" + (",0" if venue else ""))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_single_fd_header_plus_one_minute_and_exact_240(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    day = date(2026, 8, 26)
    _write_csv(raw, "600519.SH", day, venue=True)
    entry = materialize_symbol_day(raw, "600519.SH", day)
    assert entry.source["parser_variant"] == "seven_column_venue"
    assert entry.artifact["rows"] == 240
    assert entry.artifact["first_close"] == "10"
    assert entry.artifact["last_close"] == "12.39"

def test_closing_auction_keeps_sparse_true_trades_and_boundary_close(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    day = date(2026, 8, 26)
    path = _write_csv(raw, "600519.SH", day)
    rows = path.read_text(encoding="utf-8").splitlines()
    adjusted = [rows[0]]
    midday_inserted = False
    for row in rows[1:]:
        fields = row.split(",")
        if not midday_inserted and fields[0] >= "13:00":
            adjusted.append("11:30,77,1,1,77,2")
            midday_inserted = True
        if fields[0] in {"14:57", "14:58", "14:59"}:
            fields[2] = "0"
        adjusted.append(",".join(fields))
    adjusted.append("15:00,99,1,1,99,2")
    path.write_text("\n".join(adjusted) + "\n", encoding="utf-8")
    snapshot = snapshot_source(path)
    bars = aggregate_bars("600519.SH", day, snapshot.ticks)
    assert len(bars) == 238
    assert next(bar for bar in bars if bar.ts.strftime("%H:%M") == "11:30").close == 77
    assert bars[-1].ts.strftime("%H:%M") == "15:00"
    assert bars[-1].close == 99
    entry = materialize_symbol_day(raw, "600519.SH", day)
    assert entry.artifact["rows"] == 238
    assert entry.artifact["five_minute_windows"] == 48
    assert entry.artifact["missing_close_timestamps"] == ["14:58", "14:59"]


def test_header_and_incomplete_day_fail_closed(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    day = date(2026, 8, 26)
    path = _write_csv(raw, "600519.SH", day, drop=0)
    with pytest.raises(MaterializationSkipped):
        materialize_symbol_day(raw, "600519.SH", day)
    path.write_text("bad,header\n", encoding="utf-8")
    with pytest.raises(OrderedTransIntegrityError):
        materialize_symbol_day(raw, "600519.SH", day)


def test_symlink_source_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    day = date(2026, 8, 26)
    original = _write_csv(raw, "600519.SH", day)
    target = original.with_name("sh600519-real.csv")
    original.rename(target)
    original.symlink_to(target)
    with pytest.raises((OrderedTransIntegrityError, MaterializationSkipped)):
        materialize_symbol_day(raw, "600519.SH", day)


def test_generation_intersection_and_cas_conflict(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    root = tmp_path / "published"
    day = date(2026, 8, 26)
    for symbol in ("600519.SH", "000001.SZ"):
        _write_csv(raw, symbol, day)
    built = build_generation_staging(snapshot_root=root, raw_root=raw, symbols=["600519.SH", "000001.SZ"], days=[day])
    assert built.complete_days == [day]
    expected = read_current_bytes(root)
    root.mkdir(exist_ok=True)
    (root / "current.json").write_bytes(b'{"generation":"other"}\n')
    outcome = publish_staged_generation(root, built, expected)
    assert outcome.status == "conflict"
    assert read_current_bytes(root) == b'{"generation":"other"}\n'
