# A6：彻底移除 TickFlow provider 与全部遗留 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 删除 `TickFlowProvider`、TickFlow SDK 依赖、探测/判档逻辑、pools/tiers.yaml 以及 A2/A3 遗留的兼容 shim，最终 `app/tickflow/` 目录整体消失，`detect_capabilities` 只剩 provider capability 一条路径。

**架构：** 探测/判档（`_probe_real`/`_classify_tier`/tiers.yaml 全家）随 TickFlow 一起死；`detect_capabilities` 收缩为"读 provider capabilities → 翻译成 CapabilitySet → 持久化 label"，迁至新家 `app/data_providers/capability_gate.py`。业务侧所有 `provider_name == "tickflow"` 分支删除（它们全是 fallback 分支，删掉后走已有的 provider 主路径）。

**技术栈：** Python 3.12 / FastAPI / React+TS。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- `registry.py` 仍注册 `TickFlowProvider`，默认 provider/异常回退仍可落到 `tickflow`，因此删除必须同时改默认值和偏好清洗。
- `settings.py`、前端设置页和 onboarding 仍暴露 TickFlow key、endpoint、tier/升级语义；只删 provider 文件会留下坏入口和误导 UI。
- `kline.py`、`daily_pipeline.py`、`extend_history.py`、`depth_service.py`、`quote_service.py` 里仍存在 tickflow/pools/tier fallback 分支；A6 必须先删业务分支，再物理删包。
- `tiers.yaml`、`tickflow[all]` 依赖、`app/tickflow/policy.py` 探测/判档逻辑只服务 TickFlow，A1/A2/A4 完成后应由 provider capabilities 替代。

---

## ⛔ 前置门（不满足则不执行）

1. **产品决策已落档**：不再保留 `DATA_PROVIDER=tickflow` 退路。决策记入 `docs/development-roadmap.md` A6 条目或独立 ADR，注明日期与理由。
2. **A1-A5 全部完成且已合并**（A2 shim、A3 shim、A4 嵌套块过渡期均已跑过至少一个使用周期）。
3. fquant_local 在日常使用中稳定（无需要临时切回 tickflow 排障的场景）。

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `backend/app/data_providers/capability_gate.py` | 创建：detect_capabilities/_provider_capset/label 的新家 |
| `backend/app/tickflow/`（整个目录） | 最终删除（policy/client/pools + A2/A3 shim） |
| `backend/app/data_providers/tickflow_provider.py` | 删除 |
| `backend/app/data_providers/registry.py` | 移除 tickflow 注册，默认 provider 改 `fquant_local` |
| `backend/app/api/settings.py` | 删 switch_endpoint / tickflow-key 端点 + 顶层旧字段 + `tickflow` 嵌套块 |
| `backend/app/api/kline.py:500-513,870-890`、`app/jobs/daily_pipeline.py:55-72`、`app/services/extend_history.py:55-70` | 删 pools fallback 分支 |
| `backend/app/services/depth_service.py:569-587`、`app/services/quote_service.py:251-291` | 删 tickflow tier 分支 |
| `backend/app/main.py:19,32-35` | 删 tf_client 引用 |
| 根目录 `tiers.yaml` + `backend/app/config.py:104-107` | 删除 |
| `backend/pyproject.toml:23` | 删 `tickflow[all]` 依赖 |
| `backend/tests/data_providers/test_tickflow_mode.py` 等 | 删/改（见任务 6） |
| `frontend/src/pages/settings/Keys.tsx`、`Onboarding.tsx`、`lib/api.ts`、`lib/capability-labels.ts` | 删 TickFlow UI 与类型 |

---

### 任务 1：capability_gate 新家（先立后破）

**文件：**
- 创建：`backend/app/data_providers/capability_gate.py`
- 测试：`backend/tests/data_providers/test_capability_gate.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/data_providers/test_capability_gate.py
from app.capabilities import Cap
from app.data_providers import capability_gate
from app.data_providers.base import ProviderCapabilities


class _Stub:
    def __init__(self, caps):
        self.capabilities = caps


def test_detect_translates_provider_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(capability_gate, "_active_provider_name", lambda: "fquant_local")
    monkeypatch.setattr(
        "app.data_providers.get_provider",
        lambda name: _Stub(ProviderCapabilities(daily=True, minute=True, realtime=True)),
    )
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    cs = capability_gate.detect_capabilities()
    assert cs.has(Cap.KLINE_DAILY_BATCH)
    assert cs.has(Cap.KLINE_MINUTE_BATCH)
    assert not cs.has(Cap.DEPTH5_BATCH)
    assert capability_gate.tier_label() == "Fquant_local"
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/data_providers/test_capability_gate.py -v`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现 capability_gate.py**

