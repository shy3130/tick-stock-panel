# 多 AI 配置（后端）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。设计依据见 `docs/superpowers/specs/2026-07-03-multi-ai-provider-design.md`。

**目标：** 后端支持**多条具名 AI 配置**（[[AI 配置]]，代码 `profile`）+ 全局默认；每次 AI 调用可带 `profile_id` 选用其一（缺省走默认）；新增 `acp` [[Provider 类型]]（[[ACP 传输]] 统一驱动会说 ACP 的本机 agent）；旧单配置**自动迁移**、用户无感。

**架构：** secrets.json 从扁平字段 → `ai_profiles[]` + `ai_default_profile_id`。新建 `ai_profiles.py`（存取/迁移/`resolve_profile`）与 `ai_acp.py`（ACP 客户端）。`ai_provider.py` 的 `generate_ai_text/stream_ai_text` 加 `profile_id`，按 profile 的 provider 类型分派（openai_compat HTTP / acp / codex_cli）。settings 单配置 CRUD → profile CRUD。5 个 AI 端点透传 profile_id。

**技术栈：** Python 3.12 / FastAPI / httpx。测试 `cd backend && uv run --extra dev pytest`。

**关键现状：**
- 单配置存 secrets.json：`ai_provider/ai_base_url/ai_api_key/ai_model/ai_codex_command/ai_user_agent`；`secrets_store.save(updates)` 合并写（可存 list）、`clear(*keys)` 删键、`mask()` 脱敏。
- AI 调用最深处：`stream_ai_text`（stock_analyzer:337 / market_recap:308 / financial_analyzer:177）、`generate_ai_text`（agent:35,49 / ai_generator:81）。
- codex 现有实现：`_run_codex_cli`/`_codex_prompt`/`_prepare_codex_home`（`ai_provider.py`），读全局配置。

**向后兼容底线：** `generate_ai_text/stream_ai_text` 新增的 `profile_id` 默认 None → 默认 profile；所有现有调用不传即行为不变。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/ai_profiles.py` | profile 模型 + JSON 存取 + 迁移 + resolve | 创建 |
| `backend/app/services/ai_acp.py` | ACP 客户端适配器（spawn + handshake + 拒 tool 权限 + 收文本） | 创建 |
| `backend/app/services/ai_provider.py` | 按 profile 分派 | 改：generate/stream 加 profile_id；内部吃 profile 字段 |
| `backend/app/api/settings.py` | AI 配置 CRUD | 改：单 `POST /ai` → profile CRUD 端点组 |
| `backend/app/api/stock_analysis.py`/`agent.py`/`financials.py`/`strategy.py` + 对应 service | 5 入口透传 | 改：request 收 profile_id → 传到 AI 调用 |
| `backend/tests/services/test_ai_profiles.py` 等 | 单测 | 创建 |

---

### 任务 1：profile 数据模型 + store + 迁移

**文件：**
- 创建：`backend/app/services/ai_profiles.py`
- 测试：`backend/tests/services/test_ai_profiles.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_ai_profiles.py
import pytest

from app.services import ai_profiles as ap


@pytest.fixture(autouse=True)
def _tmp_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    (tmp_path / "user_data").mkdir(parents=True, exist_ok=True)


def test_create_list_default():
    p = ap.create_profile(name="OpenAI", provider="openai_compat",
                          base_url="https://api.openai.com/v1", api_key="sk-x", model="gpt-4o")
    assert p["id"].startswith("p_")
    assert ap.get_default_profile_id() == p["id"]     # 首条自动设默认
    assert [x["name"] for x in ap.list_profiles()] == ["OpenAI"]


def test_masked_list_hides_key():
    ap.create_profile(name="A", provider="openai_compat", api_key="sk-secret", model="m")
    masked = ap.list_profiles_masked()
    assert "sk-secret" not in str(masked)
    assert masked[0]["has_api_key"] is True


def test_resolve_falls_back_to_default():
    a = ap.create_profile(name="A", provider="openai_compat", api_key="k", model="m")
    ap.create_profile(name="B", provider="codex_cli", codex_command="codex")
    ap.set_default(a["id"])
    assert ap.resolve_profile(None)["id"] == a["id"]
    assert ap.resolve_profile("nonexistent")["id"] == a["id"]   # 非法 id 静默回落默认
    assert ap.resolve_profile(a["id"])["name"] == "A"


