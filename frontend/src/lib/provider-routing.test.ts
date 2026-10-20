import { describe, expect, it } from 'vitest'
import type { CapabilityRoute, ProviderField } from '@/lib/api'
import { DEFAULT_ROUTING, buildProviderRouting } from '@/lib/provider-routing'

/** 与后端能力注册表同形的路由能力 (id = 数据集名, field = 偏好字段)。 */
const CAPS: Pick<CapabilityRoute, 'id' | 'field'>[] = [
  { id: 'daily', field: 'daily_data_provider' },
  { id: 'adj_factor', field: 'adj_factor_provider' },
  { id: 'realtime', field: 'realtime_data_provider' },
  { id: 'minute', field: 'minute_data_provider' },
  { id: 'depth5', field: 'depth5_data_provider' },
  { id: 'financial', field: 'financial_data_provider' },
  { id: 'full_minute', field: 'full_minute_data_provider' },
]

const ALL_FIELDS: ProviderField[] = [
  'daily_data_provider',
  'adj_factor_provider',
  'realtime_data_provider',
  'minute_data_provider',
  'depth5_data_provider',
  'financial_data_provider',
  'full_minute_data_provider',
]

const DEMO = 'demo_source'

describe('buildProviderRouting', () => {
  it('接管声明过的全部能力, 包括 depth5 与 full_minute', () => {
    const routing = buildProviderRouting(
      DEMO,
      ['realtime', 'depth5', 'daily', 'minute', 'full_minute'],
      CAPS,
    )
    expect(routing.daily_data_provider).toBe(DEMO)
    expect(routing.realtime_data_provider).toBe(DEMO)
    expect(routing.minute_data_provider).toBe(DEMO)
    expect(routing.depth5_data_provider).toBe(DEMO)
    expect(routing.full_minute_data_provider).toBe(DEMO)
  })

  it('未声明的能力回落默认源, 不残留上一个数据源', () => {
    const routing = buildProviderRouting(
      DEMO,
      ['realtime', 'depth5', 'daily', 'minute', 'full_minute'],
      CAPS,
    )
    expect(routing.adj_factor_provider).toBe('tickflow')
    expect(routing.financial_data_provider).toBe('tickflow')
  })

  it('每个可路由字段都被覆盖 (防止再漏能力)', () => {
    const routing = buildProviderRouting(DEMO, [], CAPS)
    for (const field of ALL_FIELDS) {
      expect(routing[field], `missing field ${field}`).toBeDefined()
    }
  })

  it('切换源时旧源的能力全部被覆盖', () => {
    // 声明 6 项 (无 financial) 的源
    const routing = buildProviderRouting(
      DEMO,
      ['realtime', 'depth5', 'daily', 'adj_factor', 'minute', 'full_minute'],
      CAPS,
    )
    expect(routing.adj_factor_provider).toBe(DEMO)
    expect(routing.financial_data_provider).toBe('tickflow')
    expect(Object.keys(routing).sort()).toEqual([...ALL_FIELDS].sort())
  })

  it('能力矩阵未加载时 fail-closed 抛错', () => {
    expect(() => buildProviderRouting(DEMO, ['daily'], [])).toThrow(/能力矩阵未加载/)
  })

  it('不可路由能力 (field=null) 被跳过', () => {
    const routing = buildProviderRouting(DEMO, ['daily'], [
      { id: 'daily', field: 'daily_data_provider' },
      { id: 'tickflow_only', field: null },
    ])
    expect(routing.daily_data_provider).toBe(DEMO)
    expect(Object.keys(routing).sort()).toEqual([...ALL_FIELDS].sort())
  })

  it('默认路由覆盖全部字段且均为 tickflow', () => {
    expect(Object.keys(DEFAULT_ROUTING).sort()).toEqual([...ALL_FIELDS].sort())
    expect(new Set(Object.values(DEFAULT_ROUTING))).toEqual(new Set(['tickflow']))
  })
})
