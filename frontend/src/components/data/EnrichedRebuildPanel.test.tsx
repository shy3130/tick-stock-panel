import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'
import { EnrichedRebuildPanel } from './EnrichedRebuildPanel'

vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { enriched_batch_size: 1000 } }),
}))

const rebuildEnriched = vi.spyOn(api, 'rebuildEnriched')

function renderPanel(onStart: (jobId: string) => void) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <EnrichedRebuildPanel isRunning={false} onStart={onStart} />
    </QueryClientProvider>,
  )
}

describe('EnrichedRebuildPanel', () => {
  beforeEach(() => rebuildEnriched.mockReset())

  it('reports the started rebuild job to its owner', async () => {
    rebuildEnriched.mockResolvedValue({ status: 'started', job_id: 'job-42' })
    const onStart = vi.fn()
    const user = userEvent.setup()

    renderPanel(onStart)
    await user.click(screen.getByRole('button', { name: '全量计算' }))

    await waitFor(() => expect(onStart).toHaveBeenCalledWith('job-42'))
  })
})
