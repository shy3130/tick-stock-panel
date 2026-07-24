import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { Settings } from './Settings'
import { Auth } from './Auth'

vi.mock('./settings/Keys', () => ({ SettingsKeysPanel: () => <div>TickFlow 设置</div> }))
vi.mock('./settings/AI', () => ({ SettingsAIPanel: () => <div>AI 设置</div> }))
vi.mock('./settings/Monitoring', () => ({ SettingsMonitoringPanel: () => <div>实时监控</div> }))
vi.mock('./settings/ExtPages', () => ({ SettingsExtPagesPanel: () => <div>扩展页面</div> }))
vi.mock('./settings/MenuSettings', () => ({ SettingsMenuSettingsPanel: () => <div>菜单设置</div> }))
vi.mock('./settings/System', () => ({ SettingsSystemPanel: () => <div>系统设置</div> }))
vi.mock('./settings/CustomSignals', () => ({ SettingsCustomSignalsPanel: () => <div>信号库</div> }))
vi.mock('./settings/DataSources', () => ({ SettingsDataSourcesPanel: () => <div>数据源</div> }))
vi.mock('@/lib/api', () => ({
  api: { authStatus: vi.fn().mockResolvedValue({ configured: true, authenticated: false }) },
}))

const routerFuture = { v7_startTransition: true, v7_relativeSplatPath: true } as const

function withQueryClient(node: ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {node}
    </QueryClientProvider>
  )
}

describe('password change settings flow', () => {
  it('opens the account security panel from settings', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/settings']} future={routerFuture}>
        <Settings />
      </MemoryRouter>,
    )

    await user.click(screen.getByRole('tab', { name: '账户安全' }))

    expect(screen.getByRole('heading', { name: '修改访问密码' })).toBeInTheDocument()
  })

  it('shows the password-changed message on login', async () => {
    render(withQueryClient(
      <MemoryRouter
        initialEntries={[{ pathname: '/login', state: { passwordChanged: true } }]}
        future={routerFuture}
      >
        <Routes>
          <Route path="/login" element={<Auth />} />
        </Routes>
      </MemoryRouter>,
    ))

    expect(await screen.findByText('密码已修改，请使用新密码重新登录')).toBeInTheDocument()
  })
})
