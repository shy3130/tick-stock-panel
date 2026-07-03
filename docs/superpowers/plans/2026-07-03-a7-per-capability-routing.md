# A7：provider capability 路由补全（financial / depth）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 补齐审计 High-3：`financial` 与 `depth` 两条能力目前跟随全局 provider（`get_active_provider_name()` 无参调用），无法像 daily/minute/realtime/adj_factor 那样独立切源。补全后 per-capability 混源配置完整。

**架构：** 完全复制既有模式（`preferences.get_daily_data_provider` → `registry.get_active_provider_name("daily")` → 调用点带 capability 参数），零新抽象。`financial_sync` 与 `depth_service` 的 provider 单例改为按对应 capability 解析。

**技术栈：** Python 3.12。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- `registry.get_active_provider_name(capability)` 已支持 `daily`、`minute`、`realtime`、`adj_factor`，但 `financial`、`depth` 仍缺 capability 分支。
- `financial_sync._get_data_provider()` 当前无参解析 active provider，导致财务无法独立跟随 financial capability 偏好。
- `depth_service` 目前借用 `kline_sync._get_data_provider`，实际按日线 capability/全局 provider 走；这会让 depth 的缺口和路由语义混在一起。
- 本计划只补对称路由，不承诺 fquant_local 已有 depth；fquant_local depth 缺口仍由 capability 降级处理。

**YAGNI 边界（先确认再动手）：** 路线图已标注"仅 per-capability 混切才需要——可能 YAGNI"。执行前问一句用户是否真有混源需求；若答复"暂无"，只做任务 1（preferences+registry 的 5 行对称补全，成本≈0，保持抽象完整），任务 2/3 记 YAGNI 搁置。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/preferences.py:125-142` | per-capability provider 偏好 | 增 financial/depth getter + set_data_provider 级联 |
| `backend/app/data_providers/registry.py:36-45` | capability→provider 解析 | 增 financial/depth 分支 |
| `backend/app/services/financial_sync.py:58` | 财务 provider 单例 | 带 capability 参数 |
| `backend/app/services/depth_service.py` | 盘口 provider | 独立解析（不再借 kline_sync 的） |
| `backend/tests/data_providers/test_registry_financial_depth.py` | 路由单测 | 创建 |

---

### 任务 1：preferences + registry 对称补全

**文件：**
- 修改：`backend/app/services/preferences.py`
- 修改：`backend/app/data_providers/registry.py`
- 测试：`backend/tests/data_providers/test_registry_financial_depth.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/data_providers/test_registry_financial_depth.py
from app.data_providers import registry


def test_financial_capability_uses_financial_pref(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(
        "app.services.preferences.get_financial_data_provider", lambda: "fquant"
    )
    assert registry.get_active_provider_name("financial") == "fquant"


def test_depth_capability_uses_depth_pref(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(
        "app.services.preferences.get_depth_data_provider", lambda: "fquant_local"
    )
    assert registry.get_active_provider_name("depth") == "fquant_local"


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "fquant_local")
    assert registry.get_active_provider_name("financial") == "fquant_local"
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/data_providers/test_registry_financial_depth.py -v`
预期：FAIL，`AttributeError: ... no attribute 'get_financial_data_provider'`

- [ ] **步骤 3：preferences 补 getter + 级联**

`backend/app/services/preferences.py`，紧跟 `get_realtime_data_provider`（142 行后）：

```python
def get_financial_data_provider() -> str:
    return _clean_data_provider(load().get("financial_data_provider", get_data_provider()))


def get_depth_data_provider() -> str:
    return _clean_data_provider(load().get("depth_data_provider", get_data_provider()))
```

`set_data_provider`（111-122 行）的 `save({...})` 字典追加两行（与 daily/minute/realtime 同样级联）：

```python
        "financial_data_provider": provider,
        "depth_data_provider": provider,
```

- [ ] **步骤 4：registry 补分支**

`backend/app/data_providers/registry.py` `get_active_provider_name` 中，`realtime` 分支之后追加：

```python
        if capability == "financial":
            return normalize_provider_name(preferences.get_financial_data_provider())
        if capability == "depth":
            return normalize_provider_name(preferences.get_depth_data_provider())
```

- [ ] **步骤 5：运行测试验证通过 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/data_providers/test_registry_financial_depth.py -v
git add -A && git commit -m "feat(provider): per-capability routing for financial/depth (A7)"
```

---

### 任务 2：financial_sync 按 financial capability 解析（若确认需要混源）

**文件：**
- 修改：`backend/app/services/financial_sync.py:58`

- [ ] **步骤 1：改单例解析**

`_get_data_provider()` 内 `provider_name = get_active_provider_name()` 改为：

```python
        provider_name = get_active_provider_name("financial")
```

docstring 的"默认 tickflow"描述同步更新为"按 financial capability 偏好解析"。

- [ ] **步骤 2：单例失效问题确认**

`_provider_instance` 是模块级缓存；切换偏好后需重置。确认 `app/api/settings.py:318` `_reset_data_provider_singletons()` 是否覆盖 `financial_sync._provider_instance`——若没有，加上：

```python
    from app.services import financial_sync
    financial_sync._provider_instance = None
```

- [ ] **步骤 3：全量测试 + Commit** `git commit -am "feat(provider): financial_sync resolves provider by financial capability"`

---

### 任务 3：depth_service 独立解析（若确认需要混源）

**文件：**
- 修改：`backend/app/services/depth_service.py:37,273`

- [ ] **步骤 1：** 删 `from app.services.kline_sync import _get_data_provider`（37 行），改为本模块内：

```python
def _get_data_provider():
    from app.data_providers.registry import get_active_provider_name, get_provider
    return get_provider(get_active_provider_name("depth"))
```

（不做模块级缓存——depth 调用频度低，每次解析开销可忽略，还省掉单例失效问题。）

- [ ] **步骤 2：** 全量测试 + 手动验证：`DATA_PROVIDER` 未设、preferences 里 depth 独立设为 fquant_local 时，`_has_capability()` 返回 False（fquant 无 depth）且日志打出 provider 名。

- [ ] **步骤 3：Commit** `git commit -am "feat(provider): depth_service resolves provider by depth capability"`
