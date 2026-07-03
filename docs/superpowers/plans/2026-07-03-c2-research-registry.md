# C2：研究假设 registry + run_card 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 给研究过程一个持久资产层：假设（hypothesis）带生命周期（exploring/testing/validated/rejected/monitoring）与证据台账（evidence ledger）；每次策略回测产出 run_card（config hash + strategy hash + 数据窗口 + 关键指标），可被假设引用为证据。

**架构：** 本地 JSON 文件存储 MVP（`data/research/`），一个 service 模块 + 一个 API router。run_card 在 `/api/backtest/strategy/run` 完成后 best-effort 生成（失败不影响回测响应）。**与 `strategy_cache` 的边界**：strategy_cache 是执行缓存（可随时清），research registry 是研究资产（用户显式创建、长期保留）——两者不共享存储、不互相引用实现。

**技术栈：** Python 3.12 / FastAPI / dataclass + json。测试 `cd backend && uv run --extra dev pytest`。

**范围（YAGNI 裁定）：** 后端 store + REST API + run_card 挂钩。前端"研究页"不在本计划（等 C9 定时研究一起定 UI）；agent 工具暴露归 C8。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/research_registry.py` | Hypothesis/RunCard 模型 + JSON store | 创建 |
| `backend/app/api/research.py` | REST API | 创建 |
| `backend/app/api/backtest.py:204-238` | strategy_run 后挂 run_card | 修改 |
| `backend/app/main.py` | 注册 router | 修改（import + include_router） |
| `backend/tests/services/test_research_registry.py` | store 单测 | 创建 |
| `backend/tests/api/test_research_api.py` | API 单测 | 创建 |

**存储布局：**

```
data/research/
├── hypotheses/{hyp_id}.json     # 假设 + 证据台账（追加在文件内 evidence 数组）
└── run_cards/{run_id}.json      # 回测运行卡（一次运行一张，不可变）
```

---

### 任务 1：research_registry store

**文件：**
- 创建：`backend/app/services/research_registry.py`
- 测试：`backend/tests/services/test_research_registry.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_research_registry.py
import pytest

from app.services import research_registry as rr


@pytest.fixture()
def store(tmp_path):
    return rr.ResearchStore(tmp_path)


def test_create_and_get_hypothesis(store):
    h = store.create_hypothesis(title="低波动组合跑赢", thesis="低波动因子在A股长期有效")
    assert h.status == "exploring"
    got = store.get_hypothesis(h.id)
    assert got.title == "低波动组合跑赢"
    assert got.evidence == []


def test_status_transition_validated(store):
    h = store.create_hypothesis(title="t", thesis="x")
    h2 = store.update_hypothesis(h.id, status="testing")
    assert h2.status == "testing"
    with pytest.raises(ValueError):
        store.update_hypothesis(h.id, status="not_a_status")


def test_evidence_append_and_order(store):
    h = store.create_hypothesis(title="t", thesis="x")
    store.add_evidence(h.id, kind="note", ref="", summary="初步观察")
    store.add_evidence(h.id, kind="backtest", ref="run-123", summary="IC=0.05")
    got = store.get_hypothesis(h.id)
    assert [e["kind"] for e in got.evidence] == ["note", "backtest"]
    assert got.evidence[1]["ref"] == "run-123"


def test_search_by_status_and_text(store):
    a = store.create_hypothesis(title="低波动", thesis="x")
    store.create_hypothesis(title="动量", thesis="y")
    store.update_hypothesis(a.id, status="validated")
    assert [h.id for h in store.search(status="validated")] == [a.id]
    assert len(store.search(query="动量")) == 1


