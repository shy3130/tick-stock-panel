# A3：`app.tickflow.repository` 迁移到中性包 `app.storage` 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把与 TickFlow 无关的本地 parquet 存储层（`DataStore`/`KlineRepository`）从 `app/tickflow/repository.py` 迁到中性包 `app/storage/`，保留一版兼容导入，为 A6 彻底删除 `app/tickflow/` 包铺路。

**架构：** `git mv` 物理移动文件到 `app/storage/repository.py`；旧路径 `app/tickflow/repository.py` 换成 3 行 re-export shim（保留一个版本周期）；全部导入方切到新路径。纯机械改动，不改任何函数体。

**技术栈：** Python 3.12。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- `backend/app/tickflow/repository.py` 承载的是 `DataStore`/`KlineRepository` 本地 parquet 存储能力，调用方包括 `main`、`kline_sync`、`index_sync`、`extend_history`、`backtest`、scripts 和 tests；它不是 TickFlow SDK 访问层。
- 去 TickFlow 审计已确认导入方是 **8 处 app 内 + 1 处测试 + 3 处 scripts**，此前漏数过 `index_sync.py`、`extend_history.py`、`backtest.py`，因此本计划必须以 grep 残留为验收门。
- 本任务是包名中性化，目标是让 A6 可以物理删除 `app/tickflow/`；不改变 parquet 路径、读写语义、raw write gate 或 repository 内部函数体。

**改动面警示（审计 High 教训）：** 去 TickFlow 审计初稿只数出 5 处导入方，实际 grep 有 **8 处 app 内 + 1 处测试 + 3 处 scripts**。本计划以下面的穷举清单为准，完成后必须跑残留 grep 验证，不得凭记忆。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/storage/__init__.py` | 新包 | 创建（re-export DataStore/KlineRepository） |
| `backend/app/storage/repository.py` | DataStore + KlineRepository 本体 | `git mv` 自 `app/tickflow/repository.py` |
| `backend/app/tickflow/repository.py` | 兼容 shim | 重建为 re-export，标注废弃 |
| 导入方 ×12 | 见任务 2 穷举清单 | 改 import 行 |

---

## 失败测试/验证门

在动文件前先确认这些验证会暴露当前问题：

- `cd backend && uv run python -c "from app.storage.repository import KlineRepository"`：迁移前应失败，证明新中性路径尚不存在。
- `grep -rn "from app.tickflow.repository import" backend/app backend/tests backend/scripts`：迁移前应列出全部旧导入方，作为替换底账。
- 迁移后 `grep -rn "tickflow.repository" app tests scripts | grep -v "app/tickflow/repository.py"` 必须无输出；这是本任务最关键验收，不允许只靠测试通过。
- 迁移后 `from app.tickflow.repository import KlineRepository` 与 `from app.storage.repository import KlineRepository` 应指向同一对象，保证兼容 shim 没有复制实现。

---

### 任务 1：物理移动 + 兼容 shim

**文件：**
- 移动：`backend/app/tickflow/repository.py` → `backend/app/storage/repository.py`
- 创建：`backend/app/storage/__init__.py`
- 重建：`backend/app/tickflow/repository.py`（shim）

- [ ] **步骤 1：git mv 移动文件**

```bash
cd backend
mkdir -p app/storage
git mv app/tickflow/repository.py app/storage/repository.py
```

- [ ] **步骤 2：创建包入口**

```python
# backend/app/storage/__init__.py
"""本地 parquet 存储层（数据源无关）。原址 app/tickflow/repository.py，A3 迁出。"""
from app.storage.repository import DataStore, KlineRepository

