"""Independent Tivelo data engine. Configuration and providers are injected."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import DataEngine
    from .storage import Candidate, SnapshotStore

__version__ = "0.1.0a1"
__all__ = ["Candidate", "DataEngine", "SnapshotStore"]


def __getattr__(name):
    if name == "DataEngine":
        from .engine import DataEngine

        return DataEngine
    if name in ("Candidate", "SnapshotStore"):
        from .storage import Candidate, SnapshotStore

        return {"Candidate": Candidate, "SnapshotStore": SnapshotStore}[name]
    raise AttributeError(name)
