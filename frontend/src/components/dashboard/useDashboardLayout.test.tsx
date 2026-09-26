// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDashboardLayout } from './useDashboardLayout'
import { DEFAULT_LAYOUT } from './registry'
import type { DashboardItem } from './layout'

// api.saveDashboardLayout 打桩捕获 PUT 载荷
const saveMock = vi.fn((_layout: unknown) => Promise.resolve({ dashboard_layout: null }))
vi.mock('@/lib/api', () => ({
  api: { saveDashboardLayout: (layout: unknown) => saveMock(layout) },
}))

// usePreferences: 返回可控的 prefs 数据
let prefsData: { dashboard_layout: unknown } | undefined
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: prefsData }),
}))

// 无 testing-library: 自建 harness 组件, 把 hook 结果外抛到变量
let latest: { items: DashboardItem[]; setItems: (n: DashboardItem[]) => void } | null = null
function Harness() {
  const h = useDashboardLayout()
  latest = h
  return null
}

let root: Root | null = null
let host: HTMLDivElement | null = null

function mountHarness() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => root!.render(<Harness />))
}

beforeEach(() => {
  saveMock.mockClear()
  prefsData = undefined
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  act(() => root?.unmount())
  host?.remove()
  latest = null
})

const CUSTOM: DashboardItem[] = [
  { i: 'indices', t: 'indices', x: 0, y: 0, w: 12, h: 2 },
  { i: 'ext-link-x', t: 'ext-link', x: 0, y: 2, w: 6, h: 8, p: { title: 'T', url: 'https://example.com/' } },
]

describe('useDashboardLayout', () => {
  it('renders default layout when prefs have no saved layout', () => {
    prefsData = { dashboard_layout: null }
    mountHarness()
    expect(latest!.items).toEqual(DEFAULT_LAYOUT)
  })

  it('renders normalized saved layout from prefs', () => {
    prefsData = {
      dashboard_layout: { v: 1, items: [{ i: 'indices', t: 'indices', x: 0, y: 0, w: 99, h: 2 }] },
    }
    mountHarness()
    expect(latest!.items).toHaveLength(1)
    expect(latest!.items[0].w).toBe(12) // 越界已规范化
  })

  it('debounces save with blob payload after local edit', () => {
    prefsData = { dashboard_layout: null }
    mountHarness()
    act(() => latest!.setItems(CUSTOM))
    act(() => vi.advanceTimersByTime(500))
    expect(saveMock).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(400))
    expect(saveMock).toHaveBeenCalledTimes(1)
    const payload = saveMock.mock.calls[0][0] as { v: number; items: DashboardItem[] }
    expect(payload.v).toBe(1)
    expect(payload.items).toHaveLength(2)
    expect(payload.items[1]!.p!.url).toBe('https://example.com/')
  })

  it('saves null (restore default semantics) when local edit equals default layout', () => {
    prefsData = { dashboard_layout: null }
    mountHarness()
    act(() => latest!.setItems(DEFAULT_LAYOUT.map(it => ({ ...it }))))
    act(() => vi.advanceTimersByTime(1000))
    expect(saveMock).toHaveBeenCalledTimes(1)
    expect(saveMock.mock.calls[0][0]).toBeNull()
  })

  it('does not save when no local edit happened', () => {
    prefsData = { dashboard_layout: { v: 1, items: [{ i: 'indices', t: 'indices', x: 0, y: 0, w: 12, h: 2 }] } }
    mountHarness()
    act(() => vi.advanceTimersByTime(5000))
    expect(saveMock).not.toHaveBeenCalled()
  })

  it('re-saves with latest items when edits arrive within debounce window', () => {
    prefsData = { dashboard_layout: null }
    mountHarness()
    act(() => latest!.setItems(CUSTOM))
    act(() => vi.advanceTimersByTime(500))
    act(() => latest!.setItems([{ ...CUSTOM[0], w: 6 }]))
    act(() => vi.advanceTimersByTime(900)) // 第二次编辑重置防抖, 须再满 800ms
    // 第一次编辑被防抖取消, 只有最终态落盘一次
    expect(saveMock).toHaveBeenCalledTimes(1)
    expect((saveMock.mock.calls[0][0] as { items: DashboardItem[] }).items[0].w).toBe(6)
  })

  it('keeps local layout when save fails (silent)', async () => {
    saveMock.mockRejectedValueOnce(new Error('network down'))
    prefsData = { dashboard_layout: null }
    mountHarness()
    act(() => latest!.setItems(CUSTOM))
    act(() => vi.advanceTimersByTime(1000))
    // 微任务冲刷(rejection 被 catch 吞掉, 不应有未处理拒绝)
    await act(async () => { await Promise.resolve() })
    expect(saveMock).toHaveBeenCalledTimes(1)
    expect(latest!.items).toEqual(CUSTOM)
  })
})
