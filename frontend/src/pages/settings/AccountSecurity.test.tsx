import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { passwordChangeErrorMessage, SettingsAccountSecurityPanel } from './AccountSecurity'

const changePassword = vi.spyOn(api, 'authChangePassword')

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={['/settings?tab=security']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/settings" element={<SettingsAccountSecurityPanel />} />
          <Route path="/login" element={<div>登录页</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function fillPasswords(current: string, next: string, confirm: string) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('当前密码'), current)
  await user.type(screen.getByLabelText('新密码'), next)
  await user.type(screen.getByLabelText('确认新密码'), confirm)
  await user.click(screen.getByRole('button', { name: '修改密码' }))
}

describe('SettingsAccountSecurityPanel', () => {
  beforeEach(() => changePassword.mockReset())

  it('rejects a new password shorter than 6 characters', async () => {
    renderPanel()
    await fillPasswords('old-password', '12345', '12345')
    expect(screen.getByText('新密码至少 6 位')).toBeInTheDocument()
    expect(changePassword).not.toHaveBeenCalled()
  })

  it('rejects mismatched new passwords', async () => {
    renderPanel()
    await fillPasswords('old-password', 'new-password', 'different-password')
    expect(screen.getByText('两次输入的新密码不一致')).toBeInTheDocument()
    expect(changePassword).not.toHaveBeenCalled()
  })

  it('rejects a new password matching the current password', async () => {
    renderPanel()
    await fillPasswords('same-password', 'same-password', 'same-password')
    expect(screen.getByText('新密码不能与当前密码相同')).toBeInTheDocument()
    expect(changePassword).not.toHaveBeenCalled()
  })

  it('submits valid passwords and navigates to login', async () => {
    changePassword.mockResolvedValue({ ok: true })
    renderPanel()
    await fillPasswords('old-password', 'new-password', 'new-password')
    expect(changePassword).toHaveBeenCalledWith('old-password', 'new-password')
    expect(await screen.findByText('登录页')).toBeInTheDocument()
  })

  it('preserves the backend error message for the form', () => {
    expect(passwordChangeErrorMessage(new Error('旧密码错误'))).toBe('旧密码错误')
  })
})
