// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { getSortValue } from './stock-table'
import { resolveIntradayConfig, hasAuctionVolumeField, mergeColumns, resolveCandleConfig, resolveChartLink, refreshExtColumns, restoreColumnsFromBaseline } from './list-columns'
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

describe('resolveCandleConfig: 默认值与 link 归一', () => {
  it('默认 (无 cfg 无 link) 返回 100/80/40/40/12/false (老配置自然回退)', () => {
    expect(resolveCandleConfig(undefined)).toEqual({
      enabledWidth: 100, enabledHeight: 80, disabledWidth: 40, disabledHeight: 40, days: 12, volume: false,
    })
  })

  it('无 link: volume 走本列字段严格布尔归一; enabledHeight 透传', () => {
    expect(resolveCandleConfig({ enabledHeight: 100 }).enabledHeight).toBe(100)
    expect(resolveCandleConfig({ volume: true }).volume).toBe(true)
    expect(resolveCandleConfig({ volume: 'yes' as unknown as boolean }).volume).toBe(false)
    expect(resolveCandleConfig({ volume: undefined }).volume).toBe(false)
  })

  it('link.linkHeight:true + height:90 → enabledHeight=90 (宽度/收起高度不受联动影响)', () => {
    const r = resolveCandleConfig({ enabledWidth: 120, enabledHeight: 100, disabledHeight: 50 }, { linkHeight: true, height: 90 })
    expect(r.enabledHeight).toBe(90)
    expect(r.enabledWidth).toBe(120)
    expect(r.disabledHeight).toBe(50)
  })

  it('link.height 越界钳制到 32–200', () => {
    expect(resolveCandleConfig(undefined, { linkHeight: true, height: 999 }).enabledHeight).toBe(200)
    expect(resolveCandleConfig(undefined, { linkHeight: true, height: -5 }).enabledHeight).toBe(32)
  })

  it('link.linkHeight:true 但 height 非有限数 → 回退旧高度, 不擅自 80', () => {
    expect(resolveCandleConfig({ enabledHeight: 120 }, { linkHeight: true, height: NaN }).enabledHeight).toBe(120)
    expect(resolveCandleConfig(undefined, { linkHeight: true, height: undefined }).enabledHeight).toBe(80) // 无旧值 → 默认
  })

  it('量带各列自管: 旧共享开关残留的 link.volume 被忽略', () => {
    // link.volume 是已废弃的共享开关字段 (本地存储可能残留), 两个 resolve 都
    // 不再读取 — 开关在哪个面板就只管哪个图
    const stale = { volume: true } as unknown as Parameters<typeof resolveCandleConfig>[1]
    expect(resolveCandleConfig({ volume: false }, stale).volume).toBe(false)
    expect(resolveCandleConfig(undefined, stale).volume).toBe(false)
  })
})

describe('resolveIntradayConfig: link 应用 (高度) 与自读回退', () => {
  it('无 link: height/volume 走旧字段 (与现状一致)', () => {
    const r = resolveIntradayConfig({ height: 60, volume: true })
    expect(r.height).toBe(60)
    expect(r.volume).toBe(true)
  })

  it('link 参数缺省时自读 cfg.link (与 resolveChartLink 同一副本, 不分叉)', () => {
    expect(resolveIntradayConfig({ height: 60, link: { linkHeight: true, height: 90 } }).height).toBe(90)
  })

  it('同一份 link 在两个 resolve 上同式生效 → 两列展开高度一致', () => {
    const link = { linkHeight: true, height: 90 }
    expect(resolveIntradayConfig({ height: 60 }, link).height).toBe(90)
    expect(resolveCandleConfig({ enabledHeight: 100 }, link).enabledHeight).toBe(90)
  })

  it('量带各列自管: 旧共享开关残留的 link.volume 被忽略', () => {
    const stale = { volume: false } as unknown as Parameters<typeof resolveIntradayConfig>[1]
    expect(resolveIntradayConfig({ volume: true }, stale).volume).toBe(true)
    expect(resolveIntradayConfig(undefined, stale).volume).toBe(false)
  })

  it('link.linkHeight:true + height 非有限数 → 回退旧高度; 越界钳制 32–200', () => {
    expect(resolveIntradayConfig({ height: 60 }, { linkHeight: true, height: NaN }).height).toBe(60)
    expect(resolveIntradayConfig({ height: 60 }, { linkHeight: true, height: 999 }).height).toBe(200)
  })
})

