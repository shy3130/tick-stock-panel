from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "recovery" / "production-1542-backend.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_recovered_backend_matches_authoritative_image_manifest() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["imageId"] == (
        "sha256:7ea697ab7204eed75a26d83ca3a2dda6743397c461003097a7563e8b5b66ddc2"
    )
    mismatches = {
        relative: (_sha256(ROOT / relative), expected)
        for relative, expected in payload["files"].items()
        if _sha256(ROOT / relative) != expected
    }
    assert mismatches == {}
