import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { DowStrategyCard } from './DowStrategyCard'

describe('DowStrategyCard', () => {
  it('recovers a transient connection before starting the non-idempotent scan', async () => {
    let healthAttempts = 0
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/health') {
        healthAttempts += 1
        if (healthAttempts === 1) throw new TypeError('Failed to fetch')
        return { ok: true, json: async () => ({ status: 'ok' }) }
      }
      if (url === '/api/dow-strategy/runs' && init?.method === 'POST') {
        return { ok: true, json: async () => ({ runId: 'scan-hk-recovered', status: 'queued' }) }
      }
      if (url === '/api/dow-strategy/runs/scan-hk-recovered') {
        return {
          ok: true,
          json: async () => ({
            runId: 'scan-hk-recovered',
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
    expect(fetcher.mock.calls.slice(0, 3).map(([url]) => url)).toEqual([
      '/health',
      '/health',
      '/api/dow-strategy/runs',
    ])
    expect(screen.queryByText('Failed to fetch')).not.toBeInTheDocument()
  })

  it('shows a Chinese retry message without submitting when the service stays unavailable', async () => {
    const fetcher = vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    })

    render(<DowStrategyCard market="hk" fetcher={fetcher as any} />)
    await userEvent.click(screen.getByRole('button', { name: '执行选股' }))

    expect(await screen.findByText('服务连接中断，请稍后重新执行')).toBeInTheDocument()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(screen.queryByText('Failed to fetch')).not.toBeInTheDocument()
  })

  it('shows a completion message when the HK scan succeeds with no matches', async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/health') {
        return { ok: true, json: async () => ({ status: 'ok' }) }
      }
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
