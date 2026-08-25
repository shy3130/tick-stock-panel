import type { ParameterGridScenario } from '@/lib/api'

export type FrontierPoint = [
  drawdownPct: number,
  returnPct: number,
  score: number,
  rank: number,
  scenarioId: string,
]

const finite = (value: unknown) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** 从参数网格场景构建收益–回撤散点；出错或缺指标的场景不得进入图表。 */
export const buildFrontierPoints = (scenarios: ParameterGridScenario[]): FrontierPoint[] => scenarios.flatMap((scenario): FrontierPoint[] => {
  if (scenario.error) return []
  const drawdown = finite(scenario.stats?.max_drawdown)
  const totalReturn = finite(scenario.stats?.total_return)
  const score = finite(scenario.score)
  if (drawdown == null || totalReturn == null || score == null) return []
  return [[Math.abs(drawdown) * 100, totalReturn * 100, score, scenario.rank, scenario.scenario_id]]
})

/** 只有后端严格第一层 Pareto 场景高亮；旧实验缺失字段时不得误标。 */
export const selectParetoVisibleIds = (scenarios: ParameterGridScenario[]): Set<string> => new Set(
  scenarios
    .filter(scenario => !scenario.error && scenario.pareto_front === 1)
    .map(scenario => scenario.scenario_id),
)
