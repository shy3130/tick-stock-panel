# 趋势监控四组指标列表设计

日期：2026-07-29

状态：待用户书面规格确认

视觉原型：`output/playwright/dow-monitor-indicator-prototype.html`

## 1. 目标

在现有 `/dow-monitor` 高密度列表中，用四组组合列替换分散的“通道、控制线、动能、量比、主动资金”列，使用户在每页 20 只股票的前提下，同时看到：

1. 趋势与价格位置；
2. 短线动能与盘中涨速；
3. 量价与资金强弱；
4. 突破距离与波动风险。

正式买卖信号继续单独显示，并且只能来自后端完成 K 线结果或持久化通知。任何实时 quote、depth 或形成中的 1 分钟 K 线都不得生成、清除、翻转或升级正式买卖信号。

## 2. 已确认的页面结构

列表按以下列顺序展示：

1. 股票；
2. 价格 / 涨跌；
3. 当日 mini 趋势线；
4. 趋势 / 位置；
5. 动能 / 涨速；
6. 量价 / 资金；
7. 突破 / 风险；
8. 买卖信号；
9. 查看详情。

四组指标每组固定两行。页面继续固定每页 20 只，列表自身允许横向滚动，页面视口不得产生横向溢出。点击同一股票或“查看详情”仍按现有规则展开/收拢列表下方详情。

视觉标识：

- `实时`：最多每秒刷新一次，只表示盘中观察数据；
- `稳`：只读取完成 K 线、后端分钟决策或 HTTP 概览；
- 红色表示向上或偏强，绿色表示向下或偏弱，灰/黄色表示中性、等待或缺失；
- 原型中的数字均为布局示例，不作为数据验收证据。

## 3. 四组指标语义

### 3.1 趋势 / 位置

第一行显示通道，第二行显示控制线距离与当日平均成交成本偏离。

#### 通道

来源：HTTP 概览中的 15m、30m 图表，只使用最近完成 K 线。

- 两个周期均满足 `close > ma5 > ma10 > ma20`：`上升通道`；
- 两个周期均满足 `close < ma5 < ma10 < ma20`：`下降通道`；
- 两个周期均有数据但不同向：`震荡/过渡`；
- 只有一个周期可用：`待确认`；
- 均不可用：`--`。

#### 控制线距离

来源：15m 快照的 `price_to_line_pct` 和 `line_role`；15m 缺失时仅回退到 30m。不得继续回退到 5m。

显示示例：`控制 +0.82%`。悬停或详情可继续看到线角色与周期，列表不增加第三行。

#### 当日平均成交成本偏离

来源：`minute_decision.daily_summary.vwap_distance_pct`。页面文案使用“成本”，不直接裸露 VWAP 术语。

显示示例：`成本 +0.17%`。字段缺失或非有限数时显示 `--`，不得补零或沿用另一只股票的数据。

### 3.2 动能 / 涨速

第一行显示实时 1m 涨速，第二行显示稳定的 5m/15m 动能。

#### 1m 涨速

来源：WebSocket `candlestick` 当前 1 分钟 K 线。

公式：

```text
(current_1m_close - current_1m_open) / current_1m_open * 100
```

它可以随盘中行情变化，必须带 `实时` 标识；它只是观察值，不得参与正式信号计算。K 线缺失、延迟或开盘价无效时显示 `--`。

#### 5m / 15m 动能

来源：各周期最近两根完成 K 线的收盘价。

公式：

```text
(latest_completed_close - previous_completed_close)
/ previous_completed_close * 100
```

形成中的 K 线不得参与。方向箭头与百分比必须来自同一个有符号值，避免出现两个重复的无标签数字。

### 3.3 量价 / 资金

第一行显示量比和实时 1m 量速，第二行显示主动买入占比和五档盘口压力。

#### 量比

来源：控制周期快照的 `volume_ratio_20`，优先 15m、缺失时回退 30m。属于稳定字段。

#### 1m 量速

来源：

