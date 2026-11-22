"""Compatibility import for the extracted v1 market clock."""

import sys

from tivelo_market_cn.legacy import market_time as _implementation

sys.modules[__name__] = _implementation
