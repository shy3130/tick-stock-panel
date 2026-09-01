"""Run one registered factor over the sealed full-market PIT cohort."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import resource
import sys
import tempfile
import threading
from contextlib import contextmanager, suppress
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "POLARS_MAX_THREADS",
)
for variable in THREAD_ENV_VARS:
    try:
        configured = int(os.environ.get(variable, "2"))
    except ValueError:
        configured = 2
    os.environ[variable] = str(min(max(configured, 1), 2))

DEFAULT_MAX_RSS_GIB = 8.0
DEFAULT_LOCK_PATH = Path(tempfile.gettempdir()) / "tickflow-full-market-research.lock"

from app.services.full_market_research import (  # noqa: E402
    FullMarketRunnerError,
    registered_factor_names,
    run_full_market_research,
    write_payload_json,
)
from app.storage.repository import DataStore, KlineRepository  # noqa: E402


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _build_repo(data_dir: Path | None) -> KlineRepository:
    return KlineRepository(DataStore(data_dir))


@contextmanager
def _single_run_lock(path: Path):
    """Prevent concurrent full-market processes on one workstation."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FullMarketRunnerError(
                f"another full-market research process holds {path}"
            ) from exc
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


@contextmanager
def _rss_guard(max_rss_gib: float):
    """Hard-stop this runner before its peak RSS can exhaust workstation swap."""
    if not 0 < max_rss_gib <= 64:
        raise ValueError("--max-rss-gib must be within (0, 64]")
    limit = int(max_rss_gib * 1024**3)
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(0.25):
            peak = _peak_rss_bytes()
            if peak <= limit:
                continue
            message = (
                "full-market research stopped: "
                f"peak RSS {peak / 1024**3:.2f} GiB exceeded "
                f"{max_rss_gib:.2f} GiB limit\n"
            )
            os.write(2, message.encode())
            os._exit(75)

    worker = threading.Thread(
        target=monitor,
        name="full-market-rss-guard",
        daemon=True,
    )
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2.0)


def _emit_payload(payload: dict, output: Path | None) -> None:
    if output is None:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return
    target = write_payload_json(payload, output)
    print(
        json.dumps(
            {
                "status": "written",
                "output": str(target),
                "research_id": payload["research_id"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor", choices=registered_factor_names(), required=True)
    parser.add_argument("--start", type=_day, required=True)
    parser.add_argument("--end", type=_day, required=True)
    parser.add_argument("--oos-start", type=_day)
    parser.add_argument("--cost-bps", type=float)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, help="explicit JSON output path")
    parser.add_argument(
        "--max-rss-gib",
        type=float,
        default=DEFAULT_MAX_RSS_GIB,
        help=f"hard per-process peak RSS limit (default: {DEFAULT_MAX_RSS_GIB:.1f} GiB)",
    )
    args = parser.parse_args(argv)
    try:
        if args.start > args.end:
            raise ValueError("--start must be <= --end")
        with _single_run_lock(DEFAULT_LOCK_PATH), _rss_guard(args.max_rss_gib):
            with suppress(OSError):
                os.nice(10)
            payload = run_full_market_research(
                args.factor,
                _build_repo(args.data_dir),
                args.start,
                args.end,
                oos_start=args.oos_start,
                cost_bps=args.cost_bps,
            )
            _emit_payload(payload, args.output)
        return 0
    except (FullMarketRunnerError, OSError, ValueError) as exc:
        print(f"full-market research failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
