# A1：分钟K month 扩展去 TickFlow tier 门控 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把分钟K"按月扩展"从 `tier_label()==expert` 的 TickFlow VIP 门控，改为 provider capability 门控，解除 fquant_local 本地模式下的真实 403 阻断。

**架构：** 新增一个 capability `Cap.KLINE_MINUTE_MONTH` 与 provider 侧声明位 `ProviderCapabilities.minute_month_extension`；本地 provider（FQuantProvider）声明支持，`_provider_capset()` 把它翻译进 CapabilitySet；kline.py 端点把 tier 判断换成 `capset.has(Cap.KLINE_MINUTE_MONTH)`。**关键约束（来自 Grilling 裁决）**：不是裸删 expert 判断——裸删会让 month 扩展对所有 provider 无条件放行（原"month 成本较高"是成本考量，本地抽多月分钟K仍非零成本）；替换后由 provider 显式声明"是否负担得起 month 扩展"，未声明的 provider 仍返回 403。

**技术栈：** Python 3.12 / FastAPI / dataclass / StrEnum。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- 本地模式下 `label=Fquant_local` 不是 `expert`，分钟K month 扩展会被旧 TickFlow tier 语义拦成 403，即使本地分钟数据存在。
- `registry.get_active_provider_name(capability)` 已支持按能力选择 daily/minute/realtime/adj_factor；A1 应接入这套 provider capability 语义，而不是继续读 TickFlow tier。
- `FQuantProvider` 已具备分钟数据来源（磁盘/engine/fstore fallback），但缺少“可负担按月扩展”的显式能力位。
- 该修复必须保留成本门控：裸删 expert 判断会让所有 provider 无条件放行 month 扩展，等价于引入另一种 bug。

**TickFlow 行为说明（有意的、已知的）：** TickFlow provider 走的是探测式 capset（`detect_capabilities` 的 probe 分支，**不经** `_provider_capset`），不会拿到新的 `Cap.KLINE_MINUTE_MONTH`，因此 TickFlow 下 month 扩展将被关闭。这是**有意为之且可接受**——TickFlow 正在按路线图 A6 移除，month 扩展是 expert 档的边缘功能。若将来仍需为 TickFlow expert 保留，另在 `tiers.yaml` 的 expert 档与 tier→cap 构建处补 `Cap.KLINE_MINUTE_MONTH`，不属本计划范围。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/capabilities.py` | Cap 枚举 | 新增 `KLINE_MINUTE_MONTH` 一行 |
| `backend/app/data_providers/base.py` | ProviderCapabilities 契约 | 新增 `minute_month_extension: bool = False` |
| `backend/app/data_providers/fquant_provider.py` | FQuant provider 能力声明 | capabilities 加 `minute_month_extension=True` |
| `backend/app/data_providers/capability_gate.py` | provider→Cap 映射 | `_provider_capset()` 增加 month 映射 |
| `backend/app/api/kline.py` | 分钟K扩展端点 | 抽出 `_ensure_minute_capable()` helper 并替换 tier 判断 |
| `backend/tests/data_providers/test_minute_month_capability.py` | 映射单测 | 新建 |
| `backend/tests/api/test_minute_month_gate.py` | 门控 helper 单测 | 新建 |

---

### 任务 1：capability 管道（Cap + provider 声明位 + 映射）

把"month 扩展"变成一个可声明、可翻译进 CapabilitySet 的能力。用 `_provider_capset()` 单测锁定行为。

**文件：**
- 修改：`backend/app/tickflow/capabilities.py:27`（Cap 枚举末尾）
- 修改：`backend/app/data_providers/base.py:28`（ProviderCapabilities 末尾字段）
- 修改：`backend/app/data_providers/fquant_provider.py`（FQuantProvider.capabilities，约第 163-172 行）
- 修改：`backend/app/tickflow/policy.py:278-280`（`_provider_capset` 的 minute 分支）
- 创建：`backend/tests/tickflow/__init__.py`（空文件；`tests/tickflow/` 目录尚不存在，项目约定测试包带 `__init__.py`）
- 测试：`backend/tests/tickflow/test_provider_capset_minute_month.py`

- [x] **步骤 0：建测试包目录**

运行：`cd backend && mkdir -p tests/tickflow && touch tests/tickflow/__init__.py`

- [x] **步骤 1：编写失败的测试**

```python
# backend/tests/tickflow/test_provider_capset_minute_month.py
"""_provider_capset() 是否按 provider 的 minute_month_extension 标志翻译出 KLINE_MINUTE_MONTH。"""
from app.data_providers.base import ProviderCapabilities
from app.tickflow import policy
from app.tickflow.capabilities import Cap


class _StubProvider:
    def __init__(self, caps: ProviderCapabilities) -> None:
        self.capabilities = caps


def _run_with(monkeypatch, caps: ProviderCapabilities):
    monkeypatch.setattr(policy, "_active_provider_name", lambda: "fquant_local")
    monkeypatch.setattr("app.data_providers.get_provider", lambda name: _StubProvider(caps))
    return policy._provider_capset()


