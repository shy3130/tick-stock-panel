"""The v1 stock/ETF repository uses one shared raw-price migration marker."""

from pathlib import Path

from .repository_files import MINUTE_MARKER, MINUTE_TABLE

MIN_ANCHOR_RATIO = 0.2
MAX_ANCHOR_RATIO = 5.0


def minute_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / MINUTE_TABLE


def marker_path(data_dir: Path | str) -> Path:
    return minute_dir(data_dir) / MINUTE_MARKER


def minute_basis_is_raw(data_dir: Path | str) -> bool:
    return marker_path(data_dir).exists()