- 当前 WebSocket 1m K 线成交量；
- 最近 12 根完成 5m K 线成交量。

基准每分钟成交量：

```text
average(last_12_completed_5m_volume) / 5
```

当前分钟投影成交量：

```text
current_1m_volume * 60 / elapsed_seconds
```

量速：

```text
projected_current_1m_volume / baseline_per_minute_volume
```

约束：

- 当前分钟开始不足 20 秒时显示 `--`，避免开头噪声；
- `elapsed_seconds` 最大按 60 计算；
- 完成 5m K 线不足 12 根、分母为零、当前 K 线延迟或时间戳不属于当前分钟时显示 `--`；
- 量速带 `实时` 标识，只作观察，不参与正式信号。

#### 主动买入占比

来源：`intraday_capital.total_in` 与 `total_out`。

公式：

```text
total_in / (total_in + total_out) * 100
```

仅当 `quality == COMPLETE` 且分母大于零时显示，否则显示 `未确认`。

#### 五档盘口压力

WebSocket 当前页订阅的 `depthLevels` 从 1 调整为 5。

公式：

```text
(sum(bid_volume_1_to_5) - sum(ask_volume_1_to_5))
/ (sum(bid_volume_1_to_5) + sum(ask_volume_1_to_5)) * 100
```

正值表示买盘挂单量较强，负值表示卖盘挂单量较强。必须至少有一档有效买盘和一档有效卖盘，且总量大于零；否则显示 `--`。盘口压力带 `实时` 标识，不得被解释为正式买卖信号。

### 3.4 突破 / 风险

第一行显示距当日高点和低点，第二行显示 ATR14 与后端确认周期。

#### 距当日高低点

来源：WebSocket quote 的 `lastDone`、`high`、`low`。

公式：

```text
距日高 = max(high - price, 0) / price * 100
距日低 = max(price - low, 0) / price * 100
```

显示示例：`高 0.7% · 低 4.2%`。任一字段无效时对应位置显示 `--`。这是距离描述，不得在前端据此生成“临近突破”“买入”等建议文案。

#### ATR14

来源：15m 图表中的完成 K 线。只用于描述短线波动风险。

对每根 K 线计算：

```text
TR = max(
  high - low,
  abs(high - previous_close),
  abs(low - previous_close)
)
```

取最近 14 个有效 TR 的算术平均值，并换算为最近完成收盘价的百分比：

```text
ATR14_pct = average(last_14_TR) / latest_completed_close * 100
```

有效完成 K 线不足 15 根时显示 `--`。形成中的 K 线不得参与。

#### 确认周期

来源：后端 `minute_decision.dominant_timeframe` 与 `confirmation_timeframes`。

后端正式主确认周期只有 15m 和 30m，因此显示值只能是：

- `0/2`：没有主周期同向；
- `1/2`：只有一个主周期同向；
- `2/2`：15m 和 30m 同向。

视觉原型中的 `2/3` 是布局占位，开发版必须修正为上述后端真实分母，不得虚构第三个周期。

若存在 `minute_decision.risk_warning`，列表可在该组显示后端返回的简短风险标题；前端不得根据距离、ATR 或盘口自行编造风险/突破结论。

## 4. 正式信号边界

买卖信号列维持现有权威顺序：

1. 最新持久化通知；
2. 没有正式通知时，可显示完成 K 线 turning signal 的 `WARNING`；
3. `FAILED` 或 `FALSE_BREAKOUT` 不得显示为可操作信号；
4. 数据延迟时不得把预警升级为正式信号；
5. WebSocket 实时指标变化不得改变信号列。

正式信号继续显示发生时间，并保持到后端返回新的失效状态或相反正式信号。

## 5. 刷新与延迟

