from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "recovery" / "production-1605-backend.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_recovered_backend_matches_authoritative_image_manifest() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["imageId"] == (
        "sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a"
    )
    mismatches = {
        relative: (_sha256(ROOT / relative), expected)
        for relative, expected in payload["files"].items()
        if _sha256(ROOT / relative) != expected
    }
    assert mismatches == {}
