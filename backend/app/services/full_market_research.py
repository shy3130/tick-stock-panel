"""Shared offline full-market research runner (auditable, read-only).

Resolves ONE pinned reader in-process (``repo.n_shape_research_reader``, the
canonical+markets composite), derives the PIT universe cohort for the requested
window, and hands the COMPLETE cohort to the target evaluator exactly once.

The runner never splits the cohort at the verdict layer and never averages or
stitches partial verdicts; cluster bootstrap stays inside the evaluators. Data
reads may stream per-symbol inside an evaluator — that is not visible here.

The runner opens the pinned reader for exactly one run and closes it again on
success and on failure alike. An evaluator ``status="unavailable"`` verdict is
an auditable research outcome: it is returned — and atomically writable — with
its reasons intact, never raised as an execution error.

The request echo prefers an explicit ``echo()`` on the evaluator request, then
Pydantic ``model_dump(mode="json")``, then a JSON-safe dataclass ``asdict``;
fail closed when none applies. Fail-closed errors: unknown factor, missing
pinned reader or provenance, empty cohort, unserializable echo.

Stable output schema::

    {
      "schema": "tickflow.research.full-market-runner.v1",
      "research_id": "fm-<16 hex>",
      "request": {...evaluator request echo...},
      "cohort": {"count": int, "hash": "<sha256>"},
      "provenance": {"pinned_reader": {...}},
      "coverage": {...} | null,
      "verdict": {...single-pass evaluator payload...}
    }

JSON output is written atomically (same-directory temp file + ``os.replace``)
and only to an explicitly provided path; the runner never defaults to writing
into the repository (docs or data).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

RUNNER_SCHEMA = "tickflow.research.full-market-runner.v1"
PINNED_READER_ATTR = "n_shape_research_reader"
_SHA256_CHARS = frozenset("0123456789abcdef")


def _valid_manifest_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= _SHA256_CHARS


class FullMarketRunnerError(RuntimeError):
    """Raised fail-closed when the runner cannot produce an auditable verdict."""


@dataclass(frozen=True, slots=True)
class RunnerContext:
    """Runtime-owned repositories/readers shared by one full-market run."""

    repo: Any
    reader: Any


@runtime_checkable
class FactorAdapter(Protocol):
    """Narrow adapter seam between the runner and one factor evaluator.

    ``build_request`` receives the FULL cohort; ``evaluate`` is invoked exactly
    once with that request. Extensions register a new adapter via
    :func:`register_adapter` — no runner changes required.
    """

    name: str

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
    ) -> Any: ...

    def evaluate(self, context: RunnerContext, request: Any) -> Any: ...

    def serialize_verdict(self, verdict: Any) -> dict[str, Any]: ...

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None: ...


ADAPTERS: dict[str, FactorAdapter] = {}


def register_adapter(adapter: FactorAdapter, *, overwrite: bool = False) -> None:
    """Register a factor adapter; duplicate names fail loudly unless overwritten."""
    if not getattr(adapter, "name", None):
        raise FullMarketRunnerError("adapter_name_required")
    if adapter.name in ADAPTERS and not overwrite:
        raise FullMarketRunnerError(f"adapter_already_registered: {adapter.name}")
    ADAPTERS[adapter.name] = adapter


def register_builtin_adapters() -> None:
    """Import shipped adapters lazily and fill only missing registry entries.

    Adapter modules import :class:`RunnerContext` from this module, so imports
    remain deferred until the runner surface is fully defined. Existing
    entries are preserved so tests and explicit extensions may override a
    builtin with :func:`register_adapter(overwrite=True)`.
    """
    from app.services.full_market_adapters.doji import DojiPatternsFullMarketAdapter
    from app.services.full_market_adapters.dugu import DuguTrendAdapter
    from app.services.full_market_adapters.escape_risk import EscapeRiskAdapter
    from app.services.full_market_adapters.hold_firm import HoldFirmAdapter
    from app.services.full_market_adapters.macd import MacdArmsAdapter
    from app.services.full_market_adapters.mera import MeraAdapter
    from app.services.full_market_adapters.n_depth import NDepthAdapter
    from app.services.full_market_adapters.negative_v5 import NegativeV5Adapter
    from app.services.full_market_adapters.pre_surge import PreSurgeAdapter
    from app.services.full_market_adapters.single_yang import SingleYangFullMarketAdapter
    from app.services.full_market_adapters.weekly_flagpole import WeeklyFlagpoleAdapter

    for adapter in (
        MacdArmsAdapter(),
        WeeklyFlagpoleAdapter(),
        SingleYangFullMarketAdapter(),
        DuguTrendAdapter(),
        PreSurgeAdapter(),
        HoldFirmAdapter(),
        MeraAdapter(),
        NDepthAdapter(),
        NegativeV5Adapter(),
        EscapeRiskAdapter(),
        DojiPatternsFullMarketAdapter(),
    ):
        if adapter.name not in ADAPTERS:
            register_adapter(adapter)


def registered_factor_names() -> list[str]:
    """All registered factor names with builtins ensured, sorted for CLI choices."""
    register_builtin_adapters()
    return sorted(ADAPTERS)


def resolve_pinned_reader(repo: Any) -> Any:
    """Resolve the shared pinned composite reader; ``None`` fails closed."""
    reader = getattr(repo, PINNED_READER_ATTR, None)
    return reader


def reader_provenance(reader: Any) -> dict[str, Any]:
    """Extract sealed-generation provenance; missing/invalid provenance fails closed."""
    generation = getattr(reader, "generation", None)
    manifest = getattr(reader, "manifest_sha256", None)
    if not callable(generation) or not callable(manifest):
        raise FullMarketRunnerError("pinned_reader_provenance_missing")
    generation_value, manifest_value = generation(), manifest()
    if not isinstance(generation_value, str) or not generation_value:
        raise FullMarketRunnerError("pinned_reader_provenance_invalid")
    if not _valid_manifest_hash(manifest_value):
        raise FullMarketRunnerError("pinned_reader_provenance_invalid")
    provenance: dict[str, Any] = {
        "generation": generation_value,
        "manifest_sha256": manifest_value.lower(),
    }
    provider = getattr(reader, "provider_id", None)
    if callable(provider):
        provenance["provider_id"] = provider()
    source = getattr(reader, "source_provenance", None)
    if callable(source):
        provenance["source_provenance"] = source()
    return provenance


def collect_cohort(reader: Any, start: date, end: date) -> list[str]:
    """Deduplicate, normalize and sort the PIT universe; empty cohort fails closed."""
    universe = getattr(reader, "universe", None)
    if not callable(universe):
        raise FullMarketRunnerError("reader_universe_missing")
    raw = universe(start, end)
    cohort = sorted({str(symbol).strip().upper() for symbol in raw if str(symbol).strip()})
    if not cohort:
        raise FullMarketRunnerError("universe_empty")
    return cohort


def cohort_digest(cohort: list[str]) -> str:
    return hashlib.sha256("\n".join(cohort).encode()).hexdigest()


def _research_id(factor: str, request: dict[str, Any], cohort_hash: str) -> str:
    material = json.dumps(
        {"factor": factor, "request": request, "cohort_hash": cohort_hash},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "fm-" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _close_reader(reader: Any) -> None:
    """Best-effort close of the runner-opened reader; cleanup never masks the outcome."""
    close = getattr(reader, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _request_echo(request: Any) -> dict[str, Any]:
    """JSON-safe request echo without assuming Pydantic.

    Preference order: explicit ``echo()`` (adapter-chosen representation),
    Pydantic ``model_dump(mode="json")``, dataclass ``asdict``. Fail closed
    when no strategy applies — an auditable run needs its request echoed.
    """
    if callable(getattr(request, "echo", None)):
        echoed = request.echo()
        if isinstance(echoed, dict):
            return _json_safe(echoed)
    model_dump = getattr(request, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return _json_safe(dumped)
    if is_dataclass(request) and not isinstance(request, type):
        return _json_safe(asdict(request))
    raise FullMarketRunnerError("request_echo_unserializable")


def run_full_market_research(
    factor: str,
    repo: Any,
    start: date,
    end: date,
    *,
    oos_start: date | None = None,
    cost_bps: float | None = None,
) -> dict[str, Any]:
    """Run one factor over the full pinned-market cohort in a single verdict pass.

    Opens the pinned reader and closes it again on success and failure alike.
    An evaluator ``status="unavailable"`` verdict is an auditable research
    outcome and is returned with its reasons, never raised.
    """
    register_builtin_adapters()
    adapter = ADAPTERS.get(factor)
    if adapter is None:
        raise FullMarketRunnerError(f"unknown_factor: {factor}")
    reader = resolve_pinned_reader(repo)
    if reader is None:
        raise FullMarketRunnerError("pinned_reader_missing")
    try:
        provenance = reader_provenance(reader)
        cohort = collect_cohort(reader, start, end)
        cohort_hash = cohort_digest(cohort)
        request = adapter.build_request(start, end, cohort, oos_start=oos_start, cost_bps=cost_bps)
        context = RunnerContext(repo=repo, reader=reader)
        verdict = adapter.serialize_verdict(adapter.evaluate(context, request))
        if not isinstance(verdict, dict):
            raise FullMarketRunnerError("verdict_not_serializable")
        request_echo = verdict.get("request")
        if not isinstance(request_echo, dict):
            request_echo = _request_echo(request)
        return {
            "schema": RUNNER_SCHEMA,
            "research_id": _research_id(factor, request_echo, cohort_hash),
            "request": request_echo,
            "cohort": {"count": len(cohort), "hash": cohort_hash},
            "provenance": {"pinned_reader": provenance},
            "coverage": adapter.extract_coverage(verdict),
            "verdict": verdict,
        }
    finally:
        _close_reader(reader)


def write_payload_json(payload: dict[str, Any], output: str | os.PathLike[str]) -> Path:
    """Atomically write payload JSON to an explicit path (temp file + replace)."""
    target = Path(output)
    parent = target.parent
    if not parent.is_dir():
        raise FullMarketRunnerError(f"output_parent_missing: {parent}")
    handle, temp_name = tempfile.mkstemp(dir=parent, prefix=f"{target.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                default=str,
            )
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return target
