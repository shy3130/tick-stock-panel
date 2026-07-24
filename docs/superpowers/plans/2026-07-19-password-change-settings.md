# Settings Password Change Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an “账户安全” settings tab where an authenticated user can change the panel access password and then log in again with the new password.

**Architecture:** A focused `SettingsAccountSecurityPanel` owns the password form, client validation, existing API call, and success navigation. `Settings.tsx` only registers the tab, while `Auth.tsx` reads a one-time router state message. Existing FastAPI authentication endpoints and password storage remain unchanged.

**Tech Stack:** React 18, TypeScript, React Router 6, TanStack Query 5, Vitest 2, Testing Library, jsdom, Tailwind CSS.

## Global Constraints

- Keep the feature inside the existing single-user authentication model.
- Reuse `api.authChangePassword(oldPassword, newPassword)` and do not change the backend API contract.
- Require all three fields, a new password of at least 6 characters, matching new-password fields, and a new password different from the current password.
- After success, navigate to `/login` with the one-time message “密码已修改，请使用新密码重新登录”.
- Do not persist, log, or refill any password.
- Match existing settings-page styles; do not add a global form abstraction or global CSS.

---

### Task 1: Add component-test infrastructure and the password form

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/pnpm-lock.yaml`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/src/test-setup.ts`
- Create: `frontend/src/pages/settings/AccountSecurity.tsx`
- Create: `frontend/src/pages/settings/AccountSecurity.test.tsx`

**Interfaces:**
- Consumes: `api.authChangePassword(oldPassword: string, newPassword: string): Promise<{ ok: boolean }>` from `frontend/src/lib/api.ts`.
- Produces: `SettingsAccountSecurityPanel(): JSX.Element`, which navigates to `/login` with router state `{ passwordChanged: true }` after success.

- [ ] **Step 1: Install the minimal DOM test dependencies**

Run:

```bash
cd frontend
pnpm add -D @testing-library/jest-dom @testing-library/react @testing-library/user-event jsdom
```

Expected: `package.json` and `pnpm-lock.yaml` contain the four dev dependencies, with no production dependency changes.

- [ ] **Step 2: Configure the Vitest DOM environment**

Change `frontend/vite.config.ts` to import `defineConfig` from `vitest/config` and add:

```ts
test: {
  environment: 'jsdom',
  setupFiles: './src/test-setup.ts',
},
```

Create `frontend/src/test-setup.ts`:

```ts
import '@testing-library/jest-dom/vitest'
```

- [ ] **Step 3: Write failing password-form component tests**

Create `frontend/src/pages/settings/AccountSecurity.test.tsx` with a shared render helper using `MemoryRouter`, `Routes`, `Route`, `QueryClient`, and `QueryClientProvider`. Mock only the existing API boundary:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { SettingsAccountSecurityPanel } from './AccountSecurity'

vi.mock('@/lib/api', () => ({
  api: { authChangePassword: vi.fn() },
}))

const changePassword = vi.mocked(api.authChangePassword)

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/settings?tab=security']}>
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

  it('shows the backend error in the form', async () => {
    changePassword.mockRejectedValue(new Error('旧密码错误'))
    renderPanel()
    await fillPasswords('wrong-password', 'new-password', 'new-password')
    expect(await screen.findByText('旧密码错误')).toBeInTheDocument()
  })
})
```

- [ ] **Step 4: Run the new test and verify RED**

Run:

```bash
cd frontend
pnpm test -- --run src/pages/settings/AccountSecurity.test.tsx
```

Expected: FAIL because `./AccountSecurity` does not exist.

- [ ] **Step 5: Implement the minimal account-security panel**

Create `frontend/src/pages/settings/AccountSecurity.tsx`. Use three controlled password inputs with real `<label>` elements, one show/hide toggle, an inline error block, and a TanStack mutation:

```tsx
import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Eye, EyeOff, KeyRound, Loader2, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'

