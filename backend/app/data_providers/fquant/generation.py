"""Logical-name -> snapshot-root resolution for the fquant DuckDB clients
(Task 16).
``extended`` and ``tdx_moneyflow_minute`` are published to their own dedicated
roots and are overridable via ``FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED`` and
``FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE`` so a republish of the shared
fstore / engine-a generation cannot swap the file they resolve to. The shared
roots are configurable via the ``FQUANT_SNAPSHOT_ROOT_{FSTORE,ENGINE_A,ENGINE_HK}``
environment variables so the panel can be pointed at a staging mirror without
code changes.

``current_path(logical)`` returns the current generation's immutable file for a
logical database, or ``None`` when no snapshot is available (caller falls back
to raw). It mirrors engine ``pkg/snapshot`` semantics.
"""
from __future__ import annotations

import os

from app.data_providers.fquant import snapshot_resolver as sr

# owner group -> (env var, default root)
_ROOT_ENV = {
    "fstore": ("FQUANT_SNAPSHOT_ROOT_FSTORE", sr.ROOT_FSTORE),
    "fstore_extended": ("FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED", sr.ROOT_FSTORE_EXTENDED),
    "engine_a": ("FQUANT_SNAPSHOT_ROOT_ENGINE_A", sr.ROOT_ENGINE_A),
    "engine_a_moneyflow_minute": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE",
        sr.ROOT_ENGINE_A_MONEYFLOW_MINUTE,
    ),
    "engine_a_callauction": (
        "FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION",
        sr.ROOT_ENGINE_A_CALLAUCTION,
    ),
    "engine_hk": ("FQUANT_SNAPSHOT_ROOT_ENGINE_HK", sr.ROOT_ENGINE_HK),
}

# logical database name -> owner group. Must match the publishers.
LOGICAL_OWNERS = {
    "fstore": "fstore",
    "markets": "fstore",
    "klines": "fstore",
    "minutes": "fstore",
    "extended": "fstore_extended",
    "tdx": "engine_a",
    "tdx_chip": "engine_a",
    "tdx_moneyflow": "engine_a",
    "tdx_moneyflow_minute": "engine_a_moneyflow_minute",
    "tdx_callauction": "engine_a_callauction",
    "tdx_hk": "engine_hk",
    "tdx_hk_minutes": "engine_hk",
    "tdx_hk_trans": "engine_hk",
}


def root_for(logical: str) -> str | None:
    """Snapshot root for a logical database, honouring env overrides."""
    owner = LOGICAL_OWNERS.get(logical)
    if owner is None and logical.startswith("tdx_callauction_"):
        owner = "engine_a_callauction"
    if owner is None:
        return None
    env_key, default = _ROOT_ENV[owner]
    return (os.getenv(env_key) or "").strip() or default


def current_path(logical: str) -> str | None:
    """Current-generation snapshot file for a logical database, or None."""
    root = root_for(logical)
    if root is None:
        return None
    return sr.resolve(root, logical)
