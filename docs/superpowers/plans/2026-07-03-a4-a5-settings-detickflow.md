# A4+A5：settings/health 去 TickFlow 展示 + 删除无引用遗留 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框语法来跟踪进度。

**目标：** ① `/health`、`/api/settings` 不再默认以 TickFlow 语义展示；② 前端设置/引导页不再渲染 TickFlow key/档位 UI，统一展示当前 provider capability；③ 删除零引用的 `app/tickflow/scheduler.py`。

**架构：** 后端加 provider 感知的 `current_data_mode()`，settings GET 响应只保留 `mode/data_provider` 与 AI/偏好设置字段，TickFlow key/tier/endpoint/probe 字段不再出现在通用 settings 契约中；前端账号页和引导页只展示当前数据源与 capability。当前路线已进入 A6 前置状态，不再保留旧 TickFlow 过渡块。

**技术栈：** Python 3.12 / FastAPI；前端 React + TS（vite）。测试 `cd backend && uv run --extra dev pytest`；前端 `cd frontend && npm run build`（类型检查兜底）。

**前置依赖：** A2（`app.capabilities` 中性模块）已完成。未完成也可执行，但 import 路径按当时实际为准。

**范围决策（对路线图 A5 的修正）：** 路线图 A5 原含"删 `pools.py` / `tiers.yaml`"。它们是否随 TickFlowProvider 删除一起移除由 A6 再审计；本计划只确认 `scheduler.py` 已不存在且无引用。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/data_mode.py` | 数据源模式判定 | 新增 `current_data_mode()` |
| `backend/app/api/routes.py` | /health、/api/capabilities | mode 换成 provider 感知 |
| `backend/app/api/settings.py:52-81` | GET /api/settings | 加 `data_provider`，移除通用 TickFlow 字段 |
| `backend/app/tickflow/scheduler.py` | 零引用遗留 | 删除 |
| `frontend/src/lib/api.ts` | settings 类型 | settings 契约只保留 provider/AI 字段 |
| `frontend/src/pages/settings/Keys.tsx` | 账号设置页 | 数据源/capability 展示 |
| `frontend/src/pages/Onboarding.tsx` | 首次引导 | 数据源确认，不再配置 key |
| `frontend/src/components/Layout.tsx` | 侧栏状态卡 | 数据源卡片，不再提示配置 Key |
| `backend/tests/services/test_data_mode.py` | mode 单测 | 创建 |
| `backend/tests/api/test_settings_provider_block.py` | settings 响应单测 | 创建 |

---

### 任务 1：`current_data_mode()` + /health

**文件：**
- 修改：`backend/app/services/data_mode.py`
- 修改：`backend/app/api/routes.py:13-20`
- 测试：`backend/tests/services/test_data_mode.py`

- [x] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_data_mode.py
from app.services import data_mode


def test_mode_is_provider_name_when_not_tickflow(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name", lambda cap=None: "fquant_local"
    )
    assert data_mode.current_data_mode() == "fquant_local"


def test_mode_falls_back_to_fquant_local(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda capability=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert data_mode.current_data_mode() == "fquant_local"
```

- [x] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/test_data_mode.py -v`
预期：FAIL，`AttributeError: ... has no attribute 'current_data_mode'`

- [x] **步骤 3：实现 `current_data_mode()`**

在 `backend/app/services/data_mode.py` 追加：

```python
def current_data_mode() -> str:
    """健康检查/设置页展示用的运行模式。

    非 tickflow provider: 直接返回 provider 名（fquant / fquant_local）。
    registry 不可用时回退 fquant_local，避免健康检查退回 TickFlow 语义。
    """
    from app.data_providers.registry import get_active_provider_name

    try:
        name = get_active_provider_name()
    except Exception:  # noqa: BLE001
        return "fquant_local"
```

- [x] **步骤 4：/health 与 /api/capabilities 换用**

`backend/app/api/routes.py`：删除 `from app.tickflow import client as tf_client`（第 7 行），`health()` 中 `"mode": tf_client.current_mode()` 改为：

```python
from app.services.data_mode import current_data_mode
# ...
        "mode": current_data_mode(),
