import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Eye,
  EyeOff,
  Loader2,
  Save,
  Check,
  CheckCircle2,
  AlertCircle,
  ArrowRight,
  ArrowLeft,
  ExternalLink,
  Sparkles,
  LineChart,
  ScanSearch,
  Flame,
  Zap,
  Radar,
  ShieldCheck,
  BellRing,
  TrendingUp,
  FileText,
  Landmark,
  Database,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useCapabilities, useSettings } from '@/lib/useSharedQueries'
import { QK } from '@/lib/queryKeys'
import { CAP_LABELS } from '@/lib/capability-labels'
import { Logo } from '@/components/Logo'

// ===== 引导页:5 步向导 =====
// 0. 声明  1. 欢迎  2. 输入 Key(可跳过)  3. 能力探测结果  4. 完成 → 写标记 → 进面板

const STEPS = ['声明', '欢迎', '配置 Key', '能力探测', '完成'] as const

const BRAND = '#8B5CF6'

const HIGHLIGHTS = [
  { icon: LineChart,   title: '看板与自选', desc: '市场全景看板、涨跌分布、情绪雷达,自定义自选列表', tint: 'text-accent' },
  { icon: ScanSearch,  title: '策略选股',   desc: '内置多套选股策略,一键扫描全市场命中标的', tint: 'text-bull' },
  { icon: TrendingUp,  title: '个股分析',   desc: 'AI 五维分析个股,资金状态、关键价位与技术形态一目了然', tint: 'text-warning' },
  { icon: Flame,       title: '连板梯队',   desc: '涨停梯队、封板强度、炸板监控,情绪温度计', tint: 'text-warning' },
  { icon: Landmark,    title: '概念行业',   desc: '概念板块、行业维度的资金流向与热度排名', tint: 'text-accent' },
  { icon: FileText,    title: '财务分析',   desc: 'AI 解读财报,利润、资负、现金流、核心指标', tint: 'text-bear' },
  { icon: ShieldCheck, title: '回测验证',   desc: '策略历史回测、因子分析,用数据验证逻辑', tint: 'text-accent' },
  { icon: Radar,       title: '实时监控',   desc: '自定义条件 / 策略监控,盘中触发即推送告警', tint: 'text-bear' },
  { icon: BellRing,    title: '本地优先',   desc: '数据本地存储,隐私可控,断网仍可查阅', tint: 'text-bull' },
]

