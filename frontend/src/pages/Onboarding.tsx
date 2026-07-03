import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Loader2,
  CheckCircle2,
  ArrowRight,
  ArrowLeft,
  Sparkles,
  LineChart,
  ScanSearch,
  Flame,
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

// ===== 引导页:4 步向导 =====
// 0. 欢迎  1. 输入 Key(可跳过)  2. 能力探测结果  3. 完成 → 写标记 → 进面板

const STEPS = ['欢迎', '配置 Key', '能力探测', '完成'] as const

const BRAND = '#8B5CF6'

const HIGHLIGHTS = [
  { icon: LineChart,   title: '看板与自选', desc: '市场全景看板、涨跌分布、情绪雷达,自定义自选列表', tint: 'text-accent' },
  { icon: ScanSearch,  title: '策略选股',   desc: '内置多套选股策略,一键扫描全市场命中标的', tint: 'text-bull' },
  { icon: TrendingUp,  title: '个股分析',   desc: 'AI 四维分析个股,关键价位、技术形态一目了然', tint: 'text-warning' },
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
              {step === 0 && <WelcomeStep onNext={() => setStep(1)} onSkip={finish} />}
              {step === 1 && (
                <KeyStep onNext={() => setStep(2)} onSkip={() => setStep(2)} onBack={() => setStep(0)} />
              )}
              {step === 2 && <ResultStep onNext={() => setStep(3)} onBack={() => setStep(1)} />}
              {step === 3 && <FinishStep onNext={finish} onBack={() => setStep(2)} pending={complete.isPending} />}
            </motion.div>
          </AnimatePresence>
        </div>
      </main>
    </div>
  )
}

// ===== Step 0: 欢迎 =====

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

// ===== Step 1: 数据源确认 =====

function KeyStep({ onNext, onBack }: { onNext: () => void; onSkip: () => void; onBack: () => void }) {
  const settings = useSettings()
  const provider = settings.data?.data_provider ?? settings.data?.mode ?? 'fquant_local'

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">数据源已启用</h2>
      </div>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed">
        当前使用 <span className="font-mono text-foreground">{provider}</span> 数据源。
        继续查看已启用的能力。
      </p>

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
          className="inline-flex items-center gap-2 px-5 h-9 rounded-xl bg-accent text-white text-sm font-semibold hover:bg-accent/90 transition-all"
        >
          下一步
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

// ===== Step 2: 能力探测结果 =====

function ResultStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const caps = useCapabilities()
  const capList = caps.data ? Object.entries(caps.data.capabilities) : []

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <ScanSearch className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">能力探测结果</h2>
      </div>

      <p className="mt-2.5 text-sm text-secondary leading-relaxed">
        以下是当前数据源声明的可用能力。
      </p>

      <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-5">
        <div className="flex items-baseline justify-between">
          <span className="text-[10px] uppercase tracking-widest text-muted">数据源能力</span>
          <span className="font-mono text-2xl font-bold tracking-tight text-foreground">
            {caps.data?.label ?? '—'}
          </span>
        </div>

        {caps.isLoading ? (
          <div className="mt-4 flex items-center gap-2 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            正在读取能力…
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
          <div className="mt-4 text-xs text-muted">暂未读取到能力缓存</div>
        )}
      </div>

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

// ===== Step 3: 完成 =====

function FinishStep({ onNext, onBack, pending }: { onNext: () => void; onBack: () => void; pending: boolean }) {
  // 快速上手入口(精简为核心功能)
  const tips = [
    { icon: TrendingUp, text: '「个股分析」:输入代码,AI 四维分析 + 关键价位' },
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
        进入面板后系统会自动引导你获取行情数据，完成后即可使用已启用功能。
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
