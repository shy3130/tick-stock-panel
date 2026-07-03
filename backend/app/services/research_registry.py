"""Research hypothesis registry and immutable run cards."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATUSES = {"exploring", "testing", "validated", "rejected", "monitoring"}
EVIDENCE_KINDS = {"backtest", "note", "observation"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_hashes(config: dict, strategy_def: dict | None = None) -> tuple[str, str]:
    cfg_hash = hashlib.sha256(_canonical(config).encode()).hexdigest()[:16]
    strategy_hash = hashlib.sha256(_canonical(strategy_def).encode()).hexdigest()[:16] if strategy_def else ""
    return cfg_hash, strategy_hash


@dataclass
class Hypothesis:
    id: str
    title: str
    thesis: str
    status: str = "exploring"
    tags: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RunCard:
    run_id: str
    kind: str
    config: dict
    config_hash: str
    strategy_hash: str
    stats: dict
    created_at: str


class ResearchStore:
    def __init__(self, data_dir: Path) -> None:
        root = Path(data_dir) / "research"
        self.hyp_dir = root / "hypotheses"
        self.card_dir = root / "run_cards"

    def create_hypothesis(self, title: str, thesis: str, status: str = "exploring", tags: list[str] | None = None) -> Hypothesis:
        _check_status(status)
        now = _now()
        h = Hypothesis(
            id=f"hyp-{uuid.uuid4().hex[:8]}",
            title=title,
            thesis=thesis,
            status=status,
            tags=tags or [],
            created_at=now,
            updated_at=now,
        )
        self._write_hypothesis(h)
        return h

    def get_hypothesis(self, hyp_id: str) -> Hypothesis:
        path = self.hyp_dir / f"{hyp_id}.json"
        if not path.exists():
            raise KeyError(hyp_id)
        return Hypothesis(**json.loads(path.read_text(encoding="utf-8")))

    def update_hypothesis(self, hyp_id: str, **fields) -> Hypothesis:
        h = self.get_hypothesis(hyp_id)
        if "status" in fields and fields["status"] is not None:
            _check_status(fields["status"])
        for key in ("title", "thesis", "status", "tags"):
            if key in fields and fields[key] is not None:
                setattr(h, key, fields[key])
        h.updated_at = _now()
        self._write_hypothesis(h)
        return h

    def add_evidence(self, hyp_id: str, kind: str, ref: str, summary: str) -> Hypothesis:
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"invalid evidence kind: {kind}")
        h = self.get_hypothesis(hyp_id)
        h.evidence.append({"ts": _now(), "kind": kind, "ref": ref, "summary": summary})
        h.updated_at = _now()
        self._write_hypothesis(h)
        return h

    def search(self, status: str | None = None, query: str | None = None) -> list[Hypothesis]:
        if status is not None:
            _check_status(status)
        if not self.hyp_dir.exists():
            return []
        q = (query or "").lower()
        out = []
        for path in sorted(self.hyp_dir.glob("*.json")):
            h = Hypothesis(**json.loads(path.read_text(encoding="utf-8")))
            if status and h.status != status:
                continue
            if q and q not in (h.title + "\n" + h.thesis).lower():
                continue
            out.append(h)
        return out

    def save_run_card(self, run_id: str, kind: str, config: dict, stats: dict, strategy_def: dict | None = None) -> RunCard:
        config_hash, strategy_hash = build_hashes(config, strategy_def)
        card = RunCard(run_id=run_id, kind=kind, config=config, config_hash=config_hash, strategy_hash=strategy_hash, stats=stats, created_at=_now())
        self.card_dir.mkdir(parents=True, exist_ok=True)
        (self.card_dir / f"{run_id}.json").write_text(json.dumps(asdict(card), ensure_ascii=False, indent=2), encoding="utf-8")
        return card

    def get_run_card(self, run_id: str) -> RunCard:
        path = self.card_dir / f"{run_id}.json"
        if not path.exists():
            raise KeyError(run_id)
        return RunCard(**json.loads(path.read_text(encoding="utf-8")))

    def _write_hypothesis(self, h: Hypothesis) -> None:
        self.hyp_dir.mkdir(parents=True, exist_ok=True)
        (self.hyp_dir / f"{h.id}.json").write_text(json.dumps(asdict(h), ensure_ascii=False, indent=2), encoding="utf-8")


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