```

（`/api/capabilities` 的 `label` 不动——本地模式下 `_persist` 已写入 provider 名作 label。）

- [x] **步骤 5：运行测试 + 冒烟**

```bash
cd backend && uv run --extra dev pytest tests/services/test_data_mode.py -v && uv run python -c "from app.main import app; print('ok')"
```

- [x] **步骤 6：Commit**

```bash
git add app/services/data_mode.py app/api/routes.py tests/services/test_data_mode.py
git commit -m "feat(settings): provider-aware /health mode via current_data_mode()"
```

---

### 任务 2：settings GET 增加 `data_provider`，移除 TickFlow 通用字段

**文件：**
- 修改：`backend/app/api/settings.py:52-81`（`get_settings`）
- 测试：`backend/tests/api/test_settings_provider_block.py`

- [x] **步骤 1：编写失败的测试**

```python
# backend/tests/api/test_settings_provider_block.py
"""GET /api/settings 不再暴露 TickFlow key/tier 通用字段。"""
from app.api.settings import get_settings


def test_settings_has_provider_mode(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name", lambda cap=None: "fquant_local"
    )
    out = get_settings()

    assert out["data_provider"] == "fquant_local"
    assert out["mode"] == "fquant_local"
    assert "tickflow" not in out
    assert "tickflow_api_key_masked" not in out
```

- [x] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_settings_provider_block.py -v`
预期：FAIL，`KeyError: 'data_provider'` 或仍返回 TickFlow 字段。

- [x] **步骤 3：实现响应重组**

`backend/app/api/settings.py` `get_settings()`：return dict 增加 `data_provider`，并把 `"mode"` 改为 `current_data_mode()`：

```python
        "data_provider": get_active_provider_name(),
```

TickFlow key/tier/endpoint/probe 字段不保留在通用 settings 响应里；A6 再删除剩余 provider/client 代码。

- [x] **步骤 4：运行测试验证通过 + 全量回归**

```bash
cd backend && uv run --extra dev pytest tests/api/test_settings_provider_block.py -v && uv run --extra dev pytest -q
```

- [x] **步骤 5：Commit**

```bash
git add app/api/settings.py tests/api/test_settings_provider_block.py
git commit -m "feat(settings): expose provider mode without tickflow settings fields"
```

---

### 任务 3：前端中性化数据源展示

**文件：**
- 修改：`frontend/src/lib/api.ts:630-670`（SettingsState 类型）
- 修改：`frontend/src/pages/settings/Keys.tsx`
- 修改：`frontend/src/pages/Onboarding.tsx`
- 修改：`frontend/src/components/Layout.tsx`

- [x] **步骤 1：api.ts 类型补充**

在 `SettingsState`（`api.ts` 约 630 行的接口）保留 provider 字段，不新增 TickFlow 嵌套块：

```ts
  mode: 'fquant' | 'fquant_local'
  data_provider: 'fquant' | 'fquant_local'
```

- [x] **步骤 2：Keys.tsx 数据源/capability 展示**

账号页只展示当前 `data_provider` 与 `caps.data.capabilities`，不渲染 TickFlow API Key、注册链接或档位梯。

- [x] **步骤 3：Onboarding.tsx 数据源确认**

引导步骤名改为「数据源」，内容展示当前 provider，不再出现「配置 Key」。

- [x] **步骤 4：前端"套餐/升级/加购"文案清理（非 tickflow 语境）**

```bash
grep -rn "套餐\|升级\|加购" frontend/src --include="*.tsx" --include="*.ts"
```
对命中的每处：若文案在 `isTickflow` 条件块内保留；若是无条件展示（如 capability 缺失提示），改为中性文案「当前数据源不支持此能力」。

- [x] **步骤 5：构建验证**

运行：`cd frontend && npm run build`
预期：类型检查 + 构建通过

- [x] **步骤 6：手动验证两种模式**

- `DATA_PROVIDER=fquant_local` 启动：设置页无 TickFlow 卡片、显示本地数据源卡；引导页无注册链接。
- 侧栏状态卡显示「数据源」和当前 provider，不再提示配置 Key。

- [x] **步骤 7：Commit**

```bash
git add frontend/src
git commit -m "feat(ui): show provider capabilities instead of tickflow key tiers"
```

---

### 任务 4（A5）：删除零引用的 `tickflow/scheduler.py`

- [x] **步骤 1：删除前最终 grep 确认（必须为 0 输出）**

```bash
cd backend
grep -rn "tickflow.scheduler\|tickflow import scheduler" app tests scripts
```

- [x] **步骤 2：删除**

```bash
git rm app/tickflow/scheduler.py
```

- [x] **步骤 3：全量测试 + 冒烟**

```bash
uv run --extra dev pytest -q && uv run python -c "from app.main import app; print('ok')"
```

- [x] **步骤 4：Commit**

```bash
git commit -m "chore: remove unreferenced app/tickflow/scheduler.py (A5)"
```

> **提醒：** `pools.py` 与根目录 `tiers.yaml` 不在本计划处理；随 A6 的 TickFlowProvider/client 删除审计统一决定。