def test_delete_reassigns_default():
    a = ap.create_profile(name="A", provider="openai_compat", api_key="k", model="m")
    b = ap.create_profile(name="B", provider="openai_compat", api_key="k2", model="m")
    ap.set_default(a["id"])
    ap.delete_profile(a["id"])
    assert ap.get_default_profile_id() == b["id"]   # 删默认 → 落到剩余第一条


def test_migrate_legacy_flat_config(monkeypatch):
    from app import secrets_store
    secrets_store.save({
        "ai_provider": "openai_compat", "ai_base_url": "https://x/v1",
        "ai_api_key": "sk-old", "ai_model": "gpt-4o",
    })
    ap.migrate_legacy_if_needed()
    profiles = ap.list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "默认"
    assert profiles[0]["api_key"] == "sk-old"
    assert ap.get_default_profile_id() == profiles[0]["id"]
    # 幂等：再跑一次不重复迁移
    ap.migrate_legacy_if_needed()
    assert len(ap.list_profiles()) == 1
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/test_ai_profiles.py -v`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现**

```python
# backend/app/services/ai_profiles.py
"""AI 配置（profile）存取 + 迁移 + 解析（多 AI 配置特性）。

secrets.json: ai_profiles: [ {id,name,provider,base_url,api_key,model,codex_command,
launch_command,user_agent} ], ai_default_profile_id: <id>。
provider ∈ openai_compat | acp | codex_cli。
"""
from __future__ import annotations

import uuid

from app import secrets_store

_PROFILE_FIELDS = ("id", "name", "provider", "base_url", "api_key", "model",
                   "codex_command", "launch_command", "user_agent")


def _all() -> list[dict]:
    return list(secrets_store.load().get("ai_profiles") or [])


def _persist(profiles: list[dict], default_id: str | None) -> None:
    secrets_store.save({"ai_profiles": profiles, "ai_default_profile_id": default_id or ""})


def list_profiles() -> list[dict]:
    return _all()


def list_profiles_masked() -> list[dict]:
    out = []
    for p in _all():
        m = {k: p.get(k) for k in _PROFILE_FIELDS if k != "api_key"}
        m["has_api_key"] = bool(p.get("api_key"))
        m["api_key_masked"] = secrets_store.mask(p.get("api_key") or "")
        m["is_default"] = p["id"] == get_default_profile_id()
        out.append(m)
    return out


def get_profile(profile_id: str) -> dict | None:
    return next((p for p in _all() if p.get("id") == profile_id), None)


def get_default_profile_id() -> str:
    did = secrets_store.load().get("ai_default_profile_id") or ""
    if did and get_profile(did):
        return did
    profiles = _all()
    return profiles[0]["id"] if profiles else ""


def resolve_profile(profile_id: str | None) -> dict | None:
    """按 id 解析；None/非法 → 默认 profile；无任何 profile → None。"""
    if profile_id:
        p = get_profile(profile_id)
        if p:
            return p
    did = get_default_profile_id()
    return get_profile(did) if did else None


def create_profile(*, name: str, provider: str, base_url: str = "", api_key: str = "",
                   model: str = "", codex_command: str = "", launch_command: str = "",
                   user_agent: str = "") -> dict:
    profile = {
        "id": f"p_{uuid.uuid4().hex[:8]}", "name": name, "provider": provider,
        "base_url": base_url, "api_key": api_key, "model": model,
        "codex_command": codex_command, "launch_command": launch_command,
        "user_agent": user_agent,
    }
    profiles = _all()
    profiles.append(profile)
    default_id = get_default_profile_id() or profile["id"]  # 首条自动默认
    _persist(profiles, default_id)
    return profile


def update_profile(profile_id: str, **fields) -> dict:
    profiles = _all()
    for p in profiles:
        if p["id"] == profile_id:
            for k, v in fields.items():
                if k in _PROFILE_FIELDS and k != "id" and v is not None:
                    p[k] = v
            _persist(profiles, get_default_profile_id())
            return p
    raise KeyError(profile_id)


