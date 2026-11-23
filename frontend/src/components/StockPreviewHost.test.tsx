// @vitest-environment jsdom
import { act, useEffect, useLayoutEffect } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Outlet, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPreviewHost } from './StockPreviewHost'
import { closeStockPreview, openStockPreview } from '@/extensions/stockPreview'
import { getStockPreviewRequest, stockPreviewUi } from '@/lib/stockPreviewStore'
import { installTspExtBridge, packToExtension } from '@/extensions/runtime'

vi.mock('./StockPreviewDialog', () => ({ StockPreviewDialog: (props: any) => props.symbol ? <section role="dialog">
  {props.symbol} {props.name}
  <button onClick={props.onClose}>关闭详情</button>
  {props.navList?.[1] && <button onClick={() => props.onNavigate(props.navList[1].symbol, props.navList[1].name)}>下一只</button>}
</section> : null }))

let host: HTMLDivElement, root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  stockPreviewUi.closeStockPreview()
  installTspExtBridge({ api: {}, QK: {}, cn: () => '', format: {}, ui: stockPreviewUi })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove(); closeStockPreview()
  delete (window as unknown as { __TSP_EXT__?: unknown }).__TSP_EXT__
})

it('reuses one host dialog for source calls, preserves list navigation and supports both close entry points', async () => {
  await act(async () => root.render(<MemoryRouter><StockPreviewHost /></MemoryRouter>))
  expect(host.querySelector('[role=dialog]')).toBeNull()
  await act(async () => { openStockPreview({ symbol: '159558.SZ', navList: [{ symbol: '159558.SZ' }, { symbol: '605365.SH', name: '立达信' }] }) })
  expect(host.textContent).toContain('159558.SZ')
  await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '下一只')!.click())
  expect(host.textContent).toContain('605365.SH 立达信')
  await act(async () => { openStockPreview({ symbol: '600519.SH' }); openStockPreview({ symbol: '159558.SZ' }) })
  expect(host.querySelectorAll('[role=dialog]')).toHaveLength(1)
  expect(host.textContent).toContain('159558.SZ')
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(getStockPreviewRequest()).toBeNull()
  await act(async () => { openStockPreview({ symbol: '159558.SZ' }); closeStockPreview() })
  expect(host.querySelector('[role=dialog]')).toBeNull()
})

it('passes the public UI bridge to an installed page and runs its normal unmount cleanup', async () => {
  installTspExtBridge({ api: {}, QK: {}, cn: () => '', format: {}, ui: stockPreviewUi })
  const cleanup = vi.fn()
  const page = packToExtension({
    id: 'test.preview', apiVersion: 1,
    routes: [{ id: 'preview', path: '/test-preview', mount: (el: HTMLElement, ctx: any) => {
      const button = document.createElement('button')
      button.textContent = '插件查看详情'
      button.onclick = () => ctx.ui.openStockPreview({ symbol: '159558.SZ', name: 'ETF' })
      el.append(button)
      return cleanup
    } }],
    navigation: [{ id: 'preview', routeId: 'preview', label: '详情测试' }],
  }).routes![0].component
  const Page = page
  await act(async () => root.render(<MemoryRouter><Page /><StockPreviewHost /></MemoryRouter>))
  await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '插件查看详情')!.click())
  expect(host.querySelector('[role=dialog]')?.textContent).toContain('159558.SZ ETF')
  await act(async () => root.unmount())
  expect(cleanup).toHaveBeenCalledOnce()
  expect(getStockPreviewRequest()).toBeNull()
  root = createRoot(host)
  await act(async () => root.render(<MemoryRouter><StockPreviewHost /></MemoryRouter>))
  expect(host.querySelector('[role=dialog]')).toBeNull()
})

it.each(['none', 'effect', 'layout'] as const)('route changes clear the old preview without closing a new page request (%s)', async mode => {
  function Start() {
    const navigate = useNavigate()
    return <button onClick={() => navigate('/next')}>换页</button>
  }
  function Next() {
    useLayoutEffect(() => { if (mode === 'layout') openStockPreview({ symbol: '600519.SH' }) }, [])
    useEffect(() => { if (mode === 'effect') openStockPreview({ symbol: '600519.SH' }) }, [])
    return null
  }
  function Shell() { return <><Outlet /><StockPreviewHost /></> }
  await act(async () => root.render(<MemoryRouter><Routes><Route element={<Shell />}>
    <Route index element={<Start />} /><Route path="next" element={<Next />} />
  </Route></Routes></MemoryRouter>))
  await act(async () => { openStockPreview({ symbol: '159558.SZ' }) })
  await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '换页')!.click())
  expect(getStockPreviewRequest()?.symbol ?? null).toBe(mode === 'none' ? null : '600519.SH')
})
