// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { StrategyOptions } from './StrategyOptions'
import { selectableStrategies, strategyOptionLabel, useStrategyLibrarySync } from '@/lib/strategyLibrary'
import { selectableDefinitions } from '@/lib/definitionOptions'
import { QK } from '@/lib/queryKeys'
import type { StrategyChoice } from '@/lib/strategyLibrary'

const choices: StrategyChoice[] = [
  { id: 'old_format', name: '旧格式策略' },
  { id: 'active', name: '活跃策略', library: { group: 'g', group_name: '长期观察', archived: false, archived_at: null, added_at: null } },
  { id: 'archived', name: '旧策略', library: { group: 'g', group_name: '长期观察', archived: true, archived_at: '2026-11-08', added_at: null } },
]
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })

it('groups new choices and hides archives while retaining a saved archived selection', async () => {
  await act(async () => root.render(<select><StrategyOptions strategies={choices} /></select>))
  expect([...host.querySelectorAll('option')].map(option => option.value)).toEqual(['old_format', 'active'])
  expect(host.querySelector('optgroup')?.label).toBe('长期观察')
  await act(async () => root.render(<select><StrategyOptions strategies={choices} selected={['archived']} /></select>))
  expect(host.querySelector('option[value="archived"]')?.textContent).toBe('旧策略（已归档）')
  expect(selectableStrategies(choices).map(item => item.id)).toEqual(['old_format', 'active'])
  expect(strategyOptionLabel(choices[0])).toBe('旧格式策略')
})

it('keeps existing factor references without offering archived factors for new choices', () => {
  const factors = [{ id: 'legacy' }, { id: 'active', archived: false }, { id: 'old', archived: true }]
  expect(selectableDefinitions(factors).map(item => item.id)).toEqual(['legacy', 'active'])
  expect(selectableDefinitions(factors, ['old'])).toEqual(factors)
})

it('refreshes cached backtest, pool and definition choices after another window changes the library', async () => {
  const qc = new QueryClient()
  const keys = [QK.strategyPool, QK.strategyList(), QK.screenerStrategies('etf', 'all'), QK.definitions, QK.factorColumns]
  keys.forEach(key => qc.setQueryData(key, { cached: true }))
  function Probe() { useStrategyLibrarySync(); return null }
  await act(async () => root.render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>))
  await act(async () => window.dispatchEvent(new StorageEvent('storage', { key: 'tsp-strategy-library-changed', newValue: 'next' })))
  for (const key of keys) expect(qc.getQueryState(key)?.isInvalidated).toBe(true)
  keys.forEach(key => qc.setQueryData(key, { cached: 'restored' }))
  await act(async () => window.dispatchEvent(new Event('focus')))
  for (const key of keys) expect(qc.getQueryState(key)?.isInvalidated).toBe(true)
  qc.clear()
})
