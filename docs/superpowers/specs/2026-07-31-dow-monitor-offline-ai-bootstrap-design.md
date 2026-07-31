# 趋势监控半小时 AI 离线数据启动设计

## 状态与授权

- Status: approved
- Specification ID: `SPEC-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`
- 用户决定：2026-07-31 明确同意“新加入股票先使用已离线保存的数据，
  不重新等待 WebSocket 累积 30 分钟”
- 影响范围：趋势监控独立半小时 AI Worker
- 不影响：实时重点解读、正式买卖信号、WebSocket 接收和分钟实时追加

## Authoritative Precedence

Startup exception: exactly one latest completed checkpoint before `created_at` is eligible for bounded offline recovery.

Normal checkpoint rule: every later completed checkpoint on or after `created_at` may use bounded offline recovery when canonical minute results are missing.

Older checkpoints before the eligible startup checkpoint remain prohibited.

## 问题

当前 Worker 只读取已经物化到
`longbridge.lb_dow_monitor_minute_results` 的分钟结果，并跳过
`window_end <= symbol.created_at` 的检查点。因此盘中新增股票即使在 ClickHouse
已有从开盘开始的离线 WebSocket 行情，也可能显示“数据不足”，并等待下一个
半小时检查点。

现有权威规格还规定历史行不得触发 `created_at` 之前的模型调用。用户本次决定
只对“新增股票的最近一个已完成检查点”覆盖该限制；不得回放当天所有更早检查点。

## 方案比较

### 方案 A：Worker 复用分钟结果物化器（采用）

Worker 在目标检查点数据不足时，调用既有离线原始数据源和分钟结果计算链路，
把开盘至目标检查点的缺失分钟结果写入 ClickHouse，再重新构建 AI 快照。

优点：

- 离线与实时分钟指标共用同一个计算器和数据表；
- 不在 AI Worker 中复制指标算法；
- 补算结果可复用、可审计；
- 继续与 WebSocket 回调隔离。

代价：需要为物化器增加一个有界的单股票、单检查点入口。

### 方案 B：AI Worker 直接读取原始离线行情

实现较短，但会在 AI 路径重新计算部分指标，与正式分钟结果产生语义漂移，
并使 AI 快照绕过既有缺失字段和版本控制。拒绝。

### 方案 C：新增独立的股票加入事件与补算服务

边界最清晰，但需要新的事件队列、幂等状态和部署单元。当前股票数量少，
超出本轮所需。暂不采用。

## 需求

### REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001

对交易时段内新加入且已启用的趋势监控股票：

1. 若当前已有至少一个已完成的半小时检查点，Worker 必须选择最近的一个作为
   唯一启动检查点；
2. 即使该检查点早于 `created_at`，也允许使用开盘至该检查点的离线数据生成
   一次分析；
3. 不得回放更早的检查点；
4. 已存在同一逻辑键的 `completed` 或 `insufficient_data` 结果时不得重复调用模型；
5. 后续只处理 `created_at` 之后新完成的正常检查点。

例如 10:17 加入 A 股时，启动检查点为 10:00；不得补调更早窗口。10:30
完成后继续执行正常分析。

### REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001

离线启动必须满足：

- 只读取 ClickHouse 已保存的原始 Quote、Depth、1m K 线和相关离线数据；
- 通过既有分钟结果计算器写入
  `longbridge.lb_dow_monitor_minute_results`，不得创建第二套指标口径；
- 单股票、单检查点串行执行，默认模型并发仍为 1；
- 补算和模型调用都在 `TickFlow_Dow_AI_Worker` 中进行，不能进入 3018 的
  WebSocket 回调或实时渲染路径；
- 每次启动补算最多生成 500 行分钟结果，墙钟时间最多 15 秒；失败只影响该股票
  检查点；
- 离线数据仍不足时保存明确的 `insufficient_data`，不编造证据，也不调用模型；
- 不改变正式买卖信号、实时重点解读或分钟实时追加结果。

## 数据流

```text
监控股票列表
  -> 找到最近已完成检查点
  -> 查询分钟结果表
  -> 数据不足时调用有界离线物化
       -> ClickHouse 原始离线行情
       -> 既有分钟结果计算器
       -> 分钟结果表
  -> 重新查询开盘至检查点的分钟结果
  -> 快照充分：调用 LLM
  -> 快照不足：保存 insufficient_data
  -> 永久保存半小时分析
```

正常半小时调度也可在自身目标窗口缺少分钟结果时使用相同的有界补算入口，
但每个逻辑检查点最多触发一次启动补算。

## 错误与资源边界

- 原始离线表不可用：记录数据源错误，当前股票降级，不停止 Worker；
- 物化超预算：保存 `insufficient_data` 和
  `BACKFILL_BUDGET_EXCEEDED`，不重试同一检查点，等待下一个正常检查点且不阻塞
  其他股票；
- 重复启动：由
  `(market, symbol, trade_date, window_end)` 逻辑键和仓储查询去重；
- 模型不可用：沿用当前 `failed` 状态；
- WebSocket 不得等待补算 Future、线程或 ClickHouse 批量写入。

## 测试与验收

必须先观察以下行为测试失败，再修改生产代码：

1. 10:17 新增股票会选择 10:00，离线补算后立即生成一次分析；
2. 10:17 新增股票不会调用 09:30 或其他更早检查点；
3. 分钟结果已充足时不调用离线物化；
4. 离线物化后仍不足时不调用 LLM；
5. 一个股票补算失败不妨碍下一股票；
6. 已存在逻辑结果时不重复补算或调用模型；
7. 正常后续 10:30 检查点继续执行；
8. WebSocket/3018 组件契约证明没有引入补算依赖。

生产验收必须核对：

- 新加入测试股票在下一个 Worker 轮询内出现最近检查点结果；
- 结果的 `data_cutoff` 等于该检查点，且没有读取之后的数据；
- ClickHouse 分钟结果行标记为 backfill；
- AI Worker、3018 和 WebSocket 均无重启、无队列堆积；
- 补算前后正式信号没有变化。

## 规格覆盖关系

本设计获最终书面批准后，应作为新的权威规格加入 `docs/spec-index.yaml`，
并新增一条已解决冲突：

- 旧规则继续控制正常半小时调度；
- `REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001` 只对新增股票最近一个
  已完成检查点优先；
- 除这一检查点外，`created_at` 之前的模型调用仍被禁止。
