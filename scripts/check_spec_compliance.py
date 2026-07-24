from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys

import yaml


VALID_PROJECT_STATUSES = {"configured", "unconfigured"}
VALID_SPEC_STATUSES = {"authoritative", "draft", "superseded"}
VALID_CONFLICT_STATUSES = {"resolved", "unresolved"}
REQUIRED_EXCEPTION_FIELDS = ("scope", "owner", "reason", "approval", "expires")
ACCEPTANCE_TYPES = {"semantic-acceptance", "independent-review", "golden", "snapshot"}
NONEXECUTABLE_TEST_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_yaml(repository: Path, relative_path: str, errors: list[str]) -> dict[str, object] | None:
    path = repository / relative_path
    if not path.is_file():
        errors.append(f"missing required file: {relative_path}")
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        summary = " ".join(str(error).split())
        errors.append(f"invalid YAML in {relative_path}: {summary}")
        return None
    except (OSError, UnicodeError) as error:
        errors.append(f"unable to read {relative_path}: {error}")
        return None
    if not isinstance(loaded, dict):
        errors.append(f"expected a mapping in {relative_path}")
        return None
    return loaded


def repository_file(value: object, repository: Path) -> Path | None:
    if not is_nonempty_string(value):
        return None
    path_value = str(value)
    if Path(path_value).is_absolute() or PurePosixPath(path_value).is_absolute() or PureWindowsPath(path_value).is_absolute():
        return None
    candidate = (repository / path_value).resolve()
    try:
        candidate.relative_to(repository.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def validate_version(contract: dict[str, object], relative_path: str, errors: list[str]) -> None:
    if type(contract.get("version")) is not int or contract["version"] != 1:
        errors.append(f"{relative_path} must contain version: 1")


def collection(contract: dict[str, object], name: str, errors: list[str]) -> list[object]:
    value = contract.get(name)
    if not isinstance(value, list):
        errors.append(f"{name} must be a list")
        return []
    return value


def validate_project_status(index: dict[str, object], errors: list[str]) -> None:
    status = index.get("project_status")
    if status not in VALID_PROJECT_STATUSES:
        errors.append("project_status must be configured or unconfigured")
    elif status == "unconfigured":
        errors.append("project is unconfigured")


def validate_specifications(
    specifications: list[object], repository: Path, errors: list[str]
) -> tuple[dict[str, str], dict[str, list[str] | None]]:
    statuses: dict[str, str] = {}
    declared: dict[str, list[str] | None] = {}
    identifiers: set[str] = set()
    for entry in specifications:
        if not isinstance(entry, dict):
            errors.append("each specification must be a mapping")
            continue
        identifier = entry.get("id")
        if not is_nonempty_string(identifier):
            errors.append("each specification needs a non-empty id")
            continue
        identifier = str(identifier)
        if identifier in identifiers:
            errors.append(f"duplicate specification id: {identifier}")
        identifiers.add(identifier)
        status = entry.get("status")
        if not isinstance(status, str) or status not in VALID_SPEC_STATUSES:
            errors.append(f"invalid status for specification {identifier}")
        else:
            statuses.setdefault(identifier, status)
        if repository_file(entry.get("path"), repository) is None:
            errors.append(f"invalid repository-relative specification path for {identifier}")
        if status != "authoritative":
            declared.setdefault(identifier, None)
            continue
        raw_requirements = entry.get("requirements")
        if not isinstance(raw_requirements, list):
            errors.append(f"authoritative specification {identifier} must contain a requirements list")
            declared.setdefault(identifier, None)
            continue
        requirement_ids: list[str] = []
        seen: set[str] = set()
        for requirement_id in raw_requirements:
            if not is_nonempty_string(requirement_id):
                errors.append(f"specification {identifier} has an invalid requirement id")
                continue
            requirement_id = str(requirement_id)
            if requirement_id in seen:
                errors.append(f"specification {identifier} has duplicate requirement id: {requirement_id}")
            seen.add(requirement_id)
            requirement_ids.append(requirement_id)
        declared.setdefault(identifier, requirement_ids)
    return statuses, declared


def validate_conflicts(
    conflicts: list[object], specification_statuses: dict[str, str], repository: Path, errors: list[str]
) -> None:
    identifiers: set[str] = set()
    known = set(specification_statuses)
    for entry in conflicts:
        if not isinstance(entry, dict):
            errors.append("each conflict must be a mapping")
            continue
        identifier = entry.get("id")
        if not is_nonempty_string(identifier):
            errors.append("each conflict needs a non-empty id")
            continue
        identifier = str(identifier)
        if identifier in identifiers:
            errors.append(f"duplicate conflict id: {identifier}")
        identifiers.add(identifier)
        referenced = entry.get("specifications")
        if not isinstance(referenced, list):
            errors.append(f"conflict {identifier} must reference at least two known specifications")
        else:
            known_references: set[str] = set()
            for specification in referenced:
                if not is_nonempty_string(specification) or specification not in known:
                    errors.append(f"conflict {identifier} references an unknown specification: {specification}")
                else:
                    known_references.add(str(specification))
            if len(known_references) < 2:
                errors.append(f"conflict {identifier} must reference at least two known specifications")
        status = entry.get("status")
        if not isinstance(status, str) or status not in VALID_CONFLICT_STATUSES:
            errors.append(f"invalid status for conflict {identifier}")
        elif status == "unresolved":
            errors.append(f"unresolved conflict: {identifier}")
        else:
            if not is_nonempty_string(entry.get("resolution")):
                errors.append(f"resolved conflict {identifier} is missing resolution")
            if repository_file(entry.get("decision"), repository) is None:
                errors.append(f"resolved conflict {identifier} has an invalid decision path")


def expires_on(value: object) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def validate_exceptions(exceptions: list[object], errors: list[str]) -> set[str]:
    identifiers: set[str] = set()
    valid_scopes: set[str] = set()
    today = datetime.now(timezone.utc).date()
    for entry in exceptions:
        if not isinstance(entry, dict):
            errors.append("each exception must be a mapping")
            continue
        identifier = entry.get("id")
        if not is_nonempty_string(identifier):
            errors.append("each exception needs a non-empty id")
            continue
        identifier = str(identifier)
        duplicate = identifier in identifiers
        if duplicate:
            errors.append(f"duplicate exception id: {identifier}")
        identifiers.add(identifier)
        complete = True
        for field in REQUIRED_EXCEPTION_FIELDS:
            if field != "expires" and not is_nonempty_string(entry.get(field)):
                errors.append(f"exception {identifier} is missing {field}")
                complete = False
        expires = entry.get("expires")
        if expires is None or expires == "":
            errors.append(f"exception {identifier} is missing expires")
            complete = False
        else:
            expiry = expires_on(expires)
            if expiry is None:
                errors.append(f"invalid expires date for exception {identifier}")
                complete = False
            elif expiry < today:
                errors.append(f"expired exception: {identifier}")
                complete = False
        if complete and not duplicate:
            valid_scopes.add(str(entry["scope"]))
    return valid_scopes


def implementation_paths(value: object) -> list[str] | None:
    if is_nonempty_string(value):
        return [str(value)]
    if isinstance(value, list) and value and all(is_nonempty_string(item) for item in value):
        return [str(item) for item in value]
    return None


def typed_evidence(value: object) -> list[dict[str, str]] | None:
    entries = value if isinstance(value, list) else [value]
    if not entries:
        return None
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "type"}:
            return None
        if not is_nonempty_string(entry.get("path")) or not is_nonempty_string(entry.get("type")):
            return None
        normalized.append({"path": str(entry["path"]), "type": str(entry["type"])})
    return normalized


