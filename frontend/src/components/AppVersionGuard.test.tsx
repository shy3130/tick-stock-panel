import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AppVersionGuard } from './AppVersionGuard'


function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  })
})

describe('AppVersionGuard', () => {
  it('prompts a visible page and reloads only after user confirmation', async () => {
    const reload = vi.fn()
    render(
      <AppVersionGuard
        currentBuildId="old"
        loadVersion={async () => ({ build_id: 'new', published_at: null })}
        reload={reload}
        pollIntervalMs={60_000}
      />,
      { wrapper },
    )

    const prompt = await screen.findByRole('status')
    expect(prompt).toHaveTextContent('发现新版本')
    expect(reload).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: '立即刷新' }))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('reloads a hidden idle page without showing a prompt', async () => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'hidden',
    })
    const reload = vi.fn()
    render(
      <AppVersionGuard
        currentBuildId="old"
        loadVersion={async () => ({ build_id: 'new', published_at: null })}
        reload={reload}
      />,
      { wrapper },
    )

    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('ignores a failed version check', async () => {
    render(
      <AppVersionGuard
        currentBuildId="old"
        loadVersion={async () => {
          throw new Error('offline')
        }}
      />,
      { wrapper },
    )

    await vi.waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })
  })
})
