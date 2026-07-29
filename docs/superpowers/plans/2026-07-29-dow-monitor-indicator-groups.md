# 趋势监控四组指标列表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已确认的四组组合指标接入趋势监控列表，在保持每页 20 只、正式信号稳定和详情开合行为不变的前提下，展示可审计的实时与完成 K 线指标。

**Architecture:** 继续由 `monitorListPresentation.ts` 把 HTTP 概览和当前页 WebSocket 状态转换为单一行视图模型，`DowMonitorList.tsx` 只负责两行组合列渲染。实时观察值逐字段检查数据集延迟，稳定值只使用完成 K 线和后端分钟决策；页面把 depth 订阅档位从 1 提升到 5，但不改变 WebSocket 消息格式或正式信号来源。

**Tech Stack:** React 19、TypeScript、Vitest、Testing Library、Tailwind CSS、FastAPI WebSocket 网关、pytest、pnpm、Docker。

## Global Constraints

- 每页固定 20 只股票；列表内部可以横向滚动，页面视口不得横向溢出。
- 四组组合列固定为“趋势 / 位置、动能 / 涨速、量价 / 资金、突破 / 风险”，每组最多两行。
- WebSocket 可驱动带 `实时` 标识的描述性观察值，但不得生成、清除、翻转或升级正式买卖信号。
- 正式买卖信号只来自持久化通知，或没有正式通知时来自后端完成 K 线 turning signal 的 `WARNING`。
- `FAILED`、`FALSE_BREAKOUT` 和延迟状态不得显示为新的可操作信号。
- 稳定字段只使用完成 K 线、后端分钟决策或持久化通知；形成中的 K 线不得参与 5m/15m 动能、通道或 ATR14。
- 指标缺失时显示 `--` 或“未确认”，不得补零、跨股票复用或跨交易日沿用实时值。
- 确认周期分母固定为后端真实的两个主周期 15m/30m，只显示 `0/2、1/2、2/2`。
- 实施必须先更新权威规格、稳定需求 ID 和 `docs/traceability.yaml`，再修改生产代码。
- 修改趋势监控、WebSocket depth 档位、静态包或发布流程时，同步更新 `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`。
- golden、截图和构建成功不能替代原始数据到指标值的语义验收。

---

## File Structure

### Production files

- `frontend/src/components/dow-monitor/monitorListPresentation.ts`
  - 唯一职责：把概览、通知和实时状态转换为列表行视图模型，包含所有公式和缺失降级。
- `frontend/src/components/dow-monitor/DowMonitorList.tsx`
  - 唯一职责：渲染九列高密度表格、实时/稳态标识和现有交互。
- `frontend/src/pages/DowMonitor.tsx`
  - 唯一职责：市场/分页/筛选状态和当前页 WebSocket 订阅；将 `depthLevels` 设为 5。

### Test files

- `frontend/src/components/dow-monitor/monitorListPresentation.test.ts`
  - 纯函数公式、完成 K 线过滤、延迟和信号边界。
- `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`
  - 四组列、两行结构、标识、缺失状态、mini 线和操作。
- `frontend/src/pages/DowMonitor.test.tsx`
  - 每页 20 只、市场/分页、五档订阅、实时值与正式信号隔离。
- `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py`
  - 权威规格、追踪条目和前端行为套件入口。

### Specification and evidence files

- `docs/specs/dow-monitor-list-websocket.md`
- `docs/spec-index.yaml`
- `docs/traceability.yaml`
- `docs/acceptance/dow-monitor-indicator-groups.md`
- `docs/reviews/dow-monitor-indicator-groups.md`
- `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

---

### Task 1: 固化权威规格与追踪关系

**Files:**

- Modify: `docs/specs/dow-monitor-list-websocket.md`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Create: `docs/acceptance/dow-monitor-indicator-groups.md`
- Create: `docs/reviews/dow-monitor-indicator-groups.md`
- Modify: `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py`

**Interfaces:**

- Consumes: 已批准设计 `docs/superpowers/specs/2026-07-29-dow-monitor-indicator-groups-design.md`。
- Produces: 四个稳定需求 ID，以及每个 ID 对应的实现、可执行测试和验收文件路径。

- [ ] **Step 1: 写入失败的规格追踪测试**

在 `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py` 增加：

```python
import yaml


GROUP_REQUIREMENTS = {
    "REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001",
    "REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001",
    "REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001",
    "REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001",
}


