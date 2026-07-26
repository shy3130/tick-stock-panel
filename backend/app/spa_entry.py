"""Select the immutable SPA entry used for a requested frontend route."""

from pathlib import Path


def spa_entry_path(static_root: Path, full_path: str) -> Path:
    if full_path.rstrip("/") == "collection-monitor":
        return static_root / "collection-monitor.html"
    return static_root / "index.html"
