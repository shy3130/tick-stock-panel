// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  SectorRotationCard,
  colorForSector,
  computeLeaderReigns,
  fullDayBuckets,
  mergeFullDayBuckets,
  selectReignLabels,
  topRowPerCol,
} from './SectorRotationCard'

const chart = vi.hoisted(() => ({
  on: vi.fn(), off: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
  isDisposed: () => false, getDom: () => null, group: '',
}))
vi.mock('echarts', () => ({
  init: () => chart,
  getInstanceByDom: () => null,
  connect: vi.fn(),
}))
vi.mock('@/lib/theme', () => ({
  useChartTheme: () => ({
    tooltipBg: '#101318', tooltipBorder: '#2a2f38', tooltipText: '#e5e7eb',
    text: '#8b93a1', textStrong: '#d7dbe2', grid: '#1b1f27', border: '#3a4150',
  }),
}))
vi.mock('@/components/Layout', () => ({
  CORE_INDEXES: [{ symbol: '000001.SH', name: '上证指数' }],
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

// 三板块六桶: 低空经济领涨 col0-2, 固态电池 col3 接管; 算力租赁 col0 缺数据
const MATRIX: (number | null)[][] = [
  [0.01, 0.012, 0.011, 0.013, 0.012, 0.014],
  [0.005, 0.004, 0.006, 0.02, 0.022, 0.021],
  [null, 0.002, 0.003, 0.004, 0.005, 0.006],
]
const BUCKETS = ['10:00', '10:05', '10:10', '10:15', '10:20', '10:25']
const SECTORS = ['低空经济', '固态电池', '算力租赁']

const apiMock = vi.hoisted(() => ({
  sectorRotation: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  api: {
    ...apiMock,
    extDataSchemaAll: vi.fn(async () => ({ items: [] })),
    indexMinute: vi.fn(async () => ({ rows: [] })),
    indexDaily: vi.fn(async () => ({ rows: [] })),
  },
}))

// 过去日期 → 稳定走「回放」分支 (指数分钟实时接口不发请求)
function payload() {
  return {
    status: 'ok',
    date: '2026-10-20',
    as_of: '15:00',
    member_count: 3,
    bucket_minutes: 5,
    basis: 'prev_close',
    flow_available: false,
    flow_field: null,
    default_exclude_sectors: ['融资融券'],
    max_auto_members: 300,
    timeline: BUCKETS.map((time, i) => ({
      time,
      leader: i < 3 ? '低空经济' : '固态电池',
      leader_pct: MATRIX[i < 3 ? 0 : 1][i],
      market_pct: 0.002,
      rotation: 0.3,
      cross_up: i === 3 ? 1 : 0,
      cross_down: 0,
    })),
    series: { sectors: SECTORS, buckets: BUCKETS, matrix: MATRIX },
    sectors: SECTORS.map((name, i) => ({
      name, pct_now: MATRIX[i][5], pct_prev: 0.004, rank_now: i + 1,
      rank_change: i === 1 ? 2 : 0, score: 60 - i * 10,
      n_members: 30, n_members_with_bars: 28, activity: 1e8, flow: null,
    })),
    universe: SECTORS.map(name => ({ name, activity: 1e8, pct_now: 0.01 })),
    cross_events: [{ time: '10:15', name: '固态电池', dir: 'up' }],
  }
}

/** N 板块 payload: 板块 i 最新值 = 0.004*(N-i) 严格降序, 用于线端名称封顶断言 */
function bigPayload(n: number) {
  const sectors = Array.from({ length: n }, (_, i) => `板块${i}`)
  const matrix = sectors.map((_, i) => BUCKETS.map((_, c) => 0.004 * (n - i) + c * 0.0001))
  return {
    status: 'ok' as const,
    date: '2026-10-20',
    as_of: '15:00',
    member_count: n,
    bucket_minutes: 5,
    basis: 'prev_close',
    flow_available: false,
    flow_field: null,
    default_exclude_sectors: [],
    max_auto_members: 300,
    timeline: BUCKETS.map((time, i) => ({
      time, leader: sectors[0], leader_pct: matrix[0][i],
      market_pct: 0.002, rotation: 0.3, cross_up: 0, cross_down: 0,
    })),
    series: { sectors, buckets: BUCKETS, matrix },
    sectors: sectors.map((name, i) => ({
      name, pct_now: matrix[i][5], pct_prev: 0.004, rank_now: i + 1,
      rank_change: 0, score: 60 - i, n_members: 30, n_members_with_bars: 30,
      activity: 1e8, flow: null,
    })),
    universe: sectors.map(name => ({ name, activity: 1e8, pct_now: 0.01 })),
    cross_events: [],
  }
}

let root: Root | null = null
let host: HTMLElement
async function renderCard(kind: 'concept' | 'industry' = 'industry', data = payload()) {
  apiMock.sectorRotation.mockResolvedValue(data)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={client}>
        <SectorRotationCard kind={kind} />
      </QueryClientProvider>,
    )
  })
  // react-query 的通知批处理需要多个微任务轮次, act 单次空 flush 不够 — 轮询等待
  await vi.waitFor(() => {
    expect(chart.setOption.mock.calls.length).toBeGreaterThan(0)
  }, { timeout: 3000 })
}
beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})
afterEach(async () => {
  if (root) await act(async () => { root!.unmount() })
  host?.remove()
  root = null
})

