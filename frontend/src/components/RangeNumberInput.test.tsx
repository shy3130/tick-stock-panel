// @vitest-environment jsdom
/** 扩展列图表宽高输入: 聚焦期间自由编辑, 失焦统一收敛。
 *  - 合法数字 → 钳制到 [min, max] 后提交
 *  - 空/非数字 → 不提交, 恢复聚焦前的数字
 *  - Escape → 丢弃草稿恢复原值; Enter → 提交
 */
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { RangeNumberInput } from './ListColumnCustomizerInner'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

const setInputValue = (input: HTMLInputElement, value: string) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  setter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function render(value: number, onCommit = vi.fn()) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); host.remove() }
  act(() => { root.render(<RangeNumberInput value={value} min={60} max={300} onCommit={onCommit} />) })
  const input = host.querySelector('input') as HTMLInputElement
  return { input, onCommit }
}

it('聚焦期间可清空/随意输入 — 不清空不钳制不提交', () => {
  const { input, onCommit } = render(150)
  act(() => { input.focus() })
  act(() => { setInputValue(input, '') })
  expect(input.value).toBe('')          // 清空后保持空, 不回落默认值
  expect(onCommit).not.toHaveBeenCalled()
  act(() => { setInputValue(input, '5') })
  expect(input.value).toBe('5')         // 低于下限也照常显示
  expect(onCommit).not.toHaveBeenCalled()
})

it('失焦时合法数字钳制到范围内提交', () => {
  const { input, onCommit } = render(150)
  act(() => { input.focus() })
  act(() => { setInputValue(input, '500') })
  act(() => { input.blur() })
  expect(onCommit).toHaveBeenCalledWith(300)

  const { input: i2, onCommit: c2 } = render(150)
  act(() => { i2.focus() })
  act(() => { setInputValue(i2, '10') })
  act(() => { i2.blur() })
  expect(c2).toHaveBeenCalledWith(60)
})

it('失焦时为空 → 恢复聚焦前的数字且不提交', () => {
  const { input, onCommit } = render(150)
  act(() => { input.focus() })
  act(() => { setInputValue(input, '') })
  act(() => { input.blur() })
  expect(onCommit).not.toHaveBeenCalled()
  expect(input.value).toBe('150')       // 恢复原值, 不是最小值也不是默认值
})

it('Escape 丢弃草稿恢复原值, Enter 提交', () => {
  const { input, onCommit } = render(120)
  act(() => { input.focus() })
  act(() => { setInputValue(input, '999') })
  act(() => {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  })
  expect(onCommit).not.toHaveBeenCalled()
  expect(input.value).toBe('120')

  act(() => { input.focus() })
  act(() => { setInputValue(input, '200') })
  act(() => {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  })
  expect(onCommit).toHaveBeenCalledWith(200)
})
