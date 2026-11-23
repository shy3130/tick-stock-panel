"""Opt-in local extension management for the independent engine consoles."""

from pathlib import Path

from tivelo_contracts.errors import ErrorCode, TiveloError


def add_commands(parent):
    command = parent.add_parser(
        "extensions", help="Inspect and manage a local signed extension installation"
    )
    configure(command)


def configure(command):
    command.add_argument(
        "--state", type=Path, required=True, help="Local operator-owned installation directory"
    )
    sub = command.add_subparsers(dest="operation", required=True)
    inspect = sub.add_parser("inspect", help="Verify a candidate without loading it")
    inspect.add_argument("--archive", type=Path, required=True)
    stage = sub.add_parser("stage", help="Save an inspected digest and review revision")
    stage.add_argument("--archive", type=Path, required=True)
    stage.add_argument("--digest", required=True)
    stage.add_argument("--revision", required=True)
    sub.add_parser("list", help="List installed versions and their activation generation")
    describe = sub.add_parser(
        "describe-source", help="Read an active signed data source catalog without running it"
    )
    describe.add_argument("--provider", required=True)
    switch = sub.add_parser("switch", help="Check and activate, roll back or disable a version")
    switch.add_argument("action", choices=("activate", "rollback", "disable"))
    switch.add_argument("--digest", required=True)
    switch.add_argument("--generation", type=int, required=True)
    sub.add_parser("identity", help="Create or read the installation public identity")
    sub.add_parser("status", help="Read the public identity and lease summaries")
    license_command = sub.add_parser(
        "license", help="Verify and import a signed lease for the active version"
    )
    license_command.add_argument("--file", type=Path, required=True)
    license_command.add_argument("--digest", required=True)
    license_command.add_argument("--revision", type=int, required=True)


def _read(path, limit):
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Input exceeds the extension size limit")
    return payload


def manage(args):
    from tivelo_contracts.extension_archive import MAX_UPLOAD
    from tivelo_contracts.signatures import MAX_ENVELOPE

    from .installation import EngineInstallation
    from .store import EnginePackageStore

    store = EnginePackageStore(args.state)
    installation = EngineInstallation(args.state)
    try:
        if args.operation in ("identity", "stage"):
            args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.operation == "identity":
            return installation.identity(create=True)
        if args.operation == "status":
            return installation.status()
        if args.operation == "list":
            return store.list_staged()
        if args.operation == "describe-source":
            pin = store.resolve(args.provider, kind="dataset_provider")
            return {
                "extension": pin.model_dump(mode="json"),
                "provider": store.dataset_descriptor(pin).model_dump(mode="json"),
            }
        if args.operation == "switch":
            return store.switch(args.digest, args.action, args.generation)
        if args.operation == "license":
            return store.import_lease(args.digest, _read(args.file, MAX_ENVELOPE), args.revision)
        archive = _read(args.archive, MAX_UPLOAD)
        if args.operation == "stage":
            return store.stage(archive, args.digest, args.revision)
        return store.inspect(archive)
    except ValueError as exc:
        code = (
            ErrorCode.LICENSE_REQUIRED
            if args.operation == "license"
            else ErrorCode.EXTENSION_INCOMPATIBLE
        )
        raise TiveloError(code, str(exc)) from exc


def main(argv=None):
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    configure(parser)
    try:
        print(json.dumps(manage(parser.parse_args(argv)), sort_keys=True))
    except TiveloError as exc:
        print(json.dumps({"code": exc.code.value, "message": str(exc)}), file=sys.stderr)
        return 1
    except OSError:
        print(
            json.dumps({"code": "PUBLICATION_FAILED", "message": "Installation I/O failed"}),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
