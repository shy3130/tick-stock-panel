import { motion } from 'framer-motion'
import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  ArrowRight,
  BarChart3,
  BookOpen,
  Bot,
  Cable,
  CheckCircle2,
  Clock3,
  Database,
  FileSearch,
  Filter,
  FlaskConical,
  Layers3,
  LineChart,
  RadioTower,
  RefreshCw,
  ScanSearch,
  Settings2,
  ShieldCheck,
  Sparkles,
  Star,
  Workflow,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { useCapabilities, useDataStatus, useQuoteStatus } from '@/lib/useSharedQueries'

type Tone = 'accent' | 'amber' | 'cyan' | 'rose' | 'indigo'

type GuideModule = {
  id: string
  index: string
  title: string
  routes: string[]
  to: string
  icon: LucideIcon
  tone: Tone
  purpose: string
  workflow: string[]
  data: string
  freshness: string
}

const TONE_STYLES: Record<Tone, { icon: string; border: string; pill: string; line: string }> = {
  accent: {
    icon: 'text-accent',
    border: 'hover:border-accent/45',
    pill: 'bg-accent/10 text-accent ring-1 ring-inset ring-accent/20',
    line: 'bg-accent',
  },
  amber: {
    icon: 'text-amber-400',
    border: 'hover:border-amber-400/45',
    pill: 'bg-amber-400/10 text-amber-300 ring-1 ring-inset ring-amber-400/20',
    line: 'bg-amber-400',
  },
  cyan: {
    icon: 'text-cyan-300',
    border: 'hover:border-cyan-300/45',
    pill: 'bg-cyan-300/10 text-cyan-200 ring-1 ring-inset ring-cyan-300/20',
    line: 'bg-cyan-300',
  },
  rose: {
    icon: 'text-rose-400',
    border: 'hover:border-rose-400/45',
    pill: 'bg-rose-400/10 text-rose-300 ring-1 ring-inset ring-rose-400/20',
    line: 'bg-rose-400',
  },
  indigo: {
    icon: 'text-indigo-300',
    border: 'hover:border-indigo-300/45',
    pill: 'bg-indigo-300/10 text-indigo-200 ring-1 ring-inset ring-indigo-300/20',
    line: 'bg-indigo-300',
  },
}