从 `app/tickflow/policy.py` **平移**以下内容（保持行为不变，只删 TickFlow 分支）：
`_active_provider_name` / `_provider_capset`（含 A1 加的 KLINE_MINUTE_MONTH 映射）/ `_persist` / `_capset_from_json` / `tier_label` / `probe_log` / `missing_caps` / `extras_caps` / `_CAPSET_CACHE_FILE` / `_CACHE_SCHEMA_VERSION`（bump 到 8，注释"v8: 移除 tickflow 探测路径"）。`detect_capabilities` 收缩为：

```python
def detect_capabilities(force: bool = False) -> CapabilitySet:  # noqa: ARG001
    """按当前 provider 声明的 capabilities 生成 CapabilitySet 并持久化。

    force 参数保留签名兼容（provider 声明是静态的,无探测成本,始终即时计算）。
    """
    capset = _provider_capset()
    _persist(
        capset,
        _active_provider_name().capitalize(),
        log=["使用 DATA_PROVIDER 数据源能力声明"],
        missing=[],
        extras=[],
    )
    return capset
```

`_provider_capset` 去掉 `if provider_name in ("", "tickflow"): return None` 分支与 `| None` 返回类型（tickflow 不再存在）。`is_invalid_key`/`base_tier_name`/`_probe_real`/`_classify_tier`/`_tier_to_capset`/`_load_tiers_yaml`/`_compute_label*`/`_override_limits_with_detected_tier`/`TIER_SIGNATURES`/`_CAP_ALIASES`/`_is_transient`/`_call_with_retry` **全部不迁移**（随 policy.py 删除）。

- [ ] **步骤 4：运行测试验证通过**

- [ ] **步骤 5：全局切换 policy 导入**

```bash
cd backend
grep -rln "app.tickflow.policy" app tests scripts | xargs sed -i '' 's/app.tickflow.policy/app.data_providers.capability_gate/g'
grep -rn "app.tickflow.policy" app tests scripts   # 预期无输出
```
逐文件检查被切换的调用是否引用了**未迁移**的符号（`tier_label().split()` 式的 base tier 解析、`base_tier_name`、`is_invalid_key`）——这些调用点本身就在 tickflow 分支里，由任务 3/4 删除；本步骤先允许暂时引用报错清单存档，在任务 3/4 完成前不跑全量测试断言。

- [ ] **步骤 6：Commit**

```bash
git add -A && git commit -m "refactor(capabilities): provider-only capability_gate, retire probe/tier logic (A6 step1)"
```

---

### 任务 2：删业务侧 tickflow fallback 分支

**文件（全部是删代码）：**
- `backend/app/api/kline.py:510-513` 与 `:878-887`：删 `if not universe and provider_name == "tickflow": from app.tickflow.pools import ...` 块
- `backend/app/jobs/daily_pipeline.py:65-72`：删 `if provider_name == "tickflow":` pools 块
- `backend/app/services/extend_history.py:62-69`：同上
- `backend/app/services/depth_service.py:578-587`：`_has_capability` 删 tickflow 套餐检查（`if provider.name != "tickflow": return True` 之后的部分整体删除，函数以 provider depth 能力判定收尾）；删 `_tickflow_tier` 静态方法；`depth_service.py:42-52` 的套餐 clamp 表改为单一默认区间（取原 expert 档 3~300s）
- `backend/app/services/quote_service.py:251-291`：删 `_current_tier`；`realtime_mode` 删 tier 分支（`provider_name != "tickflow"` 判断连同 tickflow 侧代码删除，保留 provider realtime 能力判定 → `full_market`）；`_tier_min_interval` 收缩为 `return cls.DEFAULT_INTERVAL`

- [ ] **步骤 1：逐文件删除上述分支**
- [ ] **步骤 2：全量测试**

```bash
cd backend && uv run --extra dev pytest -q
```
预期：除 tickflow 专属测试（任务 6 处理）外全绿

- [ ] **步骤 3：Commit** `git commit -am "refactor: drop tickflow fallback branches in kline/pipeline/extend/depth/quote (A6 step2)"`

---

### 任务 3：settings/main/routes 拆除