def test_month_cap_granted_when_flag_true(monkeypatch):
    cs = _run_with(monkeypatch, ProviderCapabilities(minute=True, minute_month_extension=True))
    assert cs is not None
    assert cs.has(Cap.KLINE_MINUTE_BATCH)
    assert cs.has(Cap.KLINE_MINUTE_MONTH)


def test_month_cap_absent_when_flag_false(monkeypatch):
    cs = _run_with(monkeypatch, ProviderCapabilities(minute=True, minute_month_extension=False))
    assert cs.has(Cap.KLINE_MINUTE_BATCH)      # 分钟批量仍在
    assert not cs.has(Cap.KLINE_MINUTE_MONTH)  # 但不给 month


def test_month_cap_absent_when_no_minute(monkeypatch):
    cs = _run_with(monkeypatch, ProviderCapabilities(minute=False, minute_month_extension=True))
    assert not cs.has(Cap.KLINE_MINUTE_MONTH)  # 无分钟能力就不该有 month
```

- [x] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/tickflow/test_provider_capset_minute_month.py -v`
预期：FAIL——`AttributeError: ... 'minute_month_extension'` 或 `Cap` 无 `KLINE_MINUTE_MONTH`。

- [x] **步骤 3：编写最少实现代码**

`backend/app/tickflow/capabilities.py` —— 在 `Cap` 枚举 `ADJ_FACTOR` 行后加：

```python
    ADJ_FACTOR             = "adj_factor"
    KLINE_MINUTE_MONTH     = "kline.minute.month"
```

`backend/app/data_providers/base.py` —— 在 `ProviderCapabilities` 的 `universes` 字段后加：

```python
    universes: bool = False
    # minute_month_extension: provider 能否负担"按月扩展分钟K历史"(多月分钟数据成本较高)
    minute_month_extension: bool = False
```

`backend/app/data_providers/fquant_provider.py` —— FQuantProvider.capabilities 里加一行（本地磁盘/引擎读取，可负担）：

```python
        depth=True,
        universes=True,   # 阶段 3 #3.2：fstore chengfen_gu 提供指数/板块/行业
        minute_month_extension=True,  # A1: 本地分钟K可负担按月扩展
    )
```

`backend/app/tickflow/policy.py` —— `_provider_capset()` 的 `if caps.minute:` 块内追加 month 映射：

```python
    if caps.minute:
        out[Cap.KLINE_MINUTE_BY_SYMBOL] = CapabilityLimits(batch=1)
        out[Cap.KLINE_MINUTE_BATCH] = CapabilityLimits(batch=200)
        if caps.minute_month_extension:
            out[Cap.KLINE_MINUTE_MONTH] = CapabilityLimits(batch=200)
```

- [x] **步骤 4：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/tickflow/test_provider_capset_minute_month.py -v`
预期：3 passed。

- [x] **步骤 5：Commit**

```bash
git add backend/app/tickflow/capabilities.py backend/app/data_providers/base.py backend/app/data_providers/fquant_provider.py backend/app/tickflow/policy.py backend/tests/tickflow/test_provider_capset_minute_month.py
git commit -m "feat(cap): 新增 KLINE_MINUTE_MONTH capability + provider 声明位 (A1)"
```

---

### 任务 2：端点门控 helper 替换 tier 判断

把 kline.py 端点里的"分钟能力检查 + month 的 tier 判断"抽成纯 helper，用 CapabilitySet 判定，删掉 `tier_label()` 依赖。抽 helper 是为了脱离 repo/数据依赖做单测。

**文件：**
- 修改：`backend/app/api/kline.py:716-730`（端点内的能力检查段）
- 测试：`backend/tests/api/test_minute_month_gate.py`

**当前端点代码（将被替换，见 `backend/app/api/kline.py:716-730`）：**
```python
        capset = request.app.state.capabilities

        from app.tickflow.capabilities import Cap
        if not capset.has(Cap.KLINE_MINUTE_BATCH):
            raise HTTPException(status_code=403, detail="当前数据源不支持批量分钟K")

        # month 单位的分钟K扩展成本较高，仅保留给最宽能力档
        if unit == "month":
            from app.tickflow.policy import tier_label
            base_tier = tier_label().split()[0].split("+")[0].strip().lower()
            if base_tier != "expert":
                raise HTTPException(
                    status_code=403,
                    detail="当前能力档不支持按月扩展分钟K历史",
                )
```

- [x] **步骤 1：编写失败的测试**

```python
# backend/tests/api/test_minute_month_gate.py
"""_ensure_minute_capable() 门控: 分钟批量 + month 各按 capability 判定, 不再看 TickFlow tier。"""
import pytest
from fastapi import HTTPException

from app.api.kline import _ensure_minute_capable
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def test_day_needs_only_minute_batch():
    cs = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200)})
    _ensure_minute_capable(cs, "day")  # 不抛