export function Onboarding() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [step, setStep] = useState(0)

  // 完成向导 —— 写后端标记,使守卫放行
  const complete = useMutation({
    mutationFn: api.completeOnboarding,
    onSuccess: (data) => {
      // 用接口返回值同步更新缓存,确保跳转时守卫立即看到 onboarding_completed: true
      // (避免 invalidate 后台重取未返回时, 守卫用旧缓存 false 误重定向回引导页)
      qc.setQueryData(QK.settings, (old: any) =>
        old ? { ...old, onboarding_completed: data.onboarding_completed } : old,
      )
      qc.invalidateQueries({ queryKey: QK.settings })
      navigate('/', { replace: true })
    },
    onError: () => {
      // 标记失败不应阻塞用户进入面板,仍放行
      navigate('/', { replace: true })
    },
  })

  const finish = () => complete.mutate()

  return (
    <div className="relative min-h-screen bg-base overflow-hidden flex flex-col">
      {/* 背景光晕 —— 品牌 + 主色渐变 */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          className="absolute -top-40 -left-40 h-[28rem] w-[28rem] rounded-full blur-[120px] opacity-20"
          style={{ background: `radial-gradient(circle, ${BRAND}, transparent 70%)` }}
        />
        <div
          className="absolute -bottom-40 -right-32 h-[26rem] w-[26rem] rounded-full blur-[120px] opacity-15"
          style={{ background: 'radial-gradient(circle, hsl(var(--accent)), transparent 70%)' }}
        />
        {/* 极淡网格底纹 */}
        <div
          className="absolute inset-0 opacity-[0.025]"
          style={{
            backgroundImage:
              'linear-gradient(hsl(var(--fg-primary)) 1px, transparent 1px), linear-gradient(90deg, hsl(var(--fg-primary)) 1px, transparent 1px)',
            backgroundSize: '40px 40px',
          }}
        />
      </div>

      {/* 顶栏:logo + 进度指示 */}
      <header className="relative z-10 flex items-center justify-between px-6 py-4 border-b border-border">
        <div className="flex items-center gap-2.5 text-foreground">
          <Logo
            size={24}
            className="shrink-0"
            style={{ color: BRAND, filter: `drop-shadow(0 0 8px ${BRAND}55)` }}
          />
          <span className="text-sm font-semibold tracking-tight">TickFlow Stock Panel</span>
        </div>
        {/* 步骤进度条 —— 胶囊式 */}
        <div className="flex items-center gap-1.5">
          {STEPS.map((label, i) => (
            <div key={label} className="flex items-center gap-1.5">
              {i > 0 && <div className="h-px w-3 bg-border" />}
              <motion.div
                animate={{
                  width: i === step ? 64 : 24,
                  backgroundColor: i === step
                    ? 'hsl(var(--accent))'
                    : i < step
                      ? 'hsl(var(--accent) / 0.6)'
                      : 'hsl(var(--border))',
                }}
                transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
                className="h-1.5 rounded-full"
              />
            </div>
          ))}
        </div>
        <div className="w-[88px] text-right">
          <span className="text-xs text-muted tabular">
            {step + 1} / {STEPS.length}
          </span>
        </div>
      </header>

      {/* 步骤内容 */}
      <main className="relative z-10 flex-1 flex items-center justify-center px-6 py-10">
        <div className="w-full max-w-xl">
          <AnimatePresence mode="wait">
            <motion.div
              key={step}
              initial={{ opacity: 0, x: 24 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -24 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            >
              {step === 0 && <DisclaimerStep onNext={() => setStep(1)} />}
              {step === 1 && <WelcomeStep onNext={() => setStep(2)} onSkip={finish} />}
              {step === 2 && (
                <KeyStep onNext={() => setStep(3)} onSkip={() => setStep(3)} onBack={() => setStep(1)} />
              )}
              {step === 3 && <ResultStep onNext={() => setStep(4)} onBack={() => setStep(2)} />}
              {step === 4 && <FinishStep onNext={finish} onBack={() => setStep(3)} pending={complete.isPending} />}
            </motion.div>
          </AnimatePresence>
        </div>
      </main>
    </div>
  )
}

// ===== Step 0: 声明 =====

function DisclaimerStep({ onNext }: { onNext: () => void }) {
  return (
    <div className="text-center">
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="mx-auto w-fit rounded-2xl p-4 border border-warning/40"
        style={{ background: 'linear-gradient(135deg, hsl(var(--warning) / 0.15), transparent)' }}
      >
        <AlertCircle className="h-8 w-8 text-warning" />
      </motion.div>

      <h1 className="mt-6 text-2xl font-bold text-foreground tracking-tight">使用前请知悉</h1>

      <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-5 text-left">
        <div className="flex items-start gap-2.5">
          <ShieldCheck className="h-4 w-4 text-accent shrink-0 mt-0.5" />
          <div className="space-y-2.5 text-sm text-secondary leading-relaxed">
            <p>
              本项目为<strong className="text-warning">个人开源项目</strong>,<span className="text-warning">非</span>
              <a
                href="https://tickflow.org"
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline font-medium inline-flex items-baseline gap-0.5"
              >
                TickFlow
                <ExternalLink className="h-3 w-3 self-center" />
              </a>
              <span className="text-warning">官方项目</span>。本项目独立维护，当前支持 TickFlow 数据服务。
            </p>
            <p>
              仅供学习研究使用,不构成任何投资建议。股市有风险,使用本项目产生的任何盈亏由使用者自行承担。
            </p>
            <p>
              本项目基于 MIT 协议开源。使用本项目时,请遵守数据源(TickFlow 等)的服务条款;stock-sdk 等第三方接口插件存在版权与反爬风险,使用需自行评估合规责任。
            </p>
          </div>
        </div>
      </div>

      <div className="mt-6 flex items-center justify-center">
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-6 h-11 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 hover:shadow-accent/30 transition-all"
        >
          我已了解,继续
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

// ===== Step 1: 欢迎 =====

function WelcomeStep({ onNext, onSkip }: { onNext: () => void; onSkip: () => void }) {
  return (
    <div className="text-center">
      {/* 品牌 badge */}
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="mx-auto w-fit rounded-2xl p-4 border border-border"
        style={{ background: `linear-gradient(135deg, ${BRAND}22, transparent)` }}
      >
        <Sparkles className="h-8 w-8" style={{ color: BRAND }} />
      </motion.div>

      <h1 className="mt-6 text-3xl font-bold text-foreground tracking-tight">
        欢迎使用 TickFlow Stock Panel
      </h1>
      <p className="mt-3 text-sm text-secondary leading-relaxed max-w-md mx-auto">
        一个本地化的 A 股量化分析面板 —— 行情、选股、回测、监控、财务一体化。
        花一分钟配置,即可开始使用。
      </p>

      {/* 特性卡片 —— 3×3 网格,横向布局压缩高度 */}
      <div className="mt-8 grid grid-cols-2 sm:grid-cols-3 gap-2.5 text-left">
        {HIGHLIGHTS.map((h, i) => (
          <motion.div
            key={h.title}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.04 * i + 0.1 }}
            whileHover={{ y: -2 }}
            className="group flex items-start gap-2.5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-2.5 transition-colors hover:border-accent/30"
          >
            <div className="rounded-lg bg-elevated/50 p-1.5 shrink-0">
              <h.icon className={`h-4 w-4 ${h.tint} transition-transform group-hover:scale-110`} />
            </div>
            <div className="min-w-0">
              <div className="text-xs font-medium text-foreground">{h.title}</div>
              <div className="mt-0.5 text-[11px] text-muted leading-snug line-clamp-2">{h.desc}</div>
            </div>
          </motion.div>
        ))}
      </div>

      <div className="mt-8 flex items-center justify-center gap-3">
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-6 h-11 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 hover:shadow-accent/30 transition-all"
        >
          开始配置
          <ArrowRight className="h-4 w-4" />
        </button>
        <button
          onClick={onSkip}
          className="px-4 h-11 rounded-xl text-sm text-secondary hover:text-foreground hover:bg-elevated transition-colors"
        >
          稍后再说
        </button>
      </div>
    </div>
  )
}

// ===== Step 2: 输入 TickFlow Key =====

function KeyStep({ onNext, onSkip, onBack }: { onNext: () => void; onSkip: () => void; onBack: () => void }) {
  const qc = useQueryClient()
  const settings = useSettings()

  const [keyInput, setKeyInput] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = useMutation({
    mutationFn: () => api.saveTickflowKey(keyInput.trim()),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.settings })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      if (data.ok) {
        // 仅当 key 有效(被存储)时才进入下一步看探测结果
        setSaved(true)
        setTimeout(() => onNext(), 600)
      }
      // ok=false(key 无效):不进入下一步,错误提示由 save.error / save.data 渲染
    },
  })

  // 已配置 key —— 免费档或付费档都算(只要不是 None 档)
  const alreadyHasKey = settings.data?.mode !== 'none' && settings.data?.mode !== undefined

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <ShieldCheck className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">配置 TickFlow API Key</h2>
      </div>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed">
        本项目基于 TickFlow 这款稳定的数据源为基座进行开发,正在适配其他第三方数据源。
        如果有任何建议或意见,欢迎发送邮件至{' '}
        <a
          href="mailto:415333856@qq.com"
          className="text-accent hover:underline font-medium"
        >
          415333856@qq.com
        </a>
        。
      </p>

      {/* 档位对比说明 —— None 档 vs Free 档 */}
      <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        {/* None 档 —— 不配置时默认 */}
        <div className="rounded-card border border-accent/20 bg-accent/[0.04] p-3">
          <div className="flex items-center gap-1.5">
            <span className="inline-flex h-[18px] items-center rounded px-1.5 text-[10px] font-bold font-mono bg-accent/15 text-accent/70">None</span>
            <span className="text-xs font-medium text-foreground">不配置(默认)</span>
          </div>
          <ul className="mt-2 space-y-1 text-[11px] text-muted leading-relaxed">
            <li>· 仅历史日K数据,无实时行情</li>
            <li>· 数据有延迟,盘后约 1-2 小时更新当天</li>
            <li>· 可用于策略回测、盘后分析</li>
          </ul>
        </div>
        {/* Free 档 —— 免费注册即可获取 */}
        <div className="rounded-card border border-accent/35 bg-accent/[0.08] p-3">
          <div className="flex items-center gap-1.5">
            <span className="inline-flex h-[18px] items-center rounded px-1.5 text-[10px] font-bold font-mono bg-accent/15 text-accent">Free</span>
            <span className="text-xs font-medium text-foreground">注册免费获取</span>
            <span className="inline-flex items-center rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold text-white shadow-sm shadow-accent/30">推荐</span>
          </div>
          <ul className="mt-2 space-y-1 text-[11px] text-secondary leading-relaxed">
            <li>· 无需付费,注册即享</li>
            <li>· 历史日K + 限定范围内的实时数据</li>
            <li>· 可指定个股进行实时监控</li>
          </ul>
        </div>
      </div>

      {/* Key 已配置提示 */}
      {alreadyHasKey && !save.isPending && (
        <div className="mt-4 flex items-start gap-2 rounded-btn border border-bear/30 bg-bear/10 px-3 py-2.5 text-xs text-bear">
          <CheckCircle2 className="h-3.5 w-3.5 mt-px shrink-0" />
          <span>
            已检测到配置好的 Key(<span className="font-mono">{settings.data?.tickflow_api_key_masked}</span>)。
            可直接下一步查看能力,或在下方粘贴新 Key 替换。
          </span>
        </div>
      )}

      {/* 获取 Key 的说明 —— 黄框卡片 */}
      <div className="mt-4 flex items-start gap-2 rounded-card border border-warning/40 bg-warning/10 px-3 py-2.5 text-xs text-foreground leading-relaxed">
        <AlertCircle className="h-4 w-4 shrink-0 text-warning mt-px" />
        <span>
          Key 可在{' '}
          <a
            href="https://tickflow.org/auth/register?ref=V3KDKGXPEA"
            target="_blank"
            rel="noreferrer"
            className="text-warning hover:underline inline-flex items-baseline gap-0.5 font-medium"
          >
            tickflow.org
            <ExternalLink className="h-3 w-3 self-center" />
          </a>
          获取。
          <span className="block mt-1.5 text-foreground/70">
            当前数据源基于 TickFlow 基座,其他第三方数据源正在开发适配中。
          </span>
        </span>
      </div>

      {/* 输入 */}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (keyInput.trim()) save.mutate()
        }}
        className="mt-4 space-y-2"
      >
        <div className="relative">
          <input
            type={revealing ? 'text' : 'password'}
            placeholder={alreadyHasKey ? '粘贴新 Key 替换当前' : '粘贴 TickFlow API Key'}
            value={keyInput}
            onChange={(e) => {
              setKeyInput(e.target.value)
              if (saved) setSaved(false)
            }}
            className="w-full px-3 py-2.5 pr-9 rounded-input bg-base border border-border text-sm font-mono focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/30 transition-all"
          />
          <button
            type="button"
            onClick={() => setRevealing((v) => !v)}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted hover:text-foreground transition-colors"
            tabIndex={-1}
            aria-label={revealing ? '隐藏' : '显示'}
          >
            {revealing ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>

        {/* 保存中提示 */}
        {save.isPending && (
          <div className="flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] leading-snug text-warning">
            <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
            <span>正在验证 Key 并探测能力,验证通过前请不要离开当前页面。</span>
          </div>
        )}

        {save.isError && (
          <div className="text-xs text-danger">保存失败:{String((save.error as any).message)}</div>
        )}
        {/* 无效 key —— 探测失败(key 无效/乱填)未存储,提示用户 */}
        {save.data && !save.data.ok && (
          <div className="flex items-start gap-1.5 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] leading-snug text-danger">
            <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
            <span>
              {save.data.reason === 'invalid'
                ? 'Key 无效或已过期,请检查后重试(未保存该 Key)。'
                : save.data.error ?? '保存失败'}
            </span>
          </div>
        )}
      </form>

      {/* 底部操作 */}
      <div className="mt-6 flex items-center justify-between">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1.5 px-3 h-9 rounded-btn text-sm text-secondary hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          上一步
        </button>
        <div className="flex items-center gap-2">
          <button
            onClick={onSkip}
            disabled={save.isPending}
            className="px-4 h-9 rounded-btn text-sm text-secondary hover:text-foreground transition-colors disabled:opacity-50"
          >
            {alreadyHasKey ? '下一步' : '暂不配置'}
          </button>
          <button
            onClick={() => keyInput.trim() && save.mutate()}
            disabled={save.isPending || !keyInput.trim()}
            className="inline-flex items-center gap-2 px-5 h-9 rounded-xl bg-accent text-white text-sm font-semibold hover:bg-accent/90 disabled:opacity-40 transition-all"
          >
            {save.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : saved ? (
              <Check className="h-4 w-4" />
            ) : (
              <Save className="h-4 w-4" />
            )}
            {save.isPending ? '保存中...' : saved ? '已保存' : '保存并检测'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ===== Step 3: 能力探测结果 =====

function ResultStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const settings = useSettings()
  const caps = useCapabilities()

  // 是否配置成功 —— 免费档(free)或付费档(api_key)都算;None 档算未配置
  const hasKey = settings.data?.mode === 'free' || settings.data?.mode === 'api_key'
  const capList = caps.data ? Object.entries(caps.data.capabilities) : []

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <ScanSearch className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">能力探测结果</h2>
      </div>

      {hasKey ? (
        <>
          <p className="mt-2.5 text-sm text-secondary leading-relaxed">
            Key 已生效,以下是你当前可用的全部能力。后续可在
            <span className="text-foreground font-medium"> 设置 → 账户 </span>
            中重新检测或更换 Key。
          </p>

          <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-5">
            <div className="flex items-baseline justify-between">
              <span className="text-[10px] uppercase tracking-widest text-muted">订阅档位</span>
              <span className="font-mono text-2xl font-bold tracking-tight text-foreground">
                {caps.data?.label ?? settings.data?.tier_label ?? '—'}
              </span>
            </div>

            {caps.isLoading ? (
              <div className="mt-4 flex items-center gap-2 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                正在探测能力…
              </div>
            ) : capList.length > 0 ? (
              <div className="mt-4 grid grid-cols-1 gap-1.5">
                {capList.slice(0, 8).map(([cap]) => {
                  const meta = CAP_LABELS[cap]
                  return (
                    <div key={cap} className="flex items-center gap-2 text-xs">
                      <CheckCircle2 className="h-3.5 w-3.5 text-bear shrink-0" />
                      <span className="text-foreground">{meta?.name ?? cap}</span>
                    </div>
                  )
                })}
                {capList.length > 8 && (
                  <div className="text-[11px] text-muted pl-5">…等共 {capList.length} 项</div>
                )}
              </div>
            ) : (
              <div className="mt-4 text-xs text-muted">暂未探测到能力</div>
            )}
          </div>
        </>
      ) : (
        <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-6 text-center">
          <div className="mx-auto w-fit rounded-xl bg-elevated p-3">
            <Zap className="h-6 w-6 text-warning" />
          </div>
          <div className="mt-3 text-sm font-medium text-foreground">将以 None 档继续</div>
          <p className="mt-2 text-xs text-muted leading-relaxed max-w-sm mx-auto">
            当前未配置有效 Key,仍可使用看板、选股、回测等功能 —— 进入看板后可直接获取近 1 年历史日K数据。配置 Key 后可解锁实时行情监控等能力,随时在
            <span className="text-foreground font-medium"> 设置 → 账户 </span>填写。
          </p>
        </div>
      )}

      {/* 底部操作 */}
      <div className="mt-6 flex items-center justify-between">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1.5 px-3 h-9 rounded-btn text-sm text-secondary hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          上一步
        </button>
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-5 h-9 rounded-xl bg-accent text-white text-sm font-semibold hover:bg-accent/90 transition-colors"
        >
          下一步
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

// ===== Step 4: 完成 =====

function FinishStep({ onNext, onBack, pending }: { onNext: () => void; onBack: () => void; pending: boolean }) {
  const settings = useSettings()
  // 是否已配置 Key(free 或 api_key 都算,None 档算未配置)
  const hasKey = settings.data?.mode === 'free' || settings.data?.mode === 'api_key'

  // 首要行动:获取数据(不管配没配 Key, 新用户都需要先拉数据)
  // 快速上手入口(精简为核心功能)
  const tips = [
    { icon: TrendingUp, text: '「个股分析」:输入代码,AI 五维分析 + 关键价位' },
    { icon: ScanSearch, text: '「选股」页:内置多套策略,一键扫描全市场' },
    { icon: ShieldCheck, text: '「回测」页:用历史数据验证策略表现,用数据说话' },
  ]

  return (
    <div className="text-center">
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="mx-auto w-fit"
      >
        <div
          className="relative rounded-2xl p-5 border border-border"
          style={{ background: `linear-gradient(135deg, ${BRAND}22, transparent)` }}
        >
          <CheckCircle2 className="h-12 w-12 text-bear" />
          {/* 光晕脉冲 */}
          <motion.div
            animate={{ scale: [1, 1.4], opacity: [0.4, 0] }}
            transition={{ duration: 1.8, repeat: Infinity, ease: 'easeOut' }}
            className="absolute inset-5 rounded-full bg-bear/30"
          />
        </div>
      </motion.div>

      <h1 className="mt-6 text-2xl font-bold text-foreground">一切就绪!</h1>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed max-w-md mx-auto">
        {hasKey
          ? 'Key 已生效,进入面板后系统会自动引导你获取行情数据,完成后即可使用全部功能。'
          : '当前为 None 档,进入面板后系统会自动引导你获取历史日K数据(无需 Key),即可开始体验。'}
      </p>

      {/* 首要行动:获取数据 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, delay: 0.2 }}
        className="mt-5 flex items-start gap-2.5 rounded-card border border-accent/30 bg-accent/[0.06] px-4 py-3 text-left"
      >
        <div className="rounded-lg bg-accent/15 p-1.5 shrink-0 mt-px">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <div className="min-w-0">
          <div className="text-sm font-medium text-foreground">下一步:获取行情数据</div>
          <p className="mt-1 text-xs text-secondary leading-relaxed">
            进入面板后,看板会自动引导你拉取近 1 年全 A 股日K(约 5500 只,预计 1-3 分钟)。同步期间可浏览其他页面。
          </p>
        </div>
      </motion.div>

      {/* 快速上手入口 */}
      <div className="mt-4 space-y-2 text-left">
        {tips.map((t, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.3, delay: 0.1 * i + 0.3 }}
            className="flex items-center gap-3 rounded-card border border-border bg-surface/80 backdrop-blur-sm px-3.5 py-2.5"
          >
            <div className="rounded-lg bg-accent/10 p-1.5 shrink-0">
              <t.icon className="h-3.5 w-3.5 text-accent" />
            </div>
            <span className="text-xs text-secondary">{t.text}</span>
          </motion.div>
        ))}
      </div>

      {/* 底部操作 */}
      <div className="mt-8 flex items-center justify-between">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1.5 px-3 h-10 rounded-btn text-sm text-secondary hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          上一步
        </button>
        <button
          onClick={onNext}
          disabled={pending}
          className="inline-flex items-center gap-2 px-6 h-10 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 hover:shadow-accent/30 disabled:opacity-60 transition-all"
        >
          {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
          {pending ? '正在进入…' : '进入面板'}
        </button>
      </div>
    </div>
  )
}
