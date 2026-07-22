from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "docs/sycee-integrations.json").read_text(encoding="utf-8"))


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_backend_uses_one_stable_sycee_gateway():
    main = _read("backend/app/main.py")
    sycee_lines = [line.strip() for line in main.splitlines() if "sycee" in line.lower()]

    assert sycee_lines == [
        "from app.sycee.router import router as sycee_router",
        "app.include_router(sycee_router)",
    ]

    direct_imports: list[str] = []
    for path in (ROOT / "backend/app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "backend/app/main.py" or relative.startswith("backend/app/sycee/"):
            continue
        if "app.sycee" in path.read_text(encoding="utf-8"):
            direct_imports.append(relative)
    declared = set(MANIFEST["backend"])
    assert set(direct_imports).issubset(declared)
    assert all((ROOT / path).exists() for path in declared)
    protected = (
        "backend/app/backtest/",
        "backend/app/indicators/",
        "backend/app/strategy/",
        "backend/app/tickflow/",
    )
    assert not any(path.startswith(protected) for path in direct_imports)


def test_frontend_uses_only_frozen_sycee_gateways():
    router = _read("frontend/src/router.tsx")
    navigation = _read("frontend/src/lib/navRegistry.ts")
    api = _read("frontend/src/lib/api.ts")

    assert "import { SYCEE_ROUTES } from './features/sycee/registry'" in router
    assert "...SYCEE_ROUTES" in router
    assert "ResearchLedgerPage" not in router

    assert "import { SYCEE_NAV_ITEMS } from '@/features/sycee/registry'" in navigation
    assert "...SYCEE_NAV_ITEMS" in navigation
    assert "研究账本" not in navigation

    assert "export async function request" in api
    assert "/api/sycee/" not in api

    direct_imports: list[str] = []
    for path in (ROOT / "frontend/src").rglob("*.ts*"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("frontend/src/features/sycee/"):
            continue
        source = path.read_text(encoding="utf-8")
        if "features/sycee" in source:
            direct_imports.append(relative)
    declared = set(MANIFEST["frontend"])
    assert set(direct_imports).issubset(declared)
    assert all((ROOT / path).exists() for path in declared)


def test_integration_manifest_has_actionable_reasons():
    for layer in ("backend", "frontend"):
        assert MANIFEST[layer]
        for path, reason in MANIFEST[layer].items():
            assert path.startswith(f"{layer}/")
            assert len(reason.strip()) >= 12
