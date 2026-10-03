import { describe, expect, it } from 'vitest'
import { analyzeReturns, fmtRet, sliceCurveFrom, type EquityPoint } from './returnsAnalysis'

/** 造月末资产序列：[date, value] 对，date 为该月最后一个交易日。 */
function curve(...points: [string, number][]): EquityPoint[] {
  return points.map(([date, value]) => ({ date, value }))
}

describe('analyzeReturns', () => {
  it('空曲线 / 全非法点返回 null', () => {
    expect(analyzeReturns([])).toBeNull()
    expect(analyzeReturns(curve(['2025-01-31', 0], ['2025-02-28', NaN], ['bad', 100]))).toBeNull()
  })

  it('月收益 = 月末资产环比，首月基准取 initial_capital', () => {
    const a = analyzeReturns(
      curve(
        ['2025-01-15', 101_000], // 月中点应被月末覆盖
        ['2025-01-31', 110_000],
        ['2025-02-28', 99_000],
      ),
      100_000,
    )!
    expect(a.years).toHaveLength(1)
    expect(a.years[0].months[0]).toBeCloseTo(0.10, 10) // 110000/100000 - 1
    expect(a.years[0].months[1]).toBeCloseTo(-0.10, 10) // 99000/110000 - 1
  })

  it('缺省 initial_capital 时首月基准退化为曲线首点', () => {
    const a = analyzeReturns(curve(['2025-01-31', 110_000], ['2025-02-28', 121_000]))!
    expect(a.years[0].months[0]).toBeCloseTo(0, 10) // 首点即基准
    expect(a.years[0].months[1]).toBeCloseTo(0.10, 10)
  })

  it('全年 = 月收益复利连乘，与年末/年初环比一致', () => {
    const a = analyzeReturns(
      curve(
        ['2024-12-31', 100_000],
        ['2025-01-31', 110_000],
        ['2025-02-28', 99_000],
        ['2025-03-31', 108_900],
      ),
      90_000,
    )!
    const y2025 = a.years.find(y => y.year === 2025)!
    // (1.1)(0.9)(1.1) - 1 = 0.089
    expect(y2025.annual).toBeCloseTo(0.089, 10)
    // 与 108900 / 100000(上年末) - 1 一致（2024 年 12 月资产为 2025 年基准）
    expect(y2025.annual).toBeCloseTo(108_900 / 100_000 - 1, 10)
  })

  it('跨年首月环比以上年 12 月末为基准', () => {
    const a = analyzeReturns(
      curve(['2024-12-31', 100_000], ['2025-01-31', 105_000]),
      100_000,
    )!
    expect(a.years.find(y => y.year === 2025)!.months[0]).toBeCloseTo(0.05, 10)
  })

  it('缺月年份：空格不参与复利与平均，partial 标记正确', () => {
    const a = analyzeReturns(
      curve(
        ['2025-01-31', 110_000],
        ['2025-03-31', 121_000], // 缺 2 月
        ['2025-10-31', 133_100], // 截至 10 月
      ),
      100_000,
    )!
    const y = a.years[0]
    expect(y.months[1]).toBeNull()
    expect(y.annual).toBeCloseTo(1.1 * 1.1 * 1.1 - 1, 10)
    expect(y.partial).toBe(true)
    expect(y.lastMonth).toBe(10)
    // 列平均：2 月全空为 null，1 月 = 0.1
    expect(a.monthlyAvg[1]).toBeNull()
    expect(a.monthlyAvg[0]).toBeCloseTo(0.1, 10)
  })

  it('完整年 partial=false', () => {
    const pts: [string, number][] = []
    let v = 100_000
    for (let m = 1; m <= 12; m++) {
      v *= 1.01
      pts.push([`2025-${String(m).padStart(2, '0')}-28`, v])
    }
    const a = analyzeReturns(curve(...pts), 100_000)!
    expect(a.years[0].partial).toBe(false)
    expect(a.years[0].annual).toBeCloseTo(1.01 ** 12 - 1, 6)
  })

  it('统计行：正收益占比 / 最佳 / 最差 / 年度平均', () => {
    const a = analyzeReturns(
      curve(
        ['2024-01-31', 110_000], // +10%
        ['2024-02-29', 99_000], // -10%
        ['2024-03-31', 99_000], // 0%
        ['2025-01-31', 108_900], // +10%（跨年环比）
      ),
      100_000,
    )!
    // 4 个月中 2 正（0 不计正）
    expect(a.positiveMonthRatio).toBeCloseTo(0.5, 10)
    expect(a.bestMonth).toMatchObject({ year: 2024, month: 1 })
    expect(a.bestMonth!.ret).toBeCloseTo(0.10, 10)
    expect(a.worstMonth).toMatchObject({ year: 2024, month: 2 })
    expect(a.worstMonth!.ret).toBeCloseTo(-0.10, 10)
    // 年度平均：(2024: 1.1*0.9*1.0-1 = -0.01) 与 (2025: 0.10) 的均值
    expect(a.avgAnnual).toBeCloseTo((-0.01 + 0.10) / 2, 10)
    expect(a.maxAbsMonthly).toBeCloseTo(0.10, 10)
  })

  it('归一化样本曲线（全量独立执行）：基准 1.0，首月收益含首日', () => {
    // candidate_execution 的 equity_curve 以 1.0 为起点逐日复利（非账户资产），
    // 调用方传 1.0 作基准；若误传 initial_capital 首月会算出 ≈-100%。
    const a = analyzeReturns(
      curve(['2025-01-31', 1.05], ['2025-02-28', 1.1025]),
      1.0,
    )!
    expect(a.years[0].months[0]).toBeCloseTo(0.05, 10)
    expect(a.years[0].months[1]).toBeCloseTo(0.05, 10)
    expect(a.years[0].annual).toBeCloseTo(0.1025, 10)
  })

  it('乱序输入按日期重排，月末取当月最后一个交易日', () => {
    const a = analyzeReturns(
      curve(
        ['2025-02-28', 105_000],
        ['2025-01-31', 100_000], // 乱序
        ['2025-01-10', 999_999], // 同月更早日期，不应成为月末
      ),
      100_000,
    )!
    expect(a.years[0].months[0]).toBeCloseTo(0, 10) // 100000/100000
    expect(a.years[0].months[1]).toBeCloseTo(0.05, 10) // 105000/100000
  })
})