def test_grouped_indicator_requirements_are_authoritative_and_traceable() -> None:
    index = yaml.safe_load((ROOT / "docs/spec-index.yaml").read_text(encoding="utf-8"))
    traceability = yaml.safe_load(
        (ROOT / "docs/traceability.yaml").read_text(encoding="utf-8")
    )
    specification = next(
        item
        for item in index["specifications"]
        if item["id"] == "USER-20260729-DOW-MONITOR-LIST-WEBSOCKET"
    )
    assert GROUP_REQUIREMENTS <= set(specification["requirements"])

    entries = {
        item["id"]: item
        for item in traceability["requirements"]
        if item["id"] in GROUP_REQUIREMENTS
    }
    assert set(entries) == GROUP_REQUIREMENTS
    for requirement_id, entry in entries.items():
        assert entry["specification"] == specification["id"]
        assert entry["implementation"]
        assert entry["tests"]
        assert entry["acceptance"]
        for evidence in entry["acceptance"]:
            assert (ROOT / evidence["path"]).is_file(), requirement_id
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py::test_grouped_indicator_requirements_are_authoritative_and_traceable -q
```

Expected: FAIL because the four requirement IDs are not yet present in `docs/spec-index.yaml`.

- [ ] **Step 3: 扩展现有权威规格**

在 `docs/specs/dow-monitor-list-websocket.md` 中明确替换旧的“WebSocket 只覆盖价格、涨跌幅和 mini 线”的限制，增加四个需求：

```markdown
## REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001

列表必须用四个两行组合列替换原先分散的通道、控制线、动能、量比和主动资金列。
列顺序固定为：股票、价格/涨跌、日内趋势、趋势/位置、动能/涨速、量价/资金、
突破/风险、买卖信号、操作。每页仍为 20 只，操作列仍只显示“查看详情”。

## REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001

1m 涨速、1m 量速、五档盘口压力和距日高低可以由当前页 `/ws/realtime`
驱动，必须标记为“实时”，逐字段检查 candlestick/depth/quote 延迟。
这些字段只用于观察，不得改变后端正式信号。

## REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001

通道、控制线、成本偏离、5m/15m 动能、量比、主动资金、ATR14 和确认周期
必须只使用完成 K 线或后端分钟决策。确认周期分母固定为 15m/30m 两个周期。

## REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001

任意 quote、depth 或形成中 1m K 线更新不得生成、清除、翻转或升级买卖信号。
实时字段缺失或延迟时独立显示 `--`，不得污染稳定字段或持久化正式信号。
```

- [ ] **Step 4: 更新索引、追踪和未验收状态文件**

将四个 ID 添加到 `USER-20260729-DOW-MONITOR-LIST-WEBSOCKET.requirements`。

在 `docs/traceability.yaml` 中为四个 ID 建立条目。实现路径使用本计划的三个生产文件；测试路径使用三个前端测试和规格契约测试；验收路径统一指向：

```yaml
acceptance:
  - {path: docs/acceptance/dow-monitor-indicator-groups.md, type: semantic-acceptance}
  - {path: docs/reviews/dow-monitor-indicator-groups.md, type: independent-review}
```

创建两个证据文件并明确写为“实施中，尚未通过语义验收”，列出四个需求 ID 和本计划第 6 项的验收条件。不得提前写“通过”。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py::test_grouped_indicator_requirements_are_authoritative_and_traceable -q
```

Expected: PASS.

- [ ] **Step 6: 提交规格基线**

```powershell
git add docs/specs/dow-monitor-list-websocket.md docs/spec-index.yaml docs/traceability.yaml docs/acceptance/dow-monitor-indicator-groups.md docs/reviews/dow-monitor-indicator-groups.md tests/spec_contracts/test_dow_monitor_list_websocket_contract.py
git commit -m "docs(dow-monitor): specify grouped indicator semantics"
```

---

### Task 2: 用完成 K 线实现稳态指标

**Files:**

- Modify: `frontend/src/components/dow-monitor/monitorListPresentation.test.ts`
- Modify: `frontend/src/components/dow-monitor/monitorListPresentation.ts`

**Interfaces:**

- Consumes: `DowMonitorOverviewSymbol.states`、`minute_decision.daily_summary`、`minute_decision.risk_warning`。
- Produces: `MonitorRowPresentation.trendPosition`、`momentumSpeed.momentum5m/15m`、`volumeFunds.relativeVolume/activeFunds`、`breakoutRisk.atr14Pct/confirmedTimeframes/totalTimeframes/riskTitle`。

- [ ] **Step 1: 扩充行视图模型的类型契约测试**

将测试 fixture 的 `minute_decision` 设置为真实主周期语义：

```ts
dominant_timeframe: '15m',
confirmation_timeframes: ['30m'],
daily_summary: {
  as_of_minute: '2026-07-29T09:35:00+08:00',
  direction: 'BULLISH',
  direction_label: '偏涨',
  action: 'WATCH_BUY',
  action_label: '买入观察',
  confidence: 72,
  phase_path: [],
  summary_text: '走势偏强',
  key_evidence: [],
  reversal_condition: '跌回控制线下方',
  data_status: 'COMPLETE',
  status_label: '数据完整',
  current_price: 10.5,
  vwap_price: 10.48,
  vwap_distance_pct: 0.19,
  input_event_ids: [],
},
```

