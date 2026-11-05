import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import {
  Loader2,
  CheckCircle2,
  AlertCircle,
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
  Puzzle,
  Coins,
  Route,
  Code2,
  ExternalLink,
  ChevronDown,
} from 'lucide-react'
import { api, type ProviderField } from '@/lib/api'
import { usePreferences, useSettings } from '@/lib/useSharedQueries'
import { QK } from '@/lib/queryKeys'
import { Logo } from '@/components/Logo'
import { PROJECT_LICENSE_NAME, PROJECT_LICENSE_TEXT, PROJECT_REPO_URL } from '@/data/ossLicenses'

// ===== 引导页:5 步向导 =====
// 0. 使用须知  1. 欢迎  2. 数据源与 Key  3. 能力路由检测  4. 完成 → 写标记 → 进面板

const STEPS = ['使用须知', '功能介绍', '数据源', '能力路由', '完成'] as const

const BRAND = '#8B5CF6'

const HIGHLIGHTS = [
  { icon: LineChart,   title: '看板与自选', desc: '市场全景看板、涨跌分布、情绪雷达,自定义自选列表', tint: 'text-accent' },
  { icon: ScanSearch,  title: '策略研究',   desc: '自行创建或导入策略,按自定义规则筛选标的', tint: 'text-bull' },
  { icon: TrendingUp,  title: '个股分析',   desc: 'AI 四维分析个股,关键价位、技术形态一目了然', tint: 'text-warning' },
  { icon: Flame,       title: '连板梯队',   desc: '涨停梯队、封板强度、炸板监控,情绪温度计', tint: 'text-warning' },
  { icon: Landmark,    title: '概念行业',   desc: '概念板块、行业维度的资金流向与热度排名', tint: 'text-accent' },
  { icon: FileText,    title: '财务概况',   desc: 'AI 解读财报,利润、资负、现金流、核心指标', tint: 'text-bear' },
  { icon: ShieldCheck, title: '回测验证',   desc: '策略历史回测、因子分析,用数据验证逻辑', tint: 'text-accent' },
  { icon: Radar,       title: '实时监控',   desc: '自定义条件 / 策略监控,盘中触发即推送告警', tint: 'text-bear' },
  { icon: BellRing,    title: '本地优先',   desc: '数据本地存储,隐私可控,断网仍可查阅', tint: 'text-bull' },
]