def delete_profile(profile_id: str) -> None:
    profiles = [p for p in _all() if p["id"] != profile_id]
    default_id = get_default_profile_id()
    if default_id == profile_id:
        default_id = profiles[0]["id"] if profiles else ""
    _persist(profiles, default_id)


def set_default(profile_id: str) -> None:
    if not get_profile(profile_id):
        raise KeyError(profile_id)
    _persist(_all(), profile_id)


def migrate_legacy_if_needed() -> None:
    """旧扁平配置 → 一条 name=默认 的 profile（幂等，仅在无 ai_profiles 时）。"""
    data = secrets_store.load()
    if data.get("ai_profiles"):
        return
    if not any(data.get(k) for k in ("ai_provider", "ai_api_key", "ai_codex_command")):
        return  # 无旧配置，不建空 profile
    create_profile(
        name="默认", provider=data.get("ai_provider") or "openai_compat",
        base_url=data.get("ai_base_url") or "", api_key=data.get("ai_api_key") or "",
        model=data.get("ai_model") or "", codex_command=data.get("ai_codex_command") or "",
        user_agent=data.get("ai_user_agent") or "",
    )
```

- [ ] **步骤 4：运行测试验证通过**

- [ ] **步骤 5：迁移接入启动 + Commit**

在 `app/main.py` lifespan 早期调用 `ai_profiles.migrate_legacy_if_needed()`（在 AI 相关初始化前）。

```bash
cd backend && uv run --extra dev pytest tests/services/test_ai_profiles.py -v
git add app/services/ai_profiles.py app/main.py tests/services/test_ai_profiles.py
git commit -m "feat(ai): multi AI-config store with legacy migration (ai_profiles)"
```

---

### 任务 2：ai_provider.py 按 profile 分派（openai_compat + codex）

给 AI 入口加 `profile_id`，内部按 profile 字段取配置（不再读全局）。**本任务先不含 acp**（任务 5 加），acp 类型暂时抛"未接入"清晰错误。

**文件：**
- 修改：`backend/app/services/ai_provider.py`
- 测试：`backend/tests/services/test_ai_provider_profile.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_ai_provider_profile.py
import pytest

from app.services import ai_provider, ai_profiles as ap


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    (tmp_path / "user_data").mkdir(parents=True, exist_ok=True)


def test_resolve_openai_config_from_profile():
    p = ap.create_profile(name="A", provider="openai_compat",
                          base_url="https://a/v1", api_key="sk-a", model="m-a")
    cfg = ai_provider._openai_config_for(ap.resolve_profile(p["id"]))
    assert cfg["base_url"] == "https://a/v1"
    assert cfg["api_key"] == "sk-a"
    assert cfg["model"] == "m-a"


@pytest.mark.asyncio
async def test_generate_uses_selected_profile(monkeypatch):
    a = ap.create_profile(name="A", provider="openai_compat", base_url="https://a/v1",
                          api_key="sk-a", model="m-a")
    b = ap.create_profile(name="B", provider="openai_compat", base_url="https://b/v1",
                          api_key="sk-b", model="m-b")
    seen = {}

    async def fake_openai(messages, *, profile, **kw):
        seen["model"] = profile["model"]
        return "ok"

    monkeypatch.setattr(ai_provider, "_run_openai_once", fake_openai)
    await ai_provider.generate_ai_text([{"role": "user", "content": "hi"}], profile_id=b["id"])
    assert seen["model"] == "m-b"   # 用了指定 profile，而非默认 A
```

- [ ] **步骤 2：运行验证失败**

- [ ] **步骤 3：实现**

在 `ai_provider.py`：
1. 加解析 helper：

```python
def _openai_config_for(profile: dict) -> dict:
    return {"base_url": profile.get("base_url") or "", "api_key": profile.get("api_key") or "",
            "model": profile.get("model") or "", "user_agent": profile.get("user_agent") or ""}
