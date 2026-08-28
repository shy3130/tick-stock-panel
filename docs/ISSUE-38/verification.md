# Issue #38 验证与验收记录

日期：2026-08-27

## 自动验证

### Focused 契约测试

覆盖 F1-F4 detector、共同执行/统计、API 生命周期与 fail-closed 响应：

```text
65 passed in 3.38s
```

测试文件：

- `backend/tests/services/test_hold_firm_first_yin.py`
- `backend/tests/services/test_hold_firm_breakout_pullback.py`
- `backend/tests/services/test_hold_firm_gentle_slope.py`
- `backend/tests/services/test_hold_firm_platform_breakout.py`
- `backend/tests/services/test_hold_firm_evaluation.py`
- `backend/tests/api/test_hold_firm_patterns_api.py`

### 后端全量回归

```text
3597 passed, 3 skipped, 8 warnings in 194.00s
```

3 个 skip 与 8 个 warning 均来自既有测试/Polars 警告；本轮无失败。

### Ruff

对全部新增 Python 文件、`backend/app/api/research.py` 与新增测试执行：

```text
ruff check --select F,E9
All checks passed!
```

### API 路由冒烟

从实际 `app.main:app` 加载并断言以下路由已注册：

- `GET /api/research/hold-firm-patterns`
- `POST /api/research/factors/hold-firm-patterns/evaluate`

结果：`hold-firm routes registered`。

## 本地工具链说明

Issue worktree 内直接执行 `uv run pytest` 会触发 hatchling 对 `backend/pyproject.toml` 中 `../README.md` 的 worktree editable-build 路径校验失败。验证复用了原工作区已锁定的 `backend/.venv`，通过绝对测试路径与 `PYTHONPATH=<issue-worktree>/backend` 加载本分支代码；API 冒烟另用 `python -P` 避免当前工作目录优先导入主工作区。全量测试 warning 堆栈均指向 Issue worktree，证明被测源码来自本分支。

## 验收边界

工程实现、契约测试、全量回归、Ruff 与独立 coding review 已完成。真实 canonical v2 全历史上的 IS/OOS 统计尚未在本次交付中运行，因此四因子的生产 verdict 仍为待评估；Issue #38 与 PR 使用 `Refs #38`，不在本次交付中关闭。
