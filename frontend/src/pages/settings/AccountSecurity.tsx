import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Eye, EyeOff, KeyRound, Loader2, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'

const INPUT_CLASS = 'h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors focus:border-accent/50'

export function passwordChangeErrorMessage(reason: unknown) {
  return reason instanceof Error && reason.message ? reason.message : '密码修改失败'
}

export function SettingsAccountSecurityPanel() {
  const navigate = useNavigate()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPasswords, setShowPasswords] = useState(false)
  const [error, setError] = useState('')
  const [isPending, setIsPending] = useState(false)

  const changePassword = () => {
    setIsPending(true)
    const showRequestError = (reason: unknown) => {
      setError(passwordChangeErrorMessage(reason))
    }
    let request: ReturnType<typeof api.authChangePassword>
    try {
      request = api.authChangePassword(currentPassword, newPassword)
    } catch (reason) {
      showRequestError(reason)
      setIsPending(false)
      return
    }
    void request.then(() => navigate('/login', {
        replace: true,
        state: { passwordChanged: true },
      }), showRequestError)
      .then(() => setIsPending(false))
  }

  const handleSubmit = (event: FormEvent) => {
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

    void changePassword()
  }

  const inputType = showPasswords ? 'text' : 'password'

  return (
    <>
      <PageHeader
        title="账户安全"
        subtitle="修改面板访问密码"
      />

      <section className="max-w-xl rounded-card border border-border bg-surface p-5">
        <div className="mb-5 flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">修改访问密码</h3>
        </div>

        <form className="space-y-4" onSubmit={handleSubmit}>
          <label htmlFor="current-password" className="block text-xs text-secondary">
            <span className="mb-1.5 block">当前密码</span>
            <input
              id="current-password"
              type={inputType}
              autoComplete="current-password"
              value={currentPassword}
              onChange={event => setCurrentPassword(event.target.value)}
              className={INPUT_CLASS}
            />
          </label>

          <label htmlFor="new-password" className="block text-xs text-secondary">
            <span className="mb-1.5 block">新密码</span>
            <input
              id="new-password"
              type={inputType}
              autoComplete="new-password"
              value={newPassword}
              onChange={event => setNewPassword(event.target.value)}
              className={INPUT_CLASS}
            />
          </label>

          <label htmlFor="confirm-password" className="block text-xs text-secondary">
            <span className="mb-1.5 block">确认新密码</span>
            <input
              id="confirm-password"
              type={inputType}
              autoComplete="new-password"
              value={confirmPassword}
              onChange={event => setConfirmPassword(event.target.value)}
              className={INPUT_CLASS}
            />
          </label>

          <button
            type="button"
            onClick={() => setShowPasswords(value => !value)}
            className="inline-flex items-center gap-1.5 text-xs text-secondary transition-colors hover:text-foreground"
          >
            {showPasswords
              ? <EyeOff className="h-3.5 w-3.5" />
              : <Eye className="h-3.5 w-3.5" />}
            {showPasswords ? '隐藏密码' : '显示密码'}
          </button>

          {error && (
            <div className="flex items-start gap-1.5 rounded-btn bg-danger/10 px-3 py-2 text-xs text-danger">
              <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={isPending}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-4 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
          >
            {isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {isPending ? '修改中…' : '修改密码'}
          </button>
        </form>
      </section>
    </>
  )
}
