import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  AlertTriangle,
  Banknote,
  CheckCircle2,
  ChevronDown,
  CircleDollarSign,
  ClipboardCheck,
  Eye,
  FileText,
  FlaskConical,
  Info,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  WalletCards,
} from 'lucide-react'

import { PageHeader } from '@/components/PageHeader'
import { api } from '@/lib/api'
import {
  actionPresentation,
  dataPhasePresentation,
  formatAdvisorCoverage,
  presentDailyBriefCandidate,
  presentResearchSnapshot,
  presentTrustDatasets,
  resolvePaperActionState,
  selectDailyBriefCandidates,
  selectPlanMonitorStrategyIds,
  type AdvisorActionState,
  type AdvisorDataPhase,
  type DailyBriefCandidatePresentation,
  type TrustDatasetPresentation,
} from '@/lib/advisor'
import {
  canRecordPaperTrade,
  createPaperTradeDraft,
  lotGuidance,
  paperMutationErrorMessage,
  portfolioRiskPresentation,
  preparePaperTradeDraftForSubmit,
  toPaperTradeRequest,
  validatePaperTradeDraft,
  type PaperAccountResponse,
  type PaperJournalEntry,
  type PaperPosition,
  type PaperTradeDraft,
  type PaperTradeRequest,
  type PaperTradeSide,
} from '@/lib/paper-account'
import { QK } from '@/lib/queryKeys'

const money = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
})

const compactDateTime = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

const PLAN_MONITOR_REFRESH_MS = 5 * 60 * 1000

const PORTFOLIO_RISK_TONE = {
  neutral: 'border-border bg-elevated/20 text-secondary',
  safe: 'border-bear/25 bg-bear/5 text-bear',
  warning: 'border-warning/30 bg-warning/5 text-warning',
  danger: 'border-danger/30 bg-danger/5 text-danger',
} as const

const ACTION_TONE: Record<ReturnType<typeof actionPresentation>['tone'], {
  border: string
  background: string
  icon: string
  text: string
}> = {
  warning: {
    border: 'border-warning/40',
    background: 'bg-warning/[0.04]',
    icon: 'bg-warning/10 text-warning',
    text: 'text-warning',
  },
  accent: {
    border: 'border-accent/35',
    background: 'bg-accent/[0.04]',
    icon: 'bg-accent/10 text-accent',
    text: 'text-accent',
  },
  success: {
    border: 'border-bear/35',
    background: 'bg-bear/[0.04]',
    icon: 'bg-bear/10 text-bear',
    text: 'text-bear',
  },
}

const inputClass = 'h-11 w-full min-w-0 rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent disabled:cursor-not-allowed disabled:opacity-50'
const labelClass = 'mb-1.5 block text-xs font-medium text-secondary'

