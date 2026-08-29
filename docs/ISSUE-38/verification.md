# Issue #38 验证与验收记录

日期：2026-08-29

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

## PR #39 review 修复后复验

2026-08-28 对三条行级 review 修复重新执行六个 focused 契约测试文件：

```text
67 passed in 4.08s
```

同一批实现与测试文件重新执行：

```text
ruff check --select F,E9
All checks passed!
```

后端全量回归：

```text
3599 passed, 3 skipped, 8 warnings in 370.00s
```

独立 reviewer 复核当前 diff，确认 universe membership 日期预取、censor 的 PIT 优先级与首阴后 anchor 三处修复完整，未见 blocker/major。

## 验收边界

工程实现、契约测试、全量回归、Ruff 与独立 coding review 已完成；生产 OOS 也已运行。四因子均按自身冻结门禁得到 `unavailable`，属于 Issue 接受的诚实样本不足结论，不是整体通过，也不触发生产提升。

## Issue #40 dependency addendum

验证与生产运行必须使用独立 published `presence_v1`。presence 无法证明不在池，
故 production `pit_universe_ineligible` 恒为空；任何 `NOT_OBSERVED`、缺 snapshot、
coverage、非市场日或完整性错误均为整单 `unavailable_universe_presence`。响应需披露
retrospective presence manifest 与 source pin 的完整 provenance。

## Issue #40 presence_v1 集成与 production 冒烟

- published root：`/Volumes/WD1/duckdb/snapshots/tickflow-universe-presence`
- generation：`20260829T020332Z-6e648967c37e6739`
- manifest SHA-256：`2c407072371bc024de46fd2d5b1d282d964b478e94196da9fc575a0bac1781d4`
- schema/rule：`2` / `presence_v1`，`retrospective=true`
- source generation/hash：`20260829T000704` / `a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`
- capability 冒烟：`status=ok`；生产 reader 与 capability/evaluate 使用同一 identity，未回退当前 instruments 或 eligible 历史猜测。

presence consumer 的最终自有 focused 契约测试 **71 passed in 2.08s**，Ruff F/E9 **All checks passed**；独立复核在“事件实际 membership 日期”口径修正后未见 blocker/P1/P2。Issue #40 publisher strict identity 测试另有 **4 passed** 的先行证据；最终依赖集成后的全量回归见后续收口记录。

## 真实 OOS（2026-08-29）

请求：确定性 canonical symbol 升序前 10 只，`2024-08-07` 至 `2026-08-28`，OOS 起点 `2025-08-19`，20 日 horizon、10 bps、bootstrap `seed=42` / `rounds=5000`。

运行返回 `status=ok`、`unavailable_reason=null`，并携带 100 个实际事件 membership 日期的 presence day identity；这证明原 `unavailable_universe_history` 工程阻断已消除。

| 因子 | parent | qualified | not_selected | OOS qualified complete | OOS holding paired | verdict |
|---|---:|---:|---:|---:|---:|---|
| `first_yin_complement` | 3 | 3 | 0 | 0 | 0 | `unavailable` |
| `breakout_pullback` | 48 | 16 | 31 | 6 | 6 | `unavailable` |
| `low_gentle_slope` | 5 | 0 | 5 | 0 | 0 | `unavailable` |
| `bottom_platform_breakout` | 66 | 5 | 61 | 4 | 4 | `unavailable` |

所有 factor 的 `denominator_audit` 均为 0；`unavailable` 来自各自有效 OOS 样本不足，未聚合成单一胜率。完整机器摘要见 [oos-verdict.json](oos-verdict.json)。