def test_month_blocked_without_month_cap():
    cs = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200)})
    with pytest.raises(HTTPException) as exc:
        _ensure_minute_capable(cs, "month")
    assert exc.value.status_code == 403
    assert "按月" in exc.value.detail


def test_month_allowed_with_month_cap():
    cs = CapabilitySet({
        Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200),
        Cap.KLINE_MINUTE_MONTH: CapabilityLimits(batch=200),
    })
    _ensure_minute_capable(cs, "month")  # 不抛


def test_no_minute_batch_blocks_all():
    cs = CapabilitySet({})
    with pytest.raises(HTTPException) as exc:
        _ensure_minute_capable(cs, "day")
    assert exc.value.status_code == 403
    assert "批量分钟K" in exc.value.detail
```

- [x] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_minute_month_gate.py -v`
预期：FAIL——`ImportError: cannot import name '_ensure_minute_capable'`。

- [x] **步骤 3：编写最少实现代码**

`backend/app/api/kline.py` —— 在模块内（端点函数外，靠近文件顶部的 helper 区）新增 helper：

```python
def _ensure_minute_capable(capset, unit: str) -> None:
    """分钟K能力门控。month 扩展由 provider capability 决定, 不再看 TickFlow tier (A1)。"""
    from app.tickflow.capabilities import Cap
    if not capset.has(Cap.KLINE_MINUTE_BATCH):
        raise HTTPException(status_code=403, detail="当前数据源不支持批量分钟K")
    if unit == "month" and not capset.has(Cap.KLINE_MINUTE_MONTH):
        raise HTTPException(status_code=403, detail="当前数据源不支持按月扩展分钟K历史")
```

然后把端点内 `backend/app/api/kline.py:716-730` 的整段（上面"当前端点代码"块）替换为：

```python
        capset = request.app.state.capabilities
        _ensure_minute_capable(capset, unit)
```

（即：删掉端点内对 `Cap` 的局部 import、`tier_label` 的局部 import 和两处 raise，全部收敛进 helper。）

- [x] **步骤 4：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/api/test_minute_month_gate.py -v`
预期：4 passed。

- [x] **步骤 5：Commit**

```bash
git add backend/app/api/kline.py backend/tests/api/test_minute_month_gate.py
git commit -m "feat(kline): 分钟K month 门控改 capability 判定, 去 tier_label (A1)"
```

---

### 任务 3：回归验证 + 本地模式端到端确认

**文件：** 无（仅验证）

- [x] **步骤 1：全量后端测试**

运行：`cd backend && uv run --extra dev pytest -q`
预期：全部 passed（原 134 + 新增 7）。

- [x] **步骤 2：确认 tier_label 依赖已从该端点摘除**

运行：`cd backend && grep -n "tier_label" app/api/kline.py`
预期：无输出（该文件不再引用 tier_label）。

- [x] **步骤 3：本地模式实测 month 扩展不再被门控挡**

运行（在 fquant_local 已生效的环境）：
```bash
cd backend && uv run python3 -c "
from app.tickflow import policy
cs = policy.detect_capabilities(force=True)
from app.tickflow.capabilities import Cap
print('KLINE_MINUTE_BATCH:', cs.has(Cap.KLINE_MINUTE_BATCH))
print('KLINE_MINUTE_MONTH:', cs.has(Cap.KLINE_MINUTE_MONTH))
"
```
预期：两者均为 `True`——证明 fquant_local 的 capset 现在带 month 能力，端点不会再对 month 请求返回 403。

- [x] **步骤 4：Commit（若步骤 1-3 促成任何微调）**

```bash
git add -A
git commit -m "test(kline): A1 分钟K month 门控回归验证"
```
（若无微调则跳过。）

---

## 显式不做（YAGNI）

- **不为 TickFlow 保留 expert-month**：TickFlow 走 probe capset，不经 `_provider_capset`，month 将关闭；这是有意的（TickFlow 按 A6 移除）。要保留需改 `tiers.yaml` + tier→cap 构建，不属 A1。
- **不加本地配置开关**：`minute_month_extension` 由 provider 静态声明即可表达"能否负担"；用户级 opt-in 开关是 YAGNI，除非将来出现"本地 provider 但要限制 month"的真实需求。
- **不改 day 单位路径**：day 扩展本就只需 `KLINE_MINUTE_BATCH`，不动。

## 自检记录

- **规格覆盖**：裁决约束"不裸删、provider-capability 门控替代"→ 任务1（声明位+映射）+ 任务2（helper 用 capability 判定）覆盖；TickFlow 行为变化已显式说明并被任务1的 `test_month_cap_absent_when_no_minute` 与文档note 覆盖。
- **占位符扫描**：无 TODO/待定；每个代码步骤均给出完整代码。
- **类型一致性**：`Cap.KLINE_MINUTE_MONTH`、`ProviderCapabilities.minute_month_extension`、`_ensure_minute_capable(capset, unit)` 三个新符号在任务1定义、任务2消费，命名一致；`_provider_capset` 返回 `CapabilitySet | None`，测试用 `_run_with` 已断言非 None。
