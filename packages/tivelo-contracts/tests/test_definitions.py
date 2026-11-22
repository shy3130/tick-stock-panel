import pytest
from pydantic import ValidationError

from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.definitions import DefinitionBundle, DefinitionContents, DefinitionFile
from tivelo_contracts.execution import PackageReference


def contents(**changes):
    values = dict(
        definition_id="example.strategy",
        version="1",
        kind="strategy",
        entrypoint="strategy:build",
        files=(DefinitionFile(path="strategy.py", sha256="a" * 64, size_bytes=1),),
    )
    values.update(changes)
    return DefinitionContents(**values)


def test_definition_identity_binds_code_dependencies_and_kind_not_invocation_parameters():
    bundle = DefinitionBundle.create(contents())
    assert DefinitionBundle.model_validate_json(canonical_bytes(bundle)) == bundle
    first, second = bundle.reference('{"size":1}'), bundle.reference('{"size":2}')
    assert first.sha256 == second.sha256 == bundle.bundle_id
    assert first.parameters_json != second.parameters_json
    dependency = first.model_copy(update={"definition_id": "example.factor"})
    changed = DefinitionBundle.create(contents(dependencies=(dependency,)))
    assert changed.bundle_id != bundle.bundle_id
    with pytest.raises(ValidationError):
        DefinitionBundle(bundle_id="b" * 64, contents=bundle.contents)


@pytest.mark.parametrize(
    "entrypoint", ["strategy", "../strategy:build", "strategy:bad-name", "__init__:build"]
)
def test_definition_entrypoint_is_a_module_factory(entrypoint):
    with pytest.raises(ValidationError):
        contents(entrypoint=entrypoint)


@pytest.mark.parametrize(
    "path",
    [
        "../strategy.py",
        "definition.json",
        "__pycache__/strategy.pyc",
        "strategy.pyc",
        "CON.py",
        "utils./main.py",
    ],
)
def test_definition_paths_are_portable_and_cannot_replace_manifest(path):
    with pytest.raises(ValidationError):
        DefinitionFile(path=path, sha256="a" * 64, size_bytes=1)


def test_definition_requires_declared_entrypoint_and_unique_portable_file_paths():
    with pytest.raises(ValidationError):
        contents(entrypoint="missing:build")
    with pytest.raises(ValidationError):
        contents(
            files=(
                DefinitionFile(path="strategy.py", sha256="a" * 64, size_bytes=1),
                DefinitionFile(path="Strategy.py", sha256="b" * 64, size_bytes=1),
            )
        )
    with pytest.raises(ValidationError):
        contents(
            files=(
                DefinitionFile(path="strategy.py", sha256="a" * 64, size_bytes=1),
                DefinitionFile(path="strategy/__init__.py", sha256="b" * 64, size_bytes=1),
            )
        )


def test_package_digest_kind_preserves_existing_wheel_lock_encoding():
    wheel = PackageReference(name="example", version="1", sha256="a" * 64)
    assert "artifact_kind" not in wheel.model_dump(mode="json")
    tree = PackageReference(
        name="example", version="1", sha256="a" * 64, artifact_kind="installed_tree_v1"
    )
    assert tree.model_dump(mode="json")["artifact_kind"] == "installed_tree_v1"


def test_controlled_definition_is_json_only_without_code_dependencies():
    file = DefinitionFile(path="program.json", sha256="a" * 64, size_bytes=2)
    value = contents(language="controlled_daily_v1", entrypoint="program.json", files=(file,))
    assert DefinitionBundle.create(value).contents.language == "controlled_daily_v1"
    for changes in (
        {"kind": "factor"}, {"requirements": ("some-package",)},
        {"entrypoint": "strategy:build"}, {"files": (file, DefinitionFile(path="source.py", sha256="b" * 64, size_bytes=1))},
    ):
        with pytest.raises(ValidationError):
            type(value).model_validate({**value.model_dump(), **changes})
