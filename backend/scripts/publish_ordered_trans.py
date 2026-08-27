"""Publish raw TDX trans CSVs as an immutable ordered-trans generation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data_providers.fquant.ordered_trans import (  # noqa: E402
    DEFAULT_ORDERED_TRANS_ROOT,
    MaterializationSkipped,
    build_generation_staging,
    publish_staged_generation,
    read_current_bytes,
)


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _dates(args: argparse.Namespace) -> list[date]:
    if args.date:
        if args.start or args.end:
            raise ValueError("--date cannot be combined with --start/--end")
        return sorted(set(args.date))
    if (args.start is None) != (args.end is None):
        raise ValueError("--start and --end must be provided together")
    if args.start is None:
        raise ValueError("provide at least one --date or --start/--end")
    if args.start > args.end:
        raise ValueError("--start must be <= --end")
    out: list[date] = []
    current = args.start
    while current <= args.end:
        out.append(current)
        current += timedelta(days=1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="published generation root")
    parser.add_argument("--raw-root", type=Path, required=True, help="raw CSV root")
    parser.add_argument("--symbols", nargs="+", required=True, help="canonical symbols, e.g. 600519.SH")
    parser.add_argument("--date", action="append", type=_day, help="one bounded date (repeatable)")
    parser.add_argument("--start", type=_day)
    parser.add_argument("--end", type=_day)
    args = parser.parse_args(argv)
    try:
        days = _dates(args)
        root = args.root or Path(os.getenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_ORDERED_TRANS", DEFAULT_ORDERED_TRANS_ROOT))
        root.mkdir(parents=True, exist_ok=True)
        expected = read_current_bytes(root)
        built = build_generation_staging(snapshot_root=root, raw_root=args.raw_root, symbols=args.symbols, days=days)
        outcome = publish_staged_generation(root, built, expected)
        print(json.dumps({"status": outcome.status, "generation": outcome.generation, "complete_days": [value.isoformat() for value in built.complete_days], "skipped": list(built.skipped), "reason": outcome.reason}, ensure_ascii=False, sort_keys=True))
        return 0 if outcome.status == "published" else 3
    except (ValueError, OSError, MaterializationSkipped) as exc:
        print(f"ordered-trans publish failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
