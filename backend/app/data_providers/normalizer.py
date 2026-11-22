"""Compatibility import for the normalizers now owned by Tivelo Data."""

import sys

from tivelo_data.legacy import normalizer as _implementation

sys.modules[__name__] = _implementation
