// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let host: HTMLDivElement
let root: Root | undefined
let remote: Record<string, unknown>
let read: () => Promise<Response>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetModules()
  vi.useFakeTimers()
  localStorage.clear()
  document.documentElement.classList.add('dark')
  remote = {}
  read = async () => Response.json({ ui_prefs: remote })
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      Object.assign(remote, JSON.parse(init.body as string))
      return Response.json({ ui_prefs: remote })
    }
    return read()
  }))
  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function renderTheme() {
  const theme = await import('./theme')
  function Probe() { return <output>{theme.useTheme()}</output> }
  root = createRoot(host)
  await act(async () => root!.render(<Probe />))
  return theme
}

async function restore() {
  const { hydrateUiPrefs } = await import('./uiPrefs')
  const { applyTheme, getTheme } = await import('./theme')
  await act(async () => { await hydrateUiPrefs(); applyTheme(getTheme()) })
}

it('persists a theme toggle and immediately updates the page and subscribers', async () => {
  const { setTheme } = await renderTheme()
  await act(async () => setTheme('light'))
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(localStorage.getItem('tf-theme')).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it.each(['light', 'dark'] as const)('restores %s on a new origin and updates mounted charts through useTheme', async mode => {
  remote.theme = mode
  if (mode === 'dark') document.documentElement.classList.remove('dark')
  await renderTheme()
  await restore()
  expect(localStorage.getItem('tf-theme')).toBe(mode)
  expect(document.documentElement.classList.contains('dark')).toBe(mode === 'dark')
  expect(host.textContent).toBe(mode)
})

it('uses the saved theme instead of a stale cache from a previous port', async () => {
  remote.theme = 'light'
  localStorage.setItem('tf-theme', 'dark')
  await renderTheme()
  await restore()
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it('preserves a user toggle made during slow startup restoration', async () => {
  let resolve!: (value: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  const { setTheme } = await renderTheme()
  const { hydrateUiPrefs } = await import('./uiPrefs')
  const restoring = hydrateUiPrefs()
  await act(async () => setTheme('light'))
  await act(async () => { resolve(Response.json({ ui_prefs: { theme: 'dark' } })); await restoring })
  await restore()
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it('migrates an existing local theme when no backend preference exists', async () => {
  localStorage.setItem('tf-theme', 'light')
  await renderTheme()
  await restore()
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it('keeps the default dark theme without overwriting an unset preference', async () => {
  await renderTheme()
  await restore()
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(host.textContent).toBe('dark')
  expect(remote).toEqual({})
})

it('applies a theme changed from another tab to both CSS and subscribers', async () => {
  await renderTheme()
  await act(async () => {
    localStorage.setItem('tf-theme', 'light')
    window.dispatchEvent(new StorageEvent('storage', { key: 'tf-theme', newValue: 'light' }))
  })
  expect(host.textContent).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})

it('restores the saved theme after an unauthorized startup request and SPA login', async () => {
  const { hydrateUiPrefs } = await import('./uiPrefs')
  read = async () => new Response(null, { status: 401 })
  await hydrateUiPrefs()
  remote.theme = 'light'
  read = async () => Response.json({ ui_prefs: remote })
  await renderTheme()
  expect(host.textContent).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})

it('keeps a theme changed by another tab while hydration was pending', async () => {
  localStorage.setItem('tf-theme', 'dark')
  let resolve!: (value: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  await renderTheme()
  await act(async () => {
    localStorage.setItem('tf-theme', 'light')
    window.dispatchEvent(new StorageEvent('storage', { key: 'tf-theme', newValue: 'light' }))
  })
  await act(async () => resolve(Response.json({ ui_prefs: { theme: 'dark' } })))
  expect(host.textContent).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})
