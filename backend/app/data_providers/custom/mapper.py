"""Compatibility import for the shared HTTP source mapper."""

import sys

from tivelo_data.http import mapper as _implementation

sys.modules[__name__] = _implementation