- [ ] **步骤 1：`backend/app/api/settings.py`**：删 `switch_endpoint`、`save_tickflow_key`、`clear_tickflow_key` 三个端点及 `TickflowKeyIn`/`SwitchEndpointIn`/`DEFAULT_PAID_ENDPOINT`；`get_settings` 删 `tickflow` 嵌套块与全部顶层 TickFlow 字段（A4 过渡期结束）、删 `tf_client`/`tier_label` 等 import；`mode` 用 `current_data_mode()`。检查文件内其余 `tf_client` 引用（`:15` import 与 400-418 行 data_provider 切换处的 `tf_client.current_mode()`）一并清理。
- [ ] **步骤 2：`backend/app/main.py`**：删 `:19` `from app.tickflow import client as tf_client` 与启动日志里的 `tf_client.current_mode()`（换 `current_data_mode()`）；`detect_capabilities` import 改自 `app.data_providers.capability_gate`。
- [ ] **步骤 3：`backend/app/api/routes.py`**：`detect_capabilities/tier_label` import 改自 capability_gate（任务 1 步骤 5 的 sed 已覆盖，此处人工复核）。
- [ ] **步骤 4：secrets_store**：保留通用机制，删 `get_tickflow_key` 专用函数及调用（grep `get_tickflow_key`）。
- [ ] **步骤 5：全量测试 + 冒烟 + Commit** `git commit -am "refactor(settings): remove tickflow key/endpoint endpoints and fields (A6 step3)"`

---

### 任务 4：物理删除

- [ ] **步骤 1：删文件**

```bash
cd backend
git rm app/data_providers/tickflow_provider.py
git rm -r app/tickflow          # policy.py client.py pools.py + A2/A3 shim + __init__.py
git rm ../tiers.yaml
```

- [ ] **步骤 2：`registry.py`**：删 `TickFlowProvider` import 与 `"tickflow"` 注册项；`normalize_provider_name` 默认值与 `get_active_provider_name` 的异常回退改为 `"fquant_local"`；`preferences._clean_data_provider` 的默认值同步改（`app/services/preferences.py:102`）。
- [ ] **步骤 3：`config.py:104-107`**：删 `tiers_yaml` 配置与相关注释；packaging 里若引用 tiers.yaml（`grep -rn tiers ../packaging`）一并删。
- [ ] **步骤 4：`pyproject.toml:23`**：删 `"tickflow[all]>=0.1.23"`；运行 `uv sync --extra dev` 重建锁。
- [ ] **步骤 5：残留审计**

```bash
grep -rn "tickflow" app scripts --include="*.py" | grep -vi "tickflow-stock-panel"
```
预期：仅剩注释/文档性提及（如 CHANGELOG 类），无代码引用。

- [ ] **步骤 6：Commit** `git commit -am "chore: delete TickFlowProvider, app/tickflow package, tiers.yaml, SDK dep (A6 step4)"`

---

### 任务 5：前端拆除

- [ ] **步骤 1：`frontend/src/pages/settings/Keys.tsx`**：删 A4 留下的 `{isTickflow && ...}` 块本体（TickFlow key 卡、档位梯、端点切换 UI），保留本地数据源卡。
- [ ] **步骤 2：`frontend/src/pages/Onboarding.tsx`**：删 key 引导步骤与 tickflow.org 链接。
- [ ] **步骤 3：`frontend/src/lib/api.ts`**：删 `tickflow` 嵌套类型、`tier_label` 等字段、`switch_endpoint`/tickflow-key 相关 API 函数（1300 行附近）。
- [ ] **步骤 4：`frontend/src/lib/capability-labels.ts`**：`ALL_TIERS`/`tierStyle` 等档位梯工具若仅 Keys.tsx 使用则删；`TierTag` 若用于显示 provider label 则保留改名 `SourceTag`。
- [ ] **步骤 5：`cd frontend && npm run build`** 通过；手动过一遍设置/引导/数据页。
- [ ] **步骤 6：Commit** `git commit -am "feat(ui): remove TickFlow key/tier UI (A6 step5)"`

---

### 任务 6：测试清理 + 回归

- [ ] **步骤 1：** `tests/data_providers/test_tickflow_mode.py` 删除；grep `tickflow` in tests，删/改所有以 tickflow provider 为前提的用例（保留以 stub provider 测 capability_gate 的）。
- [ ] **步骤 2：** 全量 `uv run --extra dev pytest -q` 全绿；`uv run python -c "from app.main import app; print('ok')"`。
- [ ] **步骤 3：** 更新 `docs/fquant-local-tickflow-removal-audit.md`：全部条目勾结；`CONTEXT.md` 上游源词条已注明"取代原 TickFlow 付费 SDK"，复核无需改。
- [ ] **步骤 4：Commit** `git commit -am "test/docs: finalize TickFlow removal (A6 done)"`

## 非目标

- 不在 A6 新增 fquant_local 的缺失能力；若某能力仍只有 TickFlow 来源，应先回到审计清单补实现，不能靠删除掩盖。
- 不保留 `DATA_PROVIDER=tickflow` 的降级兼容；A6 的前置门要求产品决策已经确认 TickFlow 退场。
- 不改变本地 parquet 数据结构、raw write gate 或数据回填策略；本任务只移除 TickFlow 代码面和 UI/配置面。
