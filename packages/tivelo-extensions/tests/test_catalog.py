import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from tivelo_extensions.cli import main
from tivelo_extensions.data_descriptors import CATALOG, data_descriptors


def test_empty_installation_reads_without_creating_files(tmp_path, capsys):
    root = tmp_path / "uncreated"
    assert main(["--state", str(root), "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not root.exists()


@pytest.mark.parametrize(
    "case", ["good", "duplicate", "missing_convention", "version", "extra_id", "unknown_field"]
)
def test_signed_catalog_scope_and_version(case):
    cap = dict(
        record_type="bar",
        market_id="cn.equity",
        asset_class="equity",
        timeframes=["1d"],
        price_bases=["raw"],
        knowledge_basis="historical_versions",
        normalization_version="1",
        bar_convention_id="cn.day",
    )
    descriptor = dict(
        provider_id="example.feed", version="1.0", source_ids=["example"], capabilities=[cap]
    )
    if case == "duplicate":
        descriptor["capabilities"].append(dict(cap))
    if case == "missing_convention":
        cap["bar_convention_id"] = None
    if case == "version":
        descriptor["version"] = "2"
    if case == "extra_id":
        descriptor["provider_id"] = "other"
    if case == "unknown_field":
        cap["secret_field"] = True
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            CATALOG, json.dumps(dict(format="tivelo-dataset-providers-v1", providers=[descriptor]))
        )
    release = SimpleNamespace(version="1.0", provides=[SimpleNamespace(id="example.feed")])
    if case == "good":
        assert data_descriptors(stream.getvalue(), release)["example.feed"].source_ids == (
            "example",
        )
    else:
        with pytest.raises(ValueError):
            data_descriptors(stream.getvalue(), release)
