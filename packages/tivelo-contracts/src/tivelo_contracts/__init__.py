"""Tivelo public contracts (API v2). No TSP or engine implementation imports."""

from .base import Contract, canonical_bytes, fingerprint
from .errors import ErrorCode, TiveloError, require_api_major
from .protocols import (
    ColumnarDataReader,
    DailyExecutionRules,
    DataReader,
    ExecutionDataReader,
    ExecutionRules,
    MarketRules,
    Observer,
    PriceViewProvider,
    PriceViewRuntime,
    Strategy,
)

__version__ = "0.1.0a1"
API_MAJOR = 2

__all__ = [
    "API_MAJOR",
    "Contract",
    "ColumnarDataReader",
    "DailyExecutionRules",
    "DataReader",
    "ExecutionDataReader",
    "ErrorCode",
    "ExecutionRules",
    "MarketRules",
    "Observer",
    "PriceViewProvider",
    "PriceViewRuntime",
    "Strategy",
    "TiveloError",
    "canonical_bytes",
    "fingerprint",
    "require_api_major",
]