function ActionOverview({
  actionState,
  todayMessage,
  nextStep,
  dataPassed,
  dataPhase,
}: {
  actionState: AdvisorActionState
  todayMessage: string
  nextStep: string
  dataPassed: boolean
  dataPhase: AdvisorDataPhase
}) {
  const presentation = actionPresentation(actionState)
  const tone = ACTION_TONE[presentation.tone]
  const phasePresentation = dataPhasePresentation(dataPhase, dataPassed)
  const phaseTextClass = phasePresentation.tone === 'success'
    ? 'text-bear'
    : phasePresentation.tone === 'warning'
      ? 'text-warning'
      : 'text-danger'

  return (
    <section className={`rounded-card border ${tone.border} ${tone.background} p-4 sm:p-5`}>
      <div className="grid min-w-0 gap-4 lg:grid-cols-[0.82fr_1fr_1.25fr] lg:items-stretch lg:gap-0">
        <div className="min-w-0 lg:pr-6">
          <p className="text-xs font-medium text-secondary">今日行动</p>
          <div className="mt-3 flex items-center gap-3">
            <span className={`grid h-12 w-12 shrink-0 place-items-center rounded-card ${tone.icon}`}>
              {actionState === 'OBSERVE_ONLY'
                ? <Eye className="h-6 w-6" />
                : actionState === 'SIMULATE_ONLY'
                  ? <FlaskConical className="h-6 w-6" />
                  : <ClipboardCheck className="h-6 w-6" />}
            </span>
            <div className="min-w-0">
              <div className={`text-3xl font-semibold tracking-tight ${tone.text}`}>
                {presentation.label}
              </div>
              <p className="mt-1 text-xs leading-relaxed text-secondary">
                {presentation.description}
              </p>
            </div>
          </div>
        </div>

        <div className="min-w-0 border-t border-border/70 pt-4 lg:border-l lg:border-t-0 lg:px-6 lg:pt-0">
          <div className="flex items-start gap-2.5">
            {phasePresentation.tone === 'success'
              ? <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-bear" />
              : <AlertTriangle className={`mt-0.5 h-5 w-5 shrink-0 ${phaseTextClass}`} />}
            <div className="min-w-0">
              <h2 className={`text-sm font-semibold ${phaseTextClass}`}>
                {phasePresentation.label}
              </h2>
              <p className="mt-1 text-xs leading-relaxed text-secondary">{todayMessage}</p>
            </div>
          </div>
        </div>

        <div className="min-w-0 border-t border-border/70 pt-4 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
          <div className="flex items-start gap-2.5">
            <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full border border-accent/50 text-accent">
              <ChevronDown className="h-3 w-3" />
            </span>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-foreground">下一步</h2>
              <p className="mt-1 break-words text-xs leading-relaxed text-secondary">{nextStep}</p>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function CandidateCard({
  candidate,
  index,
}: {
  candidate: DailyBriefCandidatePresentation
  index: number
}) {
  const monitorItems = candidate.planMonitor
    ? [
        `复核日期：${candidate.planMonitor.asOf}`,
        ...(candidate.planMonitor.lastPrice == null
          ? []
          : [`最新价：${money.format(candidate.planMonitor.lastPrice)}`]),
        ...(candidate.planMonitor.changePct == null
          ? []
          : [`当日涨跌：${(candidate.planMonitor.changePct * 100).toFixed(2)}%`]),
        ...candidate.planMonitor.evidence,
      ]
    : []
  const sections = [
    ...(candidate.planMonitor
      ? [{
          title: '盘中监控依据',
          icon: Eye,
          iconClass: candidate.planMonitor.tone === 'danger'
            ? 'text-danger'
            : candidate.planMonitor.tone === 'success'
              ? 'text-bear'
              : 'text-warning',
          items: monitorItems,
          empty: '尚无盘中复核证据。',
        }]
      : []),
    {
      title: '为什么入选',
      icon: ClipboardCheck,
      iconClass: 'text-accent',
      items: candidate.reasons,
      empty: '后端未返回入选原因。',
    },
    {
      title: '状态推进',
      icon: CheckCircle2,
      iconClass: 'text-bear',
      items: candidate.observationConditions,
      empty: '后端未返回继续观察条件。',
    },
    {
      title: '失效条件',
      icon: AlertCircle,
      iconClass: 'text-danger',
      items: candidate.invalidationConditions,
      empty: '后端未返回失效条件。',
    },
    {
      title: '风险拦截',
      icon: ShieldAlert,
      iconClass: 'text-warning',
      items: candidate.riskMessages,
      empty: '当前未返回硬风险标记。',
    },
  ]
  const statusClass = candidate.planMonitor?.tone === 'danger'
    ? 'border-danger/35 bg-danger/5 text-danger'
    : candidate.planMonitor?.tone === 'success'
      ? 'border-bear/35 bg-bear/5 text-bear'
      : candidate.planMonitor?.tone === 'warning'
        ? 'border-warning/35 bg-warning/5 text-warning'
        : candidate.readyForSimulation
          ? 'border-bear/35 bg-bear/5 text-bear'
          : 'border-warning/35 bg-warning/5 text-warning'

  return (
    <article className="min-w-0 rounded-card border border-border bg-surface p-4">
      <header className="flex min-w-0 items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full border border-accent/40 font-mono text-sm text-accent">
            {index + 1}
          </span>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-foreground">{candidate.name}</div>
            <div className="mt-0.5 break-all font-mono text-[11px] text-muted">{candidate.symbol}</div>
            <div className="mt-1 text-[10px] text-muted">
              连续确认 {candidate.goStreak} 日 · {candidate.lotSize} 股约
              {candidate.lotCost == null ? ' 未提供' : ` ${money.format(candidate.lotCost)}`}
            </div>
          </div>
        </div>
        <span className={`shrink-0 rounded border px-2 py-1 text-[10px] ${statusClass}`}>
          {candidate.planMonitor?.label ?? candidate.statusLabel}
        </span>
      </header>

      <div className="mt-4 divide-y divide-border/70 rounded-card border border-border/70">
        {sections.map(({ title, icon: Icon, iconClass, items, empty }) => (
          <section key={title} className="grid min-w-0 grid-cols-[18px_1fr] gap-2.5 p-3">
            <Icon className={`mt-0.5 h-4 w-4 ${iconClass}`} />
            <div className="min-w-0">
              <h3 className="text-xs font-medium text-foreground">{title}</h3>
              {items.length > 0 ? (
                <ul className="mt-1.5 space-y-1">
                  {items.map(item => (
                    <li key={item} className="break-words text-[11px] leading-relaxed text-secondary">
                      {item}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{empty}</p>
              )}
            </div>
          </section>
        ))}
      </div>
    </article>
  )
}

function TrustReceiptCard({ receipt }: { receipt: TrustDatasetPresentation }) {
  const hasReasons = receipt.reasons.length > 0
  return (
    <article className={`min-w-0 rounded-card border p-3.5 ${
      hasReasons ? 'border-danger/30 bg-danger/[0.025]' : 'border-border bg-surface'
    }`}>
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">{receipt.label}</h3>
        <span className={`text-[10px] font-medium ${hasReasons ? 'text-danger' : 'text-bear'}`}>
          {receipt.statusLabel}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-[11px]">
        <dt className="text-muted">数据源</dt>
        <dd className="min-w-0 break-all text-right font-mono text-secondary">{receipt.provider}</dd>
        <dt className="text-muted">覆盖率</dt>
        <dd className="text-right font-mono text-secondary">
          {formatAdvisorCoverage(receipt.coverageRatio)}
        </dd>
        <dt className="text-muted">日期范围</dt>
        <dd className="min-w-0 break-words text-right font-mono text-secondary">
          {receipt.observedStart || '未提供'} 至 {receipt.observedEnd || '未提供'}
        </dd>
      </dl>
      {hasReasons && (
        <div className="mt-3 space-y-2 border-t border-danger/15 pt-3 text-[11px] leading-relaxed">
          {receipt.reasons.map(reason => (
            <p key={reason} className="break-words text-danger">原因：{reason}</p>
          ))}
          {receipt.nextActions.map(action => (
            <p key={action} className="break-words text-secondary">下一步：{action}</p>
          ))}
        </div>
      )}
    </article>
  )
}

function Metric({
  label,
  value,
  valueClass = 'text-foreground',
}: {
  label: string
  value: string
  valueClass?: string
}) {
  return (
    <div className="min-w-0 rounded-card border border-border bg-base p-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`mt-1 break-all font-mono text-base font-semibold tabular ${valueClass}`}>
        {value}
      </div>
    </div>
  )
}

function PositionCard({
  position,
  warning,
}: {
  position: PaperPosition
  warning?: string
}) {
  return (
    <article className="min-w-0 rounded-card border border-border bg-base p-3.5">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-foreground">{position.name}</div>
          <div className="mt-0.5 break-all font-mono text-[10px] text-muted">{position.symbol}</div>
        </div>
        <span className={`shrink-0 font-mono text-xs ${
          position.unrealized_pnl > 0
            ? 'text-bull'
            : position.unrealized_pnl < 0
              ? 'text-bear'
              : 'text-secondary'
        }`}>
          {money.format(position.unrealized_pnl)}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
        <div>
          <dt className="text-muted">持有 / 可卖</dt>
          <dd className="mt-0.5 font-mono text-secondary">
            {position.quantity} / {position.sellable_quantity} 股
          </dd>
        </div>
        <div>
          <dt className="text-muted">平均成本</dt>
          <dd className="mt-0.5 font-mono text-secondary">{money.format(position.average_cost)}</dd>
        </div>
        <div>
          <dt className="text-muted">当前估值</dt>
          <dd className="mt-0.5 font-mono text-secondary">{money.format(position.mark_price)}</dd>
        </div>
        <div>
          <dt className="text-muted">估值来源</dt>
          <dd className="mt-0.5 text-secondary">
            {position.mark_source === 'STRATEGY_CACHE' ? '策略缓存' : '持仓成本回退'}
          </dd>
        </div>
        <div>
          <dt className="text-muted">占账户总资产</dt>
          <dd className="mt-0.5 font-mono text-secondary">
            {position.portfolio_weight_pct.toFixed(2)}%
          </dd>
        </div>
        <div>
          <dt className="text-muted">占已投资部分</dt>
          <dd className="mt-0.5 font-mono text-secondary">
            {position.invested_weight_pct.toFixed(2)}%
          </dd>
        </div>
      </dl>
      {warning && (
        <p className="mt-3 flex items-start gap-1.5 border-t border-warning/15 pt-2.5 text-[10px] leading-relaxed text-warning">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span className="break-words">{warning}</span>
        </p>
      )}
    </article>
  )
}

function JournalCard({ entry }: { entry: PaperJournalEntry }) {
  let timestamp = entry.timestamp
  try {
    timestamp = compactDateTime.format(new Date(entry.timestamp))
  } catch {
    // Keep the exact backend timestamp when the browser cannot parse it.
  }

  return (
    <article className="min-w-0 rounded-card border border-border bg-base p-3.5">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded border px-2 py-0.5 text-[10px] ${
              entry.side === 'BUY'
                ? 'border-accent/30 bg-accent/10 text-accent'
                : 'border-warning/30 bg-warning/10 text-warning'
            }`}>
              {entry.side === 'BUY' ? '模拟买入' : '模拟卖出'}
            </span>
            <span className="font-mono text-[10px] text-muted">{timestamp}</span>
          </div>
          <div className="mt-2 truncate text-sm font-medium text-foreground">{entry.name}</div>
          <div className="mt-0.5 break-all font-mono text-[10px] text-muted">{entry.symbol}</div>
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-xs text-foreground">{entry.quantity} 股</div>
          <div className="mt-1 font-mono text-[10px] text-muted">{money.format(entry.price)}</div>
        </div>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-2 border-t border-border/70 pt-3 text-[10px]">
        <div>
          <dt className="text-muted">费用</dt>
          <dd className="mt-0.5 font-mono text-secondary">{money.format(entry.total_fees)}</dd>
        </div>
        <div>
          <dt className="text-muted">已实现盈亏</dt>
          <dd className="mt-0.5 font-mono text-secondary">{money.format(entry.realized_pnl)}</dd>
        </div>
      </dl>
      <div className="mt-3 space-y-2 text-[11px] leading-relaxed">
        <p className="break-words text-secondary">
          <span className="text-muted">模拟计划：</span>{entry.plan_note || '未填写'}
        </p>
        <p className="break-words text-secondary">
          <span className="text-muted">失效条件：</span>{entry.invalidation_note || '未填写'}
        </p>
      </div>
    </article>
  )
}

function PaperAccountSection({
  account,
  accountLoading,
  accountError,
  actionState,
  briefUnavailable,
}: {
  account?: PaperAccountResponse
  accountLoading: boolean
  accountError: unknown
  actionState?: AdvisorActionState
  briefUnavailable: boolean
}) {
  const queryClient = useQueryClient()
  const [initialCash, setInitialCash] = useState<5000 | 10000>(10000)
  const [confirmation, setConfirmation] = useState('')
  const [formErrors, setFormErrors] = useState<string[]>([])
  const [tradeDateEdited, setTradeDateEdited] = useState(false)
  const [draft, setDraft] = useState<PaperTradeDraft>(() => createPaperTradeDraft())

  const resetMutation = useMutation({
    mutationFn: () => api.paperReset({
      initial_cash: initialCash,
      confirmation: 'RESET',
    }),
    onSuccess: async () => {
      setConfirmation('')
      setFormErrors([])
      setTradeDateEdited(false)
      setDraft(createPaperTradeDraft())
      await queryClient.invalidateQueries({ queryKey: QK.paperAccount })
    },
  })

  const tradeMutation = useMutation({
    mutationFn: (payload: PaperTradeRequest) => api.paperTrade(payload),
    onSuccess: async () => {
      setFormErrors([])
      setTradeDateEdited(false)
      setDraft(createPaperTradeDraft())
      await queryClient.invalidateQueries({ queryKey: QK.paperAccount })
    },
  })

  const tradeBlocked = !canRecordPaperTrade(actionState, draft.side)
  let safetyMessage: string | null = null
  if (draft.side === 'SELL') {
    safetyMessage = '模拟卖出不受新增买入门槛限制；后端仍会检查持仓数量和 T+1。'
  } else if (briefUnavailable) {
    safetyMessage = '安全保护：今日日报刷新失败，暂不能记录模拟买入。'
  } else if (actionState === 'OBSERVE_ONLY') {
    safetyMessage = '安全保护：数据或市场条件未通过，暂不能记录模拟买入。'
  } else if (actionState === 'RESEARCH_ONLY') {
    safetyMessage = '候选仅确认第 1 天；下一可信交易日再次通过后，才允许模拟买入。'
  } else if (actionState === 'NO_CANDIDATE') {
    safetyMessage = '本批候选已全部淘汰，暂不能记录模拟买入。'
  } else if (actionState === 'MODEL_WARNING') {
    safetyMessage = '连续 10 个完整交易日没有候选完成确认，模型校准前暂停模拟买入。'
  } else if (!actionState) {
    safetyMessage = '安全保护：正在确认今日行动，暂不能记录模拟买入。'
  }

  const warningBySymbol = useMemo(
    () => new Map((account?.valuation_warnings ?? []).map(item => [item.symbol, item.message])),
    [account?.valuation_warnings],
  )
  const riskPresentation = account
    ? portfolioRiskPresentation(account.portfolio_risk)
    : null

  function updateDraft<K extends keyof PaperTradeDraft>(key: K, value: PaperTradeDraft[K]) {
    setDraft(current => ({ ...current, [key]: value }))
    setFormErrors([])
  }

  function submitTrade(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!actionState && draft.side === 'BUY') {
      setFormErrors(['今日行动尚未读取完成。下一步：请先刷新日报。'])
      return
    }
    const preparedDraft = preparePaperTradeDraftForSubmit(draft, tradeDateEdited)
    const errors = validatePaperTradeDraft(preparedDraft, actionState)
    setFormErrors(errors)
    if (errors.length === 0) {
      setDraft(preparedDraft)
      tradeMutation.mutate(toPaperTradeRequest(preparedDraft))
    }
  }

  function chooseSide(side: PaperTradeSide) {
    updateDraft('side', side)
  }

  return (
    <section className="min-w-0 rounded-card border border-border bg-surface">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-4 sm:px-5">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <WalletCards className="h-4 w-4 text-accent" />
            模拟账户（不会下单）
          </h2>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            只记录你手动填写的模拟成交，不连接券商，也不会读取券商凭据。
          </p>
        </div>
        {account && (
          <span className="rounded border border-border bg-base px-2 py-1 font-mono text-[10px] text-muted">
            估值日 {account.valuation_date}
          </span>
        )}
      </header>

      {accountLoading ? (
        <div className="p-8 text-center text-sm text-muted">正在读取本地模拟账户…</div>
      ) : accountError ? (
        <div className="m-4 rounded-card border border-danger/25 bg-danger/5 p-4 text-sm leading-relaxed text-danger">
          模拟账户读取失败：{accountError instanceof Error ? accountError.message : '未知错误'}
          <p className="mt-1 text-xs text-secondary">下一步：刷新页面；若仍失败，请先备份本地账户文件。</p>
        </div>
      ) : account ? (
        <div className="space-y-5 p-4 sm:p-5">
          <div className="grid min-w-0 grid-cols-2 gap-2 lg:grid-cols-5">
            <Metric label="现金" value={money.format(account.cash)} />
            <Metric label="总资产" value={money.format(account.total_equity)} />
            <Metric
              label="已实现盈亏"
              value={money.format(account.realized_pnl)}
              valueClass={account.realized_pnl > 0 ? 'text-bull' : account.realized_pnl < 0 ? 'text-bear' : 'text-foreground'}
            />
            <Metric
              label="未实现盈亏"
              value={money.format(account.unrealized_pnl)}
              valueClass={account.unrealized_pnl > 0 ? 'text-bull' : account.unrealized_pnl < 0 ? 'text-bear' : 'text-foreground'}
            />
            <div className="col-span-2 lg:col-span-1">
              <Metric
                label="总盈亏"
                value={money.format(account.total_pnl)}
                valueClass={account.total_pnl > 0 ? 'text-bull' : account.total_pnl < 0 ? 'text-bear' : 'text-foreground'}
              />
            </div>
          </div>

          {riskPresentation && (
            <section className={`rounded-card border px-4 py-3 ${PORTFOLIO_RISK_TONE[riskPresentation.tone]}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="flex items-center gap-2 text-xs font-semibold">
                  <ShieldAlert className="h-4 w-4" />
                  组合风险：{riskPresentation.label}
                </h3>
                <span className="font-mono text-[10px]">
                  HHI {account.portfolio_risk.concentration_hhi.toFixed(4)}
                </span>
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed">
                {riskPresentation.detail}
              </p>
              {account.portfolio_risk.warnings.map(warning => (
                <p key={warning.code} className="mt-1 text-[11px] leading-relaxed">
                  {warning.message}。这只是风险提示，不是买入或卖出指令。
                </p>
              ))}
            </section>
          )}

          <div className="grid min-w-0 gap-5 xl:grid-cols-[1.1fr_0.9fr]">
            <div className="min-w-0 space-y-5">
              <section>
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-xs font-semibold text-foreground">持仓（{account.positions.length}）</h3>
                  <span className="text-[10px] text-muted">可卖数量已按 T+1 计算</span>
                </div>
                {account.positions.length === 0 ? (
                  <div className="rounded-card border border-dashed border-border bg-base px-4 py-8 text-center">
                    <CircleDollarSign className="mx-auto h-6 w-6 text-muted" />
                    <p className="mt-2 text-sm text-secondary">暂无模拟持仓</p>
                    <p className="mt-1 text-[11px] text-muted">可先填写一笔手动模拟成交，熟悉规则和费用。</p>
                  </div>
                ) : (
                  <div className="grid min-w-0 gap-2 sm:grid-cols-2">
                    {account.positions.map(position => (
                      <PositionCard
                        key={position.symbol}
                        position={position}
                        warning={warningBySymbol.get(position.symbol)}
                      />
                    ))}
                  </div>
                )}
              </section>

              <section className="rounded-card border border-border bg-base p-4">
                <h3 className="text-xs font-semibold text-foreground">模拟资金管理</h3>
                <p className="mt-1 text-[11px] leading-relaxed text-warning">
                  重置会清空全部模拟持仓和成交记录。
                </p>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {([5000, 10000] as const).map(value => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setInitialCash(value)}
                      aria-pressed={initialCash === value}
                      className={`min-h-11 rounded-btn border px-3 text-sm transition-colors ${
                        initialCash === value
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-border bg-surface text-secondary hover:text-foreground'
                      }`}
                    >
                      {money.format(value)}
                    </button>
                  ))}
                </div>
                <label className="mt-3 block">
                  <span className={labelClass}>确认文字（必须准确输入 RESET）</span>
                  <input
                    value={confirmation}
                    onChange={event => setConfirmation(event.target.value)}
                    className={inputClass}
                    autoComplete="off"
                    placeholder="RESET"
                  />
                </label>
                <button
                  type="button"
                  onClick={() => resetMutation.mutate()}
                  disabled={confirmation !== 'RESET' || resetMutation.isPending}
                  className="mt-3 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-btn border border-danger/40 bg-danger/5 px-4 text-sm font-medium text-danger transition-colors hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <RotateCcw className={`h-4 w-4 ${resetMutation.isPending ? 'animate-spin' : ''}`} />
                  确认重置
                </button>
                {resetMutation.error && (
                  <p className="mt-2 break-words text-xs leading-relaxed text-danger">
                    {paperMutationErrorMessage(resetMutation.error, 'reset')}
                  </p>
                )}
              </section>
            </div>

            <form onSubmit={submitTrade} className="min-w-0 rounded-card border border-border bg-base p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h3 className="text-xs font-semibold text-foreground">手动模拟成交</h3>
                  <p className="mt-1 text-[10px] leading-relaxed text-muted">
                    浏览器只做填写提示，最终规则由后端再次校验。
                  </p>
                </div>
                <span className="rounded border border-border bg-surface px-2 py-1 text-[10px] text-muted">
                  不会发送真实委托
                </span>
              </div>

              {safetyMessage && (
                <div className="mt-3 flex items-start gap-2 rounded-card border border-warning/30 bg-warning/5 p-3 text-xs leading-relaxed text-warning">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{safetyMessage}</span>
                </div>
              )}

              <div className="mt-4 grid grid-cols-2 gap-2">
                {([
                  ['BUY', '模拟买入'],
                  ['SELL', '模拟卖出'],
                ] as const).map(([side, label]) => (
                  <button
                    key={side}
                    type="button"
                    onClick={() => chooseSide(side)}
                    disabled={!canRecordPaperTrade(actionState, side) || tradeMutation.isPending}
                    aria-pressed={draft.side === side}
                    className={`min-h-11 rounded-btn border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                      draft.side === side
                        ? side === 'BUY'
                          ? 'border-accent bg-accent text-white'
                          : 'border-warning bg-warning/90 text-white'
                        : 'border-border bg-surface text-secondary hover:text-foreground'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              <div className="mt-4 grid min-w-0 gap-3 sm:grid-cols-2">
                <label className="min-w-0">
                  <span className={labelClass}>股票代码</span>
                  <input
                    value={draft.symbol}
                    onChange={event => updateDraft('symbol', event.target.value)}
                    disabled={tradeBlocked || tradeMutation.isPending}
                    className={inputClass}
                    placeholder="600000.SH"
                    autoComplete="off"
                  />
                </label>
                <label className="min-w-0">
                  <span className={labelClass}>股票名称</span>
                  <input
                    value={draft.name}
                    onChange={event => updateDraft('name', event.target.value)}
                    disabled={tradeBlocked || tradeMutation.isPending}
                    className={inputClass}
                    placeholder="填写当前标的名称"
                    autoComplete="off"
                  />
                </label>
                <label className="min-w-0">
                  <span className={labelClass}>模拟数量（股）</span>
                  <input
                    value={draft.quantity}
                    onChange={event => updateDraft('quantity', event.target.value)}
                    disabled={tradeBlocked || tradeMutation.isPending}
                    className={inputClass}
                    inputMode="numeric"
                    placeholder="100"
                    autoComplete="off"
                  />
                </label>
                <label className="min-w-0">
                  <span className={labelClass}>模拟成交价（元）</span>
                  <input
                    value={draft.price}
                    onChange={event => updateDraft('price', event.target.value)}
                    disabled={tradeBlocked || tradeMutation.isPending}
                    className={inputClass}
                    inputMode="decimal"
                    placeholder="0.00"
                    autoComplete="off"
                  />
                </label>
              </div>
              <p className="mt-2 text-[10px] leading-relaxed text-muted">
                {lotGuidance(draft.symbol, draft.side)}
              </p>

              <label className="mt-3 block">
                <span className={labelClass}>模拟成交日期</span>
                <input
                  type="date"
                  value={draft.trade_date}
                  onChange={event => {
                    setTradeDateEdited(true)
                    updateDraft('trade_date', event.target.value)
                  }}
                  disabled={tradeBlocked || tradeMutation.isPending}
                  className={inputClass}
                />
              </label>
              <label className="mt-3 block">
                <span className={labelClass}>模拟计划</span>
                <textarea
                  value={draft.plan_note}
                  onChange={event => updateDraft('plan_note', event.target.value)}
                  disabled={tradeBlocked || tradeMutation.isPending}
                  className="min-h-20 w-full resize-y rounded-input border border-border bg-base px-3 py-2.5 text-sm leading-relaxed text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent disabled:cursor-not-allowed disabled:opacity-50"
                  placeholder="记录为什么要做这次模拟练习"
                  maxLength={500}
                />
              </label>
              <label className="mt-3 block">
                <span className={labelClass}>失效条件</span>
                <textarea
                  value={draft.invalidation_note}
                  onChange={event => updateDraft('invalidation_note', event.target.value)}
                  disabled={tradeBlocked || tradeMutation.isPending}
                  className="min-h-20 w-full resize-y rounded-input border border-border bg-base px-3 py-2.5 text-sm leading-relaxed text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent disabled:cursor-not-allowed disabled:opacity-50"
                  placeholder="记录什么情况出现后停止这次模拟观察"
                  maxLength={500}
                />
              </label>

              {formErrors.length > 0 && (
                <ul className="mt-3 space-y-1 rounded-card border border-danger/25 bg-danger/5 p-3 text-xs leading-relaxed text-danger">
                  {formErrors.map(error => <li key={error}>· {error}</li>)}
                </ul>
              )}
              {tradeMutation.error && (
                <div className="mt-3 rounded-card border border-danger/25 bg-danger/5 p-3 text-xs leading-relaxed text-danger">
                  {paperMutationErrorMessage(tradeMutation.error, 'trade')}
                </div>
              )}

              <button
                type="submit"
                disabled={tradeBlocked || tradeMutation.isPending}
                className="mt-4 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-muted disabled:opacity-100"
              >
                <FileText className="h-4 w-4" />
                {tradeMutation.isPending
                  ? '正在记录…'
                  : draft.side === 'BUY'
                    ? '记录模拟买入'
                    : '记录模拟卖出'}
              </button>
            </form>
          </div>

          <section className="rounded-card border border-border bg-base p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="flex items-center gap-2 text-xs font-semibold text-foreground">
                <Banknote className="h-4 w-4 text-accent" />
                费用仅为模拟假设
              </h3>
              <span className="text-[10px] text-muted">{account.fee_assumptions.disclaimer}</span>
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-[11px] sm:grid-cols-4">
              <div>
                <dt className="text-muted">双边佣金</dt>
                <dd className="mt-1 font-mono text-secondary">{account.fee_assumptions.commission_rate_label}</dd>
              </div>
              <div>
                <dt className="text-muted">单笔最低佣金</dt>
                <dd className="mt-1 font-mono text-secondary">{money.format(account.fee_assumptions.minimum_commission)}</dd>
              </div>
              <div>
                <dt className="text-muted">模拟卖出印花税</dt>
                <dd className="mt-1 font-mono text-secondary">{account.fee_assumptions.sell_stamp_tax_rate_label}</dd>
              </div>
              <div>
                <dt className="text-muted">滑点</dt>
                <dd className="mt-1 break-words text-secondary">{account.fee_assumptions.slippage}</dd>
              </div>
            </dl>
          </section>

          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-semibold text-foreground">最近模拟记录</h3>
              <span className="text-[10px] text-muted">共 {account.journal.length} 条</span>
            </div>
            {account.journal.length === 0 ? (
              <div className="rounded-card border border-dashed border-border bg-base px-4 py-8 text-center">
                <FileText className="mx-auto h-6 w-6 text-muted" />
                <p className="mt-2 text-sm text-secondary">暂无模拟记录</p>
                <p className="mt-1 text-[11px] text-muted">每次手动记录都会保留费用、计划和失效条件。</p>
              </div>
            ) : (
              <div className="grid min-w-0 gap-2 md:grid-cols-2 xl:grid-cols-3">
                {[...account.journal].reverse().slice(0, 9).map(entry => (
                  <JournalCard key={entry.id} entry={entry} />
                ))}
              </div>
            )}
          </section>
        </div>
      ) : null}
    </section>
  )
}

export function Advisor() {
  const queryClient = useQueryClient()
  const briefQuery = useQuery({
    queryKey: QK.advisorBrief,
    queryFn: api.advisorDailyBrief,
    refetchInterval: query => {
      const phase = query.state.data?.data_phase?.phase
      return phase === 'LIVE_PROVISIONAL' || phase === 'EOD_PENDING'
        ? 30_000
        : false
    },
  })
  const accountQuery = useQuery({
    queryKey: QK.paperAccount,
    queryFn: api.paperAccount,
  })

  const planStrategyKey = useMemo(
    () => JSON.stringify(selectPlanMonitorStrategyIds(briefQuery.data?.candidates ?? [])),
    [briefQuery.data?.candidates],
  )
  const lastPlanRefreshRef = useRef<{ key: string; at: number } | null>(null)
  const {
    mutate: refreshPlanStrategies,
    isPending: planRefreshPending,
    isError: planRefreshFailed,
  } = useMutation({
    mutationFn: ({ asOf, strategyIds }: { asOf: string; strategyIds: string[] }) =>
      api.screenerRunAll(asOf, strategyIds, 'stock'),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: QK.advisorBrief })
    },
  })

  useEffect(() => {
    const phase = briefQuery.data?.data_phase?.phase
    const asOf = briefQuery.data?.data_phase?.as_of
    const sourceAsOf = briefQuery.data?.plan_source_as_of
    const strategyIds = JSON.parse(planStrategyKey) as string[]
    if (
      phase !== 'LIVE_PROVISIONAL'
      || !asOf
      || !sourceAsOf
      || strategyIds.length === 0
    ) return undefined

    const refreshKey = `${sourceAsOf}|${asOf}|${planStrategyKey}`
    const refreshPlan = () => {
      const now = Date.now()
      const lastRefresh = lastPlanRefreshRef.current
      if (
        planRefreshPending
        || (lastRefresh?.key === refreshKey && now - lastRefresh.at < PLAN_MONITOR_REFRESH_MS)
      ) return
      lastPlanRefreshRef.current = { key: refreshKey, at: now }
      refreshPlanStrategies({ asOf, strategyIds })
    }

    refreshPlan()
    const timer = window.setInterval(refreshPlan, PLAN_MONITOR_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [
    briefQuery.data?.data_phase?.as_of,
    briefQuery.data?.data_phase?.phase,
    briefQuery.data?.plan_source_as_of,
    planRefreshPending,
    planStrategyKey,
    refreshPlanStrategies,
  ])

  const candidates = useMemo(
    () => selectDailyBriefCandidates(briefQuery.data?.candidates ?? [])
      .map(presentDailyBriefCandidate),
    [briefQuery.data?.candidates],
  )
  const receipts = useMemo(
    () => briefQuery.data ? presentTrustDatasets(briefQuery.data.data_gate) : [],
    [briefQuery.data],
  )
  const snapshot = useMemo(
    () => presentResearchSnapshot(
      briefQuery.data?.snapshot_id ?? null,
      briefQuery.data?.snapshot_published_at ?? null,
    ),
    [briefQuery.data?.snapshot_id, briefQuery.data?.snapshot_published_at],
  )
  const planSourceAsOf = briefQuery.data?.plan_source_as_of ?? null
  const planMonitorAsOf = briefQuery.data?.data_phase?.as_of ?? null

  const refreshing = briefQuery.isFetching || accountQuery.isFetching
  const paperActionState = resolvePaperActionState(
    briefQuery.data?.action_state,
    Boolean(briefQuery.error),
  )
  async function refreshAll() {
    await Promise.all([briefQuery.refetch(), accountQuery.refetch()])
  }

  return (
    <>
      <PageHeader
        title="量化顾问"
        titleExtra={(
          <span className="rounded border border-border bg-elevated px-2 py-1 text-[10px] text-secondary">
            新手模式
          </span>
        )}
        className="flex-wrap px-4 sm:flex-nowrap sm:px-5"
        right={(
          <button
            type="button"
            onClick={refreshAll}
            disabled={refreshing}
            className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-btn border border-border bg-surface px-3 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
            刷新日报与账户
          </button>
        )}
      />

      <div className="min-w-0 space-y-5 overflow-hidden px-3 py-4 sm:px-5 sm:py-5 lg:px-8">
        {briefQuery.isLoading ? (
          <section className="rounded-card border border-border bg-surface p-8 text-center">
            <RefreshCw className="mx-auto h-5 w-5 animate-spin text-accent" />
            <p className="mt-3 text-sm text-secondary">正在读取今日行动和四项数据回执…</p>
          </section>
        ) : briefQuery.error ? (
          <section className="rounded-card border border-danger/25 bg-danger/5 p-5">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
              <div>
                <h2 className="text-sm font-semibold text-danger">今日行动读取失败</h2>
                <p className="mt-1 break-words text-xs leading-relaxed text-secondary">
                  原因：{briefQuery.error instanceof Error ? briefQuery.error.message : '未知错误'}
                </p>
                <p className="mt-1 text-xs leading-relaxed text-secondary">
                  下一步：检查后端是否运行，再点击“刷新日报与账户”。在恢复前，模拟成交保持禁用。
                </p>
              </div>
            </div>
          </section>
        ) : briefQuery.data ? (
          <>
            <ActionOverview
              actionState={briefQuery.data.action_state}
              todayMessage={briefQuery.data.today_message}
              nextStep={briefQuery.data.next_step}
              dataPassed={briefQuery.data.data_gate.decision === 'PASS'}
              dataPhase={
                briefQuery.data.data_phase?.phase
                ?? (briefQuery.data.snapshot_id ? 'EOD_SEALED' : 'UNAVAILABLE')
              }
            />

            <section className="min-w-0">
              <div className="mb-2.5 flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold text-foreground">
                    {planSourceAsOf ? '次日研究计划（最多 3 只）' : '研究候选（最多 3 只）'}
                  </h2>
                  <p className="mt-1 text-[11px] text-muted">
                    {planSourceAsOf
                      ? `来自 ${planSourceAsOf} 封存快照；盘中只复核原计划，不临时新增标的。`
                      : '这里只解释规则结果，不构成任何交易指令。'}
                  </p>
                  {planSourceAsOf && briefQuery.data.data_phase?.phase === 'LIVE_PROVISIONAL' && (
                    <p className={`mt-1 text-[11px] ${planRefreshFailed ? 'text-danger' : 'text-accent'}`}>
                      {planRefreshPending
                        ? '正在自动复核封存计划…'
                        : planRefreshFailed
                          ? '盘中自动复核失败；当前状态保持只观察，5 分钟后自动重试。'
                          : '盘中自动复核已启用：打开本页后立即复核，交易时段每 5 分钟更新。'}
                    </p>
                  )}
                </div>
                <span className="text-[10px] text-muted">
                  {planSourceAsOf
                    ? `计划来源 ${planSourceAsOf} · 监控日期 ${planMonitorAsOf || '未提供'}`
                    : '按后端确定性顺序展示'}
                </span>
              </div>
              <div className={`mb-3 rounded-card border px-3 py-2.5 text-[11px] leading-relaxed ${
                briefQuery.data.model_health.status === 'WARNING'
                  ? 'border-warning/35 bg-warning/5 text-warning'
                  : 'border-border bg-elevated/20 text-secondary'
              }`}>
                <span className="font-medium">
                  模型体检：{briefQuery.data.model_health.sample_days}/
                  {briefQuery.data.model_health.window_days} 个交易日
                </span>
                <span className="mx-1.5 text-border">|</span>
                <span>{briefQuery.data.model_health.message}</span>
                <span className="mx-1.5 text-border">|</span>
                <span>
                  本批淘汰：非主板 {briefQuery.data.excluded_counts.not_main_board}、
                  ST {briefQuery.data.excluded_counts.st_or_risk_warning}、
                  硬风险 {briefQuery.data.excluded_counts.hard_risk}、
                  超预算 {briefQuery.data.excluded_counts.over_practice_budget}
                </span>
              </div>
              {candidates.length === 0 ? (
                <div className="rounded-card border border-dashed border-border bg-surface px-4 py-8 text-center">
                  <Info className="mx-auto h-5 w-5 text-muted" />
                  <p className="mt-2 text-sm text-secondary">
                    {planSourceAsOf ? '该封存快照没有可带入今天的研究计划' : '今天没有研究候选'}
                  </p>
                  <p className="mt-1 text-[11px] text-muted">
                    按上方“下一步”处理，不需要勉强寻找标的。
                  </p>
                </div>
              ) : (
                <div className="grid min-w-0 gap-3 lg:grid-cols-3">
                  {candidates.map((candidate, index) => (
                    <CandidateCard key={candidate.symbol} candidate={candidate} index={index} />
                  ))}
                </div>
              )}
            </section>

            <section className="min-w-0">
              <div className="mb-2.5 flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
                    <ShieldCheck className="h-4 w-4 text-accent" />
                    数据可信度
                  </h2>
                  <p className="mt-1 text-[11px] text-muted">
                    逐项展示后端回执；整体结论以“今日行动”中的数据检查结果为准。
                  </p>
                </div>
                <div
                  className="rounded border border-border bg-elevated px-2.5 py-1.5 text-[10px] leading-relaxed text-secondary"
                  title={snapshot.fullId ?? undefined}
                >
                  <span className={snapshot.fullId ? 'text-bear' : 'text-warning'}>
                    研究快照：{snapshot.statusLabel}
                  </span>
                  <span className="mx-1.5 text-border">|</span>
                  <span className="font-mono">{snapshot.shortId}</span>
                  <span className="mx-1.5 text-border">|</span>
                  <span>发布时间：{snapshot.publishedAt}</span>
                </div>
              </div>
              <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-4">
                {receipts.map(receipt => (
                  <TrustReceiptCard key={receipt.key} receipt={receipt} />
                ))}
              </div>
              {briefQuery.data.data_gate.reasons.length > 0 && (
                <div className="mt-2 rounded-card border border-danger/25 bg-danger/[0.035] p-3 text-[11px] leading-relaxed">
                  <h3 className="font-medium text-danger">整体拦截原因</h3>
                  <ul className="mt-1.5 space-y-1 text-secondary">
                    {briefQuery.data.data_gate.reasons.map(reason => (
                      <li key={reason} className="break-words">· {reason}</li>
                    ))}
                  </ul>
                  {briefQuery.data.data_gate.next_actions.length > 0 && (
                    <>
                      <h3 className="mt-3 font-medium text-foreground">处理步骤</h3>
                      <ul className="mt-1.5 space-y-1 text-secondary">
                        {briefQuery.data.data_gate.next_actions.map(action => (
                          <li key={action} className="break-words">· {action}</li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}
            </section>
          </>
        ) : null}

        <PaperAccountSection
          account={accountQuery.data}
          accountLoading={accountQuery.isLoading}
          accountError={accountQuery.error}
          actionState={paperActionState}
          briefUnavailable={Boolean(briefQuery.error)}
        />

        <footer className="flex items-start gap-2 rounded-card border border-border/70 bg-elevated/10 px-4 py-3 text-[10px] leading-relaxed text-muted">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {briefQuery.data?.disclaimer || '仅供个人研究与模拟练习。'}
            本页不接券商、不自动下单，不提供实盘指令或收益承诺。
          </span>
        </footer>
      </div>
    </>
  )
}