增加断言：

```ts
expect(row.trendPosition.costDistancePct).toBe(0.19)
expect(row.breakoutRisk).toMatchObject({
  confirmedTimeframes: 2,
  totalTimeframes: 2,
})
```

- [ ] **Step 2: 写控制周期与 ATR14 的失败测试**

构造 16 根 15m 完成 K 线，其中最后 14 个 TR 可手算；另加一根 `FORMING` 大波动 K 线。断言：

```ts
expect(row.trendPosition.control?.timeframe).toBe('30m')
expect(row.breakoutRisk.atr14Pct).toBeCloseTo(expectedAtrPct, 6)
```

另构造只有 5m `price_to_line_pct` 的样例并断言：

```ts
expect(row.trendPosition.control).toBeNull()
expect(row.volumeFunds.relativeVolume).toBeNull()
```

这会锁定“15m 缺失只回退 30m，不回退 5m”。

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: FAIL because grouped view-model fields and ATR14 do not exist, and current control logic still falls back to 5m.

- [ ] **Step 4: 实现最小稳态视图模型**

在 `monitorListPresentation.ts` 中把控制周期改为：

```ts
const CONTROL_TIMEFRAMES: DowTimeframe[] = ['15m', '30m']
```

先提取复用类型，再用分组字段替换旧的分散字段：

```ts
export interface MonitorChannel {
  code: 'UP' | 'DOWN' | 'RANGE' | 'PENDING' | 'UNKNOWN'
  label: string
}

export interface MonitorControl {
  timeframe: DowTimeframe
  role: string
  distancePct: number
}

export interface MonitorRelativeVolume {
  timeframe: DowTimeframe
  ratio: number
}

export interface MonitorActiveFunds {
  confirmed: boolean
  buyRatioPct: number | null
}

trendPosition: {
  channel: MonitorChannel
  control: MonitorControl | null
  costDistancePct: number | null
}
momentumSpeed: {
  momentum1m: MonitorMomentum
  momentum5m: MonitorMomentum
  momentum15m: MonitorMomentum
}
volumeFunds: {
  relativeVolume: MonitorRelativeVolume | null
  volumeSpeed: number | null
  activeFunds: MonitorActiveFunds
  depthPressurePct: number | null
}
breakoutRisk: {
  toDayHighPct: number | null
  fromDayLowPct: number | null
  atr14Pct: number | null
  confirmedTimeframes: number
  totalTimeframes: 2
  riskTitle: string | null
}
```

新增纯函数：

```ts
function atr14Pct(state: DowMonitorTimeframeState | undefined): number | null {
  const bars = completedBars(state)
  if (bars.length < 15) return null
  const ranges = bars.slice(1).map((bar, index) => {
    const previousClose = bars[index].close
    return Math.max(
      bar.high - bar.low,
      Math.abs(bar.high - previousClose),
      Math.abs(bar.low - previousClose),
    )
  })
  const recent = ranges.slice(-14)
  const latestClose = bars.at(-1)?.close
  if (recent.length !== 14 || !finite(latestClose) || latestClose <= 0) return null
  return recent.reduce((sum, value) => sum + value, 0) / 14 / latestClose * 100
}
```

确认周期只计算 15m/30m：

```ts
function confirmedTimeframes(item: DowMonitorOverviewSymbol): number {
  const decision = item.minute_decision
  if (!decision || decision.direction === 'RANGE') return 0
  const values = new Set([
    decision.dominant_timeframe,
    ...decision.confirmation_timeframes,
  ])
  return ['15m', '30m'].filter(timeframe => values.has(timeframe as DowTimeframe)).length
}
```

成本偏离只接受有限数：

```ts
const costDistancePct = item.minute_decision?.daily_summary?.vwap_distance_pct
return finite(costDistancePct) ? costDistancePct : null
```

风险标题只透传后端：

```ts
const riskTitle = item.minute_decision?.risk_warning?.title?.trim() || null
```

保留现有正式信号函数，不改变其输入或优先级。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: PASS.

- [ ] **Step 6: 提交稳态指标**

```powershell
git add frontend/src/components/dow-monitor/monitorListPresentation.ts frontend/src/components/dow-monitor/monitorListPresentation.test.ts
git commit -m "feat(dow-monitor): derive stable grouped indicators"
```

---

### Task 3: 实现逐字段延迟保护的实时指标

**Files:**

- Modify: `frontend/src/components/dow-monitor/monitorListPresentation.test.ts`
- Modify: `frontend/src/components/dow-monitor/monitorListPresentation.ts`

**Interfaces:**

- Consumes: `RealtimeSymbolState.quote`、`depth`、`candlestick` 以及完成 5m K 线。
- Produces: `momentumSpeed.momentum1m`、`volumeFunds.volumeSpeed/depthPressurePct`、`breakoutRisk.toDayHighPct/fromDayLowPct`。

