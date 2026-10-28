// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { ExtSparkChart } from './ExtSparkChart'

// 回归 (2026-10-27 用户反馈): 主力净流入列 (日内多盘柱状图) 渲染成
// 「10 根等高满柱 / 塌成基线虚线」 — 同日多行同值时 min-max 归一化分母退化:
// 全等负值走 min<0 分支画满柱, 全等正值柱高 max(1,0)=1px。
// 契约: 全等值序列必须回退数值文本, 不画伪趋势。

let root: Root | null = null
let host: HTMLElement

function render(props: Parameters<typeof ExtSparkChart>[0]) {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => { root!.render(<ExtSparkChart {...props} />) })
}

beforeEach(() => { Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }) })
afterEach(() => {
  if (root) act(() => { root!.unmount() })
  host?.remove()
  root = null
})

it('全等负值序列回退数值文本, 不再渲染满柱', () => {
  const points = Array.from({ length: 10 }, (_, i) => [`09:${i}0:00`, -32580312] as [string, number])
  render({ points, kind: 'bar' })
  expect(host.querySelector('svg')).toBeNull()
  expect(host.textContent).toContain('-32580312')
})

it('全等正值序列回退数值文本, 不再渲染塌线', () => {
  const points: [string, number][] = [['09:20:00', 5], ['09:25:00', 5], ['09:30:00', 5]]
  render({ points, kind: 'bar' })
  expect(host.querySelector('svg')).toBeNull()
  expect(host.textContent).toContain('5')
})

it('单点回退数值, 空序列显示占位符', () => {
  render({ points: [['09:25:00', 1.5]], kind: 'line' })
  expect(host.textContent).toContain('1.5')
  render({ points: [], kind: 'line' })
  expect(host.textContent).toContain('—')
})

it('值有变化的柱状图正常渲染且柱高不等 (归一化保留差异)', () => {
  render({ points: [['d1', 10], ['d2', 20], ['d3', 30]], kind: 'bar', width: 120, height: 40 })
  const rects = [...host.querySelectorAll('svg rect')]
  expect(rects.length).toBe(3)
  const heights = rects.map(r => Number(r.getAttribute('height')))
  // 逐值差异必须映射为柱高差异 — 退化 (全等高) 即回归
  expect(new Set(heights).size).toBe(3)
})

it('跨零柱状图: 正负值都以 0 轴为基线, 最后一根高亮', () => {
  render({ points: [['d1', -5], ['d2', 3], ['d3', 0]], kind: 'bar', width: 120, height: 40 })
  const rects = [...host.querySelectorAll('svg rect')]
  expect(rects.length).toBe(3)
  expect(rects[2].getAttribute('opacity')).toBe('1')
  expect(rects[0].getAttribute('opacity')).toBe('0.55')
})
