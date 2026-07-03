# A2：capability 语义中性化 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 `Cap/CapabilityLimits/CapabilitySet/CapabilityDenied` 从 `app/tickflow/capabilities.py` 迁到中性顶层模块 `app/capabilities.py`，异常文案从"加购能力"（TickFlow 商业语境）改为"当前数据源不支持"（数据源能力语境），旧路径保留一版兼容 shim。

**架构：** 与 A3 同一套路：`git mv` + shim + grep 驱动的导入方全量切换。capability 这套抽象本身与 TickFlow 无关（业务代码只认 `CapabilitySet`），TickFlow 特有的只有 policy.py 的探测/判档逻辑——那部分**不动**（A4/A6 处理）。本计划不改任何门控行为，只改模块位置与文案。

**技术栈：** Python 3.12。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- capability 抽象本体现在位于 `backend/app/tickflow/capabilities.py`，但 `Cap/CapabilitySet/CapabilityDenied` 已被 API、service、job、tests 多处业务代码引用，语义不再是 TickFlow SDK 专属。
- `backend/app/tickflow/policy.py` 仍通过相对导入读取 capability；A2 迁出后必须改成 `app.capabilities`，否则 A6 删除 shim 时会断。
- 当前 `CapabilityDenied` 默认 suggestion 带“加购”商业语义；本地数据源模式下这会把“provider 不支持”误导成“TickFlow 付费升级”。

**执行顺序注意：** 若 A1（分钟K month 门控）已先执行，代码里会多出 `Cap.KLINE_MINUTE_MONTH` 与 `tests/tickflow/` 下的新测试文件——本计划的替换步骤全部用 grep 驱动而非固定清单，自动覆盖它们；已知清单仅作核对底账。

**范围外（勿做）：** `depth_service.py`/`preferences.py` 里"套餐 clamp 区间"的**逻辑**（按档位定轮询区间）是 TickFlow provider 的运行时行为，归 A4/A6；`settings.py`/`stock_analyzer.py` 等处面向用户的"套餐/升级"文案归 A4（与前端联动改）。本计划只改 `CapabilityDenied` 的 suggestion 默认文案这一处语义中性化。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/capabilities.py` | Cap/CapabilityLimits/CapabilitySet/CapabilityDenied 本体 | `git mv` 自 `app/tickflow/capabilities.py` + 文案改 |
| `backend/app/tickflow/capabilities.py` | 兼容 shim | 重建为 re-export |
| `backend/app/tickflow/policy.py:24` | 相对导入改绝对 | `from .capabilities import` → `from app.capabilities import` |
| 导入方（grep 驱动） | 见任务 2 底账 | 改 import 行 |
| `backend/tests/test_capabilities_neutral.py` | 新路径 + 文案锁定测试 | 创建 |

**已知导入方底账（执行时以 grep 为准）：**
app 内：`api/financials.py:14`、`api/indices.py:13`、`api/kline.py`（内联 import ×6：159/476/567/718/782/866）、`api/settings.py`（内联 ×3：1053/1070/1086）、`jobs/daily_pipeline.py:25`、`main.py:279`、`services/depth_service.py:581`、`services/extend_history.py:29`、`services/financial_sync.py:22`、`services/index_sync.py:22`、`services/kline_sync.py:21`、`services/watchlist.py:14`、`tickflow/policy.py:24`（相对导入）。
tests：`tests/jobs/test_daily_pipeline_local.py:7`、`tests/api/test_indices_asset_type.py:7`、`tests/services/test_index_sync_asset_type.py:6`（+ A1 若已执行新增的 `tests/tickflow/` 文件）。

---

### 任务 1：迁移模块 + 中性文案 + shim

**文件：**
- 移动：`backend/app/tickflow/capabilities.py` → `backend/app/capabilities.py`
- 重建：`backend/app/tickflow/capabilities.py`（shim）
- 修改：`backend/app/tickflow/policy.py:24`
- 测试：`backend/tests/test_capabilities_neutral.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/test_capabilities_neutral.py
"""capability 抽象迁到中性模块 app.capabilities，文案不再含商业化措辞。"""


def test_import_from_neutral_module():
    from app.capabilities import Cap, CapabilityLimits, CapabilitySet, CapabilityDenied  # noqa: F401


def test_compat_shim_still_works():
    from app.tickflow.capabilities import Cap as OldCap
    from app.capabilities import Cap as NewCap
    assert OldCap is NewCap  # shim 必须 re-export 同一对象，不是复制


def test_denied_suggestion_is_provider_wording():
    from app.capabilities import Cap, CapabilityDenied
    exc = CapabilityDenied(Cap.FINANCIAL)
    assert "加购" not in exc.suggestion
    assert "数据源" in exc.suggestion
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/test_capabilities_neutral.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'app.capabilities'`（注意：若报的是别的错先停下排查）

- [ ] **步骤 3：git mv + 重建 shim + 改文案**

```bash
cd backend
git mv app/tickflow/capabilities.py app/capabilities.py
```

```python
# backend/app/tickflow/capabilities.py（重建）
"""兼容导入（A2 保留一版）：请改用 app.capabilities。A6 移除本文件。"""
from app.capabilities import (  # noqa: F401
    Cap,
    CapabilityDenied,
    CapabilityLimits,
    CapabilitySet,
)
```

在 `backend/app/capabilities.py` 中修改两处：

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

- [ ] **步骤 4：改 policy.py 的相对导入**

`backend/app/tickflow/policy.py:24`：

```python
from app.capabilities import Cap, CapabilityLimits, CapabilitySet
```

（原 `from .capabilities import Cap, CapabilityLimits, CapabilitySet`。必须改：否则 policy 经 shim 绕一圈，A6 删 shim 时会断。）

- [ ] **步骤 5：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/test_capabilities_neutral.py -v`
预期：3 项全 PASS

- [ ] **步骤 6：Commit**

```bash
git add app/capabilities.py app/tickflow/capabilities.py app/tickflow/policy.py tests/test_capabilities_neutral.py
git commit -m "refactor(capabilities): move Cap abstractions to neutral app.capabilities, neutral denied wording"
```

---

### 任务 2：grep 驱动切换全部导入方

- [ ] **步骤 1：批量替换（含内联 import）**

```bash
cd backend
grep -rl "from app.tickflow.capabilities import" app tests scripts 2>/dev/null | \
  grep -v "app/tickflow/capabilities.py" | \
  xargs sed -i '' 's/from app.tickflow.capabilities import/from app.capabilities import/'
```

- [ ] **步骤 2：与底账核对 + 残留校验**

```bash
git diff --stat        # 对照计划头部"已知导入方底账"，少了要查原因（可能 A1 未执行），多了确认是 A1 新增文件
grep -rn "tickflow.capabilities" app tests scripts | grep -v "app/tickflow/capabilities.py"
```
预期：第二条命令无输出

- [ ] **步骤 3：全量测试 + 启动冒烟**

```bash
cd backend && uv run --extra dev pytest -q && uv run python -c "from app.main import app; print('ok')"
```
预期：与基线相同 + `ok`

- [ ] **步骤 4：手动验证 403 文案（本地模式下真实请求）**

启动 dev server 后请求一个无能力端点（fquant_local 无 depth 能力）：

```bash
curl -s -X POST localhost:8000/api/settings/preferences/limit-ladder-monitor/run | head -c 300
```
预期：403，`suggestion` 含"当前数据源不支持"，不含"加购"

- [ ] **步骤 5：Commit**

```bash
git add -A
git commit -m "refactor(capabilities): switch all importers to app.capabilities"
```