- [ ] **Step 1: 写 1m 涨速、日高低和五档压力失败测试**

构造实时状态：

```ts
const realtime = {
  symbol: '700.HK',
  streamId: 'stream-1',
  sequence: 10,
  eventAt: '2026-07-29T09:35:30+08:00',
  publishedAt: '2026-07-29T09:35:30+08:00',
  quote: {
    lastDone: 101,
    prevClose: 100,
    high: 102,
    low: 95,
    timestamp: '2026-07-29T09:35:30+08:00',
  },
  candlestick: {
    period: 'min_1' as const,
    timestamp: '2026-07-29T09:35:00+08:00',
    open: 100,
    close: 101,
    volume: 40,
  },
  depth: {
    bids: [
      { position: 1, volume: 100 },
      { position: 2, volume: 80 },
      { position: 3, volume: 60 },
      { position: 4, volume: 40 },
      { position: 5, volume: 20 },
    ],
    asks: [
      { position: 1, volume: 70 },
      { position: 2, volume: 60 },
      { position: 3, volume: 50 },
      { position: 4, volume: 40 },
      { position: 5, volume: 30 },
    ],
  },
  quoteDelayed: false,
  depthDelayed: false,
  candlestickDelayed: false,
}
```

断言：

```ts
expect(row.momentumSpeed.momentum1m.valuePct).toBeCloseTo(1)
expect(row.breakoutRisk.toDayHighPct).toBeCloseTo(100 / 101)
expect(row.breakoutRisk.fromDayLowPct).toBeCloseTo(600 / 101)
expect(row.volumeFunds.depthPressurePct).toBeCloseTo((300 - 250) / 550 * 100)
```

- [ ] **Step 2: 写量速窗口失败测试**

将最近 12 根完成 5m K 线成交量设为 500，当前 1m 在第 30 秒成交量为 40。基准每分钟为 100，当前分钟投影为 80，断言：

```ts
expect(row.volumeFunds.volumeSpeed).toBeCloseTo(0.8)
```

分别把当前时间改为第 10 秒、把完成 5m K 线减少到 11 根、把 `candlestickDelayed` 设为 `true`，断言三种情况均为 `null`。

- [ ] **Step 3: 写信号隔离失败测试**

使用同一个带正式 BUY 通知的概览，分别传入买盘占优和卖盘占优的 depth：

```ts
expect(bidHeavy.signal).toEqual(askHeavy.signal)
expect(bidHeavy.signal).toMatchObject({ level: 'CONFIRMED', side: 'BUY' })
```

- [ ] **Step 4: 运行测试并确认 RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: FAIL because the four realtime metrics do not exist.

- [ ] **Step 5: 实现实时指标**

1m 涨速：

```ts
function momentumFromPct(valuePct: number): MonitorMomentum {
  return {
    direction: Math.abs(valuePct) < 0.005 ? 'FLAT' : valuePct > 0 ? 'UP' : 'DOWN',
    valuePct,
  }
}

function realtimeMomentum1m(realtime: RealtimeSymbolState | undefined): MonitorMomentum {
  const candle = realtime?.candlestick
  if (
    realtime?.candlestickDelayed
    || !finite(candle?.open)
    || !finite(candle?.close)
    || candle.open <= 0
  ) return { direction: 'UNKNOWN', valuePct: null }
  return momentumFromPct((candle.close - candle.open) / candle.open * 100)
}
```

量速实现为：

```ts
function volumeSpeed(
  item: DowMonitorOverviewSymbol,
  realtime: RealtimeSymbolState | undefined,
  nowMs: number,
): number | null {
  const candle = realtime?.candlestick
  if (
    realtime?.candlestickDelayed
    || !candle
    || !finite(candle.volume)
    || candle.volume < 0
  ) return null

  const candleStart = timestampMs(candle.timestamp)
  if (candleStart == null) return null
  const elapsedSeconds = (nowMs - candleStart) / 1000
  if (elapsedSeconds < 20 || elapsedSeconds >= 75) return null

  const volumes = completedBars(item.states['5m'])
    .slice(-12)
    .map(bar => bar.volume)
  if (volumes.length !== 12 || volumes.some(value => !finite(value) || value < 0)) {
    return null
  }
  const baselinePerMinute = volumes.reduce((sum, value) => sum + value, 0) / 12 / 5
  if (baselinePerMinute <= 0) return null
  const projectedVolume = candle.volume * 60 / Math.min(elapsedSeconds, 60)
  return projectedVolume / baselinePerMinute
}
```

五档压力只累加前五个有效档位，并要求买卖两侧都存在：