export function SettingsAccountSecurityPanel() {
  const navigate = useNavigate()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPasswords, setShowPasswords] = useState(false)
  const [error, setError] = useState('')

  const changePassword = useMutation({
    mutationFn: () => api.authChangePassword(currentPassword, newPassword),
    onSuccess: () => navigate('/login', { replace: true, state: { passwordChanged: true } }),
    onError: (reason: Error) => setError(reason.message || '密码修改失败'),
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (!currentPassword || !newPassword || !confirmPassword) {
      setError('请填写全部密码字段')
      return
    }
    if (newPassword.length < 6) {
      setError('新密码至少 6 位')
      return
    }
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致')
      return
    }
    if (newPassword === currentPassword) {
      setError('新密码不能与当前密码相同')
      return
    }
    changePassword.mutate()
  }

  const inputType = showPasswords ? 'text' : 'password'

  return (
    <>
      <PageHeader title="账户安全" subtitle="修改面板访问密码" />
      <section className="max-w-xl rounded-card border border-border bg-surface p-5">
        <div className="mb-5 flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">修改访问密码</h3>
        </div>
        <form className="space-y-4" onSubmit={submit}>
          {[
            ['current-password', '当前密码', currentPassword, setCurrentPassword],
            ['new-password', '新密码', newPassword, setNewPassword],
            ['confirm-password', '确认新密码', confirmPassword, setConfirmPassword],
          ].map(([id, label, value, setter]) => (
            <label key={id as string} htmlFor={id as string} className="block text-xs text-secondary">
              <span className="mb-1.5 block">{label as string}</span>
              <input
                id={id as string}
                type={inputType}
                value={value as string}
                autoComplete={id === 'current-password' ? 'current-password' : 'new-password'}
                onChange={event => (setter as (value: string) => void)(event.target.value)}
                className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent/50"
              />
            </label>
          ))}
          <button type="button" onClick={() => setShowPasswords(value => !value)} className="inline-flex items-center gap-1.5 text-xs text-secondary hover:text-foreground">
            {showPasswords ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            {showPasswords ? '隐藏密码' : '显示密码'}
          </button>
          {error && <div className="flex items-start gap-1.5 rounded-btn bg-danger/10 px-3 py-2 text-xs text-danger"><ShieldAlert className="mt-px h-3.5 w-3.5" />{error}</div>}
          <button type="submit" disabled={changePassword.isPending} className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-4 text-sm font-medium text-white disabled:opacity-50">
            {changePassword.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {changePassword.isPending ? '修改中…' : '修改密码'}
          </button>
        </form>
      </section>
    </>
  )
}
```

- [ ] **Step 6: Run the focused test and verify GREEN**

Run:

```bash
cd frontend
pnpm test -- --run src/pages/settings/AccountSecurity.test.tsx
```

Expected: 5 tests PASS with no warnings.

- [ ] **Step 7: Commit Task 1**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/vite.config.ts frontend/src/test-setup.ts frontend/src/pages/settings/AccountSecurity.tsx frontend/src/pages/settings/AccountSecurity.test.tsx
git commit -m "feat: add account security password form"
```

---

### Task 2: Register the settings entry and show the post-change login message

**Files:**
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/Auth.tsx`
- Create: `frontend/src/pages/password-change-flow.test.tsx`

**Interfaces:**
- Consumes: `SettingsAccountSecurityPanel` from Task 1 and router state `{ passwordChanged?: boolean }`.
- Produces: the `/settings?tab=security` entry and a one-time success message on `/login`.

- [ ] **Step 1: Write the failing settings/login flow tests**

Create `frontend/src/pages/password-change-flow.test.tsx`. Mock heavy settings panels to simple text nodes, render routes inside `MemoryRouter`, and assert the entry and the navigation-state message:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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

function withQueryClient(node: ReactNode) {
  return <QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>
}

describe('password change settings flow', () => {
  it('opens the account security panel from settings', async () => {
    render(<MemoryRouter initialEntries={['/settings']}><Settings /></MemoryRouter>)
    await userEvent.click(screen.getByRole('button', { name: '账户安全' }))
    expect(screen.getByRole('heading', { name: '修改访问密码' })).toBeInTheDocument()
  })

  it('shows the password-changed message on login', async () => {
    render(withQueryClient(
      <MemoryRouter initialEntries={[{ pathname: '/login', state: { passwordChanged: true } }]}>
        <Routes><Route path="/login" element={<Auth />} /></Routes>
      </MemoryRouter>,
    ))
    expect(await screen.findByText('密码已修改，请使用新密码重新登录')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the flow test and verify RED**

Run:

```bash
cd frontend
pnpm test -- --run src/pages/password-change-flow.test.tsx
```

Expected: FAIL because the “账户安全” tab and password-changed message do not exist.

- [ ] **Step 3: Register the account-security tab**

In `frontend/src/pages/Settings.tsx`:

```tsx
import { BarChart3, Database, Key, LockKeyhole, Radio, SlidersHorizontal, Sparkles, Settings2, Zap } from 'lucide-react'
import { SettingsAccountSecurityPanel } from './settings/AccountSecurity'
```

Add the tab immediately after the TickFlow tab:

```tsx
{ key: 'security', label: '账户安全', icon: LockKeyhole, panel: SettingsAccountSecurityPanel },
```

- [ ] **Step 4: Display the one-time login message**

In `frontend/src/pages/Auth.tsx`, import `useLocation`, read the state, and render the success banner above the form:

```tsx
import { useLocation, useNavigate } from 'react-router-dom'
import { CheckCircle2, Eye, EyeOff, Loader2, Lock, ShieldCheck, ShieldAlert, Sparkles } from 'lucide-react'

const location = useLocation()
const passwordChanged = Boolean((location.state as { passwordChanged?: boolean } | null)?.passwordChanged)
```

```tsx
{passwordChanged && !isSetup && (
  <div className="mb-3 flex items-start gap-1.5 rounded-btn bg-bull/10 px-3 py-2 text-[11px] text-bull">
    <CheckCircle2 className="mt-px h-3.5 w-3.5 shrink-0" />
    <span>密码已修改，请使用新密码重新登录</span>
  </div>
)}
```

Use router location state rather than query parameters or storage, so the message is not persisted and passwords never enter the URL.

- [ ] **Step 5: Run both focused test files and verify GREEN**

Run:

```bash
cd frontend
pnpm test -- --run src/pages/settings/AccountSecurity.test.tsx src/pages/password-change-flow.test.tsx
```

Expected: 7 tests PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add frontend/src/pages/Settings.tsx frontend/src/pages/Auth.tsx frontend/src/pages/password-change-flow.test.tsx
git commit -m "feat: expose password change in settings"
```

---

### Task 3: Full verification and browser acceptance

**Files:**
- Modify only if verification reveals a defect in files introduced by Tasks 1–2.

**Interfaces:**
- Consumes: completed settings password-change flow.
- Produces: verified test, build, and browser behavior.

- [ ] **Step 1: Run the full frontend test suite**

Run:

```bash
cd frontend
pnpm test -- --run
```

Expected: all tests PASS with no unhandled errors.

- [ ] **Step 2: Run the production build**

Run:

```bash
cd frontend
pnpm build
```

Expected: TypeScript and Vite build complete successfully and write `frontend/dist`.

- [ ] **Step 3: Verify the live interaction in a browser**

Start the existing development stack, sign in, and verify:

1. `/settings?tab=security` displays the three password fields.
2. Empty, short, mismatched, and unchanged passwords show the exact inline errors without a request.
3. The show/hide control changes all fields between `password` and `text`.
4. A wrong current password displays the backend error without clearing input.
5. A successful change redirects to `/login` and displays “密码已修改，请使用新密码重新登录”.
6. The old password fails and the new password signs in successfully.
7. The panel remains readable in both dark and light themes.

Expected: all seven checks pass. Because the final check changes a real credential, use a temporary test deployment or restore the intended password immediately after acceptance.

- [ ] **Step 4: Review the final diff for scope and secrets**

Run:

```bash
git diff HEAD~2 --check
git diff HEAD~2 -- frontend
```

Expected: only the planned settings/auth/test files and dependency metadata changed; no literal real password, token, generated `dist`, or unrelated formatting appears.