__all__ = ["DataStore", "KlineRepository"]
```

- [ ] **步骤 3：重建旧路径为兼容 shim**

```python
# backend/app/tickflow/repository.py
"""兼容导入（A3 保留一版）：请改用 app.storage.repository。A6 移除本文件。"""
from app.storage.repository import DataStore, KlineRepository  # noqa: F401
```

- [ ] **步骤 4：全量测试验证兼容层生效（所有旧导入仍应工作）**

运行：`cd backend && uv run --extra dev pytest -q`
预期：与迁移前基线相同（全部通过；若基线本有失败项，逐条比对确认无新增失败）

- [ ] **步骤 5：Commit**

```bash
git add app/storage app/tickflow/repository.py
git commit -m "refactor(storage): move DataStore/KlineRepository to app.storage, keep compat shim"
```

---

### 任务 2：切换全部导入方到新路径

**穷举清单（12 处代码 + 1 处 docstring），逐个把 `from app.tickflow.repository import ...` 改成 `from app.storage.repository import ...`，导入的符号名不变：**

**文件（修改）：**
- `backend/app/main.py:21`（`DataStore, KlineRepository`）
- `backend/app/backtest/engine.py:22`（`KlineRepository`）
- `backend/app/jobs/daily_pipeline.py:26`（`KlineRepository`）
- `backend/app/services/screener.py:17`（`KlineRepository`）
- `backend/app/services/index_sync.py:23`（`KlineRepository`）
- `backend/app/services/extend_history.py:30`（`KlineRepository`）；同文件 `:15` docstring 里的 `tickflow.repository.KlineRepository` 一并改为 `storage.repository.KlineRepository`
- `backend/app/services/backtest.py:18`（`KlineRepository`）
- `backend/app/services/kline_sync.py:22`（`KlineRepository`）
- `backend/tests/services/test_raw_write_gate.py:5`（`DataStore, KlineRepository`）
- `backend/scripts/backfill_etf_daily.py:21`（`DataStore, KlineRepository`）
- `backend/scripts/backfill_broad_benchmarks.py:20`（`DataStore, KlineRepository`）
- `backend/scripts/refresh_polluted_daily.py:22`（`KlineRepository`）

- [ ] **步骤 1：批量替换（sed 后逐文件 diff 核对）**

```bash
cd backend
grep -rl "from app.tickflow.repository import" app tests scripts | \
  xargs sed -i '' 's/from app.tickflow.repository import/from app.storage.repository import/'
# extend_history.py 的 docstring 单独处理
sed -i '' 's/tickflow\.repository\.KlineRepository/storage.repository.KlineRepository/' app/services/extend_history.py
git diff --stat   # 应恰好 13 个文件（12 代码 + extend_history docstring 同文件）→ 实际 12 个文件
```

- [ ] **步骤 2：残留 grep 验证（除 shim 本身外必须为 0）**

```bash
grep -rn "tickflow.repository" app tests scripts | grep -v "app/tickflow/repository.py"
```
预期：无输出

- [ ] **步骤 3：全量测试**

运行：`cd backend && uv run --extra dev pytest -q`
预期：与基线相同

- [ ] **步骤 4：应用启动冒烟（import 链验证）**

运行：`cd backend && uv run python -c "from app.main import app; print('ok')"`
预期：输出 `ok`，无 ImportError

- [ ] **步骤 5：Commit**

```bash
git add -A
git commit -m "refactor(storage): switch all 12 importers to app.storage.repository"
```

---

### 任务 3：回归收尾

- [ ] **步骤 1：再跑一次穷举校验（防止步骤间有人新增导入）**

```bash
cd backend
grep -rn "tickflow.repository" app tests scripts | grep -v "app/tickflow/repository.py"
grep -c "from app.storage.repository import" app/main.py   # 预期 1
```

- [ ] **步骤 2：全量测试 + 启动冒烟**

```bash
cd backend && uv run --extra dev pytest -q && uv run python -c "from app.main import app; print('ok')"
```

- [ ] **步骤 3：在去 TickFlow 审计文档勾掉 A3**

修改 `docs/fquant-local-tickflow-removal-audit.md`：把 repository rename 项标注「已完成（A3，兼容 shim 保留至 A6）」。

- [ ] **步骤 4：Commit**

```bash
git add docs/fquant-local-tickflow-removal-audit.md
git commit -m "docs: mark A3 repository rename done in removal audit"
```

## 非目标

- 不改 `DataStore`/`KlineRepository` 的任何方法体、分区路径、schema 或写盘行为。
- 不借 A3 顺手拆 raw write gate、ETF 回填、日线 enriched 逻辑；这些属于独立数据链路任务。
- 不在 A3 删除 `app/tickflow/repository.py` shim；物理删除留给 A6，避免一次性破坏旧导入兼容。
