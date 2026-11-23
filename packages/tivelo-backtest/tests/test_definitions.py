import hashlib
import sys
from dataclasses import replace

import pytest
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.definitions import DefinitionBundle, DefinitionContents, DefinitionFile
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.definitions import DefinitionLoader, seal_definition


def write_definition(
    root,
    *,
    code=None,
    files=None,
    kind="strategy",
    identity="example.strategy",
    dependencies=(),
    requirements=(),
):
    files = files or {
        "strategy.py": code
        or b"""
class Strategy:
    def __init__(self, value):
        self.value = value
    def decide(self, context):
        return None
def build(*, parameters_json, dependencies):
    return Strategy(parameters_json)
"""
    }
    root.mkdir(parents=True)
    for path, payload in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    bundle = DefinitionBundle.create(
        DefinitionContents(
            definition_id=identity,
            version="1",
            kind=kind,
            entrypoint="strategy:build",
            dependencies=dependencies,
            requirements=requirements,
            files=tuple(
                DefinitionFile(
                    path=path, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
                )
                for path, payload in sorted(files.items())
            ),
        )
    )
    (root / "definition.json").write_bytes(canonical_bytes(bundle))
    return bundle


def archive(root, bundle, store):
    destination = store / bundle.bundle_id
    destination.parent.mkdir(exist_ok=True)
    root.rename(destination)


def test_inspection_does_not_execute_code_and_loaded_source_is_the_verified_copy(tmp_path):
    marker = tmp_path / "executed"
    code = (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n".encode()
        + b"""
from .helper import Strategy
def build(*, parameters_json, dependencies):
    return Strategy(parameters_json)
"""
    )
    files = {
        "strategy.py": code,
        "helper.py": b"""
class Strategy:
    def __init__(self, value): self.value = value
    def decide(self, context): return None
""",
    }
    root = tmp_path / "source"
    bundle = write_definition(root, files=files)
    loader = DefinitionLoader()
    inspected = loader.inspect(root, parameters_json='{"size": 2}')
    assert not marker.exists()
    (root / "helper.py").write_text("raise RuntimeError('changed after inspection')")
    before = tuple(sys.path)
    importers = {path for path in sys.path_importer_cache if "tivelo-definition-" in path}
    with loader.load_trusted_python(inspected) as binding:
        assert binding.reference == bundle.reference('{"size":2}')
        assert binding.implementation.value == '{"size":2}'
        assert marker.read_text() == "loaded"
        namespace = binding.implementation.__class__.__module__.split(".")[0]
        assert namespace in sys.modules
        assert tuple(sys.path) == before
    assert not any(name == namespace or name.startswith(namespace + ".") for name in sys.modules)
    assert {path for path in sys.path_importer_cache if "tivelo-definition-" in path} == importers
    with pytest.raises(TiveloError) as raised:
        loader.inspect(root)
    assert raised.value.code == ErrorCode.DEFINITION_CORRUPT


def test_dependency_closure_is_verified_and_parameters_and_instances_are_isolated(tmp_path):
    factor_root = tmp_path / "factor"
    factor = write_definition(
        factor_root,
        identity="example.factor",
        kind="factor",
        code=b"""
def build(*, parameters_json, dependencies):
    return {"parameters": parameters_json, "calls": 0}
""",
    )
    library = tmp_path / "library"
    archive(factor_root, factor, library)
    root = tmp_path / "source"
    bundle = write_definition(
        root,
        dependencies=(factor.reference('{"window":3}'),),
        code=b"""
class Strategy:
    def __init__(self, factor): self.factor = factor
    def decide(self, context): return None
def build(*, parameters_json, dependencies):
    try:
        dependencies['other'] = None
        raise AssertionError("dependency mapping is mutable")
    except TypeError:
        pass
    return Strategy(dependencies['example.factor'])
""",
    )
    loader = DefinitionLoader(dependency_root=library)
    inspected = loader.inspect(root, expected=bundle.reference())
    assert inspected.dependencies == (factor.reference('{"window":3}'),)
    with loader.load_trusted_python(inspected) as first:
        assert first.implementation.factor["parameters"] == '{"window":3}'
        first.implementation.factor["calls"] = 5
        with loader.load_trusted_python(inspected) as second:
            assert second.implementation.factor["calls"] == 0
            assert type(first.implementation) is not type(second.implementation)


def test_no_factory_runs_when_any_dependency_is_missing_or_corrupt(tmp_path):
    marker = tmp_path / "executed"
    root = tmp_path / "source"
    missing = dict(definition_id="example.factor", version="1", sha256="a" * 64)
    write_definition(
        root,
        dependencies=(missing,),
        code=f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode(),
    )
    loader = DefinitionLoader(dependency_root=tmp_path / "library")
    with pytest.raises(TiveloError) as raised:
        loader.inspect(root)
    assert raised.value.code == ErrorCode.DEFINITION_NOT_FOUND
    assert not marker.exists()


