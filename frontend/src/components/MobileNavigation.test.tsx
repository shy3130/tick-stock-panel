import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, NavLink } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { MobileNavigation, mobilePageTitle } from './MobileNavigation'

const items = [
  { to: '/', label: '看板' },
  { to: '/watchlist', label: '自选股' },
]

function renderNavigation() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <MobileNavigation title="看板" market="cn" onMarketChange={vi.fn()}>
        <NavLink to="/watchlist">自选股</NavLink>
      </MobileNavigation>
    </MemoryRouter>,
  )
}

describe('MobileNavigation', () => {
  it('maps nested routes to the longest matching page title', () => {
    expect(mobilePageTitle('/watchlist/detail', items)).toBe('自选股')
    expect(mobilePageTitle('/', items)).toBe('看板')
    expect(mobilePageTitle('/settings', items)).toBe('设置')
  })

  it('opens the drawer and closes it with Escape', async () => {
    const user = userEvent.setup()
    renderNavigation()
    await user.click(screen.getByRole('button', { name: '打开导航菜单' }))
    expect(screen.getByRole('dialog', { name: '主导航' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '主导航' })).not.toBeInTheDocument()
    })
  })

  it('closes the drawer after route navigation', async () => {
    const user = userEvent.setup()
    renderNavigation()
    await user.click(screen.getByRole('button', { name: '打开导航菜单' }))
    await user.click(screen.getByRole('link', { name: '自选股' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '主导航' })).not.toBeInTheDocument()
    })
  })
})