// ===== 纯函数 =====

it('computeLeaderReigns: 逐列 top 行变化即易主, null 跳过, 窗口开局领涨 prevRowIdx=null', () => {
  expect(computeLeaderReigns(MATRIX, 0)).toEqual([
    { col: 0, rowIdx: 0, prevRowIdx: null },
    { col: 3, rowIdx: 1, prevRowIdx: 0 },
  ])
})

it('computeLeaderReigns: 整列无数据跳过且不误判易主', () => {
  const matrix = [
    [0.01, null, 0.02],
    [0.005, null, 0.03],
  ]
  // col1 整列 null → 跳过; col2 与 col0 的 top (row0) 比较后 row1 接管
  expect(computeLeaderReigns(matrix, 0)).toEqual([
    { col: 0, rowIdx: 0, prevRowIdx: null },
    { col: 2, rowIdx: 1, prevRowIdx: 0 },
  ])
})

it('topRowPerCol: 易主区间展开为逐列 top 行', () => {
  const reigns = computeLeaderReigns(MATRIX, 0)
  expect(topRowPerCol(reigns, 6)).toEqual([0, 0, 0, 1, 1, 1])
})

it('selectReignLabels: 超上限时保留开局 + 最新 + 最长在位者', () => {
  const matrix = [[0.01, 0.02, 0.03, 0.04, 0.05, 0.06]]
  for (let row = 1; row < 5; row++) matrix.push(matrix[0].map(v => v - 0.001 * row))
  // 构造 0,0,1,1,1,0 的易主序列: row1 中间在位 3 列最长
  const reigns = [
    { col: 0, rowIdx: 0, prevRowIdx: null },
    { col: 2, rowIdx: 1, prevRowIdx: 0 },
    { col: 5, rowIdx: 0, prevRowIdx: 1 },
  ] as ReturnType<typeof computeLeaderReigns>
  const picked = selectReignLabels(reigns, 6, 2)
  expect(picked.has(0)).toBe(true)   // 开局
  expect(picked.has(5)).toBe(true)   // 最新
  expect(picked.size).toBe(2)
})

// ===== 渲染: 走势线模式 =====

it('trend 模式: 线末端常显板块名, 接管线上挂易主圆点标注', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  await renderCard()
  const options = chart.setOption.mock.calls.map(call => call[0] as any)
  const trendOption = options.find(o => o?.series?.[0]?.endLabel)
  expect(trendOption).toBeTruthy()
  // 3 条线 ≤8 → 全部常显末端名, 且开启 shiftY 防叠
  for (const s of trendOption.series) {
    expect(s.endLabel.show).toBe(true)
    expect(s.endLabel.formatter).toBe(s.name)
    expect(s.labelLayout.moveOverlap).toBe('shiftY')
  }
  // 低空经济: col0 开局领涨 1 个标注; 固态电池: col3 接管 1 个; 算力租赁: 无
  expect(trendOption.series[0].markPoint.data).toEqual([
    { name: '低空经济', coord: ['10:00', 0.01] },
  ])
  expect(trendOption.series[1].markPoint.data).toEqual([
    { name: '固态电池', coord: ['10:15', 0.02] },
  ])
  expect(trendOption.series[2].markPoint).toBeUndefined()
  // 图例条替代段落说明: 易主语义以色块图例呈现
  expect(host.textContent).toContain('领涨易主')
})

it('线端名称: 9 条 (默认前10 内) 全部常显', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  await renderCard('industry', bigPayload(9))
  const options = chart.setOption.mock.calls.map(call => call[0] as any)
  const trendOption = options.find(o => o?.series?.[0]?.endLabel)
  expect(trendOption).toBeTruthy()
  // 9 ≤ END_LABEL_MAX: 每条线都有线端名称 — 回归 (2026-10-27): 旧规则 >8 只显 Top6,
  // 默认「前10」下出现下缘线无名; 新规则与行数选项对齐
  for (const s of trendOption.series) {
    expect(s.endLabel?.show).toBe(true)
  }
})

