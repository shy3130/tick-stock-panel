import { useMemo } from 'react'
import type { ParameterGridScenario } from '@/lib/api'
import { useECharts } from '../charts/useECharts'
import {
  buildFrontierPoints,
  selectParetoVisibleIds,
  type FrontierPoint,
} from './parameterGridPareto'

interface Props {
  scenarios: ParameterGridScenario[]
  objective: string
  experimentId: string
}

const finite = (value: unknown) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const valueKey = (value: number) => Number(value).toPrecision(12)

type HeatmapRow = [xIndex: number, yIndex: number, score: number, rank: number, scenarioId: string]

interface HeatmapModel {
  xKey: string
  yKey: string
  xValues: number[]
  yValues: number[]
  data: HeatmapRow[]
  collapsedDimensions: number
}

const buildHeatmapModel = (scenarios: ParameterGridScenario[]): HeatmapModel | null => {
  const keys = [...new Set(scenarios.flatMap(scenario => Object.keys(scenario.params ?? {})))]
    .filter(key => new Set(scenarios.map(scenario => scenario.params?.[key])).size > 1)
  if (keys.length < 2) return null
  const [xKey, yKey] = keys
  const xValues = [...new Set(scenarios.map(scenario => finite(scenario.params?.[xKey])).filter((value): value is number => value != null))].sort((a, b) => a - b)
  const yValues = [...new Set(scenarios.map(scenario => finite(scenario.params?.[yKey])).filter((value): value is number => value != null))].sort((a, b) => a - b)
  const cells = new Map<string, ParameterGridScenario>()
  for (const scenario of scenarios) {
    const x = finite(scenario.params?.[xKey])
    const y = finite(scenario.params?.[yKey])
    const score = finite(scenario.score)
    if (x == null || y == null || score == null || scenario.error) continue
    const key = `${valueKey(x)}:${valueKey(y)}`
    const current = cells.get(key)
    if (!current || Number(scenario.score) > Number(current.score)) cells.set(key, scenario)
  }
  return {
    xKey,
    yKey,
    xValues,
    yValues,
    data: [...cells.values()].map((scenario): HeatmapRow => {
      const x = Number(scenario.params[xKey])
      const y = Number(scenario.params[yKey])
      return [xValues.indexOf(x), yValues.indexOf(y), Number(scenario.score), scenario.rank, scenario.scenario_id]
    }),
    collapsedDimensions: Math.max(0, keys.length - 2),
  }
}

// 图表拆成独立组件, 数据就绪后才挂载: useECharts 的初始化 effect 依赖为空只跑一次,
// 若首渲染时容器缺席(model 为 null 走 return null), 流式场景后续把 model 变有效时
// init 不会重跑, 图表永久空白。
function Heatmap({ scenarios, experimentId }: { scenarios: ParameterGridScenario[]; experimentId: string }) {
  const model = useMemo(() => buildHeatmapModel(scenarios), [scenarios])
  if (!model || model.data.length === 0) return null
  return <HeatmapChart model={model} experimentId={experimentId} />
}

function HeatmapChart({ model, experimentId }: { model: HeatmapModel; experimentId: string }) {
  const option = useMemo(() => ({
    animation: false,
    grid: { left: 62, right: 32, top: 24, bottom: 52 },
    tooltip: {
      position: 'top',
      formatter: (params: any) => {
        const [xIndex, yIndex, score, rank] = params.value
        return `${model.xKey} = ${model.xValues[xIndex]}<br/>${model.yKey} = ${model.yValues[yIndex]}<br/>得分 ${Number(score).toFixed(3)} · 排名 #${rank}`
      },
    },
    xAxis: {
      type: 'category',
      name: model.xKey,
      data: model.xValues.map(String),
      axisLabel: { color: '#64748b', fontSize: 10 },
      nameTextStyle: { color: '#94a3b8', fontSize: 10 },
      axisLine: { lineStyle: { color: '#334155' } },
    },
    yAxis: {
      type: 'category',
      name: model.yKey,
      data: model.yValues.map(String),
      axisLabel: { color: '#64748b', fontSize: 10 },
      nameTextStyle: { color: '#94a3b8', fontSize: 10 },
      axisLine: { lineStyle: { color: '#334155' } },
    },
    visualMap: {
      min: Math.min(...model.data.map(row => Number(row[2]))),
      max: Math.max(...model.data.map(row => Number(row[2]))),
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      textStyle: { color: '#64748b', fontSize: 9 },
      inRange: { color: ['#7f1d1d', '#334155', '#065f46'] },
    },
    series: [{
      type: 'heatmap',
      data: model.data,
      label: { show: true, color: '#e2e8f0', fontSize: 9, formatter: (params: any) => Number(params.value[2]).toFixed(2) },
      itemStyle: { borderColor: '#0f172a', borderWidth: 1 },
    }],
  }) as any, [model])
  const ref = useECharts(option, [experimentId, model.xKey, model.yKey])

  return (
    <div>
      <div className="mb-1 px-3 text-[10px] text-muted">
        取前两个变化参数；{model.collapsedDimensions > 0 ? `其余 ${model.collapsedDimensions} 维按最高得分折叠。` : '每格对应一个参数场景。'}
      </div>
      <div ref={ref} className="h-[260px]" />
    </div>
  )
}