```ts
function depthPressurePct(realtime: RealtimeSymbolState | undefined): number | null {
  if (realtime?.depthDelayed) return null
  const bids = realtime?.depth?.bids.slice(0, 5).map(level => level.volume)
    .filter(finite) ?? []
  const asks = realtime?.depth?.asks.slice(0, 5).map(level => level.volume)
    .filter(finite) ?? []
  if (bids.length === 0 || asks.length === 0) return null
  const bidVolume = bids.reduce((sum, value) => sum + value, 0)
  const askVolume = asks.reduce((sum, value) => sum + value, 0)
  const total = bidVolume + askVolume
  return total > 0 ? (bidVolume - askVolume) / total * 100 : null
}
```

量速使用设计文档的 20 秒门槛、12 根完成 5m K 线和 60 秒投影，不读取形成中 5m K 线。

日高低距离只依赖未延迟 quote：

```ts
function dayRangeDistances(realtime: RealtimeSymbolState | undefined) {
  const quote = realtime?.quote
  if (realtime?.quoteDelayed || !finite(quote?.lastDone) || quote.lastDone <= 0) {
    return { toDayHighPct: null, fromDayLowPct: null }
  }
  return {
    toDayHighPct: finite(quote.high)
      ? Math.max(quote.high - quote.lastDone, 0) / quote.lastDone * 100
      : null,
    fromDayLowPct: finite(quote.low)
      ? Math.max(quote.lastDone - quote.low, 0) / quote.lastDone * 100
      : null,
  }
}
```

- [ ] **Step 6: 运行测试并确认 GREEN**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: PASS.

- [ ] **Step 7: 提交实时指标**

```powershell
git add frontend/src/components/dow-monitor/monitorListPresentation.ts frontend/src/components/dow-monitor/monitorListPresentation.test.ts
git commit -m "feat(dow-monitor): derive realtime observation metrics"
```

---

### Task 4: 渲染四组两行组合列

**Files:**

- Modify: `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.tsx`

**Interfaces:**

- Consumes: Task 2 和 Task 3 产出的 `MonitorRowPresentation` 分组字段。
- Produces: 九列表格、每组两行、`实时`/`稳` 标识和原有操作行为。

- [ ] **Step 1: 改写列标题失败测试**

将旧的十个分散列标题断言替换为：

```ts
for (const heading of [
  '股票',
  '价格 / 涨跌',
  '日内趋势',
  '趋势 / 位置',
  '动能 / 涨速',
  '量价 / 资金',
  '突破 / 风险',
  '买卖信号',
  '操作',
]) {
  expect(screen.getByRole('columnheader', { name: new RegExp(heading) }))
    .toBeInTheDocument()
}
```

并断言旧标题不存在：

```ts
expect(screen.queryByRole('columnheader', { name: '通道' })).not.toBeInTheDocument()
expect(screen.queryByRole('columnheader', { name: '主动资金' })).not.toBeInTheDocument()
```

- [ ] **Step 2: 写两行字段和缺失降级失败测试**

为 fixture 补齐 Task 2/3 所需数据，断言页面出现：

```ts
expect(screen.getByText('成本 +0.19%')).toBeInTheDocument()
expect(screen.getByText('1m +1.00%')).toBeInTheDocument()
expect(screen.getByText('确认 2/2')).toBeInTheDocument()
expect(screen.getByText(/高 .*· 低/)).toBeInTheDocument()
expect(screen.getAllByText('实时').length).toBeGreaterThan(0)
expect(screen.getAllByText('稳').length).toBeGreaterThan(0)
```

