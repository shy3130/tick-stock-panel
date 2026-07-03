# A4+A5：settings/health 去 TickFlow 展示 + 删除无引用遗留 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** ① `/health`、`/api/settings` 不再默认以 TickFlow 语义展示（mode/key/tier/endpoint/probe 变成"可选 TickFlow provider"专属块）；② 前端设置/引导页在非 tickflow provider 下不再渲染 TickFlow key/档位 UI；③ 删除零引用的 `app/tickflow/scheduler.py`。

**架构：** 后端加一个 provider 感知的 `current_data_mode()`（非 tickflow provider → mode 即 provider 名），settings GET 响应新增 `data_provider` 字段并把 TickFlow 专属字段**同时**嵌套进 `tickflow: {...}` 块（顶层旧字段保留一版供前端过渡）；前端按 `data_provider === 'tickflow'` 条件渲染。行为不变量：`DATA_PROVIDER=tickflow` 时一切展示与现状完全一致。

**技术栈：** Python 3.12 / FastAPI；前端 React + TS（vite）。测试 `cd backend && uv run --extra dev pytest`；前端 `cd frontend && npm run build`（类型检查兜底）。

**前置依赖：** A2（`app.capabilities` 中性模块）已完成。未完成也可执行，但 import 路径按当时实际为准。

**范围决策（对路线图 A5 的修正）：** 路线图 A5 原含"删 `pools.py` / `tiers.yaml`"。但 `pools` 仍是 `DATA_PROVIDER=tickflow` 时的标的池 fallback（`kline.py:511,881`、`daily_pipeline.py:67`、`extend_history.py:64`，全部已按 `provider_name == "tickflow"` 门控），`tiers.yaml` 仍被 tickflow 探测路径读取——而 A4 明确要求保留 tickflow 退路。**故 pools/tiers.yaml 的删除移入 A6**（与 TickFlowProvider 同批），本计划只删确认零引用的 `scheduler.py`。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/data_mode.py` | 数据源模式判定 | 新增 `current_data_mode()` |
| `backend/app/api/routes.py` | /health、/api/capabilities | mode 换成 provider 感知 |
| `backend/app/api/settings.py:52-81` | GET /api/settings | 加 `data_provider` + 嵌套 `tickflow` 块 |
| `backend/app/tickflow/scheduler.py` | 零引用遗留 | 删除 |
| `frontend/src/lib/api.ts` | settings 类型 | 加 `data_provider` / `tickflow` 嵌套类型 |
| `frontend/src/pages/settings/Keys.tsx` | key/档位设置页 | TickFlow 块条件渲染 |
| `frontend/src/pages/Onboarding.tsx` | 首次引导 | 非 tickflow 跳过 key 步骤 |
| `backend/tests/services/test_data_mode.py` | mode 单测 | 创建 |
| `backend/tests/api/test_settings_provider_block.py` | settings 响应单测 | 创建 |

---

### 任务 1：`current_data_mode()` + /health

**文件：**
- 修改：`backend/app/services/data_mode.py`
- 修改：`backend/app/api/routes.py:13-20`
- 测试：`backend/tests/services/test_data_mode.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_data_mode.py
from app.services import data_mode


def test_mode_is_provider_name_when_not_tickflow(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name", lambda cap=None: "fquant_local"
    )
    assert data_mode.current_data_mode() == "fquant_local"


def test_mode_delegates_to_tf_client_when_tickflow(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name", lambda cap=None: "tickflow"
    )
    monkeypatch.setattr("app.tickflow.client.current_mode", lambda: "api_key")
    assert data_mode.current_data_mode() == "api_key"
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/test_data_mode.py -v`
预期：FAIL，`AttributeError: ... has no attribute 'current_data_mode'`

- [ ] **步骤 3：实现 `current_data_mode()`**

在 `backend/app/services/data_mode.py` 追加：

```python
def current_data_mode() -> str:
    """健康检查/设置页展示用的运行模式。

    非 tickflow provider: 直接返回 provider 名（fquant / fquant_local）。
    tickflow: 保留原三态 none/free/api_key（由 key 档位决定）。
    """
    from app.data_providers.registry import get_active_provider_name

    try:
        name = get_active_provider_name()
    except Exception:  # noqa: BLE001
        name = "tickflow"
    if name != "tickflow":
        return name
    from app.tickflow import client as tf_client
    return tf_client.current_mode()
