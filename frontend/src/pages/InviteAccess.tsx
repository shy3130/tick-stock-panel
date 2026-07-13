import { useEffect, useState, type FormEvent } from 'react'
import { motion } from 'framer-motion'
import { ArrowRight, CircleAlert, KeyRound, Loader2, ShieldCheck } from 'lucide-react'
import { Logo } from '@/components/Logo'
import { api } from '@/lib/api'
import { BRAND_NAME } from '@/lib/brand'

function redirectTarget(): string {
  const value = new URLSearchParams(window.location.search).get('redirect') || '/'
  if (!value.startsWith('/') || value.startsWith('//') || value.startsWith('/invite')) return '/'
  return value
}

export function InviteAccess() {
  const [code, setCode] = useState('')
  const [checking, setChecking] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api.inviteStatus()
      .then(status => {
        if (status.authorized) {
          window.location.replace(redirectTarget())
          return
        }
        setChecking(false)
      })
      .catch(() => setChecking(false))
  }, [])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const inviteCode = code.trim()
    if (!inviteCode || submitting) return
    setError('')
    setSubmitting(true)
    try {
      await api.redeemInvite(inviteCode)
      window.location.replace(redirectTarget())
    } catch (err) {
      setError(err instanceof Error ? err.message : '验证失败，请稍后重试')
      setSubmitting(false)
    }
  }

  if (checking) {
    return (
      <div className="grid min-h-screen place-items-center bg-[#11100d] text-[#c7ad68]">
        <Loader2 className="h-5 w-5 animate-spin" aria-label="正在验证访问状态" />
      </div>
    )
  }

  return (
    <main className="relative grid min-h-screen overflow-hidden bg-[#11100d] px-4 py-10 text-[#f5f0e5] sm:px-6">
      <div
        className="pointer-events-none absolute inset-0 opacity-40"
        style={{
          backgroundImage:
            'linear-gradient(rgba(199,173,104,0.045) 1px, transparent 1px), linear-gradient(90deg, rgba(199,173,104,0.045) 1px, transparent 1px)',
          backgroundSize: '64px 64px',
        }}
      />
      <div className="pointer-events-none absolute inset-x-0 top-[22%] h-px bg-[#c7ad68]/15" />
      <div className="pointer-events-none absolute bottom-[18%] inset-x-0 h-px bg-[#c7ad68]/10" />

      <header className="relative z-10 mx-auto flex w-full max-w-6xl items-center justify-between self-start">
        <div className="flex items-center gap-3">
          <Logo className="h-7 w-7 text-[#d0b873]" />
          <div>
            <div className="text-sm font-semibold text-[#f5f0e5]">{BRAND_NAME}</div>
            <div className="font-mono text-[9px] uppercase text-[#8f856e]">Quant Workbench</div>
          </div>
        </div>
        <div className="font-mono text-[9px] uppercase text-[#8f856e]">Private beta · 05</div>
      </header>

      <motion.section
        role="dialog"
        aria-modal="true"
        aria-labelledby="invite-title"
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 mx-auto my-auto w-full max-w-[420px] self-center rounded-lg border border-[#554a32] bg-[#191711] p-6 shadow-[0_24px_80px_rgba(0,0,0,0.42)] sm:p-8"
      >
        <div className="mb-7 flex items-start justify-between gap-4">
          <div className="grid h-11 w-11 place-items-center rounded-md border border-[#665839] bg-[#211e16] text-[#d0b873]">
            <KeyRound className="h-5 w-5" />
          </div>
          <div className="flex gap-1.5 pt-2" aria-hidden="true">
            {Array.from({ length: 5 }).map((_, index) => (
              <span
                key={index}
                className={`h-1.5 w-5 rounded-sm ${index === 0 ? 'bg-[#d0b873]' : 'bg-[#4a422f]'}`}
              />
            ))}
          </div>
        </div>

        <div className="mb-6">
          <div className="mb-2 font-mono text-[9px] uppercase text-[#a99359]">Invitation required</div>
          <h1 id="invite-title" className="text-xl font-semibold text-[#f7f2e8]">内测访问</h1>
          <p className="mt-2 text-sm leading-6 text-[#aaa08c]">使用分配给你的专属邀请码进入量化工作台。</p>
        </div>

        <form onSubmit={submit} className="space-y-3">
          <label className="block">
            <span className="mb-2 block text-[11px] font-medium text-[#c9c0ae]">邀请码</span>
            <div className="relative">
              <input
                type="text"
                value={code}
                onChange={event => setCode(event.target.value)}
                placeholder="输入专属邀请码"
                autoComplete="one-time-code"
                autoCapitalize="none"
                spellCheck={false}
                autoFocus
                className="h-11 w-full rounded-md border border-[#554a32] bg-[#100f0c] px-3 pr-10 font-mono text-sm text-[#f7f2e8] outline-none transition-colors placeholder:text-[#706858] focus:border-[#bda35f]"
              />
              <ShieldCheck className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#766a4b]" />
            </div>
          </label>

          <div className="min-h-9" aria-live="polite">
            {error && (
              <div className="flex min-h-9 items-center gap-2 rounded-md border border-[#7e3f35]/60 bg-[#3a1e1a]/60 px-3 text-[11px] text-[#f1a69a]">
                <CircleAlert className="h-3.5 w-3.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}
          </div>

          <button
            type="submit"
            disabled={!code.trim() || submitting}
            className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-md bg-[#c7ad68] text-sm font-semibold text-[#17140d] transition-colors hover:bg-[#d3bc7b] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#d3bc7b] focus-visible:ring-offset-2 focus-visible:ring-offset-[#191711] disabled:cursor-not-allowed disabled:opacity-45"
          >
            {submitting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                正在验证
              </>
            ) : (
              <>
                进入工作台
                <ArrowRight className="h-4 w-4" />
              </>
            )}
          </button>
        </form>

        <div className="mt-6 flex items-center justify-between border-t border-[#403823] pt-4 font-mono text-[9px] uppercase text-[#756c5a]">
          <span>Sycee access control</span>
          <span>Encrypted session</span>
        </div>
      </motion.section>

      <footer className="relative z-10 mx-auto flex w-full max-w-6xl items-end justify-between self-end font-mono text-[9px] uppercase text-[#6f685a]">
        <span>Intelligence, refined.</span>
        <span>© 2026 Sycee</span>
      </footer>
    </main>
  )
}