describe('resolveChartLink: 联动配置权威提取', () => {
  const intradayDefault: ColumnConfig = {
    id: 'builtin:intraday', source: { type: 'builtin', key: 'intraday' },
    label: '分时', visible: false, align: 'center',
  }

  it('从 builtin:intraday 列读取 intradayConfig.link', () => {
    const link = { linkHeight: true, height: 96 }
    expect(resolveChartLink([{ ...intradayDefault, intradayConfig: { link } }])).toEqual(link)
  })

  it('分时列隐藏 (visible:false) 仍可读取 — 不筛 visible', () => {
    const link = { linkHeight: false }
    const col: ColumnConfig = { ...intradayDefault, visible: false, intradayConfig: { link } }
    expect(resolveChartLink([col])).toBe(link)
  })

  it('无分时列或分时列无 link → undefined', () => {
    expect(resolveChartLink([])).toBeUndefined()
    expect(resolveChartLink([{ ...intradayDefault, intradayConfig: { width: 150 } }])).toBeUndefined()
  })
})

describe('refreshExtColumns: 实时 schema 刷新 ext 列显示名', () => {
  const base: ColumnConfig = {
    id: 'ext:t1:pop', source: { type: 'ext', configId: 't1', fieldName: 'pop', fieldLabel: '旧标签', fieldType: 'float' },
    label: '旧标签', visible: true, align: 'center',
  }

  it('schema 里标签已编辑 → label/fieldLabel/fieldType 跟随最新, 其余字段不动', () => {
    const out = refreshExtColumns([base], [{ id: 't1', columns: [{ name: 'pop', label: '人气值', dtype: 'int' }] }])
    expect(out[0].label).toBe('人气值')
    expect((out[0].source as { fieldLabel?: string }).fieldLabel).toBe('人气值')
    expect((out[0].source as { fieldType?: string }).fieldType).toBe('int')
    expect(out[0].visible).toBe(true)
    expect(out[0].id).toBe('ext:t1:pop')
  })

  it('schema 字段无 label → 回退字段名', () => {
    const out = refreshExtColumns([base], [{ id: 't1', columns: [{ name: 'pop' }] }])
    expect(out[0].label).toBe('pop')
  })

  it('字段已不在 schema (改名/删除) → 原样保留不抛错 (死列属另一问题)', () => {
    const out = refreshExtColumns([base], [{ id: 't1', columns: [{ name: 'other', label: 'x' }] }])
    expect(out[0]).toBe(base)
  })

  it('标签未变 / 无 schema / 空表 → 返回原数组引用 (effect 收敛锚)', () => {
    const fresh: ColumnConfig = {
      ...base, label: '人气值',
      source: { ...(base.source as { type: 'ext'; configId: string; fieldName: string; fieldLabel?: string; fieldType?: string }), fieldLabel: '人气值' },
    }
    const cols: ColumnConfig[] = [fresh]
    const schema = [{ id: 't1', columns: [{ name: 'pop', label: '人气值', type: 'float' }] }]
    expect(refreshExtColumns(cols, schema)).toBe(cols)
    expect(refreshExtColumns(cols, null)).toBe(cols)
    expect(refreshExtColumns(cols, undefined)).toBe(cols)
    expect(refreshExtColumns(cols, [])).toBe(cols)
  })
})

