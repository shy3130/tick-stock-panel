"""Compatibility import for the shared HTTP source config."""

import sys

from tivelo_data.http import config as _implementation

sys.modules[__name__] = _implementation