// 与 Heatmap 同理: 数据未就绪时 return null 会让容器缺席首渲染,
// 流式数据后续到达时 useECharts 不会重新 init, 图表永久空白。
function Frontier({ scenarios, objective, experimentId }: Props) {
  const points = useMemo(() => buildFrontierPoints(scenarios), [scenarios])
  const paretoIds = useMemo(() => selectParetoVisibleIds(scenarios), [scenarios])
  if (points.length === 0) return null
  return <FrontierChart points={points} paretoIds={paretoIds} objective={objective} experimentId={experimentId} />
}

function FrontierChart({
  points,
  paretoIds,
  objective,
  experimentId,
}: {
  points: FrontierPoint[]
  paretoIds: Set<string>
  objective: string
  experimentId: string
}) {
  const paretoPoints = points.filter(point => paretoIds.has(point[4]))
  const dominatedPoints = points.filter(point => !paretoIds.has(point[4]))
  const option = useMemo(() => ({
    animation: false,
    grid: { left: 58, right: 24, top: 24, bottom: 44 },
    tooltip: {
      trigger: 'item',
      formatter: (params: any) => {
        const pareto = paretoIds.has(String(params.value[4]))
        return `排名 #${params.value[3]}${pareto ? ' · Pareto 前沿' : ''}<br/>最大回撤 ${Number(params.value[0]).toFixed(2)}%<br/>累计收益 ${Number(params.value[1]).toFixed(2)}%<br/>${objective} 得分 ${Number(params.value[2]).toFixed(3)}`
      },
    },
    legend: { show: false },
    xAxis: {
      type: 'value', name: '最大回撤(%)',
      axisLabel: { color: '#64748b', fontSize: 10, formatter: (value: number) => `${value}%` },
      nameTextStyle: { color: '#94a3b8', fontSize: 10 },
      splitLine: { lineStyle: { color: '#1e293b' } },
    },
    yAxis: {
      type: 'value', name: '累计收益(%)',
      axisLabel: { color: '#64748b', fontSize: 10, formatter: (value: number) => `${value}%` },
      nameTextStyle: { color: '#94a3b8', fontSize: 10 },
      splitLine: { lineStyle: { color: '#1e293b' } },
    },
    series: [
      {
        type: 'scatter',
        data: dominatedPoints,
        symbolSize: (value: number[]) => Math.max(7, 18 - Math.min(Number(value[3]), 12)),
        itemStyle: { color: '#3b82f6', opacity: 0.48 },
        emphasis: { itemStyle: { color: '#60a5fa', opacity: 1 } },
      },
      {
        type: 'scatter',
        data: paretoPoints,
        symbolSize: (value: number[]) => Math.max(8, 18 - Math.min(Number(value[3]), 12)),
        itemStyle: { color: '#10b981', opacity: 0.92, borderColor: '#052e1b', borderWidth: 1 },
        emphasis: { itemStyle: { color: '#34d399', opacity: 1 } },
      },
    ],
  }) as any, [points, paretoIds, objective])
  const ref = useECharts(option, [experimentId, objective])

  return <div ref={ref} className="h-[260px]" />
}

export function ParameterGridDiagnostics(props: Props) {
  const usable = props.scenarios.filter(scenario => !scenario.error && finite(scenario.score) != null)
  const frontierCandidates = props.scenarios.filter(scenario => !scenario.error
    && finite(scenario.stats?.max_drawdown) != null
    && finite(scenario.stats?.total_return) != null
    && finite(scenario.score) != null)
  if (usable.length < 2 && frontierCandidates.length < 2) return null

  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
      {usable.length >= 2 && (
        <section className="rounded-btn border border-border overflow-hidden">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">参数热图</div>
            <div className="mt-0.5 text-[10px] text-muted">观察局部高分区域，不以单一尖峰代替稳健性</div>
          </div>
          <Heatmap scenarios={usable} experimentId={props.experimentId} />
        </section>
      )}
      {frontierCandidates.length >= 2 && (
        <section className="rounded-btn border border-border overflow-hidden">
          <div className="border-b border-border px-3 py-2">
            <div className="text-xs font-medium text-foreground">收益–回撤 Pareto 前沿</div>
            <div className="mt-0.5 text-[10px] text-muted">绿色为严格非支配层（收益/夏普更高、回撤更低）；它与目标得分排序独立</div>
          </div>
          <Frontier {...props} scenarios={frontierCandidates} />
        </section>
      )}
    </div>
  )
}
