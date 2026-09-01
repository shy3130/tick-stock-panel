"""Identity checks between runner-owned and adapter-specific pinned readers."""

from __future__ import annotations

from typing import Any

from app.services.full_market_research import RunnerContext


def source_reader_matches(
    context: RunnerContext,
    source: str,
    reader: Any,
) -> bool:
    """Return whether ``reader`` is the exact source pinned by the runner."""
    source_provenance = getattr(context.reader, "source_provenance", None)
    generation = getattr(reader, "generation", None)
    manifest_sha256 = getattr(reader, "manifest_sha256", None)
    if not all(callable(value) for value in (source_provenance, generation, manifest_sha256)):
        return False
    try:
        pinned_sources = source_provenance()
        pinned = pinned_sources.get(source) if isinstance(pinned_sources, dict) else None
        return isinstance(pinned, dict) and (
            generation(),
            manifest_sha256(),
        ) == (
            pinned.get("generation"),
            pinned.get("manifest_sha256"),
        )
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False


def production_scope_matches(
    context: RunnerContext,
    canonical_reader: Any,
    market_facts_reader: Any,
) -> bool:
    """Validate the canonical and markets legs of a production reader scope."""
    return source_reader_matches(context, "canonical", canonical_reader) and source_reader_matches(
        context, "markets", market_facts_reader
    )
