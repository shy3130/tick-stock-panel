"""Verify a complete definition graph before executing selected trusted Python.

The loader never mutates sys.path or loads code from the mutable input directory.
Digests bind declared source and dependencies, not arbitrary external I/O. This
is an explicit trusted-code entry point, not a sandbox or a price-scale proof.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import stat
import sys
import tempfile
import types
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from tivelo_contracts.base import canonical_bytes, parse_json
from tivelo_contracts.definitions import (
    MAX_DEFINITION_MANIFEST_BYTES,
    DefinitionBundle,
    DefinitionContents,
    DefinitionFile,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference
from tivelo_contracts.protocols import Strategy

from .daily import StrategyBinding
from .runtime import resolve_requirements


@dataclass(frozen=True)
class CheckedDefinition:
    bundle: DefinitionBundle
    reference: DefinitionReference
    files: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class CheckedDefinitions:
    root: DefinitionReference
    nodes: tuple[CheckedDefinition, ...]

    @property
    def dependencies(self) -> tuple[DefinitionReference, ...]:
        return tuple(
            sorted(
                (node.reference for node in self.nodes if node.reference != self.root),
                key=lambda reference: reference.definition_id,
            )
        )

    @property
    def requirements(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    requirement
                    for node in self.nodes
                    for requirement in node.bundle.contents.requirements
                }
            )
        )


def _read_file(root: Path, relative: str, maximum: int) -> bytes:
    try:
        path = root
        if path.is_symlink():
            raise TiveloError(
                ErrorCode.DEFINITION_CORRUPT, "Definition root must not be a symbolic link"
            )
        for part in Path(relative).parts:
            path /= part
            if path.is_symlink():
                raise TiveloError(
                    ErrorCode.DEFINITION_CORRUPT, "Definition file must not be a symbolic link"
                )
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise TiveloError(
                ErrorCode.DEFINITION_CORRUPT, "Definition input is not a regular file"
            )
        if info.st_size > maximum:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Definition source exceeds the byte budget"
            )
        with path.open("rb") as source:
            content = source.read(maximum + 1)
        if len(content) > maximum:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Definition source exceeds the byte budget"
            )
        return content
    except FileNotFoundError as exc:
        raise TiveloError(
            ErrorCode.DEFINITION_NOT_FOUND, "A required definition file is unavailable"
        ) from exc
    except OSError as exc:
        raise TiveloError(
            ErrorCode.DEFINITION_CORRUPT, "Definition source could not be read"
        ) from exc


def _check_files(bundle, files):
    if tuple(path for path, _ in files) != tuple(file.path for file in bundle.contents.files):
        raise TiveloError(
            ErrorCode.DEFINITION_CORRUPT, "Definition source list differs from the manifest"
        )
    for declared, (path, content) in zip(bundle.contents.files, files, strict=True):
        if (
            not isinstance(content, bytes)
            or len(content) != declared.size_bytes
            or hashlib.sha256(content).hexdigest() != declared.sha256
        ):
            raise TiveloError(ErrorCode.DEFINITION_CORRUPT, "Definition source digest differs")
        if path.endswith(".py"):
            try:
                compile(content, path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, RecursionError) as exc:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Definition contains invalid Python syntax"
                ) from exc


def seal_definition(
    root: str | Path,
    *,
    definition_id: str,
    version: str,
    kind: str,
    entrypoint: str,
    files: tuple[str, ...],
    language: str = "trusted_python",
    dependencies: tuple[DefinitionReference, ...] = (),
    requirements: tuple[str, ...] = (),
    max_total_bytes: int = 64 * 1024 * 1024,
) -> DefinitionBundle:
    """Create a content manifest from explicitly selected files; do not write or execute."""
    if (
        type(max_total_bytes) is not int
        or max_total_bytes < 1
        or not 1 <= len(files) <= 4096
        or len(set(files)) != len(files)
    ):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid definition file selection or budget")
    root = Path(root).expanduser().absolute()
    remaining, declared, payload = max_total_bytes, [], []
    try:
        for relative in sorted(files):
            DefinitionFile(path=relative, sha256="0" * 64, size_bytes=0)
            content = _read_file(root, relative, remaining)
            remaining -= len(content)
            declared.append(
                DefinitionFile(
                    path=relative,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                )
            )
            payload.append((relative, content))
        bundle = DefinitionBundle.create(
            DefinitionContents(
                definition_id=definition_id,
                version=version,
                kind=kind,
                language=language,
                entrypoint=entrypoint,
                files=tuple(declared),
                dependencies=tuple(sorted(dependencies, key=lambda item: item.definition_id)),
                requirements=tuple(sorted(set(requirements))),
            )
        )
    except (ValueError, TypeError) as exc:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Invalid definition source declaration"
        ) from exc
    if len(canonical_bytes(bundle)) > MAX_DEFINITION_MANIFEST_BYTES:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Definition manifest exceeds its size budget")
    _check_files(bundle, tuple(payload))
    return bundle


class DefinitionLoader:
    def __init__(
        self,
        dependency_root: str | Path | None = None,
        *,
        max_nodes: int = 64,
        max_depth: int = 16,
        max_files: int = 4096,
        max_total_bytes: int = 64 * 1024 * 1024,
    ):
        if any(
            type(value) is not int or value < 1
            for value in (max_nodes, max_depth, max_files, max_total_bytes)
        ):
            raise ValueError("definition budgets must be positive integers")
        self.dependency_root = (
            Path(dependency_root).expanduser().absolute() if dependency_root is not None else None
        )
        self.max_nodes, self.max_depth, self.max_total_bytes = max_nodes, max_depth, max_total_bytes
        self.max_files = max_files

    def inspect(
        self,
        root: str | Path,
        *,
        parameters_json: str | None = None,
        expected: DefinitionReference | None = None,
    ) -> CheckedDefinitions:
        """Read and verify declared bytes and installed requirements; execute nothing."""
        nodes, references, visiting = [], {}, set()
        remaining = self.max_total_bytes
        remaining_files = self.max_files

        def visit(directory, requested, parameters, depth):
            nonlocal remaining, remaining_files
            if depth > self.max_depth:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Definition graph exceeds its budget")
            try:
                encoded = _read_file(directory, "definition.json", MAX_DEFINITION_MANIFEST_BYTES)
                bundle = DefinitionBundle.model_validate(parse_json(encoded.decode("utf-8")))
                reference = bundle.reference(parameters)
            except (ValueError, UnicodeError) as exc:
                raise TiveloError(
                    ErrorCode.DEFINITION_CORRUPT, "Definition manifest failed verification"
                ) from exc
            if requested is not None and reference != requested:
                raise TiveloError(
                    ErrorCode.DEFINITION_CONFLICT, "Definition differs from the locked reference"
                )
            identity = reference.definition_id
            if identity in visiting:
                raise TiveloError(
                    ErrorCode.DEFINITION_CONFLICT, "Definition graph contains a cycle"
                )
            if identity in references:
                if references[identity] != reference:
                    raise TiveloError(
                        ErrorCode.DEFINITION_CONFLICT,
                        "A definition has conflicting versions or parameters",
                    )
                return reference
            if len(references) >= self.max_nodes:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Definition graph exceeds its node budget"
                )
            references[identity] = reference
            remaining_files -= len(bundle.contents.files)
            if remaining_files < 0:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Definition graph exceeds its file budget"
                )
            visiting.add(identity)
            payload = []
            for file in bundle.contents.files:
                if file.size_bytes > remaining:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Definition source exceeds the byte budget"
                    )
                content = _read_file(directory, file.path, remaining)
                remaining -= len(content)
                payload.append((file.path, content))
            files = tuple(payload)
            _check_files(bundle, files)
            for dependency in bundle.contents.dependencies:
                if self.dependency_root is None:
                    raise TiveloError(
                        ErrorCode.DEFINITION_NOT_FOUND,
                        "No definition dependency directory was provided",
                    )
                visit(
                    self.dependency_root / dependency.sha256,
                    dependency,
                    dependency.parameters_json,
                    depth + 1,
                )
            visiting.remove(identity)
            nodes.append(CheckedDefinition(bundle, reference, files))
            return reference

        parameters = (
            parameters_json
            if parameters_json is not None
            else (expected.parameters_json if expected else "{}")
        )
        root_path = Path(root).expanduser().absolute()
        reference = visit(root_path, expected, parameters, 1)
        result = CheckedDefinitions(reference, tuple(nodes))
        self._validate_checked(result)
        return result

    def _validate_checked(self, inspected):
        """Do not treat a user-constructed frozen dataclass as verification evidence."""
        if (
            not isinstance(inspected, CheckedDefinitions)
            or type(inspected.nodes) is not tuple
            or not 1 <= len(inspected.nodes) <= self.max_nodes
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid checked definition graph")
        available, depths, total = {}, {}, 0
        file_count = 0
        for node in inspected.nodes:
            if not isinstance(node, CheckedDefinition) or type(node.files) is not tuple:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid checked definition node")
            try:
                encoded = canonical_bytes(node.bundle)
                if len(encoded) > MAX_DEFINITION_MANIFEST_BYTES:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Definition manifest exceeds its size budget"
                    )
                bundle = DefinitionBundle.model_validate_json(encoded)
                reference = DefinitionReference.model_validate_json(canonical_bytes(node.reference))
            except (ValueError, TypeError) as exc:
                raise TiveloError(
                    ErrorCode.DEFINITION_CORRUPT, "Definition manifest failed verification"
                ) from exc
            if reference != bundle.reference(reference.parameters_json):
                raise TiveloError(
                    ErrorCode.DEFINITION_CONFLICT, "Definition differs from its reference"
                )
            if reference.definition_id in available:
                raise TiveloError(
                    ErrorCode.DEFINITION_CONFLICT, "Duplicate definition in dependency graph"
                )
            _check_files(bundle, node.files)
            file_count += len(node.files)
            if file_count > self.max_files:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Definition graph exceeds its file budget"
                )
            total += sum(len(payload) for _, payload in node.files)
            if total > self.max_total_bytes:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Definition source exceeds the byte budget"
                )
            for dependency in bundle.contents.dependencies:
                if available.get(dependency.definition_id) != dependency:
                    raise TiveloError(
                        ErrorCode.DEFINITION_CONFLICT,
                        "Definition dependency graph is incomplete or inconsistent",
                    )
            depth = 1 + max(
                (depths[dependency.definition_id] for dependency in bundle.contents.dependencies),
                default=0,
            )
            if depth > self.max_depth:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Definition graph exceeds its depth budget"
                )
            available[reference.definition_id] = reference
            depths[reference.definition_id] = depth
        if inspected.nodes[-1].reference != inspected.root:
            raise TiveloError(
                ErrorCode.DEFINITION_CONFLICT, "Definition graph has a different root"
            )
        reachable = {inspected.root.definition_id}
        for node in reversed(inspected.nodes):
            if node.reference.definition_id in reachable:
                reachable.update(ref.definition_id for ref in node.bundle.contents.dependencies)
        if reachable != set(available):
            raise TiveloError(
                ErrorCode.DEFINITION_CONFLICT, "Definition graph contains undeclared components"
            )
        resolve_requirements(inspected.requirements)

    def validate(self, inspected: CheckedDefinitions) -> None:
        """Recheck an immutable captured graph without executing factories."""
        self._validate_checked(inspected)

    @contextmanager
    def load_trusted_python(self, inspected: CheckedDefinitions):
        def bind(strategy):
            if not isinstance(strategy, Strategy) or not callable(strategy.decide):
                raise ValueError("strategy factory returned an invalid implementation")
            return StrategyBinding(strategy, inspected.root, inspected.dependencies)

        with self._load_trusted_root(inspected, bind) as binding:
            yield binding

    @staticmethod
    def _matrix_strategy(strategy):
        if not all(
            callable(getattr(strategy, name, None))
            for name in ("required_warmup_bars", "compute_signals")
        ):
            raise ValueError("strategy factory returned an invalid daily matrix strategy")
        return strategy

    def load_trusted_matrix(self, inspected: CheckedDefinitions):
        return self._load_trusted_root(inspected, self._matrix_strategy)

    def load_trusted_legacy_daily(self, inspected: CheckedDefinitions):
        from .legacy.panel_strategy import LegacyPolarsStrategy

        def bind(strategy):
            return strategy if isinstance(strategy, LegacyPolarsStrategy) else self._matrix_strategy(strategy)

        return self._load_trusted_root(inspected, bind)

    def load_trusted_legacy_minute(self, inspected: CheckedDefinitions):
        from .legacy.minute_strategy import LegacyMinuteStrategy

        def bind(strategy):
            if not isinstance(strategy, LegacyMinuteStrategy):
                raise ValueError("Strategy factory did not return a legacy minute strategy")
            return strategy

        return self._load_trusted_root(inspected, bind)

    @contextmanager
    def _load_trusted_root(self, inspected: CheckedDefinitions, bind):
        self._validate_checked(inspected)
        if any(node.bundle.contents.language != "trusted_python" for node in inspected.nodes):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Python loader requires Python definitions")
        if inspected.nodes[-1].bundle.contents.kind != "strategy":
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "The execution root must be a strategy")
        namespace = "_tivelo_definition_" + uuid.uuid4().hex
        owned_directory = None
        try:
            with tempfile.TemporaryDirectory(prefix="tivelo-definition-") as temporary:
                directory = Path(temporary)
                owned_directory = str(directory)
                # Materialize every already-verified byte before executing any factory.
                for index, node in enumerate(inspected.nodes):
                    for relative, payload in node.files:
                        path = directory / f"d{index}" / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(payload)
                parent = types.ModuleType(namespace)
                parent.__path__ = [str(directory)]
                parent.__package__ = namespace
                sys.modules[namespace] = parent
                instances = {}
                try:
                    for index, node in enumerate(inspected.nodes):
                        module_name, factory_name = node.bundle.contents.entrypoint.split(":")
                        package = f"{namespace}.d{index}"
                        module = importlib.import_module(f"{package}.{module_name}")
                        factory = getattr(module, factory_name, None)
                        if not callable(factory):
                            raise ValueError("entrypoint factory is unavailable")
                        dependencies = MappingProxyType(
                            {
                                ref.definition_id: instances[ref.definition_id]
                                for ref in node.bundle.contents.dependencies
                            }
                        )
                        instance = factory(
                            parameters_json=node.reference.parameters_json,
                            dependencies=dependencies,
                        )
                        if instance is None:
                            raise ValueError("definition factory returned no component")
                        instances[node.reference.definition_id] = instance
                    binding = bind(instances[inspected.root.definition_id])
                except (Exception, SystemExit) as exc:
                    raise TiveloError(
                        ErrorCode.STRATEGY_FAILED, "Trusted definition loading failed"
                    ) from exc
                yield binding
        finally:
            for name in tuple(sys.modules):
                if name == namespace or name.startswith(namespace + "."):
                    sys.modules.pop(name, None)
            if owned_directory is not None:
                for path in tuple(sys.path_importer_cache):
                    if path == owned_directory or path.startswith(owned_directory + os.sep):
                        sys.path_importer_cache.pop(path, None)
            importlib.invalidate_caches()
