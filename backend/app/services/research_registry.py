"""Research hypothesis registry and immutable run cards."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATUSES = {"exploring", "testing", "validated", "rejected", "monitoring"}
# factor_run 有意不进通用证据白名单：唯一合法写入入口是 link_factor_run
# （run_id 白名单 + hypothesis/run 存在性门禁 + 幂等），通用 add_evidence 对它 fail-closed。
EVIDENCE_KINDS = {"backtest", "note", "observation"}

# 做T确认幂等锁：ResearchStore 实例按请求新建，实例级锁无法跨请求互斥，
# 故用模块级锁在持久化边界串行化「按保留标签 create-or-return-existing」。
_RESERVED_TAG_LOCK = threading.Lock()

# factor_run 关联幂等锁：同一 (hyp_id, run_id) 的重复/并发 link 只允许落一条。
_FACTOR_RUN_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_hashes(config: dict, strategy_def: dict | None = None) -> tuple[str, str]:
    cfg_hash = hashlib.sha256(_canonical(config).encode()).hexdigest()[:16]
    strategy_hash = (
        hashlib.sha256(_canonical(strategy_def).encode()).hexdigest()[:16] if strategy_def else ""
    )
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
        self.data_dir = Path(data_dir)
        root = self.data_dir / "research"
        self.hyp_dir = root / "hypotheses"
        self.card_dir = root / "run_cards"

    def create_hypothesis(
        self, title: str, thesis: str, status: str = "exploring", tags: list[str] | None = None
    ) -> Hypothesis:
        h = self._new_hypothesis(title, thesis, status, tags)
        self._write_hypothesis(h)
        return h

    def create_or_get_hypothesis_by_tag(
        self,
        tag: str,
        title: str,
        thesis: str,
        status: str = "exploring",
        tags: list[str] | None = None,
    ) -> Hypothesis:
        """以精确保留标签为幂等键的原子 create-or-return-existing。

        同一 tag 的重复/并发调用返回同一条既有记录（不覆盖、不动 updated_at），
        磁盘最多一条；不存在时才创建。用于做T确认入口的持久化边界。
        """
        if tag not in (tags or []):
            raise ValueError("idempotency tag must be present in tags")
        with _RESERVED_TAG_LOCK:
            existing = self._find_hypothesis_by_tag(tag)
            if existing is not None:
                return existing
            h = self._new_hypothesis(title, thesis, status, tags)
            self._write_hypothesis(h)
            return h

    def _new_hypothesis(
        self, title: str, thesis: str, status: str, tags: list[str] | None
    ) -> Hypothesis:
        _check_status(status)
        now = _now()
        return Hypothesis(
            id=f"hyp-{uuid.uuid4().hex[:8]}",
            title=title,
            thesis=thesis,
            status=status,
            tags=tags or [],
            created_at=now,
            updated_at=now,
        )

    def _find_hypothesis_by_tag(self, tag: str) -> Hypothesis | None:
        if not self.hyp_dir.exists():
            return None
        for path in sorted(self.hyp_dir.glob("*.json")):
            h = Hypothesis(**json.loads(path.read_text(encoding="utf-8")))
            if tag in h.tags:
                return h
        return None

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
        if kind == "factor_run":
            # fail-closed: factor_run 证据只能经 link_factor_run 的验证门禁写入。
            raise ValueError("factor_run evidence must use link_factor_run")
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"invalid evidence kind: {kind}")
        h = self.get_hypothesis(hyp_id)
        h.evidence.append({"ts": _now(), "kind": kind, "ref": ref, "summary": summary})
        h.updated_at = _now()
        self._write_hypothesis(h)
        return h

    def link_factor_run(self, hyp_id: str, run_id: str, summary: str = "") -> Hypothesis:
        """关联持久 factor run 到 hypothesis 的唯一验证入口（幂等、并发安全）。

        - run_id 先过 Control Plane job_store 白名单正则（防路径穿越/非法字符）；
        - hypothesis 必须已存在（KeyError）；持久 run 必须已存在（FactorJobStore.get）；
        - 同一 (hyp_id, run_id) 重复/并发关联只保留首条，
          既有记录不覆盖、不重复、不动 updated_at。
        """
        _check_hypothesis_id(hyp_id)
        _check_factor_run_id(run_id)
        with _FACTOR_RUN_LOCK:
            hypothesis = self.get_hypothesis(hyp_id)
            if any(
                evidence.get("kind") == "factor_run" and evidence.get("ref") == run_id
                for evidence in hypothesis.evidence
            ):
                return hypothesis
            if not self._factor_run_exists(run_id):
                raise ValueError(f"factor run not found: {run_id}")
            hypothesis.evidence.append(
                {"ts": _now(), "kind": "factor_run", "ref": run_id, "summary": summary}
            )
            hypothesis.updated_at = _now()
            self._write_hypothesis(hypothesis)
            return hypothesis

    def _factor_run_exists(self, run_id: str) -> bool:
        """Control Plane 持久 run 存在性门禁（lazy import 避免模块加载环）。"""
        from app.research.job_store import FactorJobStore

        return FactorJobStore(self.data_dir).get(run_id) is not None

    def hypotheses_for_run(self, run_id: str) -> list[Hypothesis]:
        """Return hypotheses whose evidence references the durable run.

        只读反查：非法 run_id 直接返回空（不可能有合法假设引用它），按
        文件名（即 hypothesis id）稳定排序。
        """
        if _factor_run_id_is_safe(run_id) is False:
            return []
        if not self.hyp_dir.exists():
            return []
        out: list[Hypothesis] = []
        for path in sorted(self.hyp_dir.glob("*.json")):
            try:
                hypothesis = Hypothesis(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if any(
                evidence.get("kind") == "factor_run" and evidence.get("ref") == run_id
                for evidence in hypothesis.evidence
            ):
                out.append(hypothesis)
        return out

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

    def save_run_card(
        self, run_id: str, kind: str, config: dict, stats: dict, strategy_def: dict | None = None
    ) -> RunCard:
        config_hash, strategy_hash = build_hashes(config, strategy_def)
        card = RunCard(
            run_id=run_id,
            kind=kind,
            config=config,
            config_hash=config_hash,
            strategy_hash=strategy_hash,
            stats=stats,
            created_at=_now(),
        )
        self.card_dir.mkdir(parents=True, exist_ok=True)
        (self.card_dir / f"{run_id}.json").write_text(
            json.dumps(asdict(card), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return card

    def get_run_card(self, run_id: str) -> RunCard:
        path = self.card_dir / f"{run_id}.json"
        if not path.exists():
            raise KeyError(run_id)
        return RunCard(**json.loads(path.read_text(encoding="utf-8")))

    def list_run_cards(self) -> list[RunCard]:
        """只读枚举全部 run_cards, 供 BacktestRunStore 迁移; 跳过损坏文件。"""
        if not self.card_dir.exists():
            return []
        out: list[RunCard] = []
        for path in sorted(self.card_dir.glob("*.json")):
            try:
                out.append(RunCard(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return out

    def _write_hypothesis(self, h: Hypothesis) -> None:
        self.hyp_dir.mkdir(parents=True, exist_ok=True)
        (self.hyp_dir / f"{h.id}.json").write_text(
            json.dumps(asdict(h), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")


def _factor_run_id_is_safe(run_id: str) -> bool:
    """复用 Control Plane job_store 的 run_id 白名单正则。"""
    from app.research.job_store import RUN_ID_PATTERN

    return isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id) is not None


def _check_factor_run_id(run_id: str) -> str:
    if not _factor_run_id_is_safe(run_id):
        raise ValueError(f"invalid factor run_id: {run_id!r}")
    return run_id


def _check_hypothesis_id(hyp_id: str) -> str:
    if not isinstance(hyp_id, str) or re.fullmatch(r"hyp-[0-9a-f]{8}", hyp_id) is None:
        raise ValueError(f"invalid hypothesis id: {hyp_id!r}")
    return hyp_id
