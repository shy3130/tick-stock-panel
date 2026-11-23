"""Build two signed sample versions in a local directory using a throwaway publisher identity."""

import argparse
import base64
import hashlib
import io
import json
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.tags import sys_tags

PACKAGE = "example.daily-feed"
CAPABILITY = "example.synthetic-bars"
SOURCE = Path(__file__).with_name("tivelo_sample_provider.py")
REQUEST = dict(
    dataset_id="demo.daily",
    record_type="bar",
    market_id="cn.equity",
    asset_class="equity",
    instrument_ids=["demo.security"],
    start="2025-01-02T00:00:00Z",
    end="2025-01-04T00:00:00Z",
    timeframe="1d",
    price_basis="raw",
)


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def trust_document(key):
    return {
        "keys": [
            dict(
                issuer="example",
                kid="example-local",
                public_key=encoded(key.public_key().public_bytes_raw()),
                extension_ids=[PACKAGE],
                capability_ids=[CAPABILITY],
            )
        ]
    }


def compile_module(output, version, *, source=None):
    name = SOURCE.stem
    target = output / (name + sysconfig.get_config_var("EXT_SUFFIX"))
    module = output / SOURCE.name
    module.write_text(
        (source if source is not None else SOURCE.read_text()).replace(
            'VERSION = "1.0.0"', f"VERSION = {version!r}"
        )
    )
    # setuptools locates the active interpreter SDK and the platform C compiler.
    subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--isolated",
            "--python",
            sys.executable,
            "--with",
            "Cython>=3,<4",
            "--with",
            "setuptools>=75",
            "python",
            "-I",
            "-c",
            "from setuptools import setup; from Cython.Build import cythonize; "
            "setup(name='tivelo-sample-provider', "
            "ext_modules=cythonize(['tivelo_sample_provider.py'], language_level=3), "
            "script_args=['build_ext','--inplace'])",
        ],
        cwd=output,
        check=True,
        capture_output=True,
        timeout=180,
    )
    return target


def sample_bar(day, close):
    return dict(
        instrument_id="demo.security",
        timeframe="1d",
        bar_start=f"{day}T01:30:00Z",
        bar_end=f"{day}T07:00:00Z",
        trading_date=day,
        session_id="cn.synthetic.day",
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1000",
        quantity_unit="share",
        quote_currency="CNY",
        turnover=str(int(close) * 1000),
        price_basis="raw",
        available_at=f"{day}T07:00:00Z",
        ingested_at=f"{day}T07:01:00Z",
        source_id="example.archive",
        revision_id="r1",
    )


def bundle(
    binary,
    key,
    version,
    close,
    *,
    catalog_version=None,
    omit_catalog=False,
    license_kind="community",
):
    descriptor = dict(
        provider_id=CAPABILITY,
        version=catalog_version or version,
        source_ids=["example.archive"],
        capabilities=[
            dict(
                record_type="bar",
                market_id="cn.equity",
                asset_class="equity",
                timeframes=["1d"],
                price_bases=["raw"],
                knowledge_basis="historical_versions",
                normalization_version="example-bars-v1",
                bar_convention_id="cn.synthetic.day",
            )
        ],
    )
    entry = "payload/" + binary.name
    rows = [sample_bar(day, "10") for day in ("2025-01-02", "2025-01-03")]
    if close != "10":
        for day in ("2025-01-02", "2025-01-03"):
            rows.append(
                dict(
                    sample_bar(day, close),
                    revision_id="r2",
                    available_at="2025-01-06T08:00:00Z",
                    ingested_at="2025-01-06T08:01:00Z",
                )
            )
    files = {
        entry: binary.read_bytes(),
        "payload/observations.json": json.dumps(rows).encode(),
    }
    if not omit_catalog:
        files["payload/providers.json"] = json.dumps(
            dict(format="tivelo-dataset-providers-v1", providers=[descriptor])
        ).encode()
    cap = dict(id=CAPABILITY, kind="dataset_provider", api_version=2)
    if license_kind == "commercial":
        cap["feature_id"] = "example.data.read"
    claims = dict(
        format="tivelo-engine-extension",
        format_version=1,
        id=PACKAGE,
        issuer="example",
        kind="data-provider",
        version=version,
        license_kind=license_kind,
        entrypoint=entry,
        provides=[cap],
        requires=dict(
            contracts_api=2,
            extension_api=2,
            engine_version=">=0.1.0a1,<0.2",
            runtime_generation="tivelo-alpha1",
            target_tags=[str(next(sys_tags()))],
        ),
        files=[
            dict(path=k, size_bytes=len(v), sha256=hashlib.sha256(v).hexdigest())
            for k, v in sorted(files.items())
        ],
    )
    raw = rfc8785.dumps(
        dict(
            protected=dict(
                version=1, alg="Ed25519", type="engine-release", kid="example-local"
            ),
            claims=claims,
        )
    )
    files["tivelo-engine.json"] = json.dumps(
        dict(
            signed=encoded(raw),
            signature=encoded(key.sign(b"tivelo-engine-release-v1\0" + raw)),
        )
    ).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New output directory"
    )
    output = parser.parse_args().output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    key = Ed25519PrivateKey.generate()
    (output / "trust.json").write_text(json.dumps(trust_document(key), indent=2))
    capture = dict(kind="engine_data_provider", provider_id=CAPABILITY, request=REQUEST)
    (output / "capture.json").write_text(json.dumps(capture, indent=2))
    for version, close in (("1.0.0", "10"), ("2.0.0", "12")):
        with tempfile.TemporaryDirectory() as compilation:
            binary = compile_module(Path(compilation), version)
            (output / f"example-{version}.zip").write_bytes(
                bundle(binary, key, version, close)
            )


if __name__ == "__main__":
    main()