```

2. `generate_ai_text`/`stream_ai_text` 各加 `profile_id: str | None = None`，函数体开头解析并按类型分派：

```python
async def generate_ai_text(messages, *, profile_id: str | None = None, temperature=0.3,
                           max_tokens=3000, timeout=180.0) -> str:
    from app.services import ai_profiles
    profile = ai_profiles.resolve_profile(profile_id)
    if profile is None:
        raise RuntimeError("未配置任何 AI 配置")
    kind = profile.get("provider")
    if kind == "codex_cli":
        return await _run_codex_cli(messages, profile=profile, max_tokens=max_tokens,
                                    timeout=max(timeout, 600.0))
    if kind == "acp":
        raise RuntimeError("ACP 配置尚未接入（见任务 5）")  # 任务 5 替换
    return await _run_openai_once(messages, profile=profile, temperature=temperature,
                                  max_tokens=max_tokens, timeout=timeout)
```

（`stream_ai_text` 同样分派；codex/acp 走"整块返回"，openai 走真流式。）

3. `_run_openai_once`/`_openai_client`/`_run_codex_cli`/`_codex_prompt` 改为吃 `profile` 参数（base_url/api_key/model/codex_command 从 profile 取，不再 `secrets_store.get_ai_config`/`current_*`）。

4. `current_ai_provider/current_ai_model/current_codex_command/ai_configured/codex_cli_available` 保留但改为"作用于默认 profile"（`resolve_profile(None)`），供 settings 状态展示用；调用方无需改。

- [ ] **步骤 4：运行测试 + 全量回归**

```bash
cd backend && uv run --extra dev pytest tests/services/test_ai_provider_profile.py -v && uv run --extra dev pytest -q
```

- [ ] **步骤 5：Commit** `git commit -am "feat(ai): dispatch AI calls by profile (openai_compat + codex)"`

---

### 任务 3：settings profile CRUD API

**文件：**
- 修改：`backend/app/api/settings.py`（替换单 `POST /ai`，加 profile 端点组；`GET /api/settings` 的 AI 字段改为 profile 摘要）
- 测试：`backend/tests/api/test_ai_profiles_api.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/api/test_ai_profiles_api.py
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    (tmp_path / "user_data").mkdir(parents=True, exist_ok=True)
    from app.api.settings import router
    app = FastAPI(); app.include_router(router)
    return TestClient(app)


def test_profile_crud(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/settings/ai/profiles", json={"name": "A", "provider": "openai_compat",
               "base_url": "https://a/v1", "api_key": "sk-a", "model": "m"})
    assert r.status_code == 200
    pid = r.json()["id"]

    lst = c.get("/api/settings/ai/profiles").json()["profiles"]
    assert lst[0]["is_default"] is True
    assert "sk-a" not in str(lst)                 # 脱敏
    assert lst[0]["has_api_key"] is True

    c.post("/api/settings/ai/profiles", json={"name": "B", "provider": "codex_cli",
           "codex_command": "codex"})
    b_id = c.get("/api/settings/ai/profiles").json()["profiles"][1]["id"]
    assert c.post(f"/api/settings/ai/profiles/{b_id}/default").status_code == 200
    assert c.get("/api/settings/ai/profiles").json()["profiles"][1]["is_default"] is True

    assert c.delete(f"/api/settings/ai/profiles/{pid}").status_code == 200
    assert len(c.get("/api/settings/ai/profiles").json()["profiles"]) == 1


