// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { GenerateFactorStrategyDialog } from './GenerateFactorStrategyDialog'

const mocks = vi.hoisted(() => ({ save: vi.fn(), close: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { strategySaveCodeV2: mocks.save } }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
const container = document.createElement('div')
document.body.appendChild(container)
let root: Root
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.save.mockReset().mockResolvedValue({ strategy_id: 'custom_factor_momentum_5d' })
  mocks.close.mockReset()
  root = createRoot(container)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => { act(() => root.unmount()); qc.clear(); container.innerHTML = '' })

it.each(['high', 'low'] as const)('generates a loadable rank strategy using the current module path and %s scoring', async direction => {
  await act(async () => root.render(<QueryClientProvider client={qc}><GenerateFactorStrategyDialog
    item={{ id: 'momentum_5d', label: '5日动量', group: '动量', direction }} onClose={mocks.close} /></QueryClientProvider>))
  const generate = [...container.querySelectorAll('button')].find(button => button.textContent?.trim() === '生成策略')!
  await act(async () => generate.click())
  const payload = mocks.save.mock.calls[0][0]
  expect(payload.strategy_id).toBe('custom_factor_momentum_5d')
  expect(payload.target_source).toBe('custom')
  expect(payload.code).toContain('from app.strategy.research.factor_rank_research import FactorRankResearchMatrixStrategy')
  expect(payload.code).toContain('EXECUTION_BACKEND = "matrix_native"')
  expect(payload.code).toContain(`DIRECTIONS = {"momentum_5d": "${direction}"}`)
  expect(mocks.close).toHaveBeenCalledTimes(1)
})
