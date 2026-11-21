// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { SettingsAIPanel } from './AI'

const state = vi.hoisted(() => ({ data: {} as Record<string, unknown> }))
vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => state }))
vi.mock('@/lib/useCardFlash', () => ({
  useCardFlash: () => false,
  cardFlashCls: () => '',
  AnchorWrap: ({ children }: { children: unknown }) => children,
}))

const cases = [
  { label: 'MiniMax', url: 'https://api.minimax.io/v1' },
  { label: 'MiniMax (China)', url: 'https://api.minimaxi.com/v1' },
]
const models = ['MiniMax-M3.1-Flash', 'MiniMax-M3']

it.each(cases)('offers $label text presets without replacing saved models', async ({ label, url }) => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const client = new QueryClient()
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    state.data = { has_ai_key: false, ai_configured: false }
    await act(async () => root.render(<QueryClientProvider client={client}><SettingsAIPanel /></QueryClientProvider>))
    const preset = Array.from(host.querySelectorAll('button')).find(button => button.textContent?.trim().startsWith(label))
    expect(preset).toBeDefined()
    await act(async () => preset!.click())
    expect(Array.from(host.querySelectorAll('input')).map(input => input.value)).toContain(url)
    expect(Array.from(host.querySelectorAll('input')).map(input => input.value)).toContain(models[0])
    expect(Array.from(host.querySelectorAll('datalist option')).map(option => option.getAttribute('value'))).toEqual(models)

    await act(async () => root.unmount())
    const savedClient = new QueryClient()
    const savedHost = document.createElement('div')
    const savedRoot = createRoot(savedHost)
    state.data = {
      has_ai_key: true,
      ai_configured: true,
      ai_provider: 'openai_compat',
      ai_base_url: url,
      ai_openai_model: 'legacy-custom-model',
    }
    await act(async () => savedRoot.render(<QueryClientProvider client={savedClient}><SettingsAIPanel /></QueryClientProvider>))
    expect(Array.from(savedHost.querySelectorAll('input')).map(input => input.value)).toContain('legacy-custom-model')
    await act(async () => savedRoot.unmount())
    savedClient.clear()
  } finally {
    client.clear()
  }
})
