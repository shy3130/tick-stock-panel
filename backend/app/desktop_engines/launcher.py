"""Mount one certified pair before either the GUI or a spawned worker imports it."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from multiprocessing.spawn import is_forking
from pathlib import Path

from tivelo_contracts.errors import TiveloError
from tivelo_contracts.signatures import MAX_ENVELOPE

from .bundle import digest, read_limited
from .store import DesktopStore
from .workspaces import draft_installation, draft_reader, logical_root, manage, route

_PIN = "TSP_DESKTOP_CORE_COMBINATION"


def _status(store, db):
    identity, revision = store.active(db)
    history = {
        row[0]
        for row in db.execute("SELECT id FROM history WHERE baseline=?", (store.baseline_id,))
    }
    try:
        selection = {"engines": [w.model_dump(mode="json") for w in store.release(identity).wheels]}
    except (OSError, ValueError) as exc:
        selection = {"selection_error": str(exc)}
    return {
        **selection,
        "baseline_id": store.baseline_id,
        "bundled_combination": store.baseline.id,
        "combination": identity,
        "revision": revision,
        "retained": sorted(history | {store.baseline.id}),
        "compatibility_lock": store.lock.model_dump(mode="json"),
    }


def _check_candidate(store, identity):
    with tempfile.TemporaryDirectory(prefix="tsp-core-verify-") as temporary:
        report = Path(temporary) / "check.json"
        # Restart the same shipped executable. The candidate reaches neither the GUI
        # nor the activation transaction, and sees only the synthetic test dataset.
        env = {**os.environ, "DATA_DIR": str(store.data_dir), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        result = subprocess.run(
            [
                sys.executable,
                "--engine",
                "--report",
                str(report),
                "--combination",
                identity,
                "_verify",
            ],
            env=env,
            timeout=180,
            check=False,
        )
        if not report.is_file():
            raise ValueError("候选组合验证进程未返回结果")
        proof = json.loads(read_limited(report).decode("utf-8"))
        if result.returncode or proof.get("combination") != identity:
            raise ValueError(proof.get("message") or "候选组合未通过离线回测")


def _maintenance(store, args):
    parser = argparse.ArgumentParser(prog="TSP --engine " + args.action)
    parser.add_argument("--revision", type=int, required=True)
    if args.action == "install":
        parser.add_argument("--release", type=Path, required=True)
        parser.add_argument("--wheel", type=Path, required=True)
    else:
        parser.add_argument("--to", required=True)
    options = parser.parse_args(args.arguments)
    with store.guard(maintenance=True) as db:
        current, actual = store.active(db)
        if actual != options.revision:
            raise ValueError("核心组合状态已经改变，请刷新 revision")
        store.require_idle_jobs()
        if args.action == "install":
            candidate = store.stage(
                db,
                store.release(current),
                read_limited(options.release, MAX_ENVELOPE),
                options.wheel,
            )
        else:
            if not store.retained(db, options.to):
                raise ValueError("只能恢复已安装并认证的组合")
            candidate = store.release(options.to)
    _check_candidate(store, candidate.id)
    revision = store.activate(candidate, options.revision, restoring=args.action == "restore")
    return {
        "combination": candidate.id,
        "revision": revision,
        "engines": [wheel.model_dump(mode="json") for wheel in candidate.wheels],
    }


def _command(store, args, anchor=None):
    if args.action in ("install", "restore"):
        if args.combination:
            raise ValueError("维护操作以当前 revision 为准")
        return _maintenance(store, args)
    with store.guard() as db:
        state = _status(store, db)
        if args.action == "status":
            if args.arguments or args.combination:
                raise ValueError("status 不接受额外参数")
            return state
        identity = args.combination or state["combination"]
        if args.action != "_verify" and not store.retained(db, identity):
            raise ValueError("所选组合未安装")
        release = store.release(identity)
        paths = store.mount(release)
        os.environ[_PIN] = identity
        if args.action == "_workspace-check":
            from .workspace_verify import verify_contents

            if len(args.arguments) != 1:
                raise ValueError("需要原安装目录")
            return verify_contents(store.data_dir, Path(args.arguments[0]))
        if args.action in ("check", "_verify"):
            if args.arguments:
                raise ValueError("check 不接受额外参数")
            from .probe import run

            if any(
                digest(read_limited(store.bundle / w.filename)) != w.sha256
                for w in store.lock.wheels
            ):
                raise ValueError("随宿主发行的 wheel 与兼容清单不一致")
            proof = run(
                store.bundle / "tivelo-conformance.json",
                {w.name: w for w in release.wheels},
                paths,
                store.lock.dependencies,
            )
            return {"combination": identity, **proof}
        if args.action == "drain":
            if args.arguments or args.combination:
                raise ValueError("drain 只接受当前工作台组合")
            from app.services.engine_jobs.manager import EngineJobManager

            manager = EngineJobManager(store.data_dir, autostart=False)
            try:
                queued = manager.store.active_ids()
                while manager.run_once():
                    pass
                if manager.store.active_ids():
                    raise ValueError("研究任务仍在排队或执行，请在执行资源可用后再次运行 drain")
                return {
                    "combination": identity,
                    "jobs": {key: manager.store.get(key)["status"] for key in queued},
                }
            finally:
                manager.shutdown()
        if args.action == "data":
            from tivelo_data.cli import main
        else:
            from tivelo_backtest.cli import main
        if anchor is not None and store.data_dir != anchor.data_dir:
            from inspect import signature

            if "map_paths" not in signature(main).parameters:
                raise ValueError("此核心组合未声明托管工作区路径支持")
            return main(
                args.arguments, map_paths=lambda values: _workspace_paths(values, anchor, store)
            )
        return main(args.arguments)


def engine_cli(bundle, data_dir, argv):
    parser = argparse.ArgumentParser(prog="TSP --engine", description="Tivelo 桌面引擎与离线维护")
    parser.add_argument(
        "--report", type=Path, help="将命令输出保存为文件，适用于无控制台的桌面程序"
    )
    parser.add_argument("--combination", help="使用已保留的指定组合")
    parser.add_argument(
        "action",
        choices=(
            "status",
            "check",
            "data",
            "backtest",
            "drain",
            "install",
            "restore",
            "workspace",
            "_verify",
            "_workspace-check",
        ),
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    def execute():
        try:
            draft_owner = draft_installation(data_dir)
            if args.action == "_workspace-check":
                if (
                    draft_owner is None
                    or len(args.arguments) != 1
                    or Path(args.arguments[0]).resolve() != draft_owner
                    or args.combination
                ):
                    raise ValueError("工作区核验仅限迁移中的私有副本")
            elif draft_owner is not None:
                raise ValueError("工作区副本尚在迁移中")
            anchor = DesktopStore(bundle, logical_root(data_dir))
            if args.action == "workspace":
                if args.combination:
                    raise ValueError("工作区操作以宿主及 revision 为准")
                value = manage(anchor, args.arguments)
            else:
                with (
                    draft_reader(Path(data_dir).resolve()) if draft_owner else nullcontext(),
                    route(anchor, data_dir) as store,
                ):
                    if args.action != "_workspace-check":
                        _bind_workspace(anchor, store)
                    value = _command(store, args, anchor)
            if isinstance(value, int):
                return value
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0
        except (
            ImportError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            subprocess.SubprocessError,
            TiveloError,
        ) as exc:
            print(
                json.dumps(
                    {"code": str(getattr(exc, "code", "CORE_RELEASE_ERROR")), "message": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1

    if args.report is None:
        return execute()
    with tempfile.TemporaryDirectory(
        prefix=".engine-report-", dir=args.report.absolute().parent
    ) as temporary:
        out, err = (Path(temporary) / name for name in ("output", "errors"))
        with (
            out.open("w", encoding="utf-8") as stdout,
            err.open("w", encoding="utf-8") as stderr,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = execute()
        os.replace(err if code else out, args.report)
    return code


def _bind_workspace(anchor, store):
    from app.config import settings

    settings.data_dir = store.data_dir
    if store.data_dir != anchor.data_dir:
        from tivelo_extensions.installation import bind_desktop_installation

        bind_desktop_installation(store.data_dir, anchor.data_dir)


def _workspace_paths(arguments, anchor, store):
    def selected(value):
        if not isinstance(value, Path):
            return value
        path = value.expanduser().resolve()
        if path.is_relative_to(store.data_dir) or not path.is_relative_to(anchor.data_dir):
            return value
        if path.is_relative_to(anchor.root):
            raise ValueError("引擎路径指向其他工作区或核心发行存储")
        return store.data_dir / path.relative_to(anchor.data_dir)

    for key, value in vars(arguments).items():
        if isinstance(value, list):
            setattr(arguments, key, [selected(item) for item in value])
        elif isinstance(value, Path):
            setattr(arguments, key, selected(value))


def launch(gui):
    # A multiprocessing resource tracker does not execute engine code. Let its
    # native entrypoint exit on its own without extending the parent's read lease.
    if sys.argv[-2:-1] == ["-c"]:
        multiprocessing.freeze_support()
    from app.config import settings

    bundle = Path(sys._MEIPASS) / "tivelo-release"
    if sys.argv[1:2] == ["--engine"]:
        return engine_cli(bundle, settings.data_dir, sys.argv[2:])
    draft_owner = draft_installation(settings.data_dir)
    if draft_owner is not None and not is_forking(sys.argv):
        raise ValueError("工作区副本尚在迁移中")
    anchor = DesktopStore(bundle, logical_root(settings.data_dir))
    with (
        draft_reader(settings.data_dir.resolve()) if draft_owner else nullcontext(),
        route(anchor, settings.data_dir) as store,
        store.guard() as db,
    ):
        _bind_workspace(anchor, store)
        current, _ = store.active(db)
        # A resource tracker or spawn child inherits the originating process' exact
        # combination, including a retained pin used for explicit archive replay.
        child = is_forking(sys.argv)
        identity = os.environ.get(_PIN, current) if child else current
        store.mount(store.release(identity))
        os.environ[_PIN] = identity
        multiprocessing.freeze_support()
        return gui()