再渲染字段缺失样例并断言不存在 `0.00%` 伪值，缺失位置为 `--` 或“未确认”。

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/DowMonitorList.test.tsx
```

Expected: FAIL because the current table still renders ten个分散列.

- [ ] **Step 4: 实现组合列 UI**

在 `DowMonitorList.tsx` 中：

- 把表格最小宽度调整到约 `1660px`；
- 保持外层 `max-w-full overflow-x-auto`，增加 `data-testid="dow-monitor-table-scroll"`；
- 将列头改为九列，并在四组列头下显示 9px 灰色字段说明；
- 每组第一行使用主值，第二行使用更小的等宽字体；
- `实时` 使用青色小标签，`稳` 使用灰色小标签；
- 正式信号列、管理按钮、查看详情、行选择和键盘行为保持原逻辑；
- 不渲染前端生成的“临近突破”或买卖建议。

格式函数保持有符号百分比：

```ts
function compactPercent(label: string, value: number | null): string {
  return value == null ? `${label} --` : `${label} ${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function distancePercent(label: string, value: number | null): string {
  return value == null ? `${label} --` : `${label} ${value.toFixed(2)}%`
}
```

四组列头使用明确的可访问名称：

```tsx
<th>
  趋势 / 位置
  <span className="block text-[9px] text-muted">通道 · 控制线 · 成本位置</span>
</th>
<th>
  动能 / 涨速
  <span className="block text-[9px] text-muted">1m 实时 · 5m/15m 稳态</span>
</th>
<th>
  量价 / 资金
  <span className="block text-[9px] text-muted">量比 · 量速 · 主买 · 五档</span>
</th>
<th>
  突破 / 风险
  <span className="block text-[9px] text-muted">日高低 · ATR · 周期确认</span>
</th>
```

单元格严格保持两行。以“突破 / 风险”为例：

```tsx
<td className="whitespace-nowrap px-3 py-2">
  <div className="flex items-center gap-2">
    <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">
      实时
    </span>
    <span>{distancePercent('高', row.breakoutRisk.toDayHighPct)}</span>
    <span>{distancePercent('低', row.breakoutRisk.fromDayLowPct)}</span>
  </div>
  <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
    <span>{compactPercent('ATR14', row.breakoutRisk.atr14Pct)}</span>
    <span>
      确认 {row.breakoutRisk.confirmedTimeframes}/{row.breakoutRisk.totalTimeframes}
    </span>
    {row.breakoutRisk.riskTitle && <span>{row.breakoutRisk.riskTitle}</span>}
  </div>
</td>
```

其余三组同样只读取行视图模型，不得再次调用公式或读取原始 API 字段：

```tsx
<td className="whitespace-nowrap px-3 py-2">
  <div className="flex items-center gap-2">
    <span className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
    <strong>{row.trendPosition.channel.label}</strong>
  </div>
  <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
    <span>{compactPercent('控制', row.trendPosition.control?.distancePct ?? null)}</span>
    <span>{compactPercent('成本', row.trendPosition.costDistancePct)}</span>
  </div>
</td>

<td className="whitespace-nowrap px-3 py-2">
  <div className="flex items-center gap-2">
    <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">
      实时
    </span>
    <strong>1m {momentumText(row.momentumSpeed.momentum1m)}</strong>
  </div>
  <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
    <span>5m {momentumText(row.momentumSpeed.momentum5m)}</span>
    <span>15m {momentumText(row.momentumSpeed.momentum15m)}</span>
  </div>
</td>

<td className="whitespace-nowrap px-3 py-2">
  <div className="flex items-center gap-2">
    <span className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
    <strong>
      量比 {row.volumeFunds.relativeVolume
        ? `${row.volumeFunds.relativeVolume.ratio.toFixed(2)}×`
        : '--'}
    </strong>
    <span>
      量速 {row.volumeFunds.volumeSpeed == null
        ? '--'
        : `${row.volumeFunds.volumeSpeed.toFixed(2)}×`}
    </span>
  </div>
  <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
    <span>
      主买 {row.volumeFunds.activeFunds.buyRatioPct == null
        ? '未确认'
        : `${row.volumeFunds.activeFunds.buyRatioPct.toFixed(0)}%`}
    </span>
    <span>{compactPercent('五档', row.volumeFunds.depthPressurePct)}</span>
  </div>
</td>
```

- [ ] **Step 5: 运行组件测试并确认 GREEN**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/DowMonitorList.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx
```

Expected: PASS，且详情开合测试不变。

- [ ] **Step 6: 提交组合列表**

```powershell
git add frontend/src/components/dow-monitor/DowMonitorList.tsx frontend/src/components/dow-monitor/DowMonitorList.test.tsx
git commit -m "feat(dow-monitor): render grouped indicator columns"
```

---

### Task 5: 订阅五档盘口并验证页面边界

**Files:**

- Modify: `frontend/src/pages/DowMonitor.test.tsx`
- Modify: `frontend/src/pages/DowMonitor.tsx`

**Interfaces:**

- Consumes: `useRealtimeMarketData(symbols, datasets, depthLevels)`。
- Produces: 当前市场、当前页、已启用股票的 quote/depth/candlestick 五档订阅。

- [ ] **Step 1: 将当前页订阅测试改为五档**

把两个现有断言的最后一个参数从 `1` 改为 `5`：

```ts
expect(realtimeMocks.useRealtimeMarketData).toHaveBeenLastCalledWith(
  Array.from({ length: 20 }, (_, index) => `${index + 1}.HK`),
  ['quote', 'depth', 'candlestick'],
  5,
)
```

翻页样例同样要求第二页 20 只和 `5`。

- [ ] **Step 2: 扩展实时不改正式信号测试**

在现有“updates real-time price without changing the persisted signal”样例中加入 candle 和五档 depth，rerender 后同时断言：

```ts
expect(screen.getByText('1m +1.00%')).toBeInTheDocument()
expect(screen.getAllByText('买入确认').length).toBeGreaterThan(0)
```

再把 depth 买卖量反转并 rerender，正式信号数量和方向必须不变。

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitor.test.tsx
```

Expected: FAIL because production page still passes `depthLevels = 1`.

- [ ] **Step 4: 实现五档订阅**

在 `DowMonitor.tsx` 中只改当前调用的第三个参数：

```ts
const realtime = useRealtimeMarketData(
  realtimeSymbols,
  ['quote', 'depth', 'candlestick'],
  5,
)
```

不得扩大到非当前页或已停用股票。

- [ ] **Step 5: 运行页面和实时客户端测试**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitor.test.tsx src/lib/realtimeMarketData.test.ts
```

Expected: PASS；客户端仍将多个消费者的档位取最大值并限制在 1–10。

- [ ] **Step 6: 提交五档订阅**

```powershell
git add frontend/src/pages/DowMonitor.tsx frontend/src/pages/DowMonitor.test.tsx
git commit -m "feat(dow-monitor): subscribe to five-level depth"
```

---

### Task 6: 完整验证、独立复核、运行手册和生产发布

**Files:**

- Modify: `docs/acceptance/dow-monitor-indicator-groups.md`
- Modify: `docs/reviews/dow-monitor-indicator-groups.md`
- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`
- Create temporarily: `output/release/Dockerfile`

**Interfaces:**

- Consumes: Tasks 1–5 的提交和 `frontend/dist`。
- Produces: 可复核的测试结果、生产镜像、原始数据复算证据、回滚镜像和更新后的运行手册。

- [ ] **Step 1: 运行聚焦测试**

```powershell
pnpm --dir frontend exec vitest run `
  src/components/dow-monitor/monitorListPresentation.test.ts `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/components/dow-monitor/DowMonitorDetailPanel.test.tsx `
  src/pages/DowMonitor.test.tsx `
  src/lib/realtimeMarketData.test.ts

python -m pytest `
  tests/spec_contracts/test_dow_monitor_list_websocket_contract.py `
  tests/spec_contracts/test_realtime_frontend_contract.py -q

Push-Location backend
python -m pytest tests/test_realtime_websocket.py -q
Pop-Location
```

Expected: all selected tests PASS.

- [ ] **Step 2: 运行构建和规格守卫**

```powershell
pnpm --dir frontend build
python scripts/check_spec_compliance.py
```

Expected:

- frontend build exits 0；
- 规格守卫不新增与四个新需求 ID 有关的问题；
- 若仍返回非零，只允许已记录的两个基线问题：过期的
  `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001`，以及旧
  `REQ-DOW-MONITOR-DETAIL-TOGGLE-LAYOUT-001` 测试路径范围问题。

- [ ] **Step 3: 在本地浏览器做语义检查**

启动本地构建预览：

```powershell
Start-Process -FilePath "C:\Program Files\nodejs\pnpm.cmd" `
  -ArgumentList "--dir frontend preview --host 127.0.0.1 --port 4173" `
  -WorkingDirectory "E:\my_project\.worktrees\tickflow-monitor-list-v2" `
  -WindowStyle Hidden
npx --yes --package @playwright/cli playwright-cli -s=indicator-groups open "http://127.0.0.1:4173/dow-monitor?market=hk"
npx --yes --package @playwright/cli playwright-cli -s=indicator-groups resize 1800 1080
npx --yes --package @playwright/cli playwright-cli -s=indicator-groups snapshot
```

使用 Playwright 在 1800×1080 检查：

1. 四组列均为两行；
2. 表格内部横向滚动，`document.documentElement.scrollWidth === clientWidth`；
3. 每页仍有 20 个 body row；
4. mini 图只有一个 polyline；
5. 同一只股票详情第一次展开、第二次收拢；
6. 缺失实时字段显示 `--`；
7. 观察实时 depth/candle 变化时，正式信号文案和时间不变化。

截图保存到 `output/playwright/dow-monitor-indicator-groups-acceptance.png`，只作为外观辅助证据。

- [ ] **Step 4: 填写语义验收与独立复核**

在 `docs/acceptance/dow-monitor-indicator-groups.md` 记录：

- 每个测试命令及通过数量；
- 本地构建资产名和 SHA-256；
- 至少一只股票的原始 quote/candle/depth 与手工公式结果；
- 至少一只股票的完成 15m K 线 ATR14、5m/15m 动能和 2/2 确认复算；
- 信号列在两组相反 depth 快照下完全一致。

在 `docs/reviews/dow-monitor-indicator-groups.md` 独立逐项检查四个需求 ID，表格列为：

```markdown
| Requirement | Implementation | Executable test | Semantic evidence | Result |
```

每个结果只能在实现、测试和原始数据复算都存在时写 `PASS`。

- [ ] **Step 5: 更新 Obsidian 运行手册**

在运行手册增加：

- 四组列和每个指标的来源/公式；
- `depthLevels = 5`；
- WebSocket 实时观察层与正式信号层边界；
- 1m 量速 20 秒门槛和 12 根完成 5m 基准；
- 确认周期真实分母为 2；
- 新测试命令、静态包检查和生产手工复算步骤。

- [ ] **Step 6: 提交验证文档**

```powershell
git add docs/acceptance/dow-monitor-indicator-groups.md docs/reviews/dow-monitor-indicator-groups.md
git commit -m "docs(dow-monitor): record grouped indicator acceptance"
```

运行手册位于外部 Obsidian 工作区，不加入当前 Git 提交；在验收记录中注明其更新时间。

- [ ] **Step 7: 建立唯一候选镜像**

用 `apply_patch` 创建 `output/release/Dockerfile`：

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY dist/ /app/static/
```

然后执行：

```powershell
$releaseStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$candidateTag = "tickflow-stock-panel-app:dow-monitor-indicator-groups-$releaseStamp"
$currentImage = ssh 192.168.10.28 "docker inspect -f '{{.Config.Image}}' TickFlow_Stock_Panel"
$remoteBuildDir = "/tmp/tickflow-dow-monitor-indicator-groups-$releaseStamp"

ssh 192.168.10.28 "mkdir -p $remoteBuildDir/dist"
scp -r frontend/dist/* "192.168.10.28:${remoteBuildDir}/dist/"
scp output/release/Dockerfile "192.168.10.28:${remoteBuildDir}/Dockerfile"
ssh 192.168.10.28 "cd $remoteBuildDir && docker build --build-arg BASE_IMAGE=$currentImage -t $candidateTag ."
ssh 192.168.10.28 "docker image inspect -f '{{.Id}}' $candidateTag"
```

Expected: a unique image ID different from the currently running image.

- [ ] **Step 8: 备份并切换生产容器**

```powershell
$backupDir = "/home/alwin/backups/dow-monitor-indicator-groups-predeploy-$releaseStamp"
ssh 192.168.10.28 "mkdir -p $backupDir && docker cp TickFlow_Stock_Panel:/app/data/user_data/dow_monitor_symbols.json $backupDir/dow_monitor_symbols.json && docker inspect TickFlow_Stock_Panel > $backupDir/container-inspect.json"
$symbolsBefore = ssh 192.168.10.28 "docker exec TickFlow_Stock_Panel sha256sum /app/data/user_data/dow_monitor_symbols.json"

ssh 192.168.10.28 "cd /home/alwin/apps/tickflow-stock-panel && TICKFLOW_IMAGE=$candidateTag docker compose up -d --no-build"
```

不得修改或替换生产数据挂载。

- [ ] **Step 9: 验证运行态与股票清单不变**

```powershell
ssh 192.168.10.28 "docker inspect -f '{{.Config.Image}} {{.State.Status}} {{.RestartCount}}' TickFlow_Stock_Panel"
ssh 192.168.10.28 "curl -fsS http://127.0.0.1:3018/health"
$symbolsAfter = ssh 192.168.10.28 "docker exec TickFlow_Stock_Panel sha256sum /app/data/user_data/dow_monitor_symbols.json"
if ($symbolsBefore -ne $symbolsAfter) { throw 'dow_monitor_symbols.json changed during deployment' }
ssh 192.168.10.28 "docker logs --since 5m TickFlow_Stock_Panel 2>&1 | grep -E 'ERROR|CRITICAL|Traceback' || true"
```

Expected:

- candidate image is running；
- restart count is 0；
- health returns 200；
- monitor symbol file hash is unchanged；
- deployment-window logs have no new error.

- [ ] **Step 10: 生产 WebSocket 与原始数据复算**

使用生产 Origin 连接 `/ws/realtime`，订阅一只当前页股票：

```json
{
  "action": "subscribe",
  "symbols": ["1347.HK"],
  "datasets": ["quote", "depth", "candlestick"],
  "depthLevels": 5
}
```

保留一条同时含 quote、depth、candlestick 的 snapshot，并手工复算：

- `(close-open)/open*100`；
- 五档 `(bid-ask)/(bid+ask)*100`；
- 距日高和距日低。

从已登录生产页面读取 overview，手工复算同一只股票的：

- 完成 K 线 5m/15m 动能；
- 15m ATR14；
- 15m/30m 确认数；
- `daily_summary.vwap_distance_pct`。

页面显示值必须与按显示精度四舍五入后的结果一致。

- [ ] **Step 11: 完成生产浏览器验收和回滚信息**

在 A 股、港股、美股各检查至少一只股票：

- 九列标题与两行组合布局；
- 实时和稳态标识；
- 20 只分页；
- 详情开合；
- 信号时间不随实时盘口跳动；
- 页面无横向溢出和控制台错误。

把候选 tag、镜像 ID、回滚镜像 `$currentImage`、备份目录、静态资产名、SHA-256 和抽样股票值补入 acceptance、review 和运行手册。若任一语义复算失败，立即切回 `$currentImage`，保留候选镜像供排查，不声明完成。
