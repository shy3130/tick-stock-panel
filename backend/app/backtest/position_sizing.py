"""Compatibility import for the extracted position-policy contract."""
import sys

from tivelo_backtest.legacy import position_sizing as _implementation

sys.modules[__name__] = _implementation
