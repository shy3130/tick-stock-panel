# A2：capability 语义中性化 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 `Cap/CapabilityLimits/CapabilitySet/CapabilityDenied` 从 TickFlow 语境迁到中性顶层模块 `app/capabilities.py`，异常文案从"加购能力"（TickFlow 商业语境）改为"当前数据源不支持"（数据源能力语境）。当前代码已推进到 A6 方向，旧 `app.tickflow.capabilities` shim 不再保留。

**架构：** 与 A3 同一套路：`git mv` + shim + grep 驱动的导入方全量切换。capability 这套抽象本身与 TickFlow 无关（业务代码只认 `CapabilitySet`），TickFlow 特有的只有 policy.py 的探测/判档逻辑——那部分**不动**（A4/A6 处理）。本计划不改任何门控行为，只改模块位置与文案。

**技术栈：** Python 3.12。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- capability 抽象本体已位于 `backend/app/capabilities.py`，`Cap/CapabilitySet/CapabilityDenied` 被 API、service、job、tests 多处业务代码引用，语义不再是 TickFlow SDK 专属。
- provider 能力门控已位于 `backend/app/data_providers/capability_gate.py`，不再经 `app.tickflow.policy`。
- `CapabilityDenied` 默认 suggestion 已是“当前数据源不支持...能力”，不再含“加购”商业语义。

**执行顺序注意：** A1（分钟K month 门控）已先执行，`Cap.KLINE_MINUTE_MONTH` 与 `tests/data_providers/test_minute_month_capability.py` 已在中性 capability 体系下。

**范围外（勿做）：** `depth_service.py`/`preferences.py` 里"套餐 clamp 区间"的**逻辑**（按档位定轮询区间）是 TickFlow provider 的运行时行为，归 A4/A6；`settings.py`/`stock_analyzer.py` 等处面向用户的"套餐/升级"文案归 A4（与前端联动改）。本计划只改 `CapabilityDenied` 的 suggestion 默认文案这一处语义中性化。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/capabilities.py` | Cap/CapabilityLimits/CapabilitySet/CapabilityDenied 本体 | 中性能力定义 + 文案改 |
| `backend/app/data_providers/capability_gate.py` | provider capability → CapabilitySet | 取代旧 TickFlow policy 能力门控 |
| 旧 `backend/app/tickflow/capabilities.py` | 兼容 shim | 已不保留；后续 A6 删除 TickFlow 包时无需处理该文件 |
| 导入方（grep 驱动） | 见任务 2 底账 | 改 import 行 |
| `backend/tests/test_capabilities_neutral.py` | 新路径 + 文案锁定测试 | 创建 |

**导入方底账（当前已切换）：** `rg "tickflow.capabilities|from \\.capabilities" backend/app backend/tests` 无业务残留；导入方均使用 `app.capabilities`。

---

### 任务 1：迁移模块 + 中性文案

**文件：**
- 创建/保留：`backend/app/capabilities.py`
- 创建/保留：`backend/app/data_providers/capability_gate.py`
- 测试：`backend/tests/test_capabilities_neutral.py`

- [x] **步骤 1：编写失败的测试**

```python
# backend/tests/test_capabilities_neutral.py
"""capability 抽象迁到中性模块 app.capabilities，文案不再含商业化措辞。"""


def test_import_from_neutral_module():
    from app.capabilities import Cap, CapabilityLimits, CapabilitySet, CapabilityDenied  # noqa: F401


def test_denied_suggestion_is_provider_wording():
    from app.capabilities import Cap, CapabilityDenied
    exc = CapabilityDenied(Cap.FINANCIAL)
    assert "加购" not in exc.suggestion
    assert "数据源" in exc.suggestion
```

- [x] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/test_capabilities_neutral.py -v`
预期：迁移前 FAIL；当前实现为 PASS。

- [x] **步骤 3：迁移到中性模块 + 改文案**

在 `backend/app/capabilities.py` 中确认两处：

① 模块 docstring 首行改为：

```python
"""Capability 定义（数据源能力开关）。

业务代码只依赖 CapabilitySet,不感知具体数据源与其商业档位。
"""
```

② `CapabilityDenied.__init__` 的默认 suggestion（原 `f"加购『{cap}』能力可解锁"`）改为：

```python
        self.suggestion = suggestion or f"当前数据源不支持『{cap}』能力"
```

- [x] **步骤 4：改能力门控导入**

`backend/app/data_providers/capability_gate.py`：

```python
from app.capabilities import Cap, CapabilityLimits, CapabilitySet
```

- [x] **步骤 5：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/test_capabilities_neutral.py -v`
预期：3 项全 PASS

- [x] **步骤 6：Commit**

```bash
git add app/capabilities.py app/data_providers/capability_gate.py tests/test_capabilities_neutral.py
git commit -m "refactor(capabilities): move Cap abstractions to neutral app.capabilities, neutral denied wording"
```

---

### 任务 2：grep 驱动切换全部导入方

- [x] **步骤 1：批量替换（含内联 import）**

```bash
cd backend
rg "tickflow.capabilities|from \\.capabilities" app tests scripts
```

- [x] **步骤 2：与底账核对 + 残留校验**

```bash
git diff --stat        # 对照计划头部"已知导入方底账"，少了要查原因（可能 A1 未执行），多了确认是 A1 新增文件
rg "tickflow.capabilities|from \\.capabilities" app tests scripts
```
预期：无输出

- [x] **步骤 3：全量测试 + 启动冒烟**

```bash
cd backend && uv run --extra dev pytest -q && uv run python -c "from app.main import app; print('ok')"
```
预期：与基线相同 + `ok`

- [x] **步骤 4：验证 403 文案（本地模式）**

启动 dev server 后请求一个无能力端点（fquant_local 无 depth 能力）：

```bash
curl -s -X POST localhost:8000/api/settings/preferences/limit-ladder-monitor/run | head -c 300
```
预期：403，`suggestion` 含"当前数据源不支持"，不含"加购"

- [x] **步骤 5：Commit**

```bash
git add -A
git commit -m "refactor(capabilities): switch all importers to app.capabilities"
```