def validate_requirements(
    requirements: list[object], specification_statuses: dict[str, str], valid_exception_scopes: set[str],
    repository: Path, errors: list[str]
) -> dict[str, str]:
    identifiers: set[str] = set()
    ownership: dict[str, str] = {}
    tests_directory = (repository / "tests").resolve()
    for entry in requirements:
        if not isinstance(entry, dict):
            errors.append("each requirement must be a mapping")
            continue
        identifier = entry.get("id")
        if not is_nonempty_string(identifier):
            errors.append("each requirement needs a non-empty id")
            continue
        identifier = str(identifier)
        if identifier in identifiers:
            errors.append(f"duplicate requirement id: {identifier}")
        identifiers.add(identifier)
        for field in ("specification", "implementation", "tests", "acceptance"):
            if field not in entry:
                errors.append(f"requirement {identifier} is missing {field}")
        specification = entry.get("specification")
        if is_nonempty_string(specification):
            specification = str(specification)
            ownership.setdefault(identifier, specification)
            status = specification_statuses.get(specification)
            if status is None:
                errors.append(f"requirement {identifier} references an unknown specification: {specification}")
            elif status != "authoritative":
                errors.append(f"requirement {identifier} references a {status} specification: {specification}")
        elif "specification" in entry:
            errors.append(f"requirement {identifier} references an unknown specification: {specification}")

        if "implementation" in entry:
            paths = implementation_paths(entry["implementation"])
            if paths is None:
                errors.append(f"requirement {identifier} has an invalid implementation evidence shape")
            else:
                for path_value in paths:
                    if repository_file(path_value, repository) is None:
                        errors.append(f"requirement {identifier} has an invalid implementation path")

        for field in ("tests", "acceptance"):
            if field not in entry:
                continue
            evidence = typed_evidence(entry[field])
            if evidence is None:
                errors.append(f"requirement {identifier} has an invalid {field} evidence shape")
                continue
            for item in evidence:
                path_value, evidence_type = item["path"], item["type"]
                resolved = repository_file(path_value, repository)
                if resolved is None:
                    errors.append(f"requirement {identifier} has an invalid {field} path")
                if field == "tests":
                    if evidence_type != "executable-test":
                        errors.append(f"requirement {identifier} has invalid tests evidence type: {evidence_type}")
                    if resolved is not None:
                        try:
                            resolved.relative_to(tests_directory)
                        except ValueError:
                            errors.append(f"requirement {identifier} has a tests path outside tests/: {path_value}")
                    if Path(path_value).suffix.casefold() in NONEXECUTABLE_TEST_SUFFIXES:
                        errors.append(f"requirement {identifier} has non-executable tests path: {path_value}")
                elif evidence_type not in ACCEPTANCE_TYPES:
                    errors.append(f"requirement {identifier} has invalid acceptance evidence type: {evidence_type}")
            if field == "acceptance":
                types = [item["type"] for item in evidence]
                if "semantic-acceptance" not in types:
                    if set(types).issubset({"golden", "snapshot"}):
                        if identifier not in valid_exception_scopes:
                            errors.append(
                                f"requirement {identifier} has golden-only acceptance without a valid exact-scope exception"
                            )
                    else:
                        errors.append(f"requirement {identifier} lacks semantic-acceptance evidence")
    return ownership


