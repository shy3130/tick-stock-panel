// @vitest-environment jsdom
import { act, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import type { FrontendExtensionModule } from './types'

const loads = vi.hoisted(() => ({ paper: vi.fn(), abnormal: vi.fn() }))
vi.mock('../custom/paper/Page', () => {
  loads.paper()
  return { Paper: () => <div>模拟盘页面</div> }
})
vi.mock('../custom/abnormal/Page', () => {
  loads.abnormal()
  return { AbnormalMoves: () => <div>异动页面</div> }
})

afterEach(() => { vi.resetModules() })

it('注册菜单不加载页面；直接访问扩展路由只加载该页面', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const registry = await import('./registry')
  await registry.loadFrontendExtensions({
    paper: () => import('../custom/paper/extension'),
    abnormal: () => import('../custom/abnormal/extension'),
  })
  registry.finalizeFrontendExtensions(new Set(['/']))
  expect(loads.paper).not.toHaveBeenCalled()
  expect(loads.abnormal).not.toHaveBeenCalled()
  expect(registry.getFrontendExtensionNavigation().map(n => n.label).sort()).toEqual(['异动监控', '模拟盘'].sort())
  const container = document.createElement('div')
  const root = createRoot(container)
  try {
    await act(async () => root.render(
      <MemoryRouter initialEntries={['/paper']}>
        <Suspense fallback={<span>加载中</span>}>
          <Routes>{registry.getFrontendExtensionRoutes().map(route => {
            const Page = route.component
            return <Route key={route.id} path={route.path} element={<Page />} />
          })}</Routes>
        </Suspense>
      </MemoryRouter>,
    ))
    expect(container.textContent).toBe('模拟盘页面')
    expect(loads.paper).toHaveBeenCalledTimes(1)
    expect(loads.abnormal).not.toHaveBeenCalled()
  } finally {
    act(() => root.unmount())
  }
})

it('并发加载仍按源顺序处理重复 ID，隔离同步抛错的模块', async () => {
  const registry = await import('./registry')
  let finish!: (m: FrontendExtensionModule) => void
  const first = new Promise<FrontendExtensionModule>(resolve => { finish = resolve })
  const second = vi.fn(async () => ({ default: { id: 'test.same', apiVersion: 1 as const } }))
  const loading = registry.loadFrontendExtensions({
    z: second,
    a: () => first,
    broken: () => { throw new Error('bad module') },
  })
  await Promise.resolve()
  expect(second).toHaveBeenCalledTimes(1)
  finish({ default: {
    id: 'test.same', apiVersion: 1,
    routes: [{ id: 'first', path: '/first', component: () => null }],
  } })
  await loading
  registry.finalizeFrontendExtensions(new Set())
  expect(registry.getFrontendExtensionRoutes().map(r => r.path)).toEqual(['/first'])
  expect(registry.getFrontendExtensionLoadErrors().map(e => e.source)).toEqual(['broken', 'z'])
})
