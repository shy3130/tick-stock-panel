# 趋势监控增量评估实施计划

> 规格：`docs/superpowers/specs/2026-07-31-dow-monitor-incremental-evaluation-and-realtime-priority-design.md`

目标是在不改变正式买卖信号语义、半小时 AI Worker 和 WebSocket 数据链路的前提下，恢复按周期到期的道氏评估、三股票有限并发和分钟结果错峰持久化，并修正重点解读把分析调度延迟误报为预热的问题。

## 任务 1：规格与可追踪性

- 在 `docs/spec-index.yaml` 登记批准规格及五个稳定要求 ID。
- 在 `docs/traceability.yaml` 为每个要求登记实现、可执行测试和验收证据路径。
- 执行规格契约测试，确保权威入口可机器验证。

## 任务 2：增量周期调度与有限并发

- 先在 `tests/backend/test_dow_monitor_incremental_evaluation.py` 写失败测试：
  - 非周期边界不调用 19912；
  - 只有新增 5m 完成 K 时只调用 5m；
  - 新股票和暂停周期会补算；
  - 同时最多处理 3 只股票，同一股票周期顺序不变；
  - 单股失败不阻塞其他股票；
  - 状态接口暴露请求、跳过、参与股票、耗时和并发上限。
- 在 `backend/app/services/dow_monitor_service.py` 恢复纯函数周期到期判定和 `asyncio.Semaphore(3)` 调度。
- 把分钟决策刷新与周期重算解耦，缓存命中时仍使用最新实时分钟、资金和盘口信息刷新。
- 单周期失败保留上一份稳定结构，只记录该周期错误。

## 任务 3：分钟结果实时追加与错峰补齐

- 先在 `tests/backend/test_dow_monitor_minute_result_scheduling.py` 写失败测试：
  - 实时循环不等待完整历史物化；
  - 1,024 容量队列按 200 行或 2 秒批量写入并按逻辑键去重；
  - 队列或 ClickHouse 失败时实时监控继续；
  - 任一市场开盘时完整补齐返回 `MARKET_OPEN`；
  - 收盘后 20 分钟及北京时间 06:30 才运行单飞补齐；
  - 单次补齐限制为 60 秒、2,000 行；
  - `DateTime64(3, 'Asia/Shanghai')` 返回值归一化后第二次运行 0 计算、0 写入。
- 新增实时追加队列、实时上下文构造器和后台补齐调度器。
- 扩展物化器与数据源，使零缺口先返回、补齐按预算执行，不在实时循环读取十天历史。
- 服务启动和停止时正确管理后台任务，状态接口暴露队列与补齐状态。

## 任务 4：重点解读可用性

- 先扩展 `keyInterpretation.test.ts` 和前端契约测试：分析年龄超过 90 秒但稳定 5m/15m 与实时行情可用时，不得进入 `LIVE_WARMUP`。
- 在 `interpretationMarketContext.ts` 分离实时行情延迟、分钟决策时效、稳定周期可用性和资金质量。
- 在 `keyInterpretation.ts` 动态列出真实缺失内容；资金完整时禁止显示“资金仍在预热”，无稳定周期时显示“正式分析更新中”。
- 保持正式信号生成、清除、翻转和升级边界不变。

## 任务 5：分层验证、独立复核与发布

- 依次执行纯函数/仓储测试、服务集成测试、前端单元与契约测试、完整相关回归。
- 生成 `docs/acceptance/dow-monitor-incremental-evaluation.md`，记录下层语义证据、性能与发布前验收命令。
- 进行独立要求到证据复核并生成 `docs/reviews/2026-07-31-dow-monitor-incremental-evaluation-review.md`。
- 更新 `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`。
- 在 10.28 使用不可变候选镜像验证后切换 3018；不重启 19912、WebSocket 和半小时 AI Worker。
- 连续观测至少 10 轮：轮询启动间隔不超过 30 秒、非边界轮次无 19912 请求、并发不超过 3、物理重复行不增长、重点解读不再长期停留在错误预热。
- 提交并推送 GitHub 分支；若任一语义验收失败，恢复上一 3018 镜像，保留已写入分钟结果。