```

- [ ] **步骤 4：/health 与 /api/capabilities 换用**

`backend/app/api/routes.py`：删除 `from app.tickflow import client as tf_client`（第 7 行），`health()` 中 `"mode": tf_client.current_mode()` 改为：

```python
from app.services.data_mode import current_data_mode
# ...
        "mode": current_data_mode(),
```

（`/api/capabilities` 的 `label` 不动——本地模式下 `_persist` 已写入 provider 名作 label。）

- [ ] **步骤 5：运行测试 + 冒烟**

```bash
cd backend && uv run --extra dev pytest tests/services/test_data_mode.py -v && uv run python -c "from app.main import app; print('ok')"
```

- [ ] **步骤 6：Commit**

```bash
git add app/services/data_mode.py app/api/routes.py tests/services/test_data_mode.py
git commit -m "feat(settings): provider-aware /health mode via current_data_mode()"
```

---

### 任务 2：settings GET 增加 `data_provider` + 嵌套 `tickflow` 块

**文件：**
- 修改：`backend/app/api/settings.py:52-81`（`get_settings`）
- 测试：`backend/tests/api/test_settings_provider_block.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/api/test_settings_provider_block.py
"""GET /api/settings 的 TickFlow 字段应嵌套进 tickflow 块（顶层字段过渡期保留）。"""
from app.api.settings import get_settings


def test_settings_has_provider_and_tickflow_block(monkeypatch):
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name", lambda cap=None: "fquant_local"
    )
    out = get_settings()
    assert out["data_provider"] == "fquant_local"
    tf = out["tickflow"]
    assert set(tf) == {
        "api_key_masked", "has_key", "tier_label", "current_endpoint",
        "probe_log", "missing_caps", "extras_caps",
    }
    # 过渡期兼容：顶层旧字段仍在且与嵌套块一致
    assert out["tier_label"] == tf["tier_label"]
    assert out["tickflow_api_key_masked"] == tf["api_key_masked"]
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_settings_provider_block.py -v`
预期：FAIL，`KeyError: 'data_provider'`

- [ ] **步骤 3：实现响应重组**

`backend/app/api/settings.py` `get_settings()`：在 `key = secrets_store.get_tickflow_key()` 之后、return 之前构造：

```python
    from app.data_providers.registry import get_active_provider_name

    tickflow_block = {
        "api_key_masked": secrets_store.mask(key),
        "has_key": bool(key),
        "tier_label": tier_label(),
        "current_endpoint": tf_client.current_endpoint(),
        "probe_log": probe_log(),
        "missing_caps": missing_caps(),
        "extras_caps": extras_caps(),
    }
```

return dict 增加两个键（放在 `"mode"` 之后）：

```python
        "data_provider": get_active_provider_name(),
        "tickflow": tickflow_block,
