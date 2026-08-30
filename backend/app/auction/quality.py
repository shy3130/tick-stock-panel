"""竞价点与序列的质量分。缺字段 fail-closed, 不估算未匹配量。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.auction.contracts import AuctionSnapshot, UnmatchedSide


def snapshot_flags(snap: AuctionSnapshot, *, now_ms: int | None = None) -> list[str]:
    flags = list(snap.quality_flags)
    if snap.indicative_price is None:
        flags.append("missing_indicative_price")
    if snap.matched_volume is None:
        flags.append("missing_matched_volume")
    if snap.unmatched_volume is None or snap.unmatched_side == UnmatchedSide.unknown:
        flags.append("missing_unmatched")
    if now_ms is not None and "historical_backfill" not in flags:
        lag = now_ms - snap.received_at_ms
        if lag > 120_000:
            flags.append("stale_over_120s")
        elif lag > 30_000:
            flags.append("stale_over_30s")
        source_lag = snap.received_at_ms - snap.source_time_ms
        if source_lag > 15_000:
            flags.append("high_source_latency")
    return _unique(flags)


def quality_score(
    snapshots: Sequence[AuctionSnapshot],
    *,
    now_ms: int | None = None,
) -> tuple[float, list[str]]:
    """0-100。点太少、缺匹配量、过期都会降权。"""
    flags: list[str] = []
    if len(snapshots) < 3:
        flags.append("insufficient_points")
    span = 0
    if snapshots:
        times = [s.source_time_ms for s in snapshots]
        span = max(times) - min(times)
        if span < 60_000 and len(snapshots) < 8:
            flags.append("sparse_series")
    latest = snapshots[-1] if snapshots else None
    if latest is not None:
        flags.extend(snapshot_flags(latest, now_ms=now_ms))

    score = 100.0
    if "insufficient_points" in flags:
        score -= 35
    if "sparse_series" in flags:
        score -= 15
    if "missing_indicative_price" in flags:
        score -= 40
    if "missing_matched_volume" in flags:
        score -= 20
    if "missing_unmatched" in flags:
        score -= 10
    if "stale_over_120s" in flags:
        score -= 25
    elif "stale_over_30s" in flags:
        score -= 10
    if "high_source_latency" in flags:
        score -= 8
    if "historical_backfill" in flags or any(
        "historical_backfill" in s.quality_flags for s in snapshots
    ):
        flags.append("historical_backfill")
        score -= 5
    return max(0.0, min(100.0, score)), _unique(flags)


def _unique(flags: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for flag in flags:
        if flag and flag not in seen:
            seen.append(flag)
    return seen
