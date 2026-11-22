"""Load an already authenticated native entry in a dedicated worker process."""

import importlib.util
import re
from pathlib import Path


def load_native_entry(path: Path):
    """The caller must verify the release, inventory, platform and digest first."""
    name = path.name.split(".", 1)[0]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or path.suffix not in (".so", ".pyd"):
        raise ValueError("Invalid native entry name")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Python cannot load the native entry")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
