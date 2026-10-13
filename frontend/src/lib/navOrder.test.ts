// @vitest-environment node
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_NAV_PATHS,
  EXT_DOMAIN_PAGE_PATHS,
  compareByDefaultNav,
  defaultNavIndex,
  seedHiddenForNewUser,
} from './navOrder'

// 迁移前 Layout 核心导航数组的原序 (2026-10 扩展域迁移快照)。
// 这是老用户升级兼容的锚点数据: 改动任何一个位置都意味着某类未保存
// 排序用户的侧栏顺序在升级时发生变化, 需要明确决策而不是顺手调整。
describe('navOrder: 迁移前默认排序基准', () => {
  it('覆盖迁移前全部 19 个内置路径且无重复', () => {
    expect(DEFAULT_NAV_PATHS).toHaveLength(19)
    expect(new Set(DEFAULT_NAV_PATHS).size).toBe(19)
  })

  it('关键位置与迁移前核心导航数组一致', () => {
    expect(DEFAULT_NAV_PATHS[0]).toBe('/')
    expect(DEFAULT_NAV_PATHS[5]).toBe('/stock-analysis')
    expect(DEFAULT_NAV_PATHS[10]).toBe('/monitor')
    expect(DEFAULT_NAV_PATHS[11]).toBe('/regime')
    expect(DEFAULT_NAV_PATHS[16]).toBe('/review')
    expect(DEFAULT_NAV_PATHS[18]).toBe('/data')
  })

  it('未知道径返回 Infinity, 排在已知路径之后', () => {
    expect(defaultNavIndex('/data')).toBe(18)
    expect(defaultNavIndex('/company/risk')).toBe(Number.POSITIVE_INFINITY)
    expect(defaultNavIndex('/analysis/custom1')).toBe(Number.POSITIVE_INFINITY)
  })

  it('排序比较器: 已知按默认序, 未知稳定沉底 (不产生 NaN)', () => {
    const paths = ['/monitor', '/company/risk', '/', '/analysis/x', '/watchlist', '/data']
    const sorted = [...paths].sort(compareByDefaultNav)
    // 两个未知道径保持原始相对顺序 (company/risk 在 analysis/x 前)
    expect(sorted).toEqual(['/', '/watchlist', '/monitor', '/data', '/company/risk', '/analysis/x'])
  })
})

describe('seedHiddenForNewUser: 新装用户默认隐藏种子', () => {
  it('扩展域页面清单与默认基准一致且共 11 项', () => {
    expect(EXT_DOMAIN_PAGE_PATHS).toHaveLength(11)
    for (const p of EXT_DOMAIN_PAGE_PATHS) {
      expect(defaultNavIndex(p)).not.toBe(Number.POSITIVE_INFINITY)
    }
  })

  it('新用户 (未完成 onboarding): 11 个扩展页并入隐藏清单', () => {
    const next = seedHiddenForNewUser(false, [])
    expect(next).not.toBeNull()
    for (const p of EXT_DOMAIN_PAGE_PATHS) expect(next).toContain(p)
  })

  it('新用户已有隐藏项: 取并集不丢失', () => {
    const next = seedHiddenForNewUser(false, ['/indices'])
    expect(next).toContain('/indices')
    expect(next).toContain('/review')
    expect(new Set(next).size).toBe(next!.length)
  })

  it('老用户 (已完成 onboarding) 与未知状态: 不动, 返回 null', () => {
    expect(seedHiddenForNewUser(true, [])).toBeNull()
    expect(seedHiddenForNewUser(undefined, [])).toBeNull()
  })
})
