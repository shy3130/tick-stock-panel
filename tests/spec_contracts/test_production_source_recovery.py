from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "recovery" / "production-1435-backend.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_recovered_backend_matches_authoritative_image_manifest() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["imageId"] == (
        "sha256:214529b2aae4356c1b22c872d111aa1b425fc9b9184d7b8349b4e9590471b0b6"
    )
    mismatches = {
        relative: (_sha256(ROOT / relative), expected)
        for relative, expected in payload["files"].items()
        if _sha256(ROOT / relative) != expected
    }
    assert mismatches == {}
