// strategyCheck 纯逻辑单测 — bun 直跑: bun src/pages/backtest/components/strategyCheck.test.ts
// 覆盖完成计数、失败状态、未持久化主 Run 与跨 Run 状态隔离; 不测文案拼装或组件 plumbing。

import {
  applyStrategyCheckStatus,
  applyStrategyCheckStatusForRun,
  emptyStrategyCheckItems,
  emptyStrategyCheckRunState,
  strategyCheckItemsForRun,
  summarizeStrategyCheck,
  type StrategyCheckItems,
} from './strategyCheck.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function withStatuses(
  patch: Partial<StrategyCheckItems>,
): StrategyCheckItems {
  return { ...emptyStrategyCheckItems(), ...patch }
}

function testIdleSummaryHasZeroCompleted(): void {
  const summary = summarizeStrategyCheck(emptyStrategyCheckItems(), true)
  assert(summary.total === 4, '体检共 4 项诊断')
  assert(summary.completedCount === 0, '初始完成计数为 0')
  assert(summary.failedCount === 0, '初始无失败项')
  assert(summary.runningCount === 0, '初始无运行中项')
  assert(summary.idleCount === 4, '初始 4 项均为待运行')
  assert(summary.failedItems.length === 0, '初始失败列表为空')
  assert(summary.persisted === true, '传入已固化时应原样透出')
}

function testCompletedCountTracksFinishedItems(): void {
  let items = emptyStrategyCheckItems()
  items = applyStrategyCheckStatus(items, 'robustness', 'completed')
  items = applyStrategyCheckStatus(items, 'regime', 'running')
  items = applyStrategyCheckStatus(items, 'cost_sensitivity', 'completed')
  const summary = summarizeStrategyCheck(items, true)
  assert(summary.completedCount === 2, '两项已完成后计数为 2')
  assert(summary.runningCount === 1, '一项运行中')
  assert(summary.idleCount === 1, '一项仍待运行')
  assert(summary.failedCount === 0, '无失败时失败计数为 0')
}

function testFailedStatusCollectsError(): void {
  const items = applyStrategyCheckStatus(
    emptyStrategyCheckItems(),
    'style',
    'failed',
    '风格归因请求超时',
  )
  const summary = summarizeStrategyCheck(items, true)
  assert(summary.failedCount === 1, '一项失败计入失败数')
  assert(summary.completedCount === 0, '失败不计入完成')
  assert(summary.failedItems.length === 1, '失败列表长度为 1')
  assert(summary.failedItems[0].id === 'style', '失败项 id 为 style')
  assert(summary.failedItems[0].error === '风格归因请求超时', '失败项保留上报错误')
}

function testFailedWithoutMessageStillCounts(): void {
  const items = applyStrategyCheckStatus(emptyStrategyCheckItems(), 'regime', 'failed')
  const summary = summarizeStrategyCheck(items, true)
  assert(summary.failedCount === 1, '无错误文案的失败仍计数')
  assert(summary.failedItems[0].id === 'regime', '失败项 id 为 regime')
  assert(summary.failedItems[0].error == null, '未上报错误时不伪造文案')
}

function testUnpersistedMainRunIsVisible(): void {
  const summary = summarizeStrategyCheck(emptyStrategyCheckItems(), false)
  assert(summary.persisted === false, '主 Run 未固化必须出现在汇总里')
  assert(summary.completedCount === 0, '未固化不改变完成计数')
}

function testApplyDoesNotMutatePreviousItems(): void {
  const before = emptyStrategyCheckItems()
  const after = applyStrategyCheckStatus(before, 'robustness', 'running')
  assert(before.robustness.status === 'idle', 'apply 不得改写入参')
  assert(after.robustness.status === 'running', '返回值更新对应项')
  assert(after.regime.status === 'idle', '其余项保持待运行')
}

function testRerunClearsPreviousError(): void {
  const failed = applyStrategyCheckStatus(
    emptyStrategyCheckItems(),
    'cost_sensitivity',
    'failed',
    '成本敏感性分析失败',
  )
  const running = applyStrategyCheckStatus(failed, 'cost_sensitivity', 'running')
  assert(running.cost_sensitivity.status === 'running', '失败后可再次进入运行中')
  assert(running.cost_sensitivity.error == null, '重新运行须清掉旧错误')
}

function testMixedStatusesDoNotInventPassReject(): void {
  const items = withStatuses({
    robustness: { status: 'completed' },
    regime: { status: 'failed', error: '样本不足' },
    cost_sensitivity: { status: 'running' },
    style: { status: 'idle' },
  })
  const summary = summarizeStrategyCheck(items, false)
  assert(summary.completedCount === 1, '混合态完成数只计 completed')
  assert(summary.failedCount === 1, '混合态失败数只计 failed')
  assert(summary.persisted === false, '未固化主 Run 与诊断成败独立')
  assert(!('verdict' in summary), '汇总不得带投资结论字段')
}

function testNewRunStartsEmptyWithoutEffect(): void {
  const runA = applyStrategyCheckStatusForRun(
    emptyStrategyCheckRunState(),
    'run-a',
    'robustness',
    'completed',
  )
  const visibleForRunB = strategyCheckItemsForRun(runA, 'run-b')
  assert(visibleForRunB.robustness.status === 'idle', '新 Run 首帧不得继承上一 Run 完成状态')
  assert(runA.items.robustness.status === 'completed', '读取新 Run 状态不得改写旧 Run 事实')
}

function testFirstCallbackForNewRunDoesNotInheritPreviousItems(): void {
  const runA = applyStrategyCheckStatusForRun(
    emptyStrategyCheckRunState(),
    'run-a',
    'robustness',
    'completed',
  )
  const runB = applyStrategyCheckStatusForRun(runA, 'run-b', 'style', 'running')
  assert(runB.runId === 'run-b', '新 Run 回调应绑定新 runId')
  assert(runB.items.style.status === 'running', '新 Run 当前诊断状态应写入')
  assert(runB.items.robustness.status === 'idle', '新 Run 不得继承旧 Run 其他诊断状态')
}

const tests: Array<() => void> = [
  testIdleSummaryHasZeroCompleted,
  testCompletedCountTracksFinishedItems,
  testFailedStatusCollectsError,
  testFailedWithoutMessageStillCounts,
  testUnpersistedMainRunIsVisible,
  testApplyDoesNotMutatePreviousItems,
  testRerunClearsPreviousError,
  testMixedStatusesDoNotInventPassReject,
  testNewRunStartsEmptyWithoutEffect,
  testFirstCallbackForNewRunDoesNotInheritPreviousItems,
]

let failed = 0
for (const test of tests) {
  try {
    test()
    console.log(`PASS ${test.name}`)
  } catch (error) {
    failed += 1
    console.error(`FAIL ${test.name}: ${error instanceof Error ? error.message : String(error)}`)
  }
}

if (failed > 0) process.exit(1)
console.log(`${tests.length}/${tests.length} tests passed`)
