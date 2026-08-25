import { api, type StrategyRobustnessResult, type WalkForwardResult } from './api.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

// ---- fetch 桩: 捕获 strategyRobustness 发出的 path 与 JSON body ----
interface CapturedCall {
  path: string
  body: Record<string, unknown>
}

const originalFetch = globalThis.fetch
const calls: CapturedCall[] = []

const stubResponse: StrategyRobustnessResult = {
  run_id: 'wf-payload-test',
  full_stats: {},
  random_seed: 0,
  segment_stability: {
    folds: [],
    summary: { metric: 'sharpe', n_folds: 0, mean: 0, std: 0, worst: 0, positive_folds: 0 },
  },
  walk_forward: {
    enabled: false,
    scheme: 'expanding_train',
    selection_metric: 'sharpe',
    candidate_space: 'baseline + 单参数±扰动邻域',
    n_candidates: 0,
    requested_candidates: 0,
    effective_candidates: 0,
    max_executions: 24,
    warning: 'walk_forward: 未启用',
    folds: [],
    stitched_curve: [],
    summary: {
      metric: 'sharpe',
      n_folds: 0,
      positive_return_folds: 0,
      positive_fold_ratio: null,
      worst_fold_return: null,
      mean_oos_return: null,
      mean_degradation: null,
      oos_total_return: null,
      oos_sharpe: null,
      oos_max_drawdown: null,
    },
    param_drift: { n_distinct_param_sets: 0, distinct_labels: [], params: {} },
  },
  exit_breakdown: [],
}

globalThis.fetch = (async (input: unknown, init?: { body?: unknown }) => {
  const body = init?.body == null ? {} : JSON.parse(String(init.body)) as Record<string, unknown>
  calls.push({ path: String(input), body })
  return new Response(JSON.stringify(stubResponse), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}) as typeof fetch

async function testPayloadCarriesWalkForwardEnabledWhenOptedIn(): Promise<void> {
  await api.strategyRobustness({ strategy_id: 'macd', walk_forward_enabled: true })
  const last = calls[calls.length - 1]
  assert(last != null, '应发出一次请求')
  assert(last.path === '/api/backtest/strategy/robustness', `path 应为 robustness 端点, 实际 ${last.path}`)
  assert(last.body.walk_forward_enabled === true, '勾选后 payload 必须携带 walk_forward_enabled=true')
}

async function testPayloadOmitsWalkForwardByDefault(): Promise<void> {
  await api.strategyRobustness({ strategy_id: 'macd' })
  const last = calls[calls.length - 1]
  assert(!('walk_forward_enabled' in last.body), '未勾选时不得发送 walk_forward_enabled — 服务端按默认 false 处理')
}

function testLegacyWalkForwardResultWithoutEnabledFieldIsValid(): void {
  // 旧持久化响应没有 enabled/预算字段: 类型必须全部可选, undefined 视为已启用 (向后兼容)
  const legacy: WalkForwardResult = {
    scheme: 'expanding_train',
    selection_metric: 'sharpe',
    candidate_space: 'baseline + 单参数±扰动邻域 (局部邻域, 非全局优化)',
    n_candidates: 1,
    folds: [],
    stitched_curve: [],
    summary: {
      metric: 'sharpe',
      n_folds: 0,
      positive_return_folds: 0,
      positive_fold_ratio: null,
      worst_fold_return: null,
      mean_oos_return: null,
      mean_degradation: null,
      oos_total_return: null,
      oos_sharpe: null,
      oos_max_drawdown: null,
    },
    param_drift: { n_distinct_param_sets: 1, distinct_labels: ['baseline'], params: {} },
  }
  assert(legacy.enabled !== false, '缺省 enabled 不得被判为未启用 — 旧结果仍可渲染')
}

function testDisabledWalkForwardResultMetadataIsValid(): void {
  // 新响应: enabled=false + 预算元数据 + warning, UI 据此显示未启用提示而非伪造结果
  const disabled: WalkForwardResult = {
    ...stubResponse.walk_forward!,
  }
  assert(disabled.enabled === false, 'enabled=false 必须显式存在')
  assert(disabled.folds.length === 0 && disabled.stitched_curve.length === 0, '未启用不得伪造折/曲线')
  assert(disabled.max_executions === 24, '预算上限应可达')
  assert((disabled.warning ?? '').includes('walk_forward'), 'warning 必须明确说明未启用')
}

const tests: Array<() => void | Promise<void>> = [
  testPayloadCarriesWalkForwardEnabledWhenOptedIn,
  testPayloadOmitsWalkForwardByDefault,
  testLegacyWalkForwardResultWithoutEnabledFieldIsValid,
  testDisabledWalkForwardResultMetadataIsValid,
]

let failed = 0
try {
  for (const run of tests) {
    try {
      await run()
      console.log(`PASS ${run.name}`)
    } catch (e) {
      failed += 1
      console.error(`FAIL ${run.name}: ${(e as Error).message}`)
    }
  }
} finally {
  globalThis.fetch = originalFetch
}
if (failed > 0) {
  console.error(`${failed}/${tests.length} tests failed`)
  process.exit(1)
}
console.log(`${tests.length}/${tests.length} tests passed`)