def test_update_empty_key_keeps_existing(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    pid = c.post("/api/settings/ai/profiles", json={"name": "A", "provider": "openai_compat",
                 "api_key": "sk-keep", "model": "m"}).json()["id"]
    # 编辑不传 api_key（或传空）→ 不改原 key
    c.put(f"/api/settings/ai/profiles/{pid}", json={"name": "A2"})
    from app.services import ai_profiles
    assert ai_profiles.get_profile(pid)["api_key"] == "sk-keep"
```

- [ ] **步骤 2：运行验证失败**

- [ ] **步骤 3：实现**

`settings.py` 加端点（复用任务 1 的 `ai_profiles`）：

```python
class AiProfileIn(BaseModel):
    name: str
    provider: str = "openai_compat"
    base_url: str = ""
    api_key: str | None = None     # None/"" = 不改（编辑时保留原 key）
    model: str = ""
    codex_command: str = ""
    launch_command: str = ""
    user_agent: str = ""


@router.get("/ai/profiles")
def list_ai_profiles() -> dict:
    from app.services import ai_profiles
    return {"profiles": ai_profiles.list_profiles_masked(),
            "default_id": ai_profiles.get_default_profile_id()}


@router.post("/ai/profiles")
def create_ai_profile(req: AiProfileIn) -> dict:
    from app.services import ai_profiles
    p = ai_profiles.create_profile(**{**req.model_dump(), "api_key": req.api_key or ""})
    return {"id": p["id"]}


@router.put("/ai/profiles/{profile_id}")
def update_ai_profile(profile_id: str, req: AiProfileIn) -> dict:
    from app.services import ai_profiles
    fields = req.model_dump()
    if not req.api_key:           # 空/None 不覆盖原 key
        fields.pop("api_key", None)
    try:
        ai_profiles.update_profile(profile_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="AI 配置不存在")
    return {"ok": True}


@router.delete("/ai/profiles/{profile_id}")
def delete_ai_profile(profile_id: str) -> dict:
    from app.services import ai_profiles
    ai_profiles.delete_profile(profile_id)
    return {"ok": True}


@router.post("/ai/profiles/{profile_id}/default")
def set_default_ai_profile(profile_id: str) -> dict:
    from app.services import ai_profiles
    try:
        ai_profiles.set_default(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="AI 配置不存在")
    return {"ok": True}
```

**兼容处理（codex review Medium 1/2）：**
- **`GET /api/settings` 保留现有扁平 AI 字段**（`ai_provider/ai_base_url/ai_model/...` 取自默认 profile），**额外**新增 `ai_default_id` + profile 数量。理由：现有前端 `SettingsState`/`Layout`/旧 AI 页仍读扁平字段（`api.ts:627-640`、`Layout.tsx:402-405`、`settings/AI.tsx:56-72`），过渡期不能断；前端计划改造完这些消费者后再议移除。
- **旧 `POST /api/settings/ai` 保持原语义不变**（含 `api_key=""` **清空默认 profile 的 key**，对齐 `settings.py:100-106`）——**不要**套用新 profile 的"空=不改"语义，否则破坏现有清 key 行为。新 `PUT /ai/profiles/{id}` 才用"空=不改"。
- codex profile 的 `codex_command` 用现有 `normalize_codex_command` 校验。

- [ ] **步骤 4：运行测试 + Commit** `git commit -am "feat(settings): AI profile CRUD endpoints"`

---

### 任务 4：5 个 AI 入口 + 定时任务透传 profile_id

**文件（各接收可选 profile_id → 传到 AI 调用）：**
- `stock_analysis.py` `/analyze`（Form/query `profile_id`）→ `stock_analyzer.analyze_stock_stream(..., profile_id)` → `stream_ai_text(..., profile_id=profile_id)`（`stock_analyzer.py:337`）
- market recap 生成端点 → `market_recap` stream（`market_recap.py:308`）
- `financials.py` `/analyze` → `financial_analyzer` stream（`:177`）
- **策略构建（codex review High 3 — 前端真正走的入口）**：`strategy.py` `POST /build`（`BuildRequest` 加 `profile_id`）→ `AIStrategyGenerator.generate(prompt, profile_id=...)`（`strategy.py:366,382`；generate 内部 `generate_ai_text` `ai_generator.py:81`）。**这是前端 `strategyBuild` 调的两步构建入口，必须覆盖**；`/ai/generate`、`/ai/test`（`strategy.py:389,349`）一并加 `profile_id`（次要，非前端主路径）。
- `agent.py` `/chat` → `generate_ai_text(..., profile_id)`（`:35,49`）：**`AgentChatIn` 加 `profile_id` 字段**（agent 前端 UI 暂无，但后端先备好，见前端计划 per-thread 延后说明）。
- **定时任务**（定时复盘等调 market_recap 的路径）：显式 `profile_id=None`（走默认），加断言测试。

> `AIStrategyGenerator.generate(self, prompt)` 签名加 `profile_id: str | None = None`，透传给内部 `generate_ai_text`。

- [ ] **步骤 1：为每个入口写"profile_id 透传"测试**

对每入口用 monkeypatch 断言：请求带 `profile_id="p_x"` 时，最终 `stream_ai_text/generate_ai_text` 收到 `profile_id="p_x"`；不带时收到 None。示例（stock analysis）：

```python
# backend/tests/api/test_ai_profile_passthrough.py
import app.services.stock_analyzer as sa

@pytest.mark.asyncio
async def test_stock_analysis_passes_profile_id(monkeypatch):
    seen = {}
    async def fake_stream(messages, *, profile_id=None, **kw):
        seen["pid"] = profile_id
        if False: yield ""   # async generator
    monkeypatch.setattr(sa, "stream_ai_text", fake_stream)
    # 调 analyze_stock_stream(..., profile_id="p_x") 并 drain，断言 seen["pid"]=="p_x"
```

（其余 4 入口同构，各一条。）

- [ ] **步骤 2：逐入口加 `profile_id` 参数并透传**（service 函数签名加 `profile_id: str | None = None`，端点从 request 取）

- [ ] **步骤 3：定时任务断言用默认**（定时复盘调用处不传 profile_id / 显式 None，测试断言）

- [ ] **步骤 4：全量测试 + Commit** `git commit -am "feat(ai): thread profile_id through 5 AI entrypoints; scheduled uses default"`

---

### 任务 5：ACP 传输适配器（**按真实 ACP schema，本期只承诺 Hermes**）

> **codex review 修订（High 1 + High 2）**：原 stub 的报文结构是猜的、与真实 ACP schema 不符；且 claude 无原生 ACP、opencode 是 server 形态。本任务据实修正。

**本期 ACP 工具承诺（实测）：**
- **Hermes**：`hermes acp` 原生 ✅（`hermes acp --check` → "Hermes ACP check OK"）。本期 ACP **只承诺 Hermes**。
- **opencode**：`opencode acp` 存在但暴露 `--port/--hostname`，是 **server 形态**（非 stdio 子进程）→ 标"需确认 transport/桥接"，**本期不接**。
- **claude**：无 `acp` 子命令（`claude acp` 落到通用 help）→ **本期不列原生 ACP**，除非另立 bridge。

**真实 ACP schema（本机证据 `~/.hermes/hermes-agent/venv/.../acp/schema.py`）：**
- 文本增量：`session/update` **notification**，`params.update` 是 typed union；文本块是 `AgentMessageChunk`，判别键 `sessionUpdate == "agent_message_chunk"`（alias），内含 content block。**不是** `update.content`。
- 拒绝权限：对 `session/request_permission` 请求回 `RequestPermissionResponse`，`outcome = DeniedOutcome(outcome="cancelled")`（**不是**随意 deny）。允许则 `AllowedOutcome(outcome="selected", optionId=...)`。

**文件：**
- 创建：`backend/app/services/ai_acp.py`
- 修改：`backend/app/services/ai_provider.py`（acp 分派替换任务 2 的占位错误）
- 测试：`backend/tests/services/test_ai_acp.py`

- [ ] **步骤 0：Hermes ACP 真实握手 spike（先做，再写码）**

用 Python 起 `hermes acp` 子进程，手动跑一遍 `initialize → session/new → session/prompt`，打印收到的原始 JSON-RPC 消息，**核实**：① agent_message_chunk 的 content block 取文本的确切路径；② `session/request_permission` 的请求/响应字段；③ 结束信号（`session/prompt` 的 result / stopReason 字段名）。以 spike 观测到的真实报文为准，校正下面步骤的字段。若可直接 `import acp`（Hermes venv 的 SDK）复用其 pydantic 模型构造/解析，优先用 SDK 而非手拼 dict。

- [ ] **步骤 1：编写失败的测试（stub 按真实 schema 构造）**

```python
# backend/tests/services/test_ai_acp.py
import pytest
from app.services import ai_acp


@pytest.mark.asyncio
async def test_acp_denies_permission_and_accumulates_agent_chunks(monkeypatch):
    # 按真实 ACP schema：文本走 agent_message_chunk；权限请求回 DeniedOutcome(cancelled)
    transport = ai_acp._StubTransport(scripted=[
        {"jsonrpc": "2.0", "id": 7, "method": "session/request_permission",
         "params": {"toolCall": {}, "options": []}},
        {"jsonrpc": "2.0", "method": "session/update",
         "params": {"update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text", "text": "分析"}}}},
        {"jsonrpc": "2.0", "method": "session/update",
         "params": {"update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text", "text": "结果"}}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}},
    ])
    text = await ai_acp.run_acp(transport, prompt="hi", model="m")
    assert text == "分析结果"
    # 拒绝：对 id=7 的 request_permission 回 outcome=cancelled
    assert transport.responses_by_id[7]["result"]["outcome"]["outcome"] == "cancelled"


