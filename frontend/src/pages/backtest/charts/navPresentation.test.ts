import { getNavPresentation } from './navPresentation.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function testCandidateExecutionPresentation(): void {
  const presentation = getNavPresentation('candidate_execution')
  assert(presentation.isCandidateExecution, '候选执行必须被识别')
  assert(presentation.navLabel === '候选样本曲线', '候选执行图例不得写成策略净值')
  assert(presentation.navAxisLabel === '样本净值', '候选执行纵轴不得写成策略资金')
  assert(!presentation.allowsBenchmark, '候选样本曲线不得显示基准比较')
}

function testAccountPresentation(): void {
  const presentation = getNavPresentation('account_execution')
  assert(!presentation.isCandidateExecution, '账户执行不得误判为候选执行')
  assert(presentation.navLabel === '策略净值', '账户执行应保留策略净值图例')
  assert(presentation.navAxisLabel === '策略资金', '账户执行应保留策略资金纵轴')
  assert(presentation.allowsBenchmark, '账户执行应允许基准比较')
}

const tests: Array<() => void> = [
  testCandidateExecutionPresentation,
  testAccountPresentation,
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
