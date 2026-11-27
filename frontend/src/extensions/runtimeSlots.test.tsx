// @vitest-environment jsdom
import { act, type ComponentType } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { installTspExtBridge, packToExtension, type PagePackExtension } from './runtime'
import { api } from '@/lib/api'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  installTspExtBridge({ api, QK: {}, cn: (...xs) => xs.filter(Boolean).join(' '), format: {} })
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

it('运行时 Page Pack 插槽透传 props、支持就地 update 并在卸载时清理', async () => {
  const onUpdate = vi.fn()
  const onUnmount = vi.fn()
  const pack: PagePackExtension = {
    id: 'demo.runtime-slot',
    apiVersion: 1,
    slots: [{
      name: 'stock-preview.footer',
      id: 'demo-footer',
      mount(el, _ctx, props) {
        el.textContent = `symbol:${String(props.symbol)}`
        return {
          update(next) {
            onUpdate(next)
            el.textContent = `symbol:${String(next.symbol)}`
          },
          unmount: onUnmount,
        }
      },
    }],
  }
  const ext = packToExtension(pack)
  const SlotComp = ext.slots![0].component as ComponentType<Record<string, unknown>>

  await act(async () => {
    root.render(<SlotComp symbol="000001.SZ" name="平安银行" view="daily" />)
  })
  expect(host.textContent).toBe('symbol:000001.SZ')
  expect(onUpdate).not.toHaveBeenCalled()

  await act(async () => {
    root.render(<SlotComp symbol="600519.SH" name="贵州茅台" view="daily" />)
  })
  expect(onUpdate).toHaveBeenCalledWith(expect.objectContaining({ symbol: '600519.SH' }))
  expect(host.textContent).toBe('symbol:600519.SH')

  act(() => root.unmount())
  expect(onUnmount).toHaveBeenCalledOnce()
})

it('api.extensionRequest 限定 /api/extensions/<id>/... 并拦截越界路径与非法 ID', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
  )

  const res = await api.extensionRequest<{ ok: boolean }>('demo.pkg', '/status?x=1')
  expect(res).toEqual({ ok: true })
  expect(fetchSpy).toHaveBeenCalledWith('/api/extensions/demo.pkg/status?x=1', expect.any(Object))

  await expect(api.extensionRequest('BAD/ID', 'status')).rejects.toThrow('非法扩展 ID')
  await expect(api.extensionRequest('demo.pkg', '../secrets')).rejects.toThrow('非法扩展路径')
  await expect(api.extensionRequest('demo.pkg', 'https://evil.test/x')).rejects.toThrow('非法扩展路径')
})