def test_run_card_save_and_hash_deterministic(store):
    cfg = {"strategy_id": "macd_golden", "start": "2025-01-01", "end": "2025-06-30",
           "fees_pct": 0.0002}
    c1 = store.save_run_card(run_id="r1", kind="strategy", config=cfg,
                             strategy_def={"id": "macd_golden", "v": 1},
                             stats={"sharpe": 1.2}, )
    c2 = rr.build_hashes(cfg, {"id": "macd_golden", "v": 1})
    assert c1.config_hash == c2[0]
    assert c1.strategy_hash == c2[1]
    # key 顺序不同不改变 hash
    c3 = rr.build_hashes({"end": "2025-06-30", "start": "2025-01-01",
                          "strategy_id": "macd_golden", "fees_pct": 0.0002},
                         {"v": 1, "id": "macd_golden"})
    assert c3 == c2
    assert store.get_run_card("r1").stats["sharpe"] == 1.2
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/test_research_registry.py -v`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现**

```python
# backend/app/services/research_registry.py
"""研究假设 registry + 回测 run_card（C2）。

与 strategy_cache 的边界：这里是研究资产（用户显式创建、长期保留、
带生命周期），strategy_cache 是可清除的执行缓存。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("exploring", "testing", "validated", "rejected", "monitoring")
EVIDENCE_KINDS = ("backtest", "note", "observation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_hashes(config: dict, strategy_def: dict | None) -> tuple[str, str]:
    cfg_hash = hashlib.sha256(_canonical(config).encode()).hexdigest()[:16]
    sd_hash = (
        hashlib.sha256(_canonical(strategy_def).encode()).hexdigest()[:16]
        if strategy_def is not None else ""
    )
    return cfg_hash, sd_hash


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
    kind: str                 # strategy / factor / signal
    config: dict
    config_hash: str
    strategy_hash: str
    stats: dict
    created_at: str


class ResearchStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "research"
        self.hyp_dir = self.root / "hypotheses"
        self.card_dir = self.root / "run_cards"

    # ---- hypotheses ----
    def create_hypothesis(self, title: str, thesis: str,
                          status: str = "exploring",
                          tags: list[str] | None = None) -> Hypothesis:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        h = Hypothesis(id=f"hyp-{uuid.uuid4().hex[:8]}", title=title, thesis=thesis,
                       status=status, tags=tags or [],
                       created_at=_now(), updated_at=_now())
        self._write_hyp(h)
        return h

    def get_hypothesis(self, hyp_id: str) -> Hypothesis:
        path = self.hyp_dir / f"{hyp_id}.json"
        if not path.exists():
            raise KeyError(hyp_id)
        return Hypothesis(**json.loads(path.read_text(encoding="utf-8")))

    def update_hypothesis(self, hyp_id: str, **fields_) -> Hypothesis:
        h = self.get_hypothesis(hyp_id)
        if "status" in fields_ and fields_["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields_['status']}")
        for k in ("title", "thesis", "status", "tags"):
            if k in fields_ and fields_[k] is not None:
                setattr(h, k, fields_[k])
        h.updated_at = _now()
        self._write_hyp(h)
        return h

    def add_evidence(self, hyp_id: str, kind: str, ref: str, summary: str) -> Hypothesis:
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"invalid evidence kind: {kind}")
        h = self.get_hypothesis(hyp_id)
        h.evidence.append({"ts": _now(), "kind": kind, "ref": ref, "summary": summary})
        h.updated_at = _now()
        self._write_hyp(h)
        return h

    def search(self, status: str | None = None, query: str | None = None) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        if not self.hyp_dir.exists():
            return out
        for p in sorted(self.hyp_dir.glob("hyp-*.json")):
            h = Hypothesis(**json.loads(p.read_text(encoding="utf-8")))
            if status and h.status != status:
                continue
            if query and query not in (h.title + h.thesis + " ".join(h.tags)):
                continue
            out.append(h)
        return out

    # ---- run cards ----
    def save_run_card(self, run_id: str, kind: str, config: dict,
                      strategy_def: dict | None, stats: dict) -> RunCard:
        cfg_hash, sd_hash = build_hashes(config, strategy_def)
        card = RunCard(run_id=run_id, kind=kind, config=config,
                       config_hash=cfg_hash, strategy_hash=sd_hash,
                       stats=stats, created_at=_now())
        self.card_dir.mkdir(parents=True, exist_ok=True)
        (self.card_dir / f"{run_id}.json").write_text(
            json.dumps(asdict(card), ensure_ascii=False, indent=1), encoding="utf-8")
        return card

    def get_run_card(self, run_id: str) -> RunCard | None:
        path = self.card_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return RunCard(**json.loads(path.read_text(encoding="utf-8")))

    def _write_hyp(self, h: Hypothesis) -> None:
        self.hyp_dir.mkdir(parents=True, exist_ok=True)
        (self.hyp_dir / f"{h.id}.json").write_text(
            json.dumps(asdict(h), ensure_ascii=False, indent=1), encoding="utf-8")
```

- [ ] **步骤 4：运行测试验证通过**

- [ ] **步骤 5：Commit** `git add -A && git commit -m "feat(research): hypothesis registry + run_card JSON store (C2)"`

---

### 任务 2：REST API

**文件：**
- 创建：`backend/app/api/research.py`
- 修改：`backend/app/main.py`（import `research`，`app.include_router(research.router)` 加在 `trade_journal` 之后）
- 测试：`backend/tests/api/test_research_api.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/api/test_research_api.py
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    from app.api.research import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_hypothesis_crud_roundtrip(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/research/hypotheses", json={"title": "低波动", "thesis": "..."})
    assert r.status_code == 200
    hid = r.json()["id"]

    r = c.patch(f"/api/research/hypotheses/{hid}", json={"status": "testing"})
    assert r.json()["status"] == "testing"

    r = c.post(f"/api/research/hypotheses/{hid}/evidence",
               json={"kind": "backtest", "ref": "run-1", "summary": "sharpe 1.2"})
    assert len(r.json()["evidence"]) == 1

    r = c.get("/api/research/hypotheses", params={"status": "testing"})
    assert [h["id"] for h in r.json()["items"]] == [hid]


def test_invalid_status_400(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    hid = c.post("/api/research/hypotheses", json={"title": "t", "thesis": "x"}).json()["id"]
    assert c.patch(f"/api/research/hypotheses/{hid}", json={"status": "bogus"}).status_code == 400
```

- [ ] **步骤 2：运行验证失败**

- [ ] **步骤 3：实现 router**

```python
# backend/app/api/research.py
"""研究假设 registry API（C2）。"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services.research_registry import ResearchStore

