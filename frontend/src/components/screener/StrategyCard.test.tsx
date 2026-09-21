// @vitest-environment jsdom
// 分钟策略卡片数字未出时的占位引导: awaitRun → 「待计算」; computing → 脉冲 ···。
// 分钟策略不在 run_all/盘后缓存覆盖内 (Screener.tsx dailyPoolIds 过滤), 首屏
// 数字必为空, 纯空白无引导会让用户以为坏了 — 占位明确引导点击单跑。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyCard } from './StrategyCard'

const noop = () => {}
const base = {
  name: '分钟突破', description: '', source: 'custom' as const,
  active: false, loading: false, cardSize: 'normal' as const,
  onRun: noop, disabled: false, onSettings: noop,
  timeframeBadge: '分钟',
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

function render(props: Record<string, unknown>) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  act(() => { root!.render(<StrategyCard {...base} {...props} />) })
}

it('shows 待计算 hint when awaitRun and count is missing', () => {
  render({ awaitRun: true })

  expect(container.textContent).toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('shows pulsing placeholder (not hint) when computing', () => {
  render({ awaitRun: true, computing: true })

  expect(container.textContent).toContain('···')
  expect(container.textContent).not.toContain('待计算')
})

it('shows nothing extra for daily strategies without awaitRun', () => {
  render({})

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('hint yields to the real count once available', () => {
  render({ awaitRun: true, count: 7 })

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).toContain('7')
})

it('mini size also renders the hint', () => {
  render({ awaitRun: true, cardSize: 'mini' })

  expect(container.textContent).toContain('待算')
})

it('onRun fires when the card is clicked', () => {
  const onRun = vi.fn()
  render({ awaitRun: true, onRun })

  act(() => { container.querySelector('button')!.click() })

  expect(onRun).toHaveBeenCalledTimes(1)
})
