"""Export the public field definitions: python -m tivelo_contracts.schemas PATH."""

import argparse
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from .artifacts import ResultArchive, RunArchive, SnapshotBundle
from .capabilities import Capability, CapabilityFacts
from .data import (
    AdjustmentFactor,
    Bar,
    BarQuery,
    CorporateAction,
    Coverage,
    DatasetVersion,
    EventQuery,
    HistoryQuery,
    Instrument,
    OpeningObservation,
    SnapshotManifest,
    SymbolMapping,
)
from .definitions import DefinitionBundle
from .execution import (
    BacktestResult,
    BacktestSpec,
    ExecutionDiagnostic,
    LedgerEntry,
    OrderEvent,
    PriceValue,
    RunLock,
    SignalBatch,
    StrategyContext,
)
from .extension_fields import ExtensionQuery, ExtensionValue
from .market import (
    FeeQuote,
    OrderQuantityTerms,
    PriceBounds,
    ReferencePrice,
    TradingDay,
    TradingTerms,
)
from .order_books import OrderBookObservation, OrderBookQuery
from .trade_prints import TradePrint, TradePrintQuery

PUBLIC_MODELS = (
    ExtensionQuery,
    ExtensionValue,
    RunArchive,
    ResultArchive,
    DefinitionBundle,
    SnapshotBundle,
    AdjustmentFactor,
    Bar,
    BarQuery,
    CorporateAction,
    Coverage,
    DatasetVersion,
    EventQuery,
    HistoryQuery,
    Instrument,
    OpeningObservation,
    OrderBookObservation,
    OrderBookQuery,
    TradePrint,
    TradePrintQuery,
    SnapshotManifest,
    SymbolMapping,
    BacktestResult,
    BacktestSpec,
    ExecutionDiagnostic,
    OrderEvent,
    PriceValue,
    RunLock,
    SignalBatch,
    StrategyContext,
    Capability,
    CapabilityFacts,
    TradingTerms,
    OrderQuantityTerms,
    TradingDay,
    ReferencePrice,
    PriceBounds,
    FeeQuote,
    LedgerEntry,
)


def schema_bundle() -> dict:
    _, schema = models_json_schema(
        [(model, "validation") for model in PUBLIC_MODELS],
        title="Tivelo contracts v2",
        description="Reference an individual record using #/$defs/Bar (or its public model name).",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    encoded = json.dumps(schema_bundle(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if arguments.check:
        if arguments.path.read_text(encoding="utf-8") != encoded:
            parser.exit(1, "Schema artifact differs from the public contracts\n")
    else:
        arguments.path.parent.mkdir(parents=True, exist_ok=True)
        arguments.path.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
