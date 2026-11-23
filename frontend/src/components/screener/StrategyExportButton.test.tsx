// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyExportButton } from './StrategyExportButton'

const mocks = vi.hoisted(() => ({ export: vi.fn(), toast: vi.fn(), objectURL: vi.fn(), revoke: vi.fn(), anchor: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { resourcesExport: mocks.export } }))
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))
const container = document.createElement('div')
document.body.appendChild(container)
let root: Root
const button = () => container.querySelector('button')!
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(mock => mock.mockReset())
  vi.useFakeTimers()
  mocks.objectURL.mockReturnValue('blob:strategy-zip')
  vi.stubGlobal('URL', { createObjectURL: mocks.objectURL, revokeObjectURL: mocks.revoke })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { mocks.anchor(this.download, this.href) })
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  container.innerHTML = ''
})

it('downloads a strategy dependency bundle as ZIP and prevents duplicate requests while pending', async () => {
  let resolve!: (blob: Blob) => void
  const zip = new Blob(['zip'], { type: 'application/zip' })
  mocks.export.mockReturnValueOnce(new Promise(done => { resolve = done }))
  act(() => root.render(<StrategyExportButton strategyId="custom_demo" />))
  await act(async () => button().click())
  expect(button().disabled).toBe(true)
  await act(async () => button().click())
  expect(mocks.export).toHaveBeenCalledTimes(1)
  expect(mocks.export).toHaveBeenCalledWith([{ kind: 'strategy', id: 'custom_demo' }], undefined)
  await act(async () => resolve(zip))
  expect(mocks.objectURL).toHaveBeenCalledWith(zip)
  expect(mocks.anchor).toHaveBeenCalledWith('tsp-strategy-custom_demo.zip', 'blob:strategy-zip')
  expect(button().disabled).toBe(false)
  vi.runOnlyPendingTimers()
  expect(mocks.revoke).toHaveBeenCalledWith('blob:strategy-zip')
})

it('shows export errors and allows retry without starting a bogus download', async () => {
  mocks.export.mockRejectedValueOnce(new Error('资源依赖不完整'))
  act(() => root.render(<StrategyExportButton strategyId="custom_demo" />))
  await act(async () => button().click())
  expect(mocks.toast).toHaveBeenCalledWith('资源依赖不完整', 'error')
  expect(mocks.anchor).not.toHaveBeenCalled()
  expect(button().disabled).toBe(false)
})
