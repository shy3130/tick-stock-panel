import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { DowStrategyCard } from './DowStrategyCard'

describe('DowStrategyCard', () => {
  it('shows a completion message when the HK scan succeeds with no matches', async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/dow-strategy/runs' && init?.method === 'POST') {
        return { ok: true, json: async () => ({ runId: 'scan-hk-1', status: 'queued' }) }
      }
      if (url === '/api/dow-strategy/runs/scan-hk-1') {
        return {
          ok: true,
          json: async () => ({
            runId: 'scan-hk-1',
            market: 'hk',
            status: 'complete',
            completed: 2600,
            total: 2600,
            selected: 0,
            failed: 0,
          }),
        }
      }
      return { ok: true, json: async () => ({ stocks: [] }) }
    })

    render(<DowStrategyCard market="hk" fetcher={fetcher as any} />)
    await userEvent.click(screen.getByRole('button', { name: '执行选股' }))

    expect(await screen.findByText('港股选股完成，当前暂无符合条件的股票')).toBeInTheDocument()
    expect(fetcher).toHaveBeenCalledWith(
      '/api/dow-strategy/runs',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetcher).toHaveBeenCalledWith('/api/dow-strategy/runs/scan-hk-1')
    expect(fetcher).toHaveBeenCalledWith('/api/dow-strategy/pool?market=hk&limit=80')
  })
})