def test_is_available_uses_shlex(monkeypatch):
    import shutil
    monkeypatch.setattr(ai_acp.shutil, "which",
                        lambda c: "/usr/bin/hermes" if c == "hermes" else None)
    assert ai_acp.is_available("hermes acp") is True
    assert ai_acp.is_available('"/opt/x y/hermes" acp') is False  # shlex 拆出的路径 which 不到
    assert ai_acp.is_available("nope acp") is False
```

- [ ] **步骤 2：运行验证失败**

- [ ] **步骤 3：实现 `ai_acp.py`（字段以步骤 0 spike 核实为准）**

- `import shutil, shlex`。`is_available(launch_command)`：`shutil.which(shlex.split(launch_command)[0])` 非空（**用 shlex 而非 split()[0]**，处理引号/路径——Medium 3）。
- `run_acp(transport, *, prompt, model, timeout=600) -> str`：`initialize`（client 能力声明不授予任何 tool 权限）→ `session/new` → `session/prompt`；循环读：
  - 收到 `method == "session/request_permission"`（带 id）→ 回 `{"jsonrpc":"2.0","id":<id>,"result":{"outcome":{"outcome":"cancelled"}}}`（DeniedOutcome）。
  - 收到 `method == "session/update"` 且 `params.update.sessionUpdate == "agent_message_chunk"` → 取 content block 文本累积（路径以 spike 为准，通常 `params.update.content.text`）。
  - 收到 `session/prompt` 的 result（stopReason）→ 返回累积文本。
  - 超时/进程退出兜底报错。
- `_ProcessTransport(launch_command)`：`asyncio.create_subprocess_exec(*shlex.split(cmd), ...)`，逐行 JSON-RPC；`_StubTransport`：回放 `scripted`，记录 `responses_by_id`（供断言拒权限）。

- [ ] **步骤 4：ai_provider 接入 acp 分派**（同原步骤，`_ProcessTransport(cmd)` + `is_available` 兜底报错）

- [ ] **步骤 5：真 Hermes 冒烟**

```bash
cd backend && uv run --extra dev pytest tests/services/test_ai_acp.py -v
# 手动：建 acp profile(launch_command="hermes acp") → generate 一次 → 确认拿到文本、且 panel 目录无文件被改动（git status 干净）
```

- [ ] **步骤 6：Commit** `git commit -am "feat(ai): ACP transport (Hermes, real schema, tool-perms denied)"`

---

### 任务 6：收尾回归

- [ ] 全量 `uv run --extra dev pytest -q` + `uv run python -c "from app.main import app; print('ok')"`。
- [ ] 手动：配 openai_compat + codex + acp 三条 profile，分别 generate 一次，确认各走各的。
- [ ] Commit（若有文档更新）。

---

## 自检（规格覆盖）

- ✅ 多 profile 存取 + 默认 + 迁移（任务 1）
- ✅ 按 profile 分派 openai_compat/codex（任务 2）
- ✅ profile CRUD API + 脱敏（任务 3）
- ✅ 5 入口 + 定时透传（任务 4）
- ✅ ACP 传输 + 拒 tool 权限 + 可用性探测（任务 5）
- 前端多配置管理/选择器/agent per-thread → 见 `2026-07-03-multi-ai-provider-frontend.md`
