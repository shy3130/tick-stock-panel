import { describe, expect, it } from 'vitest'
import { normalizeDashboardLayout } from './DashboardGrid'
import { DEFAULT_LAYOUT, WIDGET_DEFS, isKnownWidgetType, minSizeOf, widgetDef } from './registry'
import { compactLayout, sanitizeExtUrl, toBlob, type DashboardItem } from './layout'

describe('sanitizeExtUrl', () => {
  it('accepts http(s) urls and normalizes them', () => {
    expect(sanitizeExtUrl('https://example.com/x?a=1')).toBe('https://example.com/x?a=1')
    expect(sanitizeExtUrl(' http://example.com ')).toBe('http://example.com/')
  })
  it('rejects non-http schemes, junk and oversize', () => {
    expect(sanitizeExtUrl('javascript:alert(1)')).toBeNull()
    expect(sanitizeExtUrl('data:text/html,hi')).toBeNull()
    expect(sanitizeExtUrl('not a url')).toBeNull()
    expect(sanitizeExtUrl(`https://example.com/${'a'.repeat(500)}`)).toBeNull()
    expect(sanitizeExtUrl('')).toBeNull()
  })
})

describe('registry integrity', () => {
  it('every default layout item has a registered widget', () => {
    for (const it of DEFAULT_LAYOUT) {
      expect(widgetDef(it.t), it.t).toBeDefined()
    }
  })
  it('default layout respects grid bounds and min sizes', () => {
    for (const it of DEFAULT_LAYOUT) {
      expect(it.x).toBeGreaterThanOrEqual(0)
      expect(it.x + it.w).toBeLessThanOrEqual(12)
      const min = minSizeOf(it.t)
      expect(it.w).toBeGreaterThanOrEqual(min.w)
      expect(it.h).toBeGreaterThanOrEqual(min.h)
    }
  })
  it('default layout has no duplicate built-in types', () => {
    const types = DEFAULT_LAYOUT.map(it => it.t)
    expect(new Set(types).size).toBe(types.length)
  })
  it('ext-link widget is registered and built-ins are single-instance set', () => {
    expect(isKnownWidgetType('ext-link')).toBe(true)
    expect(WIDGET_DEFS.filter(d => d.id === 'ext-link')).toHaveLength(1)
  })
})

describe('normalizeDashboardLayout', () => {
  it('falls back to default on null/undefined/broken input', () => {
    expect(normalizeDashboardLayout(null)).toEqual(DEFAULT_LAYOUT)
    expect(normalizeDashboardLayout(undefined)).toEqual(DEFAULT_LAYOUT)
    expect(normalizeDashboardLayout('junk')).toEqual(DEFAULT_LAYOUT)
    expect(normalizeDashboardLayout({ v: 2, items: [] })).toEqual(DEFAULT_LAYOUT)
    expect(normalizeDashboardLayout({ v: 1, items: [] })).toEqual(DEFAULT_LAYOUT)
  })

  it('passes through a valid blob unchanged', () => {
    const blob = {
      v: 1,
      items: [
        { i: 'indices', t: 'indices', x: 0, y: 0, w: 12, h: 2 },
        { i: 'ext-link-x1', t: 'ext-link' as const, x: 0, y: 2, w: 6, h: 8, p: { title: 'T', url: 'https://example.com/' } },
      ],
    }
    expect(normalizeDashboardLayout(blob)).toEqual(blob.items)
  })

  it('clamps out-of-range geometry and respects min sizes', () => {
    // w=99 超出网格 → 压到 12 列; h=1 低于组件最小高 → 抬到 minH
    const items = normalizeDashboardLayout({
      v: 1,
      items: [{ i: 'radar', t: 'radar', x: 20, y: -3, w: 99, h: 1 }],
    })
    expect(items).toHaveLength(1)
    expect(items[0].w).toBe(12)
    expect(items[0].h).toBe(minSizeOf('radar').h)
    expect(items[0].x).toBe(0)
    expect(items[0].y).toBe(0)

    // w 低于组件最小宽 → 抬到 minW
    const narrow = normalizeDashboardLayout({
      v: 1,
      items: [{ i: 'radar', t: 'radar', x: 0, y: 0, w: 1, h: 1 }],
    })
    expect(narrow[0].w).toBe(minSizeOf('radar').w)
  })

  it('drops unknown types and duplicate built-ins, keeps first', () => {
    const items = normalizeDashboardLayout({
      v: 1,
      items: [
        { i: 'gainers', t: 'gainers', x: 0, y: 0, w: 3, h: 10 },
        { i: 'gainers-2', t: 'gainers', x: 3, y: 0, w: 3, h: 10 },
        { i: 'mystery', t: 'no-such-widget', x: 0, y: 0, w: 3, h: 3 },
      ],
    })
    expect(items.map(it => it.i)).toEqual(['gainers'])
  })

  it('drops ext-link items with unsafe or missing urls', () => {
    const items = normalizeDashboardLayout({
      v: 1,
      items: [
        { i: 'a', t: 'ext-link' as const, x: 0, y: 0, w: 4, h: 6, p: { title: 'ok', url: 'https://example.com/' } },
        { i: 'b', t: 'ext-link' as const, x: 4, y: 0, w: 4, h: 6, p: { title: 'js', url: 'javascript:alert(1)' } },
        { i: 'c', t: 'ext-link' as const, x: 8, y: 0, w: 4, h: 6 },
      ],
    })
    expect(items).toHaveLength(1)
    expect(items[0].p?.url).toBe('https://example.com/')
  })

  it('multiple ext-link instances survive normalization with unique ids', () => {
    const items = normalizeDashboardLayout({
      v: 1,
      items: [
        { i: 'ext-1', t: 'ext-link' as const, x: 0, y: 0, w: 6, h: 6, p: { title: 'A', url: 'https://a.example.com/' } },
        { i: 'ext-2', t: 'ext-link' as const, x: 6, y: 0, w: 6, h: 6, p: { title: 'B', url: 'https://b.example.com/' } },
      ],
    })
    expect(items.map(it => it.i)).toEqual(['ext-1', 'ext-2'])
  })

  it('resolves overlapping coordinates from a saved blob (later item sinks)', () => {
    // 两项完全同格重叠的异常 blob → 后者下沉到不冲突行, 输出两两无重叠
    const items = normalizeDashboardLayout({
      v: 1,
      items: [
        { i: 'gainers', t: 'gainers', x: 0, y: 0, w: 3, h: 7 },
        { i: 'losers', t: 'losers', x: 0, y: 0, w: 3, h: 7 },
      ],
    })
    const a = items.find(it => it.i === 'gainers')!
    const b = items.find(it => it.i === 'losers')!
    expect(a.y).toBe(0)
    expect(b.y).toBeGreaterThanOrEqual(a.y + a.h)
    for (let p = 0; p < items.length; p += 1) {
      for (let q = p + 1; q < items.length; q += 1) {
        const u = items[p]
        const v = items[q]
        const overlap = !(u.x + u.w <= v.x || u.x >= v.x + v.w || u.y + u.h <= v.y || u.y >= v.y + v.h)
        expect(overlap).toBe(false)
      }
    }
  })
})

