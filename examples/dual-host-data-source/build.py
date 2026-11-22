"""Build both example release series from identical synthetic source observations."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

HERE = Path(__file__).resolve().parent
ENGINE_EXAMPLE = (
    HERE.parents[1] / "packages/tivelo-data/examples/native-provider/build.py"
)


def build(output):
    # The signed example creates a new directory and its own disposable signing identity.
    subprocess.run(
        [sys.executable, str(ENGINE_EXAMPLE), "--output", str(output)], check=True
    )
    pairs = []
    for version in ("1.0.0", "2.0.0"):
        engine = output / f"example-{version}.zip"
        with ZipFile(engine) as source:
            observations = source.read("payload/observations.json")
        host = output / f"tsp-archive-{version}.zip"
        manifest = yaml.safe_load((HERE / "plugin.yaml").read_bytes())
        manifest["version"] = version
        with ZipFile(host, "x", compression=ZIP_DEFLATED) as dest:
            dest.writestr("plugin.yaml", yaml.safe_dump(manifest, allow_unicode=True))
            dest.write(HERE / "provider.py", "provider.py")
            dest.writestr("observations.json", observations)
        pairs.append(
            {
                "version": version,
                "tsp": host.name,
                "tivelo": engine.name,
                "observations_sha256": hashlib.sha256(observations).hexdigest(),
            }
        )
    (output / "pairs.json").write_text(
        json.dumps(pairs, indent=2) + "\n", encoding="utf-8"
    )
    return pairs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="A new local output directory"
    )
    build(parser.parse_args().output.resolve())