router = APIRouter(prefix="/api/research", tags=["research"])


def _store() -> ResearchStore:
    return ResearchStore(settings.data_dir)


class HypothesisIn(BaseModel):
    title: str
    thesis: str
    status: str = "exploring"
    tags: list[str] = []


class HypothesisPatch(BaseModel):
    title: str | None = None
    thesis: str | None = None
    status: str | None = None
    tags: list[str] | None = None


class EvidenceIn(BaseModel):
    kind: str
    ref: str = ""
    summary: str


@router.post("/hypotheses")
def create(req: HypothesisIn) -> dict:
    try:
        return asdict(_store().create_hypothesis(req.title, req.thesis, req.status, req.tags))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/hypotheses")
def list_(status: str | None = None, query: str | None = None) -> dict:
    return {"items": [asdict(h) for h in _store().search(status=status, query=query)]}


@router.get("/hypotheses/{hyp_id}")
def get(hyp_id: str) -> dict:
    try:
        return asdict(_store().get_hypothesis(hyp_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="hypothesis not found")


@router.patch("/hypotheses/{hyp_id}")
def patch(hyp_id: str, req: HypothesisPatch) -> dict:
    try:
        return asdict(_store().update_hypothesis(hyp_id, **req.model_dump(exclude_unset=True)))
    except KeyError:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/hypotheses/{hyp_id}/evidence")
def add_evidence(hyp_id: str, req: EvidenceIn) -> dict:
    try:
        return asdict(_store().add_evidence(hyp_id, req.kind, req.ref, req.summary))
    except KeyError:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/run_cards/{run_id}")
def get_run_card(run_id: str) -> dict:
    card = _store().get_run_card(run_id)
    if card is None:
        raise HTTPException(status_code=404, detail="run card not found")
    return asdict(card)
```

- [ ] **步骤 4：运行测试通过 + Commit** `git commit -am "feat(research): REST API for hypotheses and run cards"`

---

### 任务 3：strategy_run 挂 run_card

**文件：**
- 修改：`backend/app/api/backtest.py:204-238`（`strategy_run`）

- [ ] **步骤 1：编写失败的测试（run_card best-effort 生成）**

```python
# 追加到 backend/tests/api/test_research_api.py
def test_run_card_written_after_strategy_run(tmp_path, monkeypatch):
    """strategy_run 成功后应落 run_card；生成失败不影响回测响应（best-effort）。"""
    from app.api import backtest as bt
    from app.services.research_registry import ResearchStore

    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    result_stats = {"sharpe": 1.0}
    bt._save_strategy_run_card(
        run_id="run-x", req_dict={"strategy_id": "macd_golden", "start": "2025-01-01"},
        strategy_def={"id": "macd_golden"}, stats=result_stats,
    )
    assert ResearchStore(tmp_path).get_run_card("run-x").config_hash
```

- [ ] **步骤 2：实现 helper 并在 strategy_run 中调用**

`backend/app/api/backtest.py` 模块级追加：

```python
def _save_strategy_run_card(run_id: str, req_dict: dict, strategy_def: dict | None,
                            stats: dict) -> None:
    """best-effort 落 run_card；任何异常只记日志，不影响回测响应。"""
    try:
        from app.config import settings
        from app.services.research_registry import ResearchStore
        ResearchStore(settings.data_dir).save_run_card(
            run_id=run_id, kind="strategy", config=req_dict,
            strategy_def=strategy_def, stats=stats)
    except Exception as e:  # noqa: BLE001
        logger.warning("run_card save failed for %s: %s", run_id, e)
```

`strategy_run()` 中 `result = svc.run(cfg)` 之后、return 之前插入：

```python
    strategy_def = None
    try:
        item = strategy_engine.get(req.strategy_id)
        strategy_def = getattr(item, "raw", None) or getattr(item, "__dict__", None)
    except Exception:  # noqa: BLE001
        pass
    _save_strategy_run_card(
        run_id=getattr(result, "run_id", ""),
        req_dict=req.model_dump(mode="json"),
        strategy_def=strategy_def,
        stats=getattr(result, "stats", {}) or {},
    )
```

（`strategy_engine.get` 的确切取定义方法在实现时以 `app/strategy/engine.py` 实际 API 为准——要求是拿到"能代表策略当前定义的可序列化 dict"；DSL 策略即其 JSON 本体。若引擎无此接口，先只 hash `strategy_id+params`，并在 run_card `strategy_hash` 备注降级。）

- [ ] **步骤 3：SSE 路径同样挂卡**

`/api/backtest/strategy/stream`（`_BacktestJob` 完成回调处）在 job 成功产出 result 后调用同一 `_save_strategy_run_card`。定位：`_BacktestJob` 保存最终 result 的位置（`app/api/backtest.py:245-` 区域，实现时按实际回调点插入）。

- [ ] **步骤 4：全量测试 + 手动验证**

跑一次真实策略回测（UI 或 curl `/api/backtest/strategy/run`），确认 `data/research/run_cards/{run_id}.json` 生成且含两个 hash。

- [ ] **步骤 5：Commit** `git commit -am "feat(research): attach run_card to strategy backtest runs"`
