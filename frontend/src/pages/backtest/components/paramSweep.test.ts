import { describe, it, expect } from 'vitest'
import { PRIMARY_OBJECTIVES, OBJECTIVES } from './paramSweep'

describe('PRIMARY_OBJECTIVES 稳健优化主目标白名单', () => {
  it('排除退化目标 (交易数少时可被运气推高)', () => {
    const ids = PRIMARY_OBJECTIVES.map(o => o.id)
    expect(ids).not.toContain('win_rate')
    expect(ids).not.toContain('avg_holding_days')
    expect(ids).not.toContain('n_trades')
    expect(ids).toContain('sortino')
    expect(ids).toContain('max_drawdown')
  })

  it('是全量目标列表的子集 (标签一致)', () => {
    const all = new Map(OBJECTIVES.map(o => [o.id, o.label]))
    PRIMARY_OBJECTIVES.forEach(o => expect(all.get(o.id)).toBe(o.label))
  })
})