describe('sliceCurveFrom', () => {
  it('基准取 start 前最后一个有效点，切片不含 start 前的点', () => {
    const { curve: c, baseline } = sliceCurveFrom(
      curve(
        ['2024-12-25', 3000],
        ['2024-12-31', 3100], // start 前最后收盘 → 基准
        ['2025-01-31', 3255],
        ['2025-02-28', 3417.75],
      ),
      '2025-01-01',
    )
    expect(baseline).toBe(3100)
    expect(c.map(p => p.date)).toEqual(['2025-01-31', '2025-02-28'])
    // 喂给 analyzeReturns: 首月 = 3255/3100 - 1 = 5%
    const a = analyzeReturns(c, baseline)!
    expect(a.years[0].months[0]).toBeCloseTo(0.05, 10)
    expect(a.years[0].annual).toBeCloseTo(1.05 * 1.05 - 1, 10)
  })

  it('无 start 前数据时基准退化为首个保留点', () => {
    const { baseline } = sliceCurveFrom(curve(['2025-03-31', 100]), '2025-01-01')
    expect(baseline).toBe(100)
  })

  it('乱序与非法点被过滤，start 当天点被保留', () => {
    const { curve: c, baseline } = sliceCurveFrom(
      curve(['2025-02-01', 0], ['2025-01-15', 50], ['2025-01-01', 40], ['bad', 1]),
      '2025-01-15',
    )
    expect(c.map(p => p.date)).toEqual(['2025-01-15'])
    expect(baseline).toBe(40)
  })

  it('全部为空时 baseline 为 null', () => {
    const { curve: c, baseline } = sliceCurveFrom([], '2025-01-01')
    expect(c).toEqual([])
    expect(baseline).toBeNull()
  })
})

describe('fmtRet', () => {
  it('正数不带符号，负数带负号，一位小数', () => {
    expect(fmtRet(0.023)).toBe('2.3')
    expect(fmtRet(-0.077)).toBe('-7.7')
    expect(fmtRet(0)).toBe('0.0')
  })

  it('非法值返回空串', () => {
    expect(fmtRet(null)).toBe('')
    expect(fmtRet(undefined)).toBe('')
    expect(fmtRet(NaN)).toBe('')
    expect(fmtRet(Infinity)).toBe('')
  })
})
