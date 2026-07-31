# 趋势监控增量评估与实时优先验收

状态：本地下层语义验收通过；10.28 候选与生产运行态验收待发布步骤补充。

适用规格：
`docs/superpowers/specs/2026-07-31-dow-monitor-incremental-evaluation-and-realtime-priority-design.md`。

## 要求与下层语义证据

### REQ-DOW-MONITOR-INCREMENTAL-TIMEFRAME-EVALUATION-001

- `due_timeframes_for_minute()` 是不依赖前端状态的纯判定：非边界分钟跳过已有 LIVE 周期；14:45 只到期 5m/15m；暂停周期立即重试；新股票补齐全部周期。
- `_evaluate_symbol()` 的真实调用测试证明 14:45 只向 19912 请求 5m、15m，顺序不变，请求数为 2、缓存跳过数为 3。
- 缓存命中仍返回 `decision_ready`，新的完成 1m K 继续刷新分钟决策；正式通知和信号转换代码未改变。
- 单周期错误继续保留既有 snapshot/chart/source timestamp 和 `LIVE` 稳定状态，在 snapshot 记录 `evaluation_error/evaluation_failed_at` 并立即重试；只有从未产生有效稳定结果时才标记 `ANALYSIS_PAUSED`。
- 部分冷启动仅评估缺失周期；历史补齐暂时不可用不会把其他已有稳定周期一起暂停。day 在当天完成收盘 K 线后到期，盘中不会提前重算。

### REQ-DOW-MONITOR-BOUNDED-SYMBOL-CONCURRENCY-001

- 6 只股票并发测试观测到实际并发至少 2、最多 3。
- 每只股票内部仍按 `5m → 15m → 30m → 60m → day` 顺序执行。
- 单股异常测试证明另一股票仍刷新分钟决策，服务记录单股错误且本轮仍有成功时间。
- 状态接口暴露 `cycle_duration_seconds`、`evaluation_request_count`、`cache_skip_count`、`evaluated_symbols`、`evaluated_timeframes` 和 `max_parallel_symbols=3`。

### REQ-DOW-MONITOR-MINUTE-RESULTS-REALTIME-APPEND-001

- 旧实现的阻塞物化测试先在 80ms 内超时；改造后同一测试通过，实时循环只提交后台请求。
- 实时上下文只使用本轮 minute rows、已保存稳定状态、quote、depth、capital 和通知，不读取十天历史。
- 实时上下文按目标股票过滤周期状态；跨股票同周期状态无法覆盖目标股票并写入 ClickHouse。
- 1,024 容量队列对同一逻辑键只接受一次，最多 200 行或 2 秒批量写；ClickHouse 异常只增加失败计数，不向实时循环抛出。
- 只有新保存的分钟决策提交实时追加；15 秒轮询不会在队列 flush 后重复提交同一决策分钟。

### REQ-DOW-MONITOR-MINUTE-RESULTS-STAGGERED-BACKFILL-001

- 任一启用市场开盘时，完整补齐决策返回 `MARKET_OPEN`。
- 各市场收盘后 20 分钟运行一次，另在北京时间 06:30 按市场最近一个已完成交易日进行夜间巡检；目标交易日显式传给物化器。
- 调度器单飞执行，物化器单次预算为 60 秒、2,000 行；剩余时间同时下传给 ClickHouse `max_execution_time`、HTTP 超时和历史上下文循环检查。
- 零缺口测试证明先查询逻辑键并直接返回，不加载十天原始历史；3 个缺口、2 行预算测试写 2 行并保留 1 个 remaining key。
- 仓储测试确认 ClickHouse naive `DateTime64(3, 'Asia/Shanghai')` 恢复为 aware 键；精确截止查询采用 `+1ms`，第二次逻辑键查询可以命中。

### REQ-DOW-MONITOR-LIVE-INTERPRETATION-AVAILABILITY-001

- 前端分别计算 WebSocket 时效、分析调度时效、稳定 5m/15m 可用性和资金可用性。
- 分析年龄旧但稳定周期与资金可用时，候选不再被 `LIVE_WARMUP` 抢占，继续输出机会、风险或观察业务解释。
- 真正缺失时逐项列出 `5m周期`、`15m周期`、`资金`；完整资金不会再显示“资金仍在预热”。
- 实时行情可用但尚无稳定周期时，显示“正式分析更新中”，同时保留 1 分钟、量速、盘口和日高低证据，不生成正式买卖信号。

## 可执行验证

本地执行结果：

```text
tests/backend/test_dow_monitor_incremental_evaluation.py
17 passed

tests/backend/test_dow_monitor_minute_result_scheduling.py
10 passed

tests/backend/test_dow_monitor*.py
131 passed

backend/tests
704 passed, 13 warnings

keyInterpretation.test.ts + interpretationMarketContext.test.ts
18 passed

frontend npm test -- --run
209 passed, 2 skipped

frontend npm run build
passed

scoped Ruff
passed

git diff --check
passed

scripts/check_spec_compliance.py
passed
```

独立复核先后识别出跨股票状态污染、部分冷启动全量重算、旧稳定状态误暂停、完成日线漏评、
夜审目标日未传递和前端真实预热边界等问题；对应回归用例均经历失败后修复并纳入上述套件。
最终独立复核文件完成后，必须重新运行并通过 `scripts/check_spec_compliance.py`。