- WebSocket quote、depth、candlestick 继续在内存中逐条合并，但 React 可见状态最多每秒发布一次；
- HTTP 概览和通知继续每 15 秒刷新；
- 实时指标只随 WebSocket 可见快照变化；
- 稳定指标只随后端完成分钟快照、分钟决策或持久化通知变化；
- quote/candlestick/depth 各自延迟时，其依赖的实时指标显示 `--` 或延迟状态，不使用旧实时值伪装当前值；
- 行情或分析整体延迟超过现有阈值时，行级“数据延迟”提示继续生效。

## 6. 数据缺失与降级

所有组合字段独立降级：

- 缺少某一个子指标时，只将该子指标显示为 `--`；
- 不因一个实时字段缺失而隐藏整组稳定字段；
- 不用零值代替缺失；
- 不跨股票、跨交易日或跨周期沿用数据；
- 休市时稳定字段可继续显示最后一个有效后端快照，实时字段显示休市/不可用状态。

## 7. 实现范围

预计生产代码改动仅限前端：

- `frontend/src/components/dow-monitor/monitorListPresentation.ts`
- `frontend/src/components/dow-monitor/DowMonitorList.tsx`
- `frontend/src/pages/DowMonitor.tsx`
- 必要时补充 `frontend/src/components/dow-monitor/types.ts`

不修改：

- 道氏趋势线算法；
- 后端分钟决策算法；
- 正式通知生成与失效逻辑；
- WebSocket 网关消息格式；
- 分页大小与详情展开行为。

## 8. 规格权威与追踪计划

现有权威规格 `docs/specs/dow-monitor-list-websocket.md` 的
`REQ-DOW-MONITOR-LIST-REALTIME-001` 将 WebSocket 可见覆盖范围限定为价格、涨跌幅和
mini 趋势线。本设计是在用户后续明确批准下对该范围的扩展，不得让新旧文字同时保持冲突。

实施前应直接扩展该现有权威规格，并在同一文件中明确：

- WebSocket 还可驱动带 `实时` 标识的描述性观察指标；
- 这些观察指标不属于后端决策字段；
- `REQ-DOW-MONITOR-LIST-SIGNAL-STABILITY-001` 的信号稳定边界保持不变。

在该权威规格中新增以下稳定需求 ID：

- `REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001`
- `REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001`
- `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001`
- `REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001`

同时更新该规格在 `docs/spec-index.yaml` 中的 requirements 列表，并更新：

- `docs/traceability.yaml`
- 语义验收记录；
- 独立需求到证据复核；
- `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

## 9. 测试与验收

### 可执行测试

先写失败测试，再实现：

1. 1m 涨速只使用当前 1m 开收盘；
2. 5m/15m 动能排除形成中 K 线；
3. 量速不足 20 秒、历史不足或延迟时降级；
4. 五档盘口压力使用五档合计而非一档；
5. ATR14 排除形成中 K 线并正确处理跳空 TR；
6. 距日高低公式与缺失字段降级；
7. 确认周期只显示 0/2、1/2、2/2；
8. VWAP 成本偏离缺失时不补零；
9. 任意 WebSocket 更新不改变正式信号；
10. 页面订阅当前页启用股票并请求 `depthLevels = 5`；
11. 列标题、两行结构、每页 20 只、详情开合与内部横向滚动保持正确。

### 语义验收

本地测试和构建通过后，生产发布按以下顺序验收：

1. 3018 健康、容器无重启、静态包为新 revision；
2. 监控股票清单发布前后逐字节一致；
3. A 股、港股、美股各抽取至少一只当前页股票；
4. WebSocket 确认订阅 quote、candlestick 与五档 depth；
5. 用原始 quote/candlestick/depth 手工复算至少一只股票的实时指标；
6. 用完成 K 线和分钟决策手工复算至少一只股票的稳定指标；
7. 连续观察实时指标变化时，正式信号列不随之跳动；
8. 数据缺失或延迟样例明确显示 `--`/延迟，不显示伪造零值；
9. 点击同一只股票可展开再收拢详情；
10. 需求到实现、测试和验收证据完成独立复核。

通过截图或 golden 只能证明外观，不得替代上述数据语义验收。