describe('toBlob', () => {
  it('clones items without sharing references', () => {
    const items: DashboardItem[] = [
      { i: 'a', t: 'ext-link', x: 0, y: 0, w: 2, h: 5, p: { title: 'T', url: 'https://example.com/' } },
    ]
    const blob = toBlob(items)
    expect(blob.v).toBe(1)
    blob.items[0].p!.title = 'changed'
    expect(items[0].p!.title).toBe('T')
  })
})

describe('compactLayout', () => {
  const box = (i: string, x: number, y: number, w: number, h: number) => ({ i, x, y, w, h })

  it('无重叠时坐标原样返回且顺序不变', () => {
    const items = [box('a', 0, 0, 4, 8), box('b', 4, 0, 4, 8), box('c', 0, 8, 4, 8)]
    expect(compactLayout(items)).toEqual(items)
  })

  it('拖拽落点与其他组件碰撞时, 被拖组件下沉到首个不冲突行', () => {
    // b 被拖到 (0,0) 与 a 同位: a 先放置(y=0,x 更小), b 下沉到 a 底部之下
    const items = [box('a', 0, 0, 4, 8), box('b', 0, 0, 4, 6)]
    const out = compactLayout(items)
    expect(out.find(it => it.i === 'a')!.y).toBe(0)
    expect(out.find(it => it.i === 'b')!.y).toBe(8)
  })

  it('下沉过程中级联避让后续碰撞', () => {
    // b 拖到 a 正上方下方位置, 下沉后又撞到 c → 继续下沉
    const items = [box('a', 0, 0, 4, 8), box('b', 0, 4, 4, 6), box('c', 0, 8, 4, 6)]
    const out = compactLayout(items)
    expect(out.find(it => it.i === 'a')!.y).toBe(0)
    expect(out.find(it => it.i === 'b')!.y).toBe(8)
    expect(out.find(it => it.i === 'c')!.y).toBe(14)
  })

  it('横向错开的组件不算碰撞', () => {
    const items = [box('a', 0, 0, 4, 8), box('b', 4, 0, 4, 8)]
    const out = compactLayout(items)
    expect(out.map(it => it.y)).toEqual([0, 0])
  })

  it('刻意留的空隙不被上浮吞掉', () => {
    // a 在 y0, b 在 y20 (中间留 8 行空隙): 不碰撞则谁都不动
    const items = [box('a', 0, 0, 4, 2), box('b', 0, 20, 4, 2)]
    expect(compactLayout(items)).toEqual(items)
  })

  it('默认布局与持久化往返坐标均无重叠', () => {
    const out = compactLayout(DEFAULT_LAYOUT.map(({ i, t, x, y, w, h }) => ({ i, t, x, y, w, h })))
    for (let m = 0; m < out.length; m++) {
      for (let n = m + 1; n < out.length; n++) {
        const a = out[m], b = out[n]
        const overlap = !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y)
        expect(overlap).toBe(false)
      }
    }
  })
})