it('线端名称: 前20 高密模式按最新值封顶 Top 10', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.setItem('sector_rotation_auto_rows_industry', '20')
  await renderCard('industry', bigPayload(12))
  const options = chart.setOption.mock.calls.map(call => call[0] as any)
  const trendOption = options.find(o => o?.series?.[0]?.endLabel)
  expect(trendOption).toBeTruthy()
  expect(trendOption.series).toHaveLength(12)
  const labeled = trendOption.series.filter((s: any) => s.endLabel?.show).map((s: any) => s.name)
  // 恰好 10 个, 且是最新值最高的前 10 (构造值严格降序 → 板块0..板块9)
  expect(labeled).toHaveLength(10)
  expect(labeled.sort()).toEqual(Array.from({ length: 10 }, (_, i) => `板块${i}`).sort())
  // 最低的两条无常显名 (悬停聚焦/图例辨认)
  expect(trendOption.series.filter((s: any) => !s.endLabel)).toHaveLength(2)
})

// ===== 渲染: 热力图模式 =====

it('heatmap 模式: 领涨格子亮琥珀描边+辉光, 格间细缝, 中性中间色', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.setItem('sector_rotation_display_industry', 'heatmap')
  await renderCard()
  const options = chart.setOption.mock.calls.map(call => call[0] as any)
  const heatOption = options.find(o => o?.series?.[0]?.type === 'heatmap')
  expect(heatOption).toBeTruthy()
  // 领涨轨迹: row0 col0-2 + row1 col3-5 = 6 个亮琥珀描边格子 (深红格上可辨)
  const trailCells = heatOption.series[0].data.filter(
    (cell: any) => cell.itemStyle?.borderColor === '#FCD34D',
  )
  expect(trailCells).toHaveLength(6)
  // 格间细缝: 系列级半透明网格线, 不再让大色块糊成一片
  expect(heatOption.series[0].itemStyle.borderColor).toBe('rgba(128,140,160,0.22)')
  // 配色: 中间色为可见中性, 不再用近透明的网格色
  expect(heatOption.visualMap.inRange.color).toEqual(
    ['#14532D', '#22C55E', '#323945', '#F87171', '#B91C1C'],
  )
  // 图例条: 轨迹语义以真实色块表达
  expect(host.textContent).toContain('领涨轨迹')
})

// ===== 资金流下拉: schema 类型名口径 =====

it('资金流下拉收录 Parquet 真实类型名 (DOUBLE/BIGINT/DECIMAL) 的数值列', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const { api } = await import('@/lib/api')
  vi.mocked(api.extDataSchemaAll).mockResolvedValueOnce({
    items: [{
      id: 'ext_flow', label: '资金流', mode: 'timeseries', time_field: null,
      columns: [
        { name: 'symbol', type: 'VARCHAR', label: '标的代码' },
        // 后端 schema 端点返回的是 Parquet 真实类型, 不是配置里的 float/int 简写
        { name: 'net', type: 'DOUBLE', label: '主力净流入' },
        { name: 'rank', type: 'BIGINT', label: '排名' },
        { name: 'ratio', type: 'DECIMAL(18,4)', label: '净占比' },
        { name: 'note', type: 'VARCHAR', label: '备注' },
      ],
    }],
  } as any)
  await renderCard()
  // 资金流下拉在「更多」折叠区里, 先展开
  const moreBtn = Array.from(host.querySelectorAll('button'))
    .find(b => (b.textContent || '').includes('更多'))
  await act(async () => { (moreBtn as HTMLButtonElement).click() })
  const select = host.querySelector('select[aria-label="资金流扩展列"]')
  expect(select).toBeTruthy()
  const values = Array.from(select!.querySelectorAll('option')).map(o => (o as HTMLOptionElement).value)
  expect(values).toContain('ext_flow.net')
  expect(values).toContain('ext_flow.rank')
  expect(values).toContain('ext_flow.ratio')
  expect(values).not.toContain('ext_flow.symbol')
  expect(values).not.toContain('ext_flow.note')
})

// ===== issue #518: 全天时间轴 / 稳定配色 =====

it('fullDayBuckets: 5 分钟桶全天 49 点, 上午止于 11:25 (桶起点), 下午含 15:00 收盘桶', () => {
  const labels = fullDayBuckets(5)
  expect(labels).toHaveLength(49)
  expect(labels[0]).toBe('09:30')
  expect(labels[23]).toBe('11:25')
  expect(labels[24]).toBe('13:00')
  expect(labels[48]).toBe('15:00')
})

