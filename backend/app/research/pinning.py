"""Bind factor execution to the exact immutable sources accepted by preflight."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any


class PinnedSourceError(RuntimeError):
    """Raised when a durable run cannot reopen its preflight source identity."""


@dataclass(frozen=True, slots=True)
class SourcePin:
    kind: str
    generation: str
    manifest_sha256: str
    pin: dict[str, Any] | None = None

def reader_identity(reader: Any) -> tuple[str | None, str | None]:
    """Normalize direct and composite reader identities into one pair."""
    identity = getattr(reader, "identity", None)
    if callable(identity):
        value = identity()
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, dict):
            return value.get("generation"), value.get("manifest_sha256")
    pin = getattr(reader, "pin", None)
    if callable(pin):
        value = pin()
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, dict):
            generations = {
                key: item
                for key, item in value.items()
                if key.endswith("_generation") and isinstance(item, str) and item
            }
            manifests = {
                key: item
                for key, item in value.items()
                if key.endswith("_manifest_sha256") and isinstance(item, str) and item
            }
            if generations and len(generations) == len(manifests):
                generation = "|".join(
                    f"{key.removesuffix('_generation')}={generations[key]}"
                    for key in sorted(generations)
                )
                digest = hashlib.sha256(
                    json.dumps(manifests, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                return generation, digest
    generation = getattr(reader, "generation", None)
    digest = getattr(reader, "manifest_sha256", None)
    return (
        generation() if callable(generation) else generation,
        digest() if callable(digest) else digest,
    )


def _pins_from_preflight(preflight: Any) -> dict[str, SourcePin]:
    if hasattr(preflight, "model_dump"):
        preflight = preflight.model_dump(mode="json")
    if not isinstance(preflight, dict):
        raise PinnedSourceError("durable run preflight is missing")
    pins: dict[str, SourcePin] = {}
    for source in preflight.get("sources") or []:
        if not isinstance(source, dict) or source.get("status") != "ready":
            continue
        kind = source.get("kind")
        generation = source.get("generation")
        manifest = source.get("manifest_sha256")
        pin_data = source.get("pin")
        if not isinstance(kind, str) or not kind:
            raise PinnedSourceError("preflight source kind is invalid")
        if not isinstance(generation, str) or not generation:
            raise PinnedSourceError(f"preflight source {kind} has no generation")
        if (
            not isinstance(manifest, str)
            or len(manifest) != 64
            or any(value not in "0123456789abcdef" for value in manifest.lower())
        ):
            raise PinnedSourceError(f"preflight source {kind} has no valid manifest")
        if pin_data is not None and not isinstance(pin_data, dict):
            raise PinnedSourceError(f"preflight source {kind} pin is invalid")
        key = "escape_intraday" if pin_data and kind in {"minutes", "trans"} else kind
        pin = SourcePin(key, generation, manifest.lower(), pin_data or None)
        previous = pins.get(key)
        if previous is not None and previous != pin:
            raise PinnedSourceError(f"preflight source {key} has conflicting pins")
        pins[key] = pin
    return pins


class PinnedResearchRepository:
    """Lazy proxy that never follows a mutable ``current`` pointer during execution."""

    def __init__(self, repo: Any, preflight: Any, factor: Any, scope_type: str) -> None:
        self._repo = repo
        self._pins = _pins_from_preflight(preflight)
        self._factor_id = str(factor.id)
        self._scope_type = scope_type
        self._require_resolvable_sources(tuple(factor.data_requirements))

    @classmethod
    def bind(cls, repo: Any, record: dict[str, Any], factor: Any) -> PinnedResearchRepository:
        scope = record.get("scope") if isinstance(record, dict) else None
        scope_type = scope.get("type") if isinstance(scope, dict) else None
        if scope_type not in {"symbols", "full_market"}:
            raise PinnedSourceError("durable run scope is invalid")
        return cls(repo, record.get("preflight"), factor, scope_type)

    @classmethod
    def from_sources(cls, repo: Any, sources: list[dict[str, Any]]) -> PinnedResearchRepository:
        """Build a temporary exact-pin proxy while preflight resolves dependencies."""
        instance = cls.__new__(cls)
        instance._repo = repo
        instance._pins = _pins_from_preflight({"sources": sources})
        instance._factor_id = ""
        instance._scope_type = "symbols"
        return instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)

    def _require_resolvable_sources(self, requirements: tuple[str, ...]) -> None:
        required: set[str] = set()
        if any(
            kind in requirements
            for kind in ("canonical", "markets", "calendar", "index_daily")
        ):
            required.add("canonical")
        if "markets" in requirements:
            required.add("markets")
        if "index_daily" in requirements:
            required.add("index_daily")
        if "universe" in requirements:
            required.add(
                "eligible_universe" if self._factor_id == "volume-breakout" else "universe"
            )
        if self._factor_id == "mtf-direction" and (
            "minutes" in requirements or "trans" in requirements
        ):
            required.add("ordered_trans")
        if self._scope_type == "full_market":
            required.update({"full_market", "canonical", "markets"})
        missing = sorted(required - self._pins.keys())
        if missing:
            raise PinnedSourceError(
                f"durable run lacks resolvable source pins: {','.join(missing)}"
            )

    def _pin(self, kind: str) -> SourcePin | None:
        return self._pins.get(kind)

    def _assert_identity(self, kind: str, reader: Any) -> Any:
        expected = self._pin(kind)
        if expected is None:
            return reader
        generation, manifest = reader_identity(reader)
        if generation != expected.generation or manifest != expected.manifest_sha256:
            close = getattr(reader, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            raise PinnedSourceError(f"reopened {kind} source identity mismatch")
        return reader

    def _open_research_canonical_pin(self) -> Any:
        from app.services.research_sealed_data import PublishedCanonicalDailyReader

        pin = self._pin("canonical")
        if pin is None:
            raise PinnedSourceError("canonical source pin is missing")
        return PublishedCanonicalDailyReader.from_pin(
            self._repo, pin.generation, pin.manifest_sha256
        )

    def _open_research_market_pin(self) -> Any:
        from app.data_providers.fquant.daily_market_research import (
            PublishedDailyMarketFactsReader,
        )

        canonical = self._open_research_canonical_pin()
        reader = PublishedDailyMarketFactsReader.from_canonical_manifest(canonical.manifest())
        return self._assert_identity("markets", reader)

    @property
    def generation_pinned_daily_reader(self) -> Any:
        return self._open_research_canonical_pin()

    @property
    def versioned_exchange_calendar(self) -> Any:
        reader = self._open_research_canonical_pin()
        expected = self._pin("calendar")
        if expected is not None:
            generation, manifest = reader_identity(reader)
            if generation != expected.generation or manifest != expected.manifest_sha256:
                raise PinnedSourceError("reopened calendar source identity mismatch")
        return reader

    @property
    def generation_pinned_market_facts_reader(self) -> Any:
        return self._open_research_market_pin()

    @property
    def index_daily_research_reader(self) -> Any:
        from app.data_providers.fquant.index_daily_research import PublishedIndexDailyReader

        canonical = self._open_research_canonical_pin()
        reader = PublishedIndexDailyReader.from_canonical_manifest(canonical.manifest())
        return self._assert_identity("index_daily", reader)

    @property
    def pit_presence_universe(self) -> Any:
        from app.services.universe_presence_history import (
            PublishedPresenceUniverseReader,
            universe_presence_root,
        )

        pin = self._pin("universe")
        if pin is None:
            return None
        reader = PublishedPresenceUniverseReader(
            universe_presence_root(),
            data_dir=getattr(getattr(self._repo, "store", None), "data_dir", None),
            generation=pin.generation,
            manifest_sha256=pin.manifest_sha256,
        )
        return self._assert_identity("universe", reader)

    @property
    def pit_universe(self) -> Any:
        return self.pit_presence_universe

    @property
    def pit_eligible_universe(self) -> Any:
        from app.services.universe_scd import PublishedUniverseScdReader, universe_scd_root

        pin = self._pin("eligible_universe")
        if pin is None:
            return None
        reader = PublishedUniverseScdReader(
            universe_scd_root(),
            data_dir=getattr(getattr(self._repo, "store", None), "data_dir", None),
            generation=pin.generation,
            manifest_sha256=pin.manifest_sha256,
        )
        return self._assert_identity("eligible_universe", reader)

    @property
    def n_shape_research_reader(self) -> Any:
        from app.services.n_shape_research_data import PublishedNShapeResearchReader

        reader = PublishedNShapeResearchReader.from_repository(self)
        if reader is None:
            return None
        return self._assert_identity("full_market", reader)

    def open_ordered_trans_reader(self) -> Any:
        from app.data_providers.fquant import generation
        from app.data_providers.fquant.ordered_trans import PublishedOrderedTransMinuteReader

        pin = self._pin("ordered_trans")
        if pin is None:
            return None
        root = generation.root_for("tdx_ordered_trans")
        if not root:
            raise PinnedSourceError("ordered-trans source root is unavailable")
        reader = PublishedOrderedTransMinuteReader(
            root,
            generation=pin.generation,
            manifest_sha256=pin.manifest_sha256,
        )
        return self._assert_identity("ordered_trans", reader)

    def open_escape_risk_intraday_reader(
        self,
        canonical_manifest: dict[str, Any],
        market_days: list[Any] | tuple[Any, ...],
    ) -> Any:
        from app.data_providers.fquant.escape_risk_intraday import (
            CatalogPinnedEscapeRiskIntradayReader,
        )

        pin = self._pin("escape_intraday")
        if pin is None or not isinstance(pin.pin, dict):
            return None
        markets = self._open_research_market_pin()
        try:
            reader = CatalogPinnedEscapeRiskIntradayReader.from_pin(
                market_days,
                markets,
                pin.pin.get("routes", {}),
            )
            return self._assert_identity("escape_intraday", reader)
        except Exception:
            close = getattr(markets, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            raise

    @contextmanager
    def open_pinned_market_facts(self):
        reader = self.generation_pinned_market_facts_reader
        try:
            yield reader
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    @contextmanager
    def open_index_daily_research_reader(self):
        reader = self.index_daily_research_reader
        try:
            yield reader
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()


__all__ = [
    "PinnedResearchRepository",
    "PinnedSourceError",
    "SourcePin",
    "reader_identity",
]
