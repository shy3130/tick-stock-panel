#!/usr/bin/env python3
"""TickFlow Pro Phase 1 probe (sanitized).

Default is dry-run (no network). Live probe requires TICKFLOW_API_KEY and
should run after 16:00 Asia/Shanghai unless --force is set (Stage A off-peak).

Never prints API keys. Writes reports under reports/tickflow_pro_probe/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
GOLDEN = ("000403.SZ", "600489.SH", "300059.SZ")
OFFPEAK_HOUR = 16


@dataclass
class ProbeGate:
    dry_run: bool
    force: bool
    now_hour: int
    has_key: bool
    allowed: bool
    reason: str


class _KlinesNS(Protocol):
    def batch(self, *args: Any, **kwargs: Any) -> Any: ...


class _QuotesNS(Protocol):
    def get(self, *args: Any, **kwargs: Any) -> Any: ...


class TickFlowLike(Protocol):
    klines: _KlinesNS
    quotes: _QuotesNS


def evaluate_gate(*, dry_run: bool, force: bool, has_key: bool, now: datetime | None = None) -> ProbeGate:
    now = now or datetime.now(SHANGHAI)
    hour = now.hour
    if dry_run:
        return ProbeGate(True, force, hour, has_key, True, "dry_run")
    if not has_key:
        return ProbeGate(False, force, hour, False, False, "missing_TICKFLOW_API_KEY")
    if hour < OFFPEAK_HOUR and not force:
        return ProbeGate(False, force, hour, True, False, f"before_{OFFPEAK_HOUR:02d}00_use_--force_or_wait")
    return ProbeGate(False, force, hour, True, True, "live_ok")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sanitize_sample(obj: object) -> object:
    """Keep structure; drop long arrays and obvious secret-like strings."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in ("key", "token", "secret", "password", "authorization")):
                out[k] = "***"
            else:
                out[k] = _sanitize_sample(v)
        return out
    if isinstance(obj, list):
        if len(obj) > 3:
            return [_sanitize_sample(x) for x in obj[:3]] + [f"...(+{len(obj) - 3} more)"]
        return [_sanitize_sample(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 120:
        return obj[:120] + "..."
    return obj


def _to_sample(raw: object) -> object:
    if hasattr(raw, "to_dict"):
        try:
            return _sanitize_sample(raw.to_dict())  # type: ignore[attr-defined]
        except Exception:
            pass
    return _sanitize_sample(raw)


def run_dry_run(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "dry_run",
        "as_of": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "symbols": list(GOLDEN),
        "planned_checks": [
            "quotes.get",
            "klines.batch(period=1d)",
            "klines.batch(period=1m)",
            "klines.ex_factors",
            "rate_limit_observation",
        ],
        "safety": {
            "SAFETY_RPM_FACTOR": 0.8,
            "offpeak_hour": OFFPEAK_HOUR,
            "note": (
                "Dry-run only validates CLI gates/plan. "
                "Live probe must not overlap Gold Stage A peak window without --force. "
                "Do not start multiple Phase 1 probe/sync processes concurrently."
            ),
        },
        # Gate-only success; does not prove SDK methods, network, or auth.
        "status": "DRY_RUN_OK",
    }
    (out_dir / "probe_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (out_dir / "probe_summary.md").write_text(
        "# TickFlow Pro probe (dry-run)\n\n"
        f"- as_of: {report['as_of']}\n"
        f"- symbols: {', '.join(GOLDEN)}\n"
        "- status: DRY_RUN_OK (gates/plan only; not a live contract proof)\n"
        f"- live command: `TICKFLOW_API_KEY=... python scripts/probe_tickflow_pro.py --live` "
        f"(after {OFFPEAK_HOUR}:00 or with --force)\n"
    )
    return report


def run_live(
    out_dir: Path,
    *,
    client: TickFlowLike | None = None,
    endpoint: str | None = None,
) -> dict:
    """Minimal live smoke: daily klines.batch + quotes.get for golden symbols.

    Pass ``client`` in tests (fake SDK). Production path builds TickFlow from env key.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    from app.tickflow.rate_limits import SAFETY_RPM_FACTOR, apply_safety_rpm

    if client is not None:
        # Test/injection path: do not import app.tickflow.client (pulls tickflow SDK).
        base = endpoint or "https://api.tickflow.org"
        tf = client
    else:
        key = os.environ.get("TICKFLOW_API_KEY", "").strip()
        if not key:
            try:
                from app.secrets_store import get_tickflow_key  # type: ignore

                key = (get_tickflow_key() or "").strip()
            except Exception:
                key = ""
        if not key:
            raise SystemExit("missing TICKFLOW_API_KEY")

        from tickflow import TickFlow

        from app.tickflow.client import PAID_ENDPOINT, _base_url

        base = endpoint or (_base_url() or PAID_ENDPOINT)
        tf = TickFlow(api_key=key, base_url=base)

    samples: dict[str, object] = {}
    errors: list[str] = []

    try:
        raw = tf.klines.batch(list(GOLDEN), period="1d", count=5, as_dataframe=False)
        samples["klines.batch.1d"] = _to_sample(raw)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"klines.batch.1d: {type(exc).__name__}")

    try:
        raw = tf.quotes.get(symbols=list(GOLDEN), as_dataframe=False)
        samples["quotes.get"] = _to_sample(raw)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"quotes.get: {type(exc).__name__}")

    report = {
        "mode": "live",
        "as_of": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "endpoint": base,
        "symbols": list(GOLDEN),
        "safety_rpm_factor": SAFETY_RPM_FACTOR,
        "example_budget_daily_batch_rpm": apply_safety_rpm(60),
        "samples": samples,
        "errors": errors,
        "status": "LIVE_PARTIAL" if errors else "LIVE_OK",
    }
    (out_dir / "probe_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (out_dir / "probe_summary.md").write_text(
        "# TickFlow Pro probe (live)\n\n"
        f"- as_of: {report['as_of']}\n"
        f"- status: {report['status']}\n"
        f"- errors: {len(errors)}\n"
        "- samples sanitized; no API key written.\n"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Call TickFlow APIs (needs key)")
    parser.add_argument("--force", action="store_true", help="Allow live probe before 16:00 Asia/Shanghai")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: <repo>/reports/tickflow_pro_probe/<timestamp>)",
    )
    args = parser.parse_args(argv)

    dry_run = not args.live
    has_key = bool(os.environ.get("TICKFLOW_API_KEY", "").strip())
    gate = evaluate_gate(dry_run=dry_run, force=args.force, has_key=has_key)
    print(json.dumps(asdict(gate), ensure_ascii=False))
    if not gate.allowed:
        return 2

    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d_%H%M%S")
    out_dir = args.out or (_repo_root() / "reports" / "tickflow_pro_probe" / stamp)
    if dry_run:
        report = run_dry_run(out_dir)
    else:
        root = _repo_root() / "backend"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        report = run_live(out_dir)
    print(json.dumps({"out": str(out_dir), "status": report.get("status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