describe('restoreColumnsFromBaseline: 恢复默认方案', () => {
  const col = (id: string, extra: Partial<ColumnConfig> = {}): ColumnConfig => ({
    id, source: { type: 'builtin', key: id.replace('builtin:', '') } as ColumnConfig['source'],
    label: id, visible: true, align: 'center', ...extra,
  })

  it('无快照 (null/空数组) → 返回 null, 调用方回退内置默认', () => {
    expect(restoreColumnsFromBaseline([col('a')], null)).toBeNull()
    expect(restoreColumnsFromBaseline([col('a')], undefined)).toBeNull()
    expect(restoreColumnsFromBaseline([col('a')], [])).toBeNull()
  })

  it('快照里有的列: 恢复显隐与配置, 按快照顺序排列', () => {
    const current = [
      col('builtin:symbol', { pinned: true }),
      col('builtin:pct', { visible: true }),
      col('builtin:price', { visible: false }),
    ]
    const snapshot = [
      col('builtin:price', { visible: true, candleConfig: { volume: true } }),
      col('builtin:pct', { visible: false }),
    ]
    const out = restoreColumnsFromBaseline(current, snapshot)!
    expect(out[0].id).toBe('builtin:symbol') // pinned 始终最前
    expect(out.map(c => c.id).slice(1)).toEqual(['builtin:price', 'builtin:pct'])
    const price = out.find(c => c.id === 'builtin:price')!
    expect(price.visible).toBe(true)
    expect(price.candleConfig?.volume).toBe(true)
    expect(out.find(c => c.id === 'builtin:pct')!.visible).toBe(false)
  })

  it('快照里没有的列 (后来新增): 置为不可见且清空配置, 排在快照列之后', () => {
    const current = [
      col('builtin:new_col', { visible: true, intradayConfig: { width: 200 } }),
      col('builtin:pct', { visible: true }),
    ]
    const snapshot = [col('builtin:pct', { visible: true })]
    const out = restoreColumnsFromBaseline(current, snapshot)!
    expect(out.map(c => c.id)).toEqual(['builtin:pct', 'builtin:new_col'])
    const added = out.find(c => c.id === 'builtin:new_col')!
    expect(added.visible).toBe(false)
    expect(added.intradayConfig).toBeUndefined()
  })

  it('恢复后可再次完整保存 (快照往返无损)', () => {
    const snapshot = [
      col('builtin:intraday', { visible: true, intradayConfig: { width: 180, link: { linkHeight: true, height: 90 } } }),
      col('builtin:candle', { visible: true, candleConfig: { volume: true } }),
    ]
    const current = [col('builtin:candle'), col('builtin:intraday'), col('builtin:pct', { visible: true })]
    const out = restoreColumnsFromBaseline(current, snapshot)!
    expect(out.find(c => c.id === 'builtin:intraday')!.intradayConfig).toEqual({ width: 180, link: { linkHeight: true, height: 90 } })
    expect(out.find(c => c.id === 'builtin:candle')!.candleConfig).toEqual({ volume: true })
    expect(out.find(c => c.id === 'builtin:pct')!.visible).toBe(false)
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

describe('mergeColumns 保留 intradayConfig (含新字段 volume/auction.width/link 深保留)', () => {
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

  it('saved 列带 intradayConfig.link → merge 后深保留 (merge 后 resolveChartLink 仍可读)', () => {
    const link = { linkHeight: true, height: 96 }
    const saved: ColumnConfig[] = [{
      ...intradayDefault, visible: true,
      intradayConfig: { width: 200, link },
    }]
    const merged = mergeColumns(saved, [intradayDefault])
    expect(merged.find(c => c.id === 'builtin:intraday')?.intradayConfig?.link).toEqual(link)
    expect(resolveChartLink(merged)).toEqual(link)
  })

  it('saved 列无 intradayConfig → merged 不带该键 (用默认渲染)', () => {
    const saved: ColumnConfig[] = [{ ...intradayDefault, visible: true }]
    const col = mergeColumns(saved, [intradayDefault]).find(c => c.id === 'builtin:intraday')
    expect(col && 'intradayConfig' in col).toBe(false)
  })
})