it('fullDayBuckets: 1/15 分钟桶边界同样对齐桶起点截断', () => {
  const oneMin = fullDayBuckets(1)
  expect(oneMin[0]).toBe('09:30')
  expect(oneMin[119]).toBe('11:29')   // 上午最后一根分钟桶
  expect(oneMin.at(-1)).toBe('15:00')
  const fifteen = fullDayBuckets(15)
  expect(fifteen).toContain('11:15')
  expect(fifteen).not.toContain('11:30')
  expect(fifteen.at(-1)).toBe('15:00')
})

it('mergeFullDayBuckets: 并集兜底数据侧边界标签, 按时间序插入不丢列', () => {
  const merged = mergeFullDayBuckets(['10:00', '10:05'], 5)
  expect(merged).toHaveLength(49)
  expect(merged).toContain('10:00')
  // 假设数据侧产出了 11:30 边界桶 (约定变化), 并集不丢
  const withEdge = mergeFullDayBuckets(['10:00', '11:30'], 5)
  expect(withEdge).toHaveLength(50)
  expect(withEdge.indexOf('11:25')).toBeLessThan(withEdge.indexOf('11:30'))
  expect(withEdge.indexOf('11:30')).toBeLessThan(withEdge.indexOf('13:00'))
})

it('colorForSector: 同名同色且与出现顺序/重复调用无关 (排序刷新不漂移)', () => {
  const a = colorForSector('低空经济')
  expect(colorForSector('低空经济')).toBe(a)
  const b = colorForSector('固态电池')
  const c = colorForSector('算力租赁')
  expect(new Set([a, b, c]).size).toBe(3)
  // 中途插入新板块不影响已有板块的颜色
  colorForSector('新板块')
  expect(colorForSector('低空经济')).toBe(a)
  expect(colorForSector('固态电池')).toBe(b)
})

it('trend 模式: 全天铺轴 — 盘中窗口只有部分桶时, 未产生桶为 null 右侧留白', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  await renderCard()
  const options = chart.setOption.mock.calls.map(call => call[0] as any)
  const trendOption = options.find(o => o?.series?.[0]?.endLabel)
  expect(trendOption.xAxis.data[0]).toBe('09:30')
  expect(trendOption.xAxis.data.at(-1)).toBe('15:00')
  expect(trendOption.xAxis.data).toHaveLength(49)
  // 10:00 之前的桶为 null (留白), 10:00 处是矩阵真实值
  const idx = trendOption.xAxis.data.indexOf('10:00')
  expect(idx).toBeGreaterThan(0)
  expect(trendOption.series[0].data[idx - 1]).toBeNull()
  expect(trendOption.series[0].data[idx]).toBe(0.01)
  // 图例 plain 不分页: 看全所有板块
  expect(trendOption.legend.type).toBe('plain')
  expect(trendOption.legend.width).toBe('94%')
})

it('切换图: 易主标注走 markPoint, 颜色与走势线同源, 强弱用圆点大小表达', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  await renderCard()
  const options = chart.setOption.mock.calls.map(call => call[0] as any)
  // 切换图 = 带「切换强度」系列的 option
  const switchOption = options.find(o => o?.series?.some((s: any) => s.name === '切换强度'))
  expect(switchOption).toBeTruthy()
  // 轴与走势图同样全天铺
  expect(switchOption.xAxis.data[0]).toBe('09:30')
  expect(switchOption.xAxis.data.at(-1)).toBe('15:00')
  const intensity = switchOption.series.find((s: any) => s.name === '切换强度')
  // 易主点 (col3 固态电池接管) 在 markPoint 上, 颜色 = 固态电池走势线色, 函数式 formatter
  const marks = intensity.markPoint.data as any[]
  expect(marks.some(m => m.name === '固态电池' && m.itemStyle.color === colorForSector('固态电池'))).toBe(true)
  expect(marks.every(m => typeof m.label.formatter === 'function')).toBe(true)
  expect(marks.every(m => m.symbolSize === 8 || m.symbolSize === 5)).toBe(true)
  // 开局领涨也是事件 (与走势图开局圆点语义一致; 开盘即领涨的板块不再无标记)
  expect(marks.some(m => m.name === '低空经济')).toBe(true)
  // 强度线同样对齐全天轴: 10:00 之前为 null
  const idx = switchOption.xAxis.data.indexOf('10:00')
  expect(intensity.data[idx - 1]).toBeNull()
  expect(typeof intensity.data[idx]).toBe('number')
})
