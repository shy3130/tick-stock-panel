# 验证记录

## 代码范围

- `backend/app/services/volume_breakout.py`
- `backend/app/api/research.py`
- `backend/tests/services/test_volume_breakout.py`
- `backend/tests/api/test_volume_breakout_api.py`
- 本目录方案、Review 与最终设计文档

## 已执行命令

```text
PYTHONPATH=. /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_volume_breakout.py tests/api/test_volume_breakout_api.py -q
# 12 passed in 1.01s

PYTHONPATH=. /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m py_compile app/services/volume_breakout.py app/api/research.py
# 通过
```

## 观察到的结果

- 缺 generation-pinned reader、PIT eligible-universe 或对应 calendar 能力时，服务返回 `status=unavailable`、结构化 reason，事件/cluster/coverage/provenance 均为空；不使用 overlay、current universe 或日线 fallback。
- 完整 fake 三项能力可运行真实事件状态机与 OOS；不满足 production 能力门禁时不读取 bars、不制造事件。
- 请求模型拒绝未知字段、缺失日期及超过 1000 个标的；非法日期区间映射为 HTTP 400。
- 响应键通过交易语义禁令检查，不含买卖、仓位或执行建议字段。
- 生产仓库缺历史 PIT eligible-universe，因此没有宣称真实事件命中或 OOS 增量。

初始 focused tests 已覆盖契约；下节补充生产化状态与累计回归。

## 生产化波次（2026-08-27）

- 已实现严格早于 E 的 20 日 volume/amount P90、3–15 日整理、首次冻结且不重置、突破柱排除、上下严格突破、失败分层、1/5/10/20 日 forward、重叠 cluster、IS/OOS 与成本诊断。
- 真实 generation-pinned reader、volume-breakout 历史 calendar seam 与 event-date PIT universe seam 已接通；collection calendar 固定来自同一 exact fstore generation 的 `trade_date` contract 及 manifest SHA-256。
- SCD collector/publisher/reader 已实现 strict fstore generation pin、next-market-day effective、immutable parent CAS/flock/fsync/path guard 与整 run fail-closed。首个真实 generation 已发布并验证，effective_from 之前仍保持 unavailable。
- 本波 focused/API 测试由主会话执行：`27 passed in 7.17s`；改动 Python 文件 `ruff --select F,E9` 通过。

## 最终集成回归

- 当前分支后端全量：`3442 passed, 3 skipped, 8 warnings in 235.15s`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。

## Universe SCD 生产化波次（2026-08-27）

### 新增代码范围

- `backend/app/services/universe_scd.py`
- `backend/app/storage/repository.py`：lazy `pit_eligible_universe` capability
- `backend/app/jobs/daily_pipeline.py`：唯一盘后 hook 的 best-effort publish
- `backend/app/services/volume_breakout.py`：event-date identity、全量 prefetch、事件窗口 clean cutover
- `backend/tests/services/test_universe_scd.py`
- `backend/tests/services/test_volume_breakout.py`

### 当前状态

- 首个真实 generation：`20260827T153316Z-20c87c09f41cffb9`
- source fstore generation：`20260827T134914`
- source manifest SHA-256：`fd4ad1e1e702af5a0095135ea86a9383374e498104e1c278cc1a85f0787d79d2`
- collection date：`2026-08-27`
- effective from：`2026-08-28`
- eligible symbols：5903
- content hash：`9744c4758f75d1399c28644fec3ccd0a9a96777ea9efe2cc834e635e32eef0c2`
- reader smoke：collection day 返回 `UniverseScdNoCoverage`；effective day 返回固定 identity 与 5903 个标的。
- 独立二次 Review：最初发现未来交易日来源、newest interval 自引用校验和 `size_bytes` 严格性缺口；修复后复核无 blocker/major。

### 生产 publish

不新增第二日常入口；后续由既有盘后管道执行（API 为 `POST /api/pipeline/run`，或等待既有调度）。hook 记录 exact source generation、trade_date calendar identity、effective_from 与结果；失败不切换 `current.json`，也不阻塞其它 pipeline stages。