def test_undeclared_local_import_and_factory_failure_are_isolated(tmp_path):
    root = tmp_path / "source"
    write_definition(root, code=b"from .secret import value\ndef build(**kwargs): return value\n")
    (root / "secret.py").write_text("value = 'not declared'")
    loader = DefinitionLoader()
    inspected = loader.inspect(root)
    before = {name for name in sys.modules if name.startswith("_tivelo_definition_")}
    with pytest.raises(TiveloError) as raised, loader.load_trusted_python(inspected):
        pass
    assert raised.value.code == ErrorCode.STRATEGY_FAILED
    assert "not declared" not in str(raised.value)
    assert {name for name in sys.modules if name.startswith("_tivelo_definition_")} == before


@pytest.mark.parametrize("case", ["symlink", "size", "hash", "reference"])
def test_invalid_code_or_reference_is_rejected_before_loading(tmp_path, case):
    root = tmp_path / "source"
    bundle = write_definition(root)
    source = root / "strategy.py"
    expected = bundle.reference()
    if case == "symlink":
        outside = tmp_path / "outside.py"
        source.rename(outside)
        try:
            source.symlink_to(outside)
        except OSError:
            pytest.skip("symbolic links unavailable")
    elif case == "size":
        source.write_bytes(source.read_bytes() + b"\n")
    elif case == "hash":
        source.write_bytes(b" " * source.stat().st_size)
    else:
        expected = expected.model_copy(update={"sha256": "b" * 64})
    with pytest.raises(TiveloError):
        DefinitionLoader().inspect(root, expected=expected)


def test_graph_budget_rejects_oversized_sources_without_execution(tmp_path):
    root = tmp_path / "source"
    write_definition(root)
    with pytest.raises(TiveloError) as raised:
        DefinitionLoader(max_total_bytes=10).inspect(root)
    assert raised.value.code == ErrorCode.CONTRACT_INVALID


@pytest.mark.parametrize("conflict", [False, True])
def test_diamond_dependencies_are_deduplicated_but_conflicting_parameters_fail(tmp_path, conflict):
    library = tmp_path / "library"
    factor_root = tmp_path / "factor"
    factor = write_definition(factor_root, identity="example.factor", kind="factor")
    archive(factor_root, factor, library)
    refs = []
    for index in (1, 2):
        root = tmp_path / f"signal{index}"
        parameters = '{"n":2}' if index == 2 and conflict else '{"n":1}'
        bundle = write_definition(
            root,
            identity=f"example.signal{index}",
            kind="signal",
            dependencies=(factor.reference(parameters),),
        )
        archive(root, bundle, library)
        refs.append(bundle.reference())
    root = tmp_path / "source"
    write_definition(root, dependencies=tuple(refs))
    loader = DefinitionLoader(dependency_root=library, max_nodes=4)
    if conflict:
        with pytest.raises(TiveloError) as raised:
            loader.inspect(root)
        assert raised.value.code == ErrorCode.DEFINITION_CONFLICT
    else:
        inspected = loader.inspect(root)
        assert len(inspected.nodes) == 4
        assert len(inspected.dependencies) == 3


def test_seal_hashes_only_explicit_files_without_writing_or_executing(tmp_path):
    root = tmp_path / "source"
    bundle = write_definition(root)
    (root / "definition.json").unlink()
    (root / "private.txt").write_text("not part of this definition")
    sealed = seal_definition(
        root,
        definition_id="example.strategy",
        version="1",
        kind="strategy",
        entrypoint="strategy:build",
        files=("strategy.py",),
    )
    assert sealed == bundle
    assert not (root / "definition.json").exists()
    assert [file.path for file in sealed.contents.files] == ["strategy.py"]
    with pytest.raises(TiveloError):
        seal_definition(
            root,
            definition_id="example.strategy",
            version="1",
            kind="strategy",
            entrypoint="strategy:build",
            files=("../private.py",),
        )


def test_checked_dataclass_cannot_bypass_source_validation(tmp_path):
    root = tmp_path / "source"
    write_definition(root)
    loader = DefinitionLoader()
    inspected = loader.inspect(root)
    altered = replace(
        inspected.nodes[0], files=(("strategy.py", b"raise RuntimeError('unverified')"),)
    )
    forged = replace(inspected, nodes=(altered,))
    with pytest.raises(TiveloError) as raised, loader.load_trusted_python(forged):
        pass
    assert raised.value.code == ErrorCode.DEFINITION_CORRUPT


def test_file_budget_bounds_even_zero_byte_resources(tmp_path):
    root = tmp_path / "source"
    write_definition(
        root, files={"strategy.py": b"def build(**kwargs): return None\n", "zero.txt": b""}
    )
    with pytest.raises(TiveloError) as raised:
        DefinitionLoader(max_files=1).inspect(root)
    assert raised.value.code == ErrorCode.CONTRACT_INVALID