const MODULES: GuideModule[] = [
  {
    id: 'market',
    index: '01',
    title: '市场观察',
    routes: ['看板', '指数', '连板梯队'],
    to: '/',
    icon: BarChart3,
    tone: 'accent',
    purpose: '从指数、涨跌分布、榜单和板块热度建立当日市场上下文。',
    workflow: ['先看看板广度与情绪', '按指数或连板梯队下钻', '再进入策略或标的研究'],
    data: '本地指数日 K、enriched 数据；实时快照仅在 provider 支持且已开启时使用。',
    freshness: '没有实时能力时自动显示最近本地日 K，不会静默改用公网行情。',
  },
  {
    id: 'watchlist',
    index: '02',
    title: '自选与报价',
    routes: ['自选'],
    to: '/watchlist',
    icon: Star,
    tone: 'amber',
    purpose: '维护研究标的池，查看已同步的行情快照、扩展字段和迷你 K 线。',
    workflow: ['按代码、名称、全拼或简拼搜索添加标的并分组置顶', '按字段排序筛选', '从标的进入分析、监控或交易计划'],
    data: '标的维表与 enriched 数据；可选实时行情仅刷新已配置范围。',
    freshness: '页面中的实时列取决于行情开关、provider 能力和本页“数据时效”状态。',
  },
  {
    id: 'screening',
    index: '03',
    title: '策略与条件选股',
    routes: ['策略', '条件选股'],
    to: '/screener',
    icon: ScanSearch,
    tone: 'cyan',
    purpose: '用内置策略、自定义 DSL 或字段条件，从已封存的 enriched 数据中筛选候选池。',
    workflow: ['选择策略或条件', '限定标的池与参数', '核对命中原因后加入自选或进入回测'],
    data: 'canonical/enriched 日线指标与策略参数；自然语言条件解析需要单独配置 AI。',
    freshness: '选股以最近完成管道的日线数据为准，不把受控外部 fallback 当作选股输入。',
  },
  {
    id: 'research',
    index: '04',
    title: '回测、因子与组合',
    routes: ['回测', '组合优化'],
    to: '/backtest',
    icon: LineChart,
    tone: 'indigo',
    purpose: '验证信号历史表现、比较因子统计、评估约束下的组合权重，并用分段稳定性、严格 IS/OOS Walk-Forward、Bootstrap、置换检验与参数扰动检查稳健性。',
    workflow: ['先定义假设、样本区间、基准与费用口径', '运行策略、因子或参数网格；网格场景可一键回填策略表单再验证', '在运行历史中搜索、收藏、打标签，选择 2~4 次结果对比指标、配置差异、交易变化与净值曲线', '查看专业诊断（滚动指标、月度热图、相对基准、持仓期 MAE/MFE）后下载自包含 HTML/JSON/CSV、复跑或打印报告'],
    data: '历史 enriched 数据；每次成功运行固化配置、数据快照、成本、指标口径、基准与随机种子，刷新或重启后仍可检索、比较与复跑。',
    freshness: '这是历史研究而非实时决策；股票池无法证明历史时点时保留幸存者偏差告警，跨区间或口径不同的比较会触发可比性提醒。',
  },
  {
    id: 'research-flow',
    index: '05',
    title: '研究流程与信号验证',
    routes: ['研究中心', '信号记分卡', '横截面分析'],
    to: '/research',
    icon: FlaskConical,
    tone: 'cyan',
    purpose: '登记研究假设、按计划执行定时研究、查询市场数据并做只读统计检验。',
    workflow: ['先注册假设并声明验证标准', '按需排期或手动运行研究', '分析计算对单标的日收益做风险、绩效、ADF 与 GARCH 检验'],
    data: '研究中心读取已入库的 enriched 日 K 与扩展查询；分析计算只读 canonical 日 K，不产生任何交易建议。',
    freshness: '统计结果以所选区间内本地日 K 的最新日期为终点；样本不足时明确显示 insufficient，不会伪造数值。',
  },
  {
    id: 'analysis',
    index: '06',
    title: '标的深度分析',
    routes: ['个股分析', '财务分析', '概念分析', '行业分析', '市场环境'],
    to: '/stock-analysis',
    icon: FileSearch,
    tone: 'rose',
    purpose: '把价格、技术形态、财务、概念和行业维度组合成可核对的研究上下文。',
    workflow: ['从自选或搜索进入标的', '查看 K 线、财务和归属维度', '必要时发起带来源标记的 AI 辅助分析'],
    data: '本地日 K、财务表、成分/概念/行业数据；不同资产和数据表的覆盖不同。',
    freshness: '以页面结果的日期、来源与 `data_as_of` 为准；财务信息按已入库报告期更新。',
  },
  {
    id: 'monitor',
    index: '07',
    title: '监控与告警',
    routes: ['监控中心'],
    to: '/monitor',
    icon: RadioTower,
    tone: 'cyan',
    purpose: '把已验证的价格、信号和市场规则持续评估，并保留每一次触发原因。',
    workflow: ['建立少量可解释规则', '开启实时行情并确认范围', '查看命中记录，再按需配置通知'],
    data: 'provider 实时快照与本地规则；命中记录写入本地事实流并可经 SSE 展示。',
    freshness: '需要 realtime capability 和用户显式开启；能力缺失时不以无标记公共接口补齐。',
  },
  {
    id: 'review',
    index: '08',
    title: '市场复盘',
    routes: ['复盘'],
    to: '/review',
    icon: Layers3,
    tone: 'amber',
    purpose: '归档市场维度、情绪与规则命中，形成可回看的盘后研究记录。',
    workflow: ['在收盘后读取市场快照', '补充事实与个人观察', '按需启用定时复盘或推送'],
    data: '已同步的市场快照、榜单、板块与告警事实。',
    freshness: '定时大盘复盘默认关闭；报告反映生成时可用的数据，不代表盘中实时结论。',
  },
  {
    id: 'agent',
    index: '09',
    title: 'AI 助手',
    routes: ['AI 助手'],
    to: '/agent',
    icon: Bot,
    tone: 'indigo',
    purpose: '通过只读工具查询行情与策略，使用强类型条件生成可复现股票池，并异步运行策略或因子回测。',
    workflow: ['先核对选股字段与时间截面', '检查股票池、回测任务和 Run Card 工具链路', '将结果作为研究输入而非执行指令'],
    data: 'AI 仅访问工具白名单；完整股票池保存在服务端 artifact，模型只接收预览、任务状态与回测摘要。',
    freshness: '筛选日是回测最早起点；AI 不接收任意 SQL，不荐股、不生成订单、不自动下单。',
  },
  {
    id: 'trading',
    index: '10',
    title: '交易计划与复盘',
    routes: ['交易', '交易复盘'],
    to: '/trading',
    icon: Cable,
    tone: 'rose',
    purpose: '记录自己的计划、执行事实、纪律门禁和盘后归因，建立审计可追溯的闭环。',
    workflow: ['先写入计划与风险边界', '追加执行事实而非覆盖历史', '收盘后复盘偏差、红旗与归因'],
    data: '用户录入的 append-only 事实流；估值仍经 data_providers，fhold 仅可选读取持仓事实。',
    freshness: '这不是交易接口；结构化计划检查和自动归因均默认关闭，且不会产生订单。',
  },
  {
    id: 'data',
    index: '11',
    title: '数据与扩展',
    routes: ['数据'],
    to: '/data',
    icon: Database,
    tone: 'accent',
    purpose: '检查本地数据覆盖、运行盘后管道、同步指数/分钟数据并维护扩展数据 schema。',
    workflow: ['先核对表覆盖与最新日期', '按资产范围运行同步', '确认 enriched 数据后再做选股和回测'],
    data: '本地 DuckDB/Parquet，经 data_providers 统一读取；扩展数据需登记 schema 后使用。',
    freshness: '本页是数据新鲜度的权威操作入口；本页上方的实时状态会同步显示关键最新日期。',
  },
  {
    id: 'settings',
    index: '12',
    title: '设置与能力开关',
    routes: ['设置'],
    to: '/settings',
    icon: Settings2,
    tone: 'amber',
    purpose: '配置数据 provider、AI profile、通知、权限与默认关闭的实验性能力。',
    workflow: ['先检查 provider capabilities', '只开启理解成本和边界的能力', '在设置中停用或调整范围'],
    data: '设置影响请求范围和功能门控，不会把外部数据自动写入 canonical/enriched 主链路。',
    freshness: '实时、通知、fallback、定时复盘等都需要显式配置；保存后以状态卡为准。',
  },
]