```

同时把 `"mode": tf_client.current_mode()` 改为 `"mode": current_data_mode()`（顶部补 `from app.services.data_mode import current_data_mode`）。顶层旧字段（tickflow_api_key_masked/has_tickflow_key/tier_label/current_endpoint/probe_log/missing_caps/extras_caps）**原样保留**，删除动作属 A6。

- [ ] **步骤 4：运行测试验证通过 + 全量回归**

```bash
cd backend && uv run --extra dev pytest tests/api/test_settings_provider_block.py -v && uv run --extra dev pytest -q
```

- [ ] **步骤 5：Commit**

```bash
git add app/api/settings.py tests/api/test_settings_provider_block.py
git commit -m "feat(settings): expose data_provider + nested tickflow block in GET /api/settings"
```

---

### 任务 3：前端条件渲染（TickFlow UI 仅 tickflow provider 显示）

**文件：**
- 修改：`frontend/src/lib/api.ts:630-670`（SettingsResponse 类型）
- 修改：`frontend/src/pages/settings/Keys.tsx`（TickFlow key 卡片 + 档位梯 353-385 行区域）
- 修改：`frontend/src/pages/Onboarding.tsx:305-330,455-465`

- [ ] **步骤 1：api.ts 类型补充**

在 `SettingsResponse`（`api.ts` 约 630 行的接口）追加：

```ts
  data_provider: string
  tickflow: {
    api_key_masked: string
    has_key: boolean
    tier_label: string
    current_endpoint: string
    probe_log: string[]
    missing_caps: string[]
    extras_caps: string[]
  }
```

- [ ] **步骤 2：Keys.tsx 条件渲染**

在组件顶部（`const masked = settings.data?.tickflow_api_key_masked` 附近，68 行）加：

```ts
  const isTickflow = (settings.data?.data_provider ?? 'tickflow') === 'tickflow'
```

把「TickFlow API Key 卡片」（含 tickflow.org 注册链接，~82 行起）与「档位梯 ALL_TIERS 展示」（~353-385 行）整块用 `{isTickflow && ( ... )}` 包裹。非 tickflow 时渲染替代卡片：

```tsx
  {!isTickflow && (
    <div className="rounded-lg border p-4 text-sm text-muted-foreground">
      当前数据源：<span className="font-mono">{settings.data?.data_provider}</span>
      （本地/自建数据源，无需 TickFlow Key。能力见「数据」页。）
    </div>
  )}
```

- [ ] **步骤 3：Onboarding.tsx 跳过 key 步骤**

同样引入 `isTickflow`；311 行「已检测到配置好的 Key…」与 323 行注册链接块加 `isTickflow &&` 条件；460 行档位显示 `caps.data?.label` 保留（本地模式 label 即 provider 名，无害）。

- [ ] **步骤 4：前端"套餐/升级/加购"文案清理（非 tickflow 语境）**

```bash
grep -rn "套餐\|升级\|加购" frontend/src --include="*.tsx" --include="*.ts"
```
对命中的每处：若文案在 `isTickflow` 条件块内保留；若是无条件展示（如 capability 缺失提示），改为中性文案「当前数据源不支持此能力」。

- [ ] **步骤 5：构建验证**

运行：`cd frontend && npm run build`
预期：类型检查 + 构建通过

- [ ] **步骤 6：手动验证两种模式**

- `DATA_PROVIDER=fquant_local` 启动：设置页无 TickFlow 卡片、显示本地数据源卡；引导页无注册链接。
- `DATA_PROVIDER=tickflow` 启动：与改动前展示一致（截图对比 Keys 页）。

- [ ] **步骤 7：Commit**

```bash
git add frontend/src
git commit -m "feat(ui): render TickFlow key/tier blocks only when data_provider=tickflow"
```

---

### 任务 4（A5）：删除零引用的 `tickflow/scheduler.py`

- [ ] **步骤 1：删除前最终 grep 确认（必须为 0 输出）**

```bash
cd backend
grep -rn "tickflow.scheduler\|tickflow import scheduler" app tests scripts
```

- [ ] **步骤 2：删除**

```bash
git rm app/tickflow/scheduler.py
```

- [ ] **步骤 3：全量测试 + 冒烟**

```bash
uv run --extra dev pytest -q && uv run python -c "from app.main import app; print('ok')"
```

- [ ] **步骤 4：Commit**

```bash
git commit -m "chore: remove unreferenced app/tickflow/scheduler.py (A5)"
```

> **提醒：** `pools.py` 与根目录 `tiers.yaml` 本计划**不删**（tickflow 退路仍依赖），随 A6 处理。
