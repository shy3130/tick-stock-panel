// 收益分析派生工具 — 从回测 equity_curve 派生月度/年度收益。
//
// 口径（与策略回测净值曲线一致）:
//   - equity_curve[].value 是账户总资产（现金+市值），非归一化净值；
//   - 月收益 = 当月最后一个交易日资产 / 上月末资产 - 1；
//   - 首月基准 = config.initial_capital（缺省时退化为曲线首个有效点，会漏掉首日盈亏）；
//   - 全年 = 年内各月收益复利连乘，与年初/年末资产环比等价；
//   - 收益数值一律为小数制（0.023 = 2.3%），展示格式化集中在 fmtRet。

export interface EquityPoint {
  date: string
  value: number
}

export interface MonthRef {
  year: number
  month: number // 1-12
  ret: number // 小数制
}

export interface YearRow {
  year: number
  /** months[0] = 1月 … months[11] = 12月；无数据为 null */
  months: (number | null)[]
  /** 年内月收益复利连乘（小数制） */
  annual: number
  /** 数据未覆盖到 12 月（回测截止年） */
  partial: boolean
  /** 有数据的最后一个月（1-12），partial 年份显示“截至M月”用 */
  lastMonth: number
}

export interface ReturnsAnalysis {
  /** 按年份升序 */
  years: YearRow[]
  /** 每个月份列跨年份的算术平均；该列全空为 null */
  monthlyAvg: (number | null)[]
  /** 年度收益的算术平均 */
  avgAnnual: number | null
  /** 正收益月份占比（小数制，分母=有数据月份数；严格 >0 计正） */
  positiveMonthRatio: number | null
  bestMonth: MonthRef | null
  worstMonth: MonthRef | null
  /** 月收益绝对值上限，热力图色阶归一用；全 0 时为 1 防除零 */
  maxAbsMonthly: number
}

/** 收益率格式化：一位小数百分数，正数不带符号（本页约定，区别于全局 fmtPct）。 */
export function fmtRet(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return ''
  return (v * 100).toFixed(1)
}

const MONTHS = 12

/**
 * 把任意价格曲线（如指数日 close）切片为「start 起的收益分析输入」：
 * curve 只保留 date >= start 的点；baseline 取 start 前最后一个有效点
 * （无前置点时退化为首个保留点，analyzeReturns 首月收益会漏掉首日变动）。
 */
export function sliceCurveFrom(
  curve: EquityPoint[],
  start: string,
): { curve: EquityPoint[]; baseline: number | null } {
  const s = String(start).slice(0, 10)
  const sorted = curve
    .map(p => ({ date: String(p.date).slice(0, 10), value: Number(p.value) }))
    .filter(p => /^\d{4}-\d{2}-\d{2}$/.test(p.date) && Number.isFinite(p.value) && p.value > 0)
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
  const kept = sorted.filter(p => p.date >= s)
  let baseline: number | null = null
  for (const p of sorted) {
    if (p.date >= s) break
    baseline = p.value
  }
  if (baseline == null) baseline = kept[0]?.value ?? null
  return { curve: kept, baseline }
}

export function analyzeReturns(
  equityCurve: EquityPoint[],
  initialCapital?: number | null,
): ReturnsAnalysis | null {
  const pts = equityCurve
    .map(p => ({ date: String(p.date).slice(0, 10), value: Number(p.value) }))
    .filter(p => /^\d{4}-\d{2}-\d{2}$/.test(p.date) && Number.isFinite(p.value) && p.value > 0)
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
  if (pts.length === 0) return null

  // 月末资产：按日期升序覆盖写入，最后留下的即当月最后一个点。
  // 假设曲线只含交易日（后端在交易日循环内追加），非交易日数据会被当作当月月末。
  const monthEnd = new Map<string, { year: number; month: number; value: number }>()
  for (const p of pts) {
    const year = Number(p.date.slice(0, 4))
    const month = Number(p.date.slice(5, 7))
    monthEnd.set(`${year}-${month}`, { year, month, value: p.value })
  }

  // 首月基准：优先 initial_capital，缺省退化为曲线首个有效点
  const baseline =
    initialCapital != null && Number.isFinite(initialCapital) && initialCapital > 0
      ? Number(initialCapital)
      : pts[0].value

  // 月收益：月末资产环比（时间顺序）
  const entries = [...monthEnd.entries()].sort((a, b) => {
    const [ay, am] = a[0].split('-').map(Number)
    const [by, bm] = b[0].split('-').map(Number)
    return ay !== by ? ay - by : am - bm
  })
  const monthly = new Map<string, number>() // 'YYYY-M' -> 小数制
  let prevEnd = baseline
  for (const [key, end] of entries) {
    monthly.set(key, end.value / prevEnd - 1)
    prevEnd = end.value
  }

  // 按年聚合
  const yearMap = new Map<number, YearRow>()
  for (const [key, ret] of monthly) {
    const [y, m] = key.split('-').map(Number)
    let row = yearMap.get(y)
    if (!row) {
      row = { year: y, months: Array(MONTHS).fill(null), annual: 0, partial: false, lastMonth: 0 }
      yearMap.set(y, row)
    }
    row.months[m - 1] = ret
  }
  const years = [...yearMap.values()].sort((a, b) => a.year - b.year)
  for (const row of years) {
    let compound = 1
    let lastMonth = 0
    for (let m = 0; m < MONTHS; m++) {
      const r = row.months[m]
      if (r != null) {
        compound *= 1 + r
        lastMonth = m + 1
      }
    }
    row.annual = compound - 1
    row.lastMonth = lastMonth
    row.partial = row.months[MONTHS - 1] == null
  }

  // 列平均 / 年度平均 / 统计行
  const monthlyAvg: (number | null)[] = Array(MONTHS).fill(null)
  for (let m = 0; m < MONTHS; m++) {
    const vals = years.map(y => y.months[m]).filter((v): v is number => v != null)
    monthlyAvg[m] = vals.length > 0 ? vals.reduce((s, v) => s + v, 0) / vals.length : null
  }
  const avgAnnual = years.length > 0
    ? years.reduce((s, y) => s + y.annual, 0) / years.length
    : null

  let positive = 0
  let total = 0
  let best: MonthRef | null = null
  let worst: MonthRef | null = null
  let maxAbs = 0
  for (const row of years) {
    for (let m = 0; m < MONTHS; m++) {
      const r = row.months[m]
      if (r == null) continue
      total++
      if (r > 0) positive++
      if (!best || r > best.ret) best = { year: row.year, month: m + 1, ret: r }
      if (!worst || r < worst.ret) worst = { year: row.year, month: m + 1, ret: r }
      maxAbs = Math.max(maxAbs, Math.abs(r))
    }
  }

  return {
    years,
    monthlyAvg,
    avgAnnual,
    positiveMonthRatio: total > 0 ? positive / total : null,
    bestMonth: best,
    worstMonth: worst,
    maxAbsMonthly: maxAbs > 0 ? maxAbs : 1,
  }
}
