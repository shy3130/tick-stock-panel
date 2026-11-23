"""Compatibility import for the provider contracts now owned by Tivelo Data."""

import sys

from tivelo_data.legacy import base as _implementation

# Preserve class identity and existing plugin imports, including monkeypatching.
sys.modules[__name__] = _implementation