## 生产验收门

发布到 10.28 前后必须补充以下证据，缺一项不能把状态改为“生产通过”：

1. 候选镜像绑定精确提交和 `BUILD_ID`，记录镜像 ID、旧 3018 镜像和备份目录。
2. 候选端口先验证 `/health`、状态指标、WebSocket hello/snapshot/unsubscribe、重点解读静态包和 ClickHouse 键查询。
3. 切换 3018 时不得重启 19912、行情 WebSocket 或 `TickFlow_Dow_AI_Worker`。
4. 连续至少 10 轮启动间隔不超过 30 秒；非周期边界轮次 19912 请求为 0，并发上限为 3。
5. 比较发布前后分钟结果物理行/唯一逻辑键；同一逻辑分钟重复运行不得继续增加物理重复行。
6. 新股票和已有股票均能保留实时重点解读；稳定周期/资金可用时不再长期显示错误预热。
7. 3018、19912 和 Worker 均健康、重启次数符合预期，发布窗口无新 ERROR/CRITICAL/Traceback。

历史已有物理重复行不在本次自动清理范围内；发布或回滚不得删除分钟结果。

## 2026-07-31 candidate rollback and HK alias root cause

- Candidate image `tickflow-stock-panel-app:dow-monitor-incremental-cb060ebaa228`
  (`sha256:321bec8ecd09c5310e508e3be140cc059a625f82cbb33185e31efd410ae856a5`)
  passed health, static-bundle, ClickHouse-availability and WebSocket protocol checks.
- The candidate was not accepted for production: its first cycle took about 128 seconds and a
  later cycle still exceeded 50 seconds, violating the 30-second cycle requirement.
- The lower-layer cause was a symbol-identity mismatch. The monitor list stores display aliases
  such as `00981.HK`, while persisted states and raw minute rows may use `981.HK`. Exact string
  matching discarded the existing state on every cycle and repeatedly requested cold history
  from midnight.
- Production was rolled back to `tickflow-stock-panel-app:dow-monitor-8a2c007931af` without
  restarting the 19912 engine, market WebSocket service, or standalone AI worker, and without
  deleting ClickHouse data.
- The corrective acceptance tests prove that a padded HK display alias reuses canonical persisted
  state, is not classified as cold, and performs zero timeframe evaluations on a non-boundary
  minute. The full targeted Dow monitor suite now contains 129 passing tests.
- Supplemental independent review found `P0=0`, `P1=0`, and `P2=0`, including probes for store
  removal, notification isolation, non-HK behavior, and distinct HK codes after canonicalization.
- Production acceptance remains pending until the corrected immutable image passes ten consecutive
  candidate cycles and ten consecutive 3018 cycles with every observed start interval at or below
  30 seconds.

## 2026-07-31 exact-close boundary follow-up

- The `ab931342f081` candidate proved that HK alias state was reused, but exposed a second
  lower-layer boundary mismatch. Some CN/HK feeds include a final row labeled exactly at the
  market close (`15:00` or `16:00`). Timeframe construction accepts that close row, while the
  completed-bucket marker previously rejected an exact session-end timestamp.
- A saved final-close state was therefore compared with the last regular minute (`14:59` or
  `15:59`) as if its prior bucket marker did not exist, causing repeated 5m/15m/30m/60m
  evaluations. Candidate observations included repeated 110-112 second cycles, so the candidate
  was stopped and never promoted to 3018.
- The corrective semantic test failed first with all four intraday periods reported due. It now
  proves that final-close-labeled CN/HK states reuse the same completed buckets as the last regular
  minute. The special case applies only to the final session close in CN/HK; midday breaks and US
  timestamps retain their prior behavior.
- Supplemental independent review found `P0=0`, `P1=0`, and `P2=0` and independently confirmed
  CN/HK final-close equivalence across all intraday periods, HK midday and non-divisible-session
  behavior, US isolation, and final-close aggregation values.

## 2026-07-31 minute-fetch versus daily-state follow-up

- The `2ec297c94449` candidate correctly made zero 19912 requests after final-close repair, but a
  later cycle still took about 103 seconds. This candidate was also stopped and never promoted.
- The fetch plan used the minimum timestamp across all five states. A daily state from an earlier
  session therefore expanded every live minute fetch to that old day even when all intraday states
  were current. The long cycle was in market-data retrieval, not Dow evaluation.
- The minute fetch plan now derives its start and cold-history decision only from
  5m/15m/30m/60m states during the normal live path. Daily due evaluation remains independent
  through the daily loader and is not suppressed.
- Independent review found that simply ignoring day could construct today's day bar from only the
  final few minutes. The corrected plan expands just that cycle to local midnight when day is
  missing, paused or failed, or when the market has closed and day is old/forming. This supplies a
  complete current-session OHLCV without classifying the symbol as a ten-day minute-history cold
  start. A prior-session final day does not expand the fetch while the market is still open.
- The failing-first regression covers an old daily fast path during the open, missing-only daily
  full-session input, and an old daily state after close, while all intraday states remain current.
- Final independent review found `P0=0`, `P1=0`, and `P2=0`. A 390-minute US-session probe
  reconstructed the correct daily OHLCV and FINAL completion, and market-open, post-close,
  midday, weekend, timezone, new-symbol and partial-cold scenarios all passed.