def validate_exact_coverage(
    statuses: dict[str, str], declared: dict[str, list[str] | None], ownership: dict[str, str],
    valid_exception_scopes: set[str], errors: list[str]
) -> None:
    for specification, status in statuses.items():
        if status != "authoritative":
            continue
        requirement_ids = declared.get(specification)
        if requirement_ids is None:
            continue
        if not requirement_ids and specification not in valid_exception_scopes:
            errors.append(
                f"authoritative specification {specification} has no requirements and no valid exact-scope exception"
            )
        declared_set = set(requirement_ids)
        owned_set = {identifier for identifier, owner in ownership.items() if owner == specification}
        for identifier in sorted(declared_set - set(ownership)):
            errors.append(f"specification {specification} declares untraced requirement id: {identifier}")
        for identifier in sorted(declared_set & set(ownership)):
            if ownership[identifier] != specification:
                errors.append(f"specification {specification} declares cross-owned requirement id: {identifier}")
        for identifier in sorted(owned_set - declared_set):
            errors.append(f"specification {specification} is missing traced requirement id: {identifier}")


def validate(repository: Path) -> list[str]:
    repository = repository.resolve()
    errors: list[str] = []
    index = load_yaml(repository, "docs/spec-index.yaml", errors)
    traceability = load_yaml(repository, "docs/traceability.yaml", errors)
    statuses: dict[str, str] = {}
    declared: dict[str, list[str] | None] = {}
    valid_exception_scopes: set[str] = set()
    if index is not None:
        validate_version(index, "docs/spec-index.yaml", errors)
        validate_project_status(index, errors)
        specifications = collection(index, "specifications", errors)
        conflicts = collection(index, "conflicts", errors)
        exceptions = collection(index, "exceptions", errors)
        statuses, declared = validate_specifications(specifications, repository, errors)
        validate_conflicts(conflicts, statuses, repository, errors)
        valid_exception_scopes = validate_exceptions(exceptions, errors)
    ownership: dict[str, str] = {}
    if traceability is not None:
        validate_version(traceability, "docs/traceability.yaml", errors)
        requirements = collection(traceability, "requirements", errors)
        if index is not None:
            ownership = validate_requirements(requirements, statuses, valid_exception_scopes, repository, errors)
    validate_exact_coverage(statuses, declared, ownership, valid_exception_scopes, errors)
    return sorted(errors)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check repository specification contracts.")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    errors = validate(parser.parse_args().repo)
    if errors:
        print("Specification compliance failed:", file=sys.stderr)
        print(*errors, sep="\n", file=sys.stderr)
        return 1
    print("Specification compliance passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
