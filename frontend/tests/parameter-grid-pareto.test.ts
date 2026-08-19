import { strict as assert } from 'node:assert'
import { buildFrontierPoints, selectParetoVisibleIds } from '../src/pages/backtest/components/parameterGridPareto'

const scenarios = [
  {
    scenario_id: 's-high-return',
    params: { vol: 1 },
    stats: { total_return: 0.5, sharpe: 2.0, max_drawdown: -0.2 },
    score: 4,
    rank: 1,
    error: null,
    elapsed_ms: 1,
    pareto_front: 1,
  },
  {
    scenario_id: 's-balanced',
    params: { vol: 2 },
    stats: { total_return: 0.4, sharpe: 1.8, max_drawdown: -0.1 },
    score: 3,
    rank: 2,
    error: null,
    elapsed_ms: 1,
    pareto_front: 1,
  },
  {
    scenario_id: 's-dominated',
    params: { vol: 3 },
    stats: { total_return: 0.39, sharpe: 1.7, max_drawdown: -0.1 },
    score: 2,
    rank: 3,
    error: null,
    elapsed_ms: 1,
    pareto_front: 2,
  },
  {
    scenario_id: 's-missing-front',
    params: { vol: 4 },
    stats: { total_return: 0.2, sharpe: 1.0, max_drawdown: -0.05 },
    score: 1,
    rank: 4,
    error: null,
    elapsed_ms: 1,
  },
  {
    scenario_id: 's-error',
    params: {},
    stats: { total_return: 1.0, sharpe: 10.0, max_drawdown: 0.0 },
    score: 99,
    rank: 5,
    error: 'boom',
    elapsed_ms: 1,
    pareto_front: 1,
  },
]

const points = buildFrontierPoints(scenarios)
assert.equal(points.length, 4, '出错场景不得进入收益-回撤散点')
assert.equal(points[0][0], 20, '回撤应转换为正百分比')

const visible = selectParetoVisibleIds(scenarios)
assert.deepEqual(visible, new Set(['s-high-return', 's-balanced']), '只有严格第一层 Pareto 场景应高亮')

const chartModel = scenarios
  .filter(scenario => !scenario.error
    && Number.isFinite(Number(scenario.stats?.max_drawdown))
    && Number.isFinite(Number(scenario.stats?.total_return))
    && Number.isFinite(Number(scenario.score)))
  .map(scenario => ({
    scenario_id: scenario.scenario_id,
    pareto: visible.has(scenario.scenario_id) ? 'front' : 'dominated',
  }))
assert.deepEqual(chartModel, [
  { scenario_id: 's-high-return', pareto: 'front' },
  { scenario_id: 's-balanced', pareto: 'front' },
  { scenario_id: 's-dominated', pareto: 'dominated' },
  { scenario_id: 's-missing-front', pareto: 'dominated' },
], '图表只允许按后端严格 Pareto 字段区分前沿与被支配点')

console.log('3/3 parameter grid Pareto presentation tests passed')