export function Onboarding() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const reduceMotion = useReducedMotion()

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
    <div className="relative min-h-full bg-base overflow-hidden flex flex-col">
      {/* 背景光晕 —— 品牌 + 主色渐变 */}
      {step !== 0 && <div className="pointer-events-none absolute inset-0 overflow-hidden">
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
      </div>}

      {/* 顶栏:logo + 进度指示 */}
      <header className="relative z-10 flex flex-wrap items-center justify-between gap-x-6 gap-y-4 border-b border-border px-5 py-4 sm:px-8">
        <div className="flex items-center gap-2.5 text-foreground">
          <Logo
            size={24}
            className="shrink-0"
            style={{ color: BRAND }}
          />
          <span className="text-sm font-semibold tracking-tight">Tick Stock Panel</span>
        </div>
        <ol aria-label="初始设置进度" className="order-last flex w-full items-center gap-2 md:order-none md:w-auto md:gap-3">
          {STEPS.map((label, i) => (
            <li key={label} aria-current={i === step ? 'step' : undefined} className="min-w-0 flex-1 md:w-14 md:flex-none">
              <div aria-hidden="true" className={`h-1 rounded-full ${i <= step ? 'bg-accent' : 'bg-border'}`} />
              <span className={`mt-2 block whitespace-nowrap text-center text-[11px] ${i === step ? 'font-semibold text-foreground' : 'text-secondary'}`}>
                {label}
              </span>
            </li>
          ))}
        </ol>
        <div className="text-right sm:w-[88px]">
          <span className="text-xs text-secondary tabular">
            {step + 1} / {STEPS.length}
          </span>
        </div>
      </header>

      {/* 步骤内容 (数据源步骤含编辑器, 加宽容器) */}
      <main className="relative z-10 flex flex-1 items-center justify-center px-5 py-8 sm:px-8 sm:py-12">
        <div className={`w-full ${step === 0 ? 'max-w-5xl' : step === 2 ? 'max-w-3xl' : 'max-w-xl'}`}>
          <AnimatePresence mode="wait">
            <motion.div
              key={step}
              initial={step === 0 || reduceMotion ? false : { opacity: 0, x: 24 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: reduceMotion ? 0 : -24 }}
              transition={{ duration: reduceMotion ? 0 : 0.25, ease: [0.16, 1, 0.3, 1] }}
            >
              {step === 0 && <DisclaimerStep onNext={() => setStep(1)} />}
              {step === 1 && <WelcomeStep onNext={() => setStep(2)} onSkip={finish} />}
              {step === 2 && (
                <DataSourceStep onNext={() => setStep(3)} onBack={() => setStep(1)} />
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

// ===== Step 0: 使用须知 =====

function DisclaimerStep({ onNext }: { onNext: () => void }) {
  return (
    <div>
      <div className="grid gap-6 md:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] md:gap-12 lg:gap-16">
        <section aria-labelledby="onboarding-intro-title" className="flex flex-col items-start">
          <h1 id="onboarding-intro-title" className="text-3xl font-semibold leading-snug tracking-tight text-foreground sm:text-4xl sm:leading-snug">
            TSP · 智能量化<br /><span className="text-violet-700 dark:text-violet-300">工作台引擎</span>
          </h1>
          <p className="mt-5 max-w-md text-sm leading-7 text-secondary">
            接入行情与扩展数据，用开源引擎回测自己的策略，盘后复核结果。
          </p>

          <ol aria-label="研究流程" className="mt-6 grid w-full grid-cols-3 gap-3 md:mt-10 md:block md:space-y-6">
            {[
              { icon: Database, title: '接入数据', description: '连接已获授权的数据源，建立研究基础。', tint: 'bg-sky-100 text-sky-700 dark:bg-sky-400/10 dark:text-sky-300' },
              { icon: Code2, title: '定义规则', description: '创建或导入策略，将研究想法转为条件。', tint: 'bg-violet-100 text-violet-700 dark:bg-violet-400/10 dark:text-violet-300' },
              { icon: LineChart, title: '回测验证', description: '检验历史表现，理解结果与局限。', tint: 'bg-teal-100 text-teal-700 dark:bg-teal-400/10 dark:text-teal-300' },
            ].map(({ icon: Icon, title, description, tint }) => (
              <li key={title} className="flex flex-col items-start gap-2 md:flex-row md:gap-3.5">
                <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${tint}`}>
                  <Icon aria-hidden="true" className="h-5 w-5" strokeWidth={1.5} />
                </span>
                <div>
                  <h2 className="text-sm font-medium text-foreground">{title}</h2>
                  <p className="mt-1 hidden text-sm leading-6 text-secondary md:block">{description}</p>
                </div>
              </li>
            ))}
          </ol>
          <a
            href={PROJECT_REPO_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-4 inline-flex min-h-11 items-center gap-2 rounded-btn text-sm text-violet-700 underline decoration-violet-300 underline-offset-4 transition-colors hover:text-violet-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-4 focus-visible:ring-offset-base dark:text-violet-300 dark:decoration-violet-500/50 dark:hover:text-violet-200 md:mt-8"
          >
            查看引擎源码
            <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
            <span className="sr-only">（在新标签页打开）</span>
          </a>
        </section>

        <section aria-labelledby="onboarding-notice-title" className="rounded-2xl border border-violet-200 bg-violet-50/70 p-5 dark:border-violet-400/20 dark:bg-violet-950/30 sm:p-7">
          <h2 id="onboarding-notice-title" className="text-lg font-semibold text-violet-950 dark:text-violet-100">开始前，请了解</h2>
          <dl className="mt-5 divide-y divide-violet-200/70 text-sm dark:divide-violet-400/15 [&_dt]:font-semibold [&_dt]:text-amber-800 dark:[&_dt]:text-amber-300">
            <div className="pb-5">
              <dt>不内置策略</dt>
              <dd className="mt-2 leading-6 text-violet-950/75 dark:text-violet-100/80">
                初次安装的策略库为空，策略由你自行创建或导入。
                第三方策略和 AI 生成内容均需自行审查与验证。
              </dd>
            </div>
            <div className="py-5">
              <dt>不荐股，不预测，不承诺收益</dt>
              <dd className="mt-2 leading-6 text-violet-950/75 dark:text-violet-100/80">
                筛选、评分、监控和 AI 输出仅用于学习与研究，不构成投资建议。
                回测呈现历史表现，不代表未来收益。
              </dd>
            </div>
            <div className="pt-5">
              <dt>遵守开源许可与服务协议</dt>
              <dd className="mt-2 space-y-2 leading-6 text-violet-950/75 dark:text-violet-100/80">
                <p>使用、修改或分发本项目须遵守 {PROJECT_LICENSE_NAME} 许可，保留版权与许可声明。软件按现状提供，不作保证。</p>
                <p>数据源、插件与模型服务适用各自条款。请取得必要授权，遵守使用范围与访问限制；软件开源不等于数据开放授权。</p>
              </dd>
            </div>
          </dl>
          <details className="group mt-4">
            <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 rounded-btn text-sm font-medium text-violet-700 underline decoration-violet-300 underline-offset-4 transition-colors hover:text-violet-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-4 focus-visible:ring-offset-surface dark:text-violet-300 dark:decoration-violet-500/50 dark:hover:text-violet-200 [&::-webkit-details-marker]:hidden">
              查看完整开源许可（{PROJECT_LICENSE_NAME}）
              <ChevronDown aria-hidden="true" className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180 motion-reduce:transition-none" />
            </summary>
            <pre className="mt-3 whitespace-pre-wrap break-words border-t border-violet-200 pt-4 font-mono text-xs leading-6 text-violet-950/75 dark:border-violet-400/15 dark:text-violet-100/80" lang="en">{PROJECT_LICENSE_TEXT}</pre>
          </details>
        </section>
      </div>

      <div className="mt-8 flex flex-col gap-5 border-t border-border pt-6 sm:mt-10 sm:flex-row sm:items-center sm:justify-between sm:gap-8">
        <div className="max-w-xl text-sm leading-6">
          <p className="font-medium text-amber-800 dark:text-amber-300">独立判断，审慎使用。</p>
          <p className="mt-1 text-secondary">市场有风险，请自行核验数据与结果，并承担决策风险。</p>
        </div>
        <button
          type="button"
          onClick={onNext}
          className="inline-flex min-h-12 shrink-0 items-center justify-center gap-3 rounded-xl bg-violet-600 px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-violet-700 active:bg-violet-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-4 focus-visible:ring-offset-base"
        >
          我已了解，继续
          <ArrowRight aria-hidden="true" className="h-4 w-4" />
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
        欢迎使用 TSP
      </h1>
      <p className="mt-3 text-sm text-secondary leading-relaxed max-w-md mx-auto">
        一个本地化的 A 股量化分析面板 —— 行情、策略、回测、监控、财务一体化。
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

// ===== Step 2: 配置数据源 =====
// 面板行 = 数据源的全部信息聚合 (图标 / 名称 / 能力 / Key / 说明), 取代"卡片只读 + 表单另排"
// 的上下分离结构。三个源均为第三方数据服务 (与步骤 0 免责声明同口径): 归属只在名称行以弱化
// 文字标注, 角色差异交给图标色与文案表达, 不再堆叠徽标。Key 为可选增强: 未配置直接给输入框,
// 已配置折叠为脱敏串。

/** datasets 标签的中文名 (与设置页数据集口径一致) */
const DATASET_LABELS: Record<string, string> = {
  daily: '日K',
  adj_factor: '除权',
  realtime: '实时',
  minute: '分钟K',
  full_minute: '全量分钟',
  depth5: '五档',
  financial: '财务',
}

/** 面板行: 一个数据源的全部信息 (图标 / 名称 / 能力 / Key / 说明) 聚合在一行内 */
type SourceRow = {
  name: string
  display: string
  env: string
  datasets: string[]
  /** 图标块: Comp 取 lucide 组件, cls 同时给底色与图标色 (蓝=默认源 / 绿=增强 / 琥珀=积分) */
  icon: { Comp: typeof LineChart; cls: string }
  paid?: boolean        // 积分计费源: 名称行追加琥珀小标 (费用提示)
  warn?: string         // 计费警示句: 频率/批量对积分消耗的影响, 行内以琥珀警示行展示
  configured: boolean
  masked: string
  desc: string
  link: { label: string; url: string } | null
  slowProbe?: boolean   // TdxQuant: 子进程实探 ~30s, 按钮文案预定预告耗时
  autoFocus?: boolean
}

function DataSourceStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const qc = useQueryClient()
  const settings = useSettings()
  const sources = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })

  // Key 表单状态按数据源名分槽; expanded = 已配置源点开「替换」后的展开态
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, string | null>>({})
  const [savedMsg, setSavedMsg] = useState<Record<string, string | null>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const plugins = sources.data?.plugins ?? []
  const builtin = sources.data?.builtin ?? []
  const customCount = (sources.data?.custom ?? []).length
  const tfItem = builtin.find(b => b.name === 'tickflow')
  const fyPlugin = plugins.find(p => p.name === 'fuyao')
  const tdxPlugin = plugins.find(p => p.name === 'tdxquant')

  // 内联 Key 配置 (先探后存): 验证通过才落盘
  const saveKey = useMutation<any, Error, { name: string; key: string }>({
    mutationFn: ({ name, key }) => (name === 'tickflow'
      ? api.saveTickflowKey(key)
      : api.savePluginKey(name, key)),
    onSuccess: (data: any, vars) => {
      qc.invalidateQueries({ queryKey: QK.dataSources })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      qc.invalidateQueries({ queryKey: QK.capabilityMatrix })
      qc.invalidateQueries({ queryKey: QK.settings })   // TickFlow 脱敏串/档位随 settings 刷新
      const setError = (msg: string | null) => setErrors(s => ({ ...s, [vars.name]: msg }))
      const setSaved = (msg: string | null) => {
        setSavedMsg(s => ({ ...s, [vars.name]: msg }))
        if (msg) setTimeout(() => setSavedMsg(s => ({ ...s, [vars.name]: null })), 6000)
      }
      if (data.ok) {
        setInputs(s => ({ ...s, [vars.name]: '' }))
        setExpanded(s => ({ ...s, [vars.name]: false }))
        setError(null)
        if (vars.name === 'tickflow') {
          setSaved(`TickFlow Key 已保存${data.tier_label ? `,当前档位:${data.tier_label}` : ''}`)
        } else if (vars.name === 'fuyao' && data.plugin_available) {
          // fuyao 定位为增强源: 仅实时行情 + 除权因子路由过去, 其余数据集保持 TickFlow
          routeFuyaoEnhanced.mutate()
          setSaved('fuyao Key 已保存:实时行情与除权因子已切换到 fuyao,其余数据集保持 TickFlow')
        } else if (vars.name === 'tdxquant' && data.plugin_available) {
          // 积分计费源不自动切路由: 消耗积分的选择权留给用户
          setSaved('TdxQuant Key 已保存。该源按积分计费,请在「设置 → 数据源」按需启用')
        } else if (data.plugin_available) {
          setSaved('Key 已保存,该数据源已就绪')
        }
      } else {
        setError(data.error || (data.reason === 'invalid' ? 'Key 验证失败,请检查后重试' : '保存失败,请重试'))
      }
    },
    onError: (e: Error, vars) => setErrors(s => ({ ...s, [vars.name]: `保存失败: ${e.message}` })),
  })

  // fuyao 增强路由: 只切实时行情 + 除权因子两个字段 (updateDataProviders 部分更新, 其余不动)
  const routeFuyaoEnhanced = useMutation({
    mutationFn: () => api.updateDataProviders({
      realtime_data_provider: 'fuyao',
      adj_factor_provider: 'fuyao',
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
    },
  })

  const rows: SourceRow[] = [
    {
      name: 'tickflow',
      display: tfItem?.display_name || 'TickFlow',
      env: 'TICKFLOW_API_KEY',
      datasets: tfItem?.datasets ?? [],
      icon: { Comp: LineChart, cls: 'bg-accent/12 text-accent' },
      configured: !!settings.data?.has_tickflow_key,
      masked: settings.data?.tickflow_api_key_masked ?? '',
      desc: '不填 Key 即免费档(历史日K可用);填写后按订阅档位解锁实时、分钟、盘口、财务等能力。',
      link: { label: '注册获取 Key', url: 'https://tickflow.org/auth/register?ref=V3KDKGXPEA' },
      autoFocus: !settings.data?.has_tickflow_key,
    },
    {
      name: 'fuyao',
      display: fyPlugin?.display_name || 'fuyao',
      env: fyPlugin?.api_key_env || 'FUYAO_API_KEY',
      datasets: fyPlugin?.datasets ?? [],
      icon: { Comp: Landmark, cls: 'bg-bear/10 text-bear' },
      configured: !!fyPlugin?.api_key_masked,
      masked: fyPlugin?.api_key_masked ?? '',
      desc: '同花顺金融数据 API。保存后实时行情与除权因子自动切换到 fuyao,其余能力保持 TickFlow。',
      link: { label: '前往官网申请 Key', url: 'https://fuyao.aicubes.cn' },
    },
    {
      name: 'tdxquant',
      display: tdxPlugin?.display_name || 'TdxQuant',
      env: tdxPlugin?.api_key_env || 'TDXQUANT_API_KEY',
      datasets: tdxPlugin?.datasets ?? [],
      icon: { Comp: Coins, cls: 'bg-warning/12 text-warning' },
      paid: true,
      configured: !!tdxPlugin?.api_key_masked,
      masked: tdxPlugin?.api_key_masked ?? '',
      desc: '通达信 TdxAiData 数据服务,覆盖实时快照、盘口、日K、除权、分钟K 等六项能力。配置后不自动切换路由,可在「设置 → 数据源」按需启用。',
      // 计费提示优先用清单 billing_note (单一来源); 缺失时用下方兜底文案
      warn: tdxPlugin?.billing_note
        || '积分消耗与频率、范围直接相关: 间隔越短、范围越大消耗越高,具体以商城「积分明细」为准。',
      link: tdxPlugin?.key_manage_url
        ? { label: '管理 Key / 查询积分', url: tdxPlugin.key_manage_url }
        : { label: '通达信量化平台', url: 'https://help.tdx.com.cn/quant/' },
      slowProbe: true,
    },
  ]

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">配置数据源</h2>
      </div>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed">
        下方数据服务均由第三方提供。TickFlow 不填 Key 即可用,填写后按档位解锁更多能力;
        其余服务填写 Key 后启用。所有 Key 仅存本地,保存前会先验证。
      </p>

      {/* 数据源面板: 每行一个源, 行内完成「看懂 → 配 Key」的闭环 */}
      {sources.isLoading ? (
        <div className="mt-4 flex items-center gap-2 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          正在加载数据源…
        </div>
      ) : (
        <div className="mt-4 overflow-hidden rounded-card border border-border/70 bg-surface/60 backdrop-blur-sm">
          <div className="divide-y divide-border/40">
            {rows.map(row => {
              const val = inputs[row.name] ?? ''
              const err = errors[row.name] ?? null
              const msg = savedMsg[row.name] ?? null
              const pending = saveKey.isPending && saveKey.variables?.name === row.name
              const showInput = !row.configured || expanded[row.name]
              const Icon = row.icon.Comp
              return (
                <div key={row.name} className="px-4 py-4">
                  <div className="flex gap-3">
                    {/* 图标块: 每行视觉锚点 (蓝=默认源 / 绿=增强 / 琥珀=积分) */}
                    <div className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${row.icon.cls}`}>
                      <Icon className="h-4 w-4" />
                    </div>

                    <div className="min-w-0 flex-1">
                      {/* 名称行: 名称 + 弱化归属 + 费用小标 + 右侧状态胶囊 */}
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="text-[15px] font-semibold leading-5 text-foreground">{row.display}</span>
                        <span className="text-[10px] leading-none text-muted/45">第三方</span>
                        {row.paid && (
                          <span className="rounded bg-warning/12 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">消耗积分</span>
                        )}
                        <span className="ml-auto shrink-0">
                          {row.configured ? (
                            <span className="inline-flex items-center gap-1 rounded-full bg-bear/10 px-1.5 py-0.5 text-[10px] font-medium leading-none text-bear">
                              <CheckCircle2 className="h-2.5 w-2.5" />
                              已配置
                            </span>
                          ) : row.name === 'tickflow' ? (
                            <span className="rounded-full bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium leading-none text-accent">免 Key 可用</span>
                          ) : (
                            <span className="rounded-full bg-elevated/60 px-1.5 py-0.5 text-[10px] leading-none text-muted/70">未配置</span>
                          )}
                        </span>
                      </div>

                      {/* 能力徽章 */}
                      <div className="mt-1.5 flex flex-wrap items-center gap-1">
                        {row.datasets.length > 0 ? (
                          row.datasets.map(ds => (
                            <span key={ds} className="rounded-md bg-elevated/45 px-1.5 py-0.5 text-[10px] leading-none text-muted/85">
                              {DATASET_LABELS[ds] ?? ds}
                            </span>
                          ))
                        ) : (
                          <span className="text-[10px] text-muted/50">未声明数据集</span>
                        )}
                      </div>

                      {/* Key 配置: 未配置直接给输入框; 已配置折叠为脱敏串, 点「替换」再展开 */}
                      <div className="mt-2.5">
                        {showInput ? (
                          <form
                            className="flex items-center gap-2"
                            onSubmit={e => {
                              e.preventDefault()
                              if (val.trim() && !saveKey.isPending) saveKey.mutate({ name: row.name, key: val.trim() })
                            }}
                          >
                            <input
                              type="password"
                              value={val}
                              onChange={e => {
                                setInputs(s => ({ ...s, [row.name]: e.target.value }))
                                setErrors(s => ({ ...s, [row.name]: null }))
                              }}
                              placeholder={`粘贴 ${row.env}`}
                              autoFocus={row.autoFocus}
                              autoComplete="off"
                              className="h-8 min-w-0 flex-1 rounded-lg border border-border bg-base px-2.5 font-mono text-xs text-foreground placeholder:text-muted/50 focus:border-accent/60 focus:outline-none transition-colors"
                            />
                            <button
                              type="submit"
                              disabled={!val.trim() || saveKey.isPending}
                              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50 transition-colors"
                            >
                              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                              {pending ? (row.slowProbe ? '验证中(约 30 秒)…' : '验证中…') : '验证并保存'}
                            </button>
                            {row.configured && (
                              <button
                                type="button"
                                onClick={() => setExpanded(s => ({ ...s, [row.name]: false }))}
                                className="shrink-0 text-[11px] text-muted/60 hover:text-secondary transition-colors"
                              >
                                取消
                              </button>
                            )}
                          </form>
                        ) : (
                          <div className="flex items-center gap-2">
                            <code className="truncate rounded-md bg-elevated/40 px-1.5 py-0.5 font-mono text-[11px] text-secondary">
                              {row.masked || '已保存'}
                            </code>
                            <button
                              type="button"
                              onClick={() => setExpanded(s => ({ ...s, [row.name]: true }))}
                              className="shrink-0 text-[11px] text-accent/75 hover:text-accent transition-colors"
                            >
                              替换 Key
                            </button>
                          </div>
                        )}
                      </div>

                      {/* 说明 + 申请/管理入口 */}
                      <div className="mt-2 flex flex-wrap items-baseline gap-x-1.5 text-[11px] leading-relaxed text-muted/70">
                        <span>{row.desc}</span>
                        {row.link && (
                          <a href={row.link.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                            {row.link.label} ↗
                          </a>
                        )}
                      </div>

                      {/* 计费警示 (如积分消耗与频率/范围的关系) */}
                      {row.warn && (
                        <p className="mt-1.5 flex items-start gap-1.5 rounded-md bg-warning/[0.08] px-2 py-1.5 text-[11px] leading-relaxed text-warning/90">
                          <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
                          <span className="min-w-0">{row.warn}</span>
                        </p>
                      )}

                      {err && (
                        <p className="mt-1.5 flex items-center gap-1 text-[11px] text-danger">
                          <AlertCircle className="h-3 w-3 shrink-0" />
                          {err}
                        </p>
                      )}
                      {msg && (
                        <p className="mt-1.5 flex items-center gap-1 text-[11px] text-bear">
                          <CheckCircle2 className="h-3 w-3 shrink-0" />
                          {msg}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 插件化提示: 自有数据源接入与切换在设置页, 向导保持极简 */}
      <div className="mt-3 flex items-start gap-2 rounded-card border border-border/60 bg-surface/60 px-3 py-2.5">
        <Puzzle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent/70" />
        <div className="text-[11px] leading-relaxed text-muted">
          <span className="text-secondary">数据源已插件化</span>
          {customCount > 0 && <>,当前已加载 {customCount} 个自有数据源</>}
          ,接入自有行情或切换数据源请前往
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">设置 → 数据源</span>
          ,方法详见
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">docs/custom-data-source.md</span>
          与
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">docs/plugin-development.md</span>
          。
        </div>
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

// ===== Step 3: 能力路由检测结果 =====
// 以能力路由矩阵呈现: 每个标准化数据集一行, 展示当前生效源与可用性,
// 不再以 TickFlow 档位为中心 —— 多源下各数据集独立路由, 矩阵即真相。
// 进入本步时按默认优先级自动设置路由: 前者可用的能力归前者, 没有则顺延下一个可用源。

// 自动路由的默认优先级 (前者可用的能力优先归前者)
const ROUTE_PRIORITY = [
  { name: 'tickflow', display: 'TickFlow' },
  { name: 'fuyao', display: 'fuyao' },
]

function ResultStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const qc = useQueryClient()
  const settings = useSettings()
  const prefs = usePreferences()
  const matrix = useQuery({
    queryKey: QK.capabilityMatrix,
    queryFn: api.capabilityMatrix,
    staleTime: 30_000,
  })

  // 自动路由结果: null=尚未执行/执行中; []=已符合优先级无需调整; 有值=本次改写的路由
  const appliedRef = useRef(false)
  const [appliedChanges, setAppliedChanges] = useState<{ label: string; to: string }[] | null>(null)
  const [applyError, setApplyError] = useState<string | null>(null)

  useEffect(() => {
    if (!matrix.data || appliedRef.current) return
    appliedRef.current = true
    const desired: Partial<Record<ProviderField, string>> = {}
    const changes: { label: string; to: string }[] = []
    for (const cap of matrix.data.capabilities) {
      if (!cap.field) continue   // 不可路由能力 (仅 TickFlow 提供) 跳过
      const pick = ROUTE_PRIORITY.find(p => (
        p.name === 'tickflow'
          ? cap.tf_available
          : cap.candidates.some(c => c.name === p.name && c.available)
      ))
      if (!pick || pick.name === cap.current) continue   // 全都不可用或无需变化: 保持现状
      desired[cap.field] = pick.name
      changes.push({ label: cap.label, to: pick.display })
    }
    if (changes.length === 0) {
      setAppliedChanges([])
      return
    }
    api.updateDataProviders(desired)
      .then(() => {
        setAppliedChanges(changes)
        return qc.invalidateQueries({ queryKey: QK.preferences })
      })
      .then(() => qc.invalidateQueries({ queryKey: QK.capabilityMatrix }))
      .catch(e => setApplyError(`自动设置能力路由失败: ${(e as Error).message}`))
  }, [matrix.data])   // eslint-disable-line react-hooks/exhaustive-deps

  const activeName = prefs.data?.daily_data_provider || 'tickflow'
  const isTickflow = activeName === 'tickflow'
  const routes = matrix.data?.capabilities ?? []
  // 是否配置成功 —— 免费档(free)或付费档(api_key)都算;None 档算未配置
  const hasKey = settings.data?.mode === 'free' || settings.data?.mode === 'api_key'
  const usableCount = routes.filter(r => r.usable).length

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <Route className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">能力路由检测</h2>
      </div>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed">
        进入本步时已按默认优先级
        <span className="text-foreground font-medium"> TickFlow → fuyao </span>
        自动设置各数据集的路由:前者可用的能力归前者,没有则顺延下一个可用源。后续可随时在
        <span className="text-foreground font-medium"> 设置 → 数据源 </span>按数据集改选。
      </p>

      {/* 自动路由执行结果 */}
      {applyError && (
        <div className="mt-2.5 flex items-start gap-1.5 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] leading-snug text-danger">
          <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
          <span>{applyError}</span>
        </div>
      )}
      {!applyError && appliedChanges !== null && (
        <div className="mt-2.5 flex items-start gap-1.5 rounded-btn border border-bull/25 bg-bull/[0.06] px-3 py-2 text-[11px] leading-snug text-secondary">
          <CheckCircle2 className="h-3.5 w-3.5 mt-px shrink-0 text-bull" />
          {appliedChanges.length > 0 ? (
            <span>
              已按优先级自动路由:{appliedChanges.map(c => `${c.label} → ${c.to}`).join('、')}
            </span>
          ) : (
            <span>当前能力路由已符合默认优先级,无需调整。</span>
          )}
        </div>
      )}

      {matrix.isLoading ? (
        <div className="mt-5 flex items-center gap-2 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          正在检测能力路由…
        </div>
      ) : matrix.isError ? (
        <div className="mt-5 flex flex-col items-center gap-2 rounded-card border border-danger/30 bg-danger/[0.04] p-5 text-center">
          <span className="text-xs text-secondary">能力路由检测失败，请重试</span>
          <button
            type="button"
            onClick={() => matrix.refetch()}
            disabled={matrix.isFetching}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-accent transition-colors hover:bg-elevated disabled:opacity-50"
          >
            <Loader2 className={matrix.isFetching ? 'h-3 w-3 animate-spin' : 'h-3 w-3'} />
            重试
          </button>
        </div>
      ) : routes.length === 0 ? (
        <div className="mt-5 rounded-card border border-border bg-surface/80 p-5 text-center text-xs text-muted">
          暂未获取到能力路由矩阵,可稍后在 设置 → 数据源 中重新检测。
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-2">
            {routes.map(r => (
              <div
                key={r.id}
                className={`rounded-card border px-3.5 py-2.5 ${
                  r.usable ? 'border-border/60 bg-elevated/20' : 'border-warning/25 bg-warning/[0.03]'
                }`}
              >
                <div className="flex items-center gap-2">
                  {r.usable ? (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bear" />
                  ) : (
                    <AlertCircle className="h-3.5 w-3.5 shrink-0 text-warning" />
                  )}
                  <span className="text-sm font-medium text-foreground">{r.label}</span>
                  <span className="ml-auto shrink-0 text-[10px] font-medium text-secondary">
                    {r.usable ? '可用' : '不可用'}
                  </span>
                </div>
                <div className="mt-1 flex items-center gap-1.5 pl-5 text-[10px] text-muted">
                  <span className="truncate">生效源:{r.effective_display}</span>
                  <span className="shrink-0 text-muted/50">·</span>
                  <span className="shrink-0">{r.desc}</span>
                </div>
                {!r.usable && (
                  <div className="mt-1 pl-5 text-[10px] text-warning/80 leading-relaxed">
    {r.candidates.length > 0
      ? `可切换候选:${r.candidates.map(c => c.display).join(' / ')}`
      : isTickflow && !hasKey
        ? '该能力目前不可用'
        : '暂无可用候选,可在设置中排查该源'}
                  </div>
                )}
              </div>
            ))}
          </div>

          <div className="mt-3 flex items-center justify-between rounded-btn bg-elevated/50 px-3.5 py-2 text-[11px] text-muted">
            <span>
              {usableCount}/{routes.length} 个数据集可用
              {isTickflow && !hasKey && ' · 未配置 TickFlow Key,按 None 档运行,历史日K不受影响'}
            </span>
            <LinkMark />
          </div>
        </>
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

/** 指向设置数据源的行内提示 (能力路由可随时调整) */
function LinkMark() {
  return <span className="shrink-0">调整: 设置 → 数据源</span>
}

// ===== Step 4: 完成 =====

function FinishStep({ onNext, onBack, pending }: { onNext: () => void; onBack: () => void; pending: boolean }) {
  const settings = useSettings()
  // 是否已配置 Key(free 或 api_key 都算,None 档算未配置)
  const hasKey = settings.data?.mode === 'free' || settings.data?.mode === 'api_key'

  // 首要行动:获取数据(不管配没配 Key, 新用户都需要先拉数据)
  // 快速上手入口(精简为核心功能)
  const tips = [
    { icon: TrendingUp, text: '「个股分析」:输入代码,AI 四维分析 + 关键价位' },
    { icon: ScanSearch, text: '「策略」页:内置多套策略,一键扫描全市场' },
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
