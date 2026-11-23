"""Compatibility module for existing installation integrations."""

import sys

from tivelo_extensions import installation

sys.modules[__name__] = installation
