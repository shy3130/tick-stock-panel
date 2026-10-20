// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { getSortValue } from './stock-table'
import { resolveIntradayConfig, hasAuctionVolumeField, mergeColumns } from './list-columns'
import type { ColumnConfig } from './list-columns'

function builtinCol(key: string): ColumnConfig {
  return { id: `builtin:${key}`, source: { type: 'builtin', key }, label: key, visible: false, align: 'center' }
}

describe('getSortValue: 自选加入信息列', () => {
  it('added_at 返回北京时间完整串 (与展示同口径, 字典序即时间序)', () => {
    expect(getSortValue({ added_at: '2026-09-18T20:30:00' }, builtinCol('added_at')))
      .toBe('2026-09-19 04:30:00')
  })

  it('added_at 缺失返回 null 而非空串', () => {
    // useTableSort 会先试 Number('') === 0, 返回空串会让空值排到最前
    for (const v of ['', null, undefined]) {
      expect(getSortValue({ added_at: v }, builtinCol('added_at'))).toBe(null)
    }
  })

  it('pct_since_added 透传数值 (含 0 与负值)', () => {
    expect(getSortValue({ pct_since_added: 0.1 }, builtinCol('pct_since_added'))).toBe(0.1)
    expect(getSortValue({ pct_since_added: -0.05 }, builtinCol('pct_since_added'))).toBe(-0.05)
    expect(getSortValue({ pct_since_added: 0 }, builtinCol('pct_since_added'))).toBe(0)
    expect(getSortValue({}, builtinCol('pct_since_added'))).toBe(undefined)
  })
})

describe('resolveIntradayConfig: volume/auctionWidth 归一', () => {
  it('默认返回 width=150/height=80/volume=false/auctionWidth=standard (老配置自然回退)', () => {
    const r = resolveIntradayConfig(undefined)
    expect(r.width).toBe(150)
    expect(r.height).toBe(80)
    expect(r.volume).toBe(false)
    expect(r.auctionWidth).toBe('standard')
  })

  it('volume:true 透传归一; 脏值 (非 true) 归一为 false', () => {
    expect(resolveIntradayConfig({ volume: true }).volume).toBe(true)
    expect(resolveIntradayConfig({ volume: false }).volume).toBe(false)
    expect(resolveIntradayConfig({ volume: undefined }).volume).toBe(false)
    expect(resolveIntradayConfig({ volume: 'yes' as unknown as boolean }).volume).toBe(false)
  })

  it('auction.width 合法档位透传归一', () => {
    expect(resolveIntradayConfig({ auction: { width: 'wide' } }).auctionWidth).toBe('wide')
    expect(resolveIntradayConfig({ auction: { width: 'narrow' } }).auctionWidth).toBe('narrow')
  })

  it('auction.width 脏持久化值归一回 standard', () => {
    expect(resolveIntradayConfig({ auction: { width: 'medium' as never } }).auctionWidth).toBe('standard')
    expect(resolveIntradayConfig({ auction: { width: 0.3 as never } }).auctionWidth).toBe('standard')
  })
})

describe('hasAuctionVolumeField: R2 强制联动判定真值表', () => {
  it('enabled + matchField/unmatchedField 任一 → true', () => {
    expect(hasAuctionVolumeField({ enabled: true, matchField: 'm' })).toBe(true)
    expect(hasAuctionVolumeField({ enabled: true, unmatchedField: 'u' })).toBe(true)
    expect(hasAuctionVolumeField({ enabled: true, matchField: 'm', unmatchedField: 'u' })).toBe(true)
  })

  it('enabled 但只配价格字段 → false', () => {
    expect(hasAuctionVolumeField({ enabled: true, priceField: 'p' })).toBe(false)
  })

  it('未 enabled / undefined → false', () => {
    expect(hasAuctionVolumeField({ enabled: false, matchField: 'm' })).toBe(false)
    expect(hasAuctionVolumeField(undefined)).toBe(false)
  })
})

describe('mergeColumns 保留 intradayConfig (含新字段 volume/auction.width 深保留)', () => {
  const intradayDefault: ColumnConfig = {
    id: 'builtin:intraday', source: { type: 'builtin', key: 'intraday' },
    label: '分时', visible: false, align: 'center',
  }

  it('saved 列带 intradayConfig → merge 后整对象深保留', () => {
    const saved: ColumnConfig[] = [{
      ...intradayDefault, visible: true,
      intradayConfig: {
        width: 200, height: 90, volume: true,
        auction: { enabled: true, configId: 't', priceField: 'p', matchField: 'm', width: 'wide' },
      },
    }]
    const col = mergeColumns(saved, [intradayDefault]).find(c => c.id === 'builtin:intraday')
    expect(col?.intradayConfig).toEqual(saved[0].intradayConfig)
    expect(col?.intradayConfig?.volume).toBe(true)
    expect(col?.intradayConfig?.auction?.width).toBe('wide')
  })

  it('saved 列无 intradayConfig → merged 不带该键 (用默认渲染)', () => {
    const saved: ColumnConfig[] = [{ ...intradayDefault, visible: true }]
    const col = mergeColumns(saved, [intradayDefault]).find(c => c.id === 'builtin:intraday')
    expect(col && 'intradayConfig' in col).toBe(false)
  })
})
