/**
 * 访问认证页 — 复用同一组件处理「首次设密码」和「登录」两种状态。
 *
 * 根据后端 /api/auth/status 的 configured 字段决定显示:
 *   - configured=false → 显示「设置访问密码」(首次)
 *   - configured=true  → 显示「登录」
 *
 * 安全:
 *   - 设密码接口后端限本机/内网; 公网用户设密码会被 403 拒绝, 页面据此提示。
 *   - 登录失败由后端限流(5次锁5分钟), 429 时前端显示等待提示。
 */
import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Eye, EyeOff, Loader2, Lock, ShieldCheck, ShieldAlert, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { Logo } from '@/components/Logo'
import { cn } from '@/lib/cn'

export function Auth() {
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')  // 仅设密码时用
  const [showPwd, setShowPwd] = useState(false)
  const [localError, setLocalError] = useState('')

  // 取认证状态(是否已设密码)
  const [status, setStatus] = useState<{ configured: boolean } | null>(null)
  useEffect(() => {
    api.authStatus().then(s => {
      setStatus(s)
      // 已登录的话直接进面板(避免登录页死循环)
      if (s.authenticated) navigate('/', { replace: true })
    }).catch(() => setStatus({ configured: false }))
  }, [navigate])

  const isSetup = !status?.configured  // configured=false → 设密码模式

  // 登录 / 设密码 共用一个 mutation(按 isSetup 调不同接口)
  const submitMut = useMutation({
    mutationFn: async () => {
      if (isSetup) {
        return api.authSetup(password)
      }
      return api.authLogin(password)
    },
    onSuccess: () => {
      // 成功: 跳回原页面(或首页)
      const redirect = new URLSearchParams(window.location.search).get('redirect') || '/'
      navigate(redirect, { replace: true })
    },
    onError: (err: any) => {
      const msg = err?.message || (isSetup ? '设置失败' : '登录失败')
      // 设密码/登录失败必须显示: 401(密码错)/403(公网设密码被拒)/429(限流) 都要提示
      setLocalError(msg)
    },
  })

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    setLocalError('')
    if (isSetup) {
      if (password.length < 6) { setLocalError('密码至少 6 位'); return }
      if (password !== confirmPassword) { setLocalError('两次密码不一致'); return }
    }
    submitMut.mutate()
  }

  if (!status) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <Loader2 className="h-6 w-6 animate-spin text-muted" />
      </div>
    )
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-base px-4">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        className="relative w-full max-w-sm"
      >
        {/* Logo */}
        <div className="mb-6 flex flex-col items-center gap-2">
          <Logo className="h-10 w-10" />
          <h1 className="text-lg font-semibold tracking-tight text-foreground">Quant Research Workbench</h1>
          <p className="section-kicker">Access Control</p>
        </div>

        <div className="panel p-5">
          {/* 标题区: 图标 + 文案随模式切换 */}
          <div className="mb-5 flex items-center gap-2.5">
            <div className={cn(
              'grid h-9 w-9 place-items-center rounded-btn',
              isSetup ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary',
            )}>
              {isSetup ? <ShieldCheck className="h-5 w-5" /> : <Lock className="h-5 w-5" />}
            </div>
            <div>
              <div className="section-title">
                {isSetup ? '设置访问密码' : '登录访问'}
              </div>
              <div className="text-[11px] text-muted">
                {isSetup ? '首次使用, 请为面板设置访问密码' : '请输入访问密码以继续'}
              </div>
            </div>
          </div>

          <form onSubmit={handleSubmit} className="space-y-3">
            {/* 密码输入 */}
            <div className="relative">
              <input
                type={showPwd ? 'text' : 'password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="访问密码"
                autoFocus
                className="control w-full pr-9"
              />
              <button
                type="button"
                onClick={() => setShowPwd(s => !s)}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-foreground"
                tabIndex={-1}
              >
                {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>

            {/* 确认密码(仅设密码模式) */}
            {isSetup && (
              <input
                type={showPwd ? 'text' : 'password'}
                value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                placeholder="再次输入密码"
                className="control w-full"
              />
            )}

            {/* 错误提示 */}
            {(localError || submitMut.error) && (
              <div className="flex items-start gap-1.5 rounded-input border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] text-danger">
                <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
                <span>{localError}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={submitMut.isPending || !password}
              className="btn-primary w-full"
            >
              {submitMut.isPending ? (
                <><Loader2 className="h-4 w-4 animate-spin" />处理中…</>
              ) : (
                <>{isSetup ? '设置并进入' : '登录'}</>
              )}
            </button>
          </form>

          {/* 提示: 设密码模式告知本机限制 */}
          {isSetup && (
            <div className="mt-3 space-y-1.5 text-[10px] leading-relaxed text-muted/70">
              <p>
                出于安全考虑, 首次设置密码需在服务器本机或内网访问时操作。公网环境下仅可登录。
              </p>
              <p>
                访问密码和网络访问策略由部署管理员按当前运行环境统一配置。
              </p>
            </div>
          )}
        </div>

        <div className="mt-4 flex items-center justify-center gap-1.5 text-[10px] text-muted">
          <Sparkles className="h-3 w-3" />
          自托管量化工作台 · 数据完全掌握在自己手里
        </div>
      </motion.div>
    </div>
  )
}