function formatDate(value: string | null | undefined) {
  return value || '暂无记录'
}

function formatTimestamp(value: string | null | undefined) {
  if (!value) return '暂无记录'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function formatAge(ms: number | null | undefined) {
  if (ms == null) return '尚无快照'
  if (ms < 1_000) return `${Math.round(ms)} ms`
  const seconds = Math.round(ms / 1_000)
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

function DataPulse({
  title,
  value,
  detail,
  tone = 'neutral',
}: {
  title: string
  value: string
  detail: string
  tone?: 'neutral' | 'good' | 'warn'
}) {
  const toneClass = tone === 'good' ? 'text-success' : tone === 'warn' ? 'text-warning' : 'text-foreground'

  return (
    <article className="panel p-3">
      <div className="section-kicker flex items-center gap-2">
        <span className={cn('status-dot', tone === 'good' && 'bg-accent', tone === 'warn' && 'bg-warning')} data-state={tone === 'good' ? 'ok' : tone === 'warn' ? 'warn' : 'idle'} />
        {title}
      </div>
      <p className={cn('metric-value mt-2 truncate !text-sm', toneClass)} title={value}>
        {value}
      </p>
      <p className="mt-1.5 min-h-8 text-[10px] leading-relaxed text-secondary">{detail}</p>
    </article>
  )
}

function ModuleCard({ module, position }: { module: GuideModule; position: number }) {
  const Icon = module.icon
  const tone = TONE_STYLES[module.tone]

  return (
    <motion.article
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.32, delay: Math.min(position * 0.035, 0.3), ease: [0.16, 1, 0.3, 1] }}
      className={cn(
        'group panel relative overflow-hidden p-3 transition-colors duration-200',
        tone.border,
      )}
    >
      <div className={cn('absolute inset-x-0 top-0 h-px opacity-60', tone.line)} />
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className={cn('grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-elevated/80', tone.icon)}>
            <Icon className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <div className="font-mono text-[10px] tracking-[0.16em] text-muted">MODULE {module.index}</div>
            <h2 className="mt-0.5 text-sm font-semibold text-foreground">{module.title}</h2>
          </div>
        </div>
        <span className={cn('shrink-0 rounded-full px-2 py-1 text-[9px] font-semibold', tone.pill)}>
          {module.routes.join(' · ')}
        </span>
      </div>

      <p className="mt-3 text-xs leading-relaxed text-secondary">{module.purpose}</p>

      <div className="mt-4 grid gap-3 border-t border-border/70 pt-3 sm:grid-cols-2">
        <div>
          <div className="flex items-center gap-1.5 text-[10px] font-medium text-foreground/85">
            <Workflow className="h-3 w-3 text-muted" aria-hidden="true" />
            建议使用顺序
          </div>
          <ol className="mt-1.5 space-y-1 text-[10px] leading-relaxed text-muted">
            {module.workflow.map((step, index) => (
              <li key={step} className="flex gap-1.5">
                <span className="font-mono text-foreground/45">{index + 1}.</span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </div>
        <div className="space-y-2.5 text-[10px] leading-relaxed">
          <div>
            <div className="flex items-center gap-1.5 font-medium text-foreground/85">
              <Database className="h-3 w-3 text-muted" aria-hidden="true" />
              数据与来源
            </div>
            <p className="mt-1 text-muted">{module.data}</p>
          </div>
          <div>
            <div className="flex items-center gap-1.5 font-medium text-foreground/85">
              <Clock3 className="h-3 w-3 text-muted" aria-hidden="true" />
              时效与边界
            </div>
            <p className="mt-1 text-muted">{module.freshness}</p>
          </div>
        </div>
      </div>

      <Link
        to={module.to}
        className={cn('mt-4 inline-flex items-center gap-1.5 text-[11px] font-medium transition-colors', tone.icon)}
      >
        打开{module.routes[0]}
        <ArrowRight className="h-3.5 w-3.5 transition-transform duration-200 group-hover:translate-x-0.5" aria-hidden="true" />
      </Link>
    </motion.article>
  )
}

export function FeatureGuide() {
  const dataStatus = useDataStatus()
  const quoteStatus = useQuoteStatus()
  const capabilities = useCapabilities()
  const data = dataStatus.data
  const quote = quoteStatus.data
  const capabilityCount = Object.keys(capabilities.data?.capabilities ?? {}).length
  const realtimeActive = Boolean(quote?.realtime_allowed && quote.enabled && quote.running)
  const quoteDetail = !quote
    ? '正在读取行情服务状态'
    : !quote.realtime_allowed
      ? '当前 provider 未提供实时行情能力'
      : !quote.enabled
        ? '实时行情尚未开启'
        : quote.running
          ? `已覆盖 ${quote.symbol_count} 个标的${quote.is_trading_hours ? '，交易时段内由 SSE 驱动更新' : '，当前非交易时段'}`
          : '已开启，等待行情服务运行'

  return (
    <div className="workspace-page">
      <PageHeader
        title="功能与数据说明"
        subtitle="模块导航 · 建议用法 · 数据时效"
        right={
          <Link
            to="/data"
            className="btn-secondary hidden !h-8 text-[11px] sm:inline-flex"
          >
            <Database className="h-3.5 w-3.5" aria-hidden="true" />
            查看数据页
          </Link>
        }
      />

      <div className="workspace-content overflow-auto">
      <div className="mx-auto w-full max-w-7xl min-w-0 space-y-6 pb-6">
        <motion.section
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          className="panel px-4 py-4 sm:px-5"
        >
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(22rem,0.85fr)] lg:items-end">
            <div>
              <div className="section-kicker flex items-center gap-2 text-accent">
                <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />
                Field Manual
              </div>
              <h1 className="mt-2 max-w-2xl text-xl font-semibold tracking-tight text-foreground sm:text-2xl">
                先确认数据，再解释信号，最后做自己的决策。
              </h1>
              <p className="mt-3 max-w-2xl text-sm leading-relaxed text-secondary">
                量化研究工作台以本地 DuckDB 与封存的 enriched 数据为研究主链路。每个模块都应先看它使用的数据范围和最新日期；实时、AI、通知与计划检查均需要明确的 capability 或用户配置。
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                {['本地数据优先', '能力门控', '可追溯研究', '不自动交易'].map((label) => (
                  <span key={label} className="rounded-full border border-border bg-elevated/75 px-2.5 py-1 text-[10px] font-medium text-secondary">
                    {label}
                  </span>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2 border-t border-border/80 pt-4 lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
              <div>
                <div className="font-mono text-lg font-semibold text-accent">01</div>
                <p className="mt-1 text-[10px] leading-relaxed text-muted">检查覆盖与最新日期</p>
              </div>
              <div>
                <div className="font-mono text-lg font-semibold text-foreground">02</div>
                <p className="mt-1 text-[10px] leading-relaxed text-muted">用策略与回测验证</p>
              </div>
              <div>
                <div className="font-mono text-lg font-semibold text-foreground">03</div>
                <p className="mt-1 text-[10px] leading-relaxed text-muted">保留人的最终判断</p>
              </div>
            </div>
          </div>
        </motion.section>

        <section aria-labelledby="freshness-title">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <div className="section-kicker flex items-center gap-2 text-accent">
                <Activity className="h-3.5 w-3.5" aria-hidden="true" />
                Live Data Ledger
              </div>
              <h2 id="freshness-title" className="section-title mt-1 text-base">当前数据时效</h2>
              <p className="mt-1 text-[11px] text-muted">状态来自当前服务；“最新日期”是本地已可用数据的终点，不等同于盘中实时更新。</p>
            </div>
            <Link to="/data" className="inline-flex items-center gap-1.5 text-[11px] font-medium text-accent hover:text-accent/80">
              管理同步与范围
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
          </div>

          <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <DataPulse
              title="A 股日线 / enriched"
              value={formatDate(data?.enriched?.latest_date ?? data?.daily?.latest_date)}
              detail={data?.enriched?.symbols_covered ? `已覆盖 ${data.enriched.symbols_covered.toLocaleString('zh-CN')} 个标的` : '等待数据状态返回'}
              tone={data?.enriched?.latest_date || data?.daily?.latest_date ? 'good' : 'warn'}
            />
            <DataPulse
              title="指数 / ETF 日线"
              value={formatDate(data?.index_enriched?.latest_date ?? data?.index_daily?.latest_date ?? data?.etf_enriched?.latest_date ?? data?.etf_daily?.latest_date)}
              detail="指数与 ETF 的覆盖按各自数据表分别维护"
              tone={data?.index_enriched?.latest_date || data?.index_daily?.latest_date ? 'good' : 'warn'}
            />
            <DataPulse
              title="分钟数据覆盖"
              value={formatDate(data?.minute?.latest_date)}
              detail={data?.minute?.symbols_covered ? `已覆盖 ${data.minute.symbols_covered.toLocaleString('zh-CN')} 个标的；分钟与逐笔按能力路由` : '分钟数据不保证所有市场或日期可用'}
              tone={data?.minute?.latest_date ? 'good' : 'warn'}
            />
            <DataPulse
              title="实时行情"
              value={realtimeActive ? `快照年龄 ${formatAge(quote?.quote_age_ms)}` : quote?.realtime_allowed ? '已配置但未运行' : '能力不可用'}
              detail={quoteDetail}
              tone={realtimeActive ? 'good' : 'warn'}
            />
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-input border border-border/70 bg-elevated/35 px-3 py-2 text-[10px] text-muted">
            <span className="inline-flex items-center gap-1.5"><RefreshCw className="h-3 w-3" aria-hidden="true" />最近管道：{formatTimestamp(data?.last_pipeline_run)}</span>
            <span className="inline-flex items-center gap-1.5"><ShieldCheck className="h-3 w-3" aria-hidden="true" />provider：{capabilities.data?.label ?? '检测中'} · {capabilityCount || '—'} 项能力</span>
            <span className="inline-flex items-center gap-1.5"><CheckCircle2 className="h-3 w-3" aria-hidden="true" />状态检查：{formatTimestamp(data?.checked_at)}</span>
          </div>
        </section>

        <section aria-labelledby="modules-title">
          <div className="flex items-end justify-between gap-4">
            <div>
              <div className="section-kicker flex items-center gap-2 text-accent">
                <Filter className="h-3.5 w-3.5" aria-hidden="true" />
                Module Map
              </div>
              <h2 id="modules-title" className="section-title mt-1 text-base">模块功能与使用方法</h2>
              <p className="mt-1 text-[11px] text-muted">按研究链路排列；点击卡片底部入口直接进入对应页面。</p>
            </div>
            <span className="hidden font-mono text-[10px] text-muted sm:block">{MODULES.length.toString().padStart(2, '0')} MODULES</span>
          </div>

          <div className="mt-3 grid gap-3 xl:grid-cols-2">
            {MODULES.map((module, index) => (
              <ModuleCard key={module.id} module={module} position={index} />
            ))}
          </div>
        </section>

        <section className="panel bg-elevated/35 p-4 sm:p-5" aria-labelledby="boundaries-title">
          <div className="flex items-start gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-warning/10 text-warning">
              <Sparkles className="h-4 w-4" aria-hidden="true" />
            </span>
            <div>
              <h2 id="boundaries-title" className="text-sm font-semibold">使用前请记住四个边界</h2>
              <ul className="mt-2 grid gap-2 text-[11px] leading-relaxed text-secondary md:grid-cols-2 xl:grid-cols-4 md:gap-5">
                <li><strong className="text-foreground">实时是可选能力：</strong>只有 provider 支持且在设置中开启后，页面才会接收实时快照；否则使用已入库的最近数据。</li>
                <li><strong className="text-foreground">五档盘口当前有缺口：</strong>本地 fquant provider 不暴露 depth5，相关页面会降级为空；受控 fallback 若另行启用，结果必须标明来源。</li>
                <li><strong className="text-foreground">外部 fallback 受控且默认关闭：</strong>仅能补真实缺口并标记来源，绝不进入 canonical、enriched、选股或回测输入。</li>
                <li><strong className="text-foreground">研究不等于执行：</strong>AI、回测、监控和计划检查用于解释、验证和审计，不提供荐股、自动下单或投资承诺。</li>
              </ul>
            </div>
          </div>
        </section>
      </div>
      </div>
    </div>
  )
}
