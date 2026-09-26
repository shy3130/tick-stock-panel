import { describe, expect, it } from 'vitest'
import { normalizeDashboardLayout } from './DashboardGrid'
import { DEFAULT_LAYOUT, WIDGET_DEFS, isKnownWidgetType, minSizeOf, widgetDef } from './registry'
import { sanitizeExtUrl, toBlob, type DashboardItem } from './layout'

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
