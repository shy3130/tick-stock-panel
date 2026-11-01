// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { AssistantLauncher } from './AssistantLauncher'

const state = vi.hoisted(() => ({ open: false, load: vi.fn() }))
vi.mock('./store', () => ({
  useAssistantStore: () => ({ open: state.open }),
  setPageContext: vi.fn(), closeAssistant: vi.fn(), toggleAssistant: vi.fn(),
}))
vi.mock('./ui/AssistantFloatingButton', () => ({ AssistantFloatingButton: () => <button>助手</button> }))
vi.mock('./ui/AssistantDrawer', () => {
  state.load()
  return { AssistantDrawer: () => <span>{state.open ? '打开' : '关闭动画'}</span> }
})

it('首次打开才加载抽屉，关闭后保留组件以完成退出动画', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const container = document.createElement('div')
  const root = createRoot(container)
  const render = () => act(async () => root.render(<AssistantLauncher collapsed={false} pathname="/" />))
  try {
    await render()
    expect(state.load).not.toHaveBeenCalled()
    expect(container.textContent).toBe('助手')
    state.open = true
    await render()
    expect(state.load).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('打开')
    state.open = false
    await render()
    expect(container.textContent).toContain('关闭动画')
  } finally {
    act(() => root.unmount())
  }
})
