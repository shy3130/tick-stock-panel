/**
 * 交易工作台 —— YMOS 交易域前端入口。
 *
 * 五个 tab:
 *   持仓     组合快照指标卡 + fhold 真实券商持仓 + 生命周期交易列表 + 新建仓
 *   单笔详情  当前事实 + 事件时间线 + 事件录入(门禁预检/绕门二次确认) + AI 归因
 *   计划台    每日交易计划 CRUD(replace 全量覆盖, 删除生效) + 计划/执行偏差
 *   账户      资金账户编辑 + 追加资金变更(只追加, 不改历史)
 *   桥接规划  信号 → 交易软件的后续接入规划(占位保留)
 *
 * 后端语义 (api/trading*.py + services/trading/*):
 *   计划中允许 prepare/revise/fill; 持仓中允许 add/tp/sl/adjust/close; 已平仓只读。
 *   事件提交 payload={kind,payload,note?,gate?}; 门禁预检未过时展示红线清单,
 *   用户确认后带 gate:{confirmed:true} 重提(绕门留痕)。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot, Briefcase, Cable, CalendarDays, FileDown, FileText, GitBranch, Plus, RefreshCw, Save,
  ShieldAlert, ShieldCheck, Square, Trash2, Wallet, type LucideIcon,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { AiExecutionMetaBadge } from '@/components/AiExecutionMetaBadge'
import { DecisionTrace } from '@/components/analysis/DecisionTrace'
import { resolveEntryProfile } from '@/lib/aiProfile'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import {
  api,
  tradingAppendEvent,
  tradingCheckPlanStream,
  tradingEvaluateGates,
  tradingGetAccounts,
  tradingGetAutopsy,
  tradingGetPlan,
  tradingGetPlanCheck,
  tradingGetPlanDeviation,
  tradingGetPortfolio,
  tradingGetPlanCheckContinuity,
  tradingGetTrade,
  tradingListTrades,
  tradingListPlanChecks,
  tradingOpenTrade,
  tradingPlanCheckExportUrl,
  tradingPutAccounts,
  tradingPutPlan,
  tradingRunAutopsy,
  type AccountChange,
  type AutopsyResult,
  type GateEvaluation,
  type PlanAction,
  type PlanCheckArtifact,
  type PlanCheckContinuityChainNode,
  type PlanCheckContinuityMeta,
  type PlanEntry,
  type PortfolioSnapshot,
  type Trade,
  type TradeEvent,
  type TradeEventKind,
  type TradeStatus,
  type TradingAccount,
  type TradingAppendEventPayload,
} from '@/lib/api'

// ===== 样式(共享工作区 classes) =====

const INPUT = 'control w-full !h-8 text-xs'
const BTN_PRIMARY = 'btn-primary !h-8 text-xs'
const BTN_GHOST = 'btn-secondary !h-8 text-xs'
const BTN_DANGER =
  'inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-btn border border-transparent bg-danger px-3 text-xs font-medium text-white transition-colors hover:bg-danger/90 disabled:cursor-not-allowed disabled:opacity-50'

const TH = ''
const TD = ''
const TD_NUM = 'text-right font-mono tabular-nums'


// ===== 领域常量(与 backend services/trading 一致) =====

type AppendKind = TradingAppendEventPayload['kind']

const KIND_LABEL: Record<TradeEventKind, string> = {
  open: '建仓', prepare: '建仓准备', revise: '修订计划', fill: '确认成交',
  add: '加仓', tp: '止盈', sl: '止损', adjust: '调整规则', close: '平仓',
}

const KIND_BADGE: Record<TradeEventKind, string> = {
  open: 'bg-accent/10 text-accent',
  prepare: 'bg-muted/10 text-muted',
  revise: 'bg-muted/10 text-muted',
  fill: 'bg-accent/10 text-accent',
  add: 'bg-accent/10 text-accent',
  tp: 'bg-warning/10 text-warning',
  sl: 'bg-danger/10 text-danger',
  adjust: 'bg-warning/10 text-warning',
  close: 'bg-danger/10 text-danger',
}

/** 状态机允许录入的事件 (lifecycle.py: 已平仓终态拒绝一切写入) */
const ALLOWED_KINDS: Record<TradeStatus, AppendKind[]> = {
  计划中: ['prepare', 'revise', 'fill'],
  持仓中: ['add', 'tp', 'sl', 'adjust', 'close'],
  已平仓: [],
}

const STATUS_BADGE: Record<TradeStatus, string> = {
  计划中: 'bg-accent/10 text-accent',
  持仓中: 'bg-warning/10 text-warning',
  已平仓: 'bg-muted/10 text-muted',
}

const ACTION_LABEL: Record<PlanAction, string> = {
  buy_new: '买入开仓', add: '加仓', tp: '止盈', sl: '止损',
  close: '平仓', adjust: '调整', watch: '观察',
}

const HEALTH_META: Record<string, { label: string; badge: string }> = {
  normal: { label: '正常', badge: 'bg-accent/10 text-accent' },
  attention: { label: '关注', badge: 'bg-warning/10 text-warning' },
  critical: { label: '严重', badge: 'bg-danger/10 text-danger' },
}

const FLAG_LABEL: Record<string, string> = {
  stop_loss_widened: '放宽止损',
  loss_add: '亏损加仓',
  gate_bypassed: '绕过门禁',
  audit_missing: '审计断链',
}

/** M25 连续性判定模式 — 徽标映射。仅比较本地数据锚点，不含执行语义。 */
const CONTINUITY_MODE_META: Record<string, { label: string; badge: string }> = {
  fresh:           { label: '全新分析',   badge: 'border-border bg-elevated/40 text-muted' },
  incremental:     { label: '增量分析',   badge: 'border-success/30 bg-success/10 text-success' },
  full_reanalysis: { label: '全量重算',   badge: 'border-warning/30 bg-warning/10 text-warning' },
  unknown:         { label: '未知',       badge: 'border-border bg-elevated/40 text-muted' },
}

// ===== 工具函数 =====

function fmtMoney(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtQty(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN')
}

/** 解析正数输入; 空串/非法/<=0 → undefined */
function posNum(s: string): number | undefined {
  const n = Number(s)
  return s.trim() !== '' && Number.isFinite(n) && n > 0 ? n : undefined
}

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** 与 backend now_str 一致: %Y-%m-%d %H:%M */
function nowStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function todayCompact(): string {
  const d = new Date()
  return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}`
}

// ===== 主页面 =====

type TabKey = 'positions' | 'detail' | 'plan' | 'accounts' | 'bridge'

const TABS: { key: TabKey; label: string; icon: LucideIcon }[] = [
  { key: 'positions', label: '持仓', icon: Briefcase },
  { key: 'detail', label: '单笔详情', icon: FileText },
  { key: 'plan', label: '计划台', icon: CalendarDays },
  { key: 'accounts', label: '账户', icon: Wallet },
  { key: 'bridge', label: '桥接规划', icon: Cable },
]

export function Trading() {
  const [tab, setTab] = useState<TabKey>('positions')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const openDetail = (id: string) => {
    setSelectedId(id)
    setTab('detail')
  }

  return (
    <div className="workspace-page">
      <PageHeader title="交易" subtitle="生命周期 · 门禁 · 计划与账户" />

      {/* tab 栏 */}
      <div className="workspace-toolbar border-b border-border px-3 sm:px-4 !min-h-0 py-2">
        {TABS.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              'inline-flex shrink-0 items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer',
              tab === t.key ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
            )}
          >
            <t.icon className="h-3.5 w-3.5" />{t.label}
          </button>
        ))}
      </div>

      <div className="workspace-content overflow-auto">
        <div className="mx-auto w-full max-w-6xl min-w-0 space-y-3">
          {tab === 'positions' && <PositionsPanel onSelectTrade={openDetail} />}
          {tab === 'detail' && (
            selectedId
              ? <DetailPanel key={selectedId} tradeId={selectedId} />
              : (
                <EmptyState
                  icon={FileText}
                  title="未选择交易"
                  hint="在「持仓」页的交易列表中点击任意一笔, 查看当前事实、事件时间线并录入生命周期事件。"
                />
              )
          )}
          {tab === 'plan' && <PlanPanel onSelectTrade={openDetail} />}
          {tab === 'accounts' && <AccountsPanel />}
          {tab === 'bridge' && <BridgePanel />}
        </div>
      </div>
    </div>
  )
}

// ================================================================
// 共享小组件
// ================================================================

function SectionCard({ title, extra, children }: {
  title: string
  extra?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="panel overflow-hidden">
      <div className="panel-header">
        <h3 className="section-title">{title}</h3>
        {extra}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  )
}


/** 门禁预检未过: 红线清单 + 绕门二次确认 */
function GateFailPanel({ evaluation, pending, onConfirm, onCancel }: {
  evaluation: GateEvaluation
  pending: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const failed = evaluation.gates.filter(g => !g.passed)
  return (
    <div className="panel space-y-3 border-danger/40 bg-danger/5 p-3">
      <div className="flex items-center gap-2 text-sm font-medium text-danger">
        <ShieldAlert className="h-4 w-4" />门禁预检未通过, 动作未执行
      </div>
      <ul className="space-y-1.5">
        {failed.map(g => (
          <li key={g.id} className="text-xs">
            <span className="font-medium text-foreground">{g.name}</span>
            <span className="ml-2 text-secondary">{g.detail}</span>
          </li>
        ))}
        {failed.length === 0 && evaluation.missing.map(m => (
          <li key={m} className="text-xs text-secondary">未过红线: {m}</li>
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={onConfirm} disabled={pending} className={BTN_DANGER}>
          {pending ? '提交中…' : '我已知晓并确认执行'}
        </button>
        <button onClick={onCancel} disabled={pending} className={BTN_GHOST}>返回修改</button>
      </div>
      <p className="text-[10px] leading-relaxed text-muted">
        确认后将以 gate.confirmed 绕门执行, 决策审计与事件流均留痕(gateBypassed)。
      </p>
    </div>
  )
}

function InlineError({ msg }: { msg: string | null }) {
  if (!msg) return null
  return <p className="text-xs text-danger">{msg}</p>
}

// ================================================================
// Tab 1: 持仓
// ================================================================

function PositionsPanel({ onSelectTrade }: { onSelectTrade: (id: string) => void }) {
  const portfolioQuery = useQuery({ queryKey: ['trading-portfolio'], queryFn: tradingGetPortfolio })
  const tradesQuery = useQuery({ queryKey: ['trading-trades'], queryFn: () => tradingListTrades() })
  const [statusFilter, setStatusFilter] = useState<'全部' | TradeStatus>('全部')

  const pf = portfolioQuery.data
  const trades = useMemo(() => {
    const all = tradesQuery.data?.trades ?? []
    return statusFilter === '全部' ? all : all.filter(t => t.status === statusFilter)
  }, [tradesQuery.data, statusFilter])

  return (
    <div className="space-y-4">
      {/* 组合快照 */}
      {portfolioQuery.isLoading ? (
        <div className="panel px-5 py-8 text-center text-sm text-muted">加载组合快照中…</div>
      ) : pf ? (
        <PortfolioHeader pf={pf} />
      ) : (
        <div className="panel px-5 py-8 text-center text-sm text-muted">组合快照不可用。</div>
      )}

      {/* fhold 真实券商持仓 */}
      {pf && <FholdTable pf={pf} />}

      {/* 生命周期交易列表 */}
      <SectionCard
        title="生命周期交易"
        extra={(
          <div className="flex items-center gap-0.5">
            {(['全部', '计划中', '持仓中', '已平仓'] as const).map(f => (
              <button
                key={f}
                onClick={() => setStatusFilter(f)}
                className={cn(
                  'rounded-md px-1.5 py-0.5 text-[10px] font-medium transition-all cursor-pointer',
                  statusFilter === f ? 'bg-accent/15 text-accent' : 'text-muted hover:bg-elevated/60 hover:text-secondary',
                )}
              >
                {f}
              </button>
            ))}
          </div>
        )}
      >
        {tradesQuery.isLoading ? (
          <p className="py-6 text-center text-xs text-muted">加载中…</p>
        ) : trades.length === 0 ? (
          <p className="py-6 text-center text-xs text-muted">暂无交易记录, 从下方「新建仓」开始。</p>
        ) : (
          <div className="data-table-scroll">
            <table className="data-table min-w-[760px]">
              <thead>
                <tr>
                  <th className={TH}>tradeId</th>
                  <th className={TH}>标的</th>
                  <th className={TH}>状态</th>
                  <th className={cn(TH, 'text-right')}>股数</th>
                  <th className={cn(TH, 'text-right')}>成本</th>
                  <th className={cn(TH, 'text-right')}>已实现盈亏</th>
                  <th className={cn(TH, 'text-right')}>止损</th>
                </tr>
              </thead>
              <tbody>
                {trades.map(t => (
                  <tr
                    key={t.tradeId}
                    onClick={() => onSelectTrade(t.tradeId)}
                    className="cursor-pointer transition-colors hover:bg-elevated/40"
                  >
                    <td className={cn(TD, 'font-mono text-[10px] text-muted')} title={t.tradeId}>
                      {t.tradeId.slice(0, 8)}
                    </td>
                    <td className={TD}>
                      <span className="font-mono text-foreground">{t.symbol}</span>
                      <span className="ml-1.5 text-secondary">{t.name}</span>
                    </td>
                    <td className={TD}>
                      <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', STATUS_BADGE[t.status])}>
                        {t.status}
                      </span>
                    </td>
                    <td className={TD_NUM}>{fmtQty(t.position.qty)}</td>
                    <td className={TD_NUM}>{fmtPrice(t.position.costPrice)}</td>
                    <td className={cn(TD_NUM, priceColorClass(t.realizedPnl))}>{fmtMoney(t.realizedPnl)}</td>
                    <td className={TD_NUM}>{fmtPrice(t.stopLoss)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      {/* 新建仓 */}
      <NewTradeForm onOpened={onSelectTrade} />
    </div>
  )
}

/** 组合快照指标卡: NAV / 剩余可开 / 浮动盈亏 / 已实现盈亏 + health/stale */
function PortfolioHeader({ pf }: { pf: PortfolioSnapshot }) {
  const health = HEALTH_META[pf.health] ?? { label: pf.health, badge: 'bg-muted/10 text-muted' }
  const cards: { label: string; value: string; cls?: string }[] = [
    { label: 'NAV 净值', value: fmtMoney(pf.nav) },
    { label: '剩余可开', value: fmtMoney(pf.available) },
    { label: '浮动盈亏', value: fmtMoney(pf.unrealizedPnl), cls: priceColorClass(pf.unrealizedPnl) },
    { label: '已实现盈亏', value: fmtMoney(pf.realizedPnl), cls: priceColorClass(pf.realizedPnl) },
  ]
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-foreground">组合快照</h3>
        <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', health.badge)}>
          健康度 {health.label}
        </span>
        {pf.stale && (
          <span className="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning">
            行情数据过期
          </span>
        )}
        <span className="ml-auto text-[10px] text-muted">
          价格源 {pf.priceSource} · 单标的上线 {Math.round(pf.maxSingleRatio * 100)}%
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {cards.map(c => (
          <div key={c.label} className="panel p-3">
            <div className="text-xs text-muted">{c.label}</div>
            <div className={cn('metric-value mt-1 text-base', c.cls)}>
              {c.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/** fhold 真实券商持仓表; available=false 显示降级提示 */
function FholdTable({ pf }: { pf: PortfolioSnapshot }) {
  const accountName = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of pf.fhold.accounts) m.set(a.id, a.name)
    return m
  }, [pf.fhold.accounts])

  return (
    <SectionCard title="券商持仓 (fhold)">
      {!pf.fhold.available ? (
        <div className="panel border-warning/40 bg-warning/5 px-4 py-3 text-xs text-warning">
          fhold 券商持仓暂不可用(未接入或读取失败), 当前仅展示生命周期记录与账户快照。
        </div>
      ) : pf.fhold.positions.length === 0 ? (
        <p className="py-4 text-center text-xs text-muted">券商账户当前无持仓。</p>
      ) : (
        <div className="data-table-scroll">
          <table className="data-table min-w-[820px]">
            <thead>
              <tr>
                <th className={TH}>账户</th>
                <th className={TH}>标的</th>
                <th className={cn(TH, 'text-right')}>数量</th>
                <th className={cn(TH, 'text-right')}>成本</th>
                <th className={cn(TH, 'text-right')}>现价</th>
                <th className={cn(TH, 'text-right')}>市值</th>
                <th className={cn(TH, 'text-right')}>持仓盈亏</th>
              </tr>
            </thead>
            <tbody>
              {pf.fhold.positions.map(p => (
                <tr key={`${p.accountId}-${p.code}`}>
                  <td className={cn(TD, 'text-secondary')}>{accountName.get(p.accountId) ?? p.accountId}</td>
                  <td className={TD}>
                    <span className="font-mono text-foreground">{p.code}</span>
                    <span className="ml-1.5 text-secondary">{p.name}</span>
                  </td>
                  <td className={TD_NUM}>{fmtQty(p.qty)}</td>
                  <td className={TD_NUM}>{fmtPrice(p.costPrice)}</td>
                  <td className={TD_NUM}>{fmtPrice(p.currentPrice)}</td>
                  <td className={TD_NUM}>{fmtMoney(p.marketValue)}</td>
                  <td className={cn(TD_NUM, priceColorClass(p.holdingPnl))}>
                    {fmtMoney(p.holdingPnl)}
                    <span className="ml-1 text-[10px]">({fmtPct(p.holdingPnlRatio)})</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SectionCard>
  )
}

/** 新建仓表单(open 建档): 提交前走 buy_new 门禁预检 */
function NewTradeForm({ onOpened }: { onOpened: (id: string) => void }) {
  const qc = useQueryClient()
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [thesisText, setThesisText] = useState('')
  const [thesisInv, setThesisInv] = useState('')
  const [stopLoss, setStopLoss] = useState('')
  const [gateFail, setGateFail] = useState<GateEvaluation | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const evalMut = useMutation({ mutationFn: tradingEvaluateGates })
  const openMut = useMutation({
    mutationFn: tradingOpenTrade,
    onSuccess: (trade) => {
      qc.invalidateQueries({ queryKey: ['trading-trades'] })
      qc.invalidateQueries({ queryKey: ['trading-portfolio'] })
      toast(`已建仓 ${trade.name}(${trade.symbol})`, 'success')
      setSymbol(''); setName(''); setThesisText(''); setThesisInv(''); setStopLoss('')
      setGateFail(null); setErr(null)
      onOpened(trade.tradeId)
    },
  })

  const canSubmit =
    symbol.trim() !== '' && name.trim() !== '' &&
    thesisText.trim() !== '' && thesisInv.trim() !== ''

  const doOpen = (bypass: boolean) => {
    const stop = posNum(stopLoss)
    openMut.mutate({
      symbol: symbol.trim(),
      name: name.trim(),
      thesis: { text: thesisText.trim(), invalidation: thesisInv.trim() },
      stopLoss: stop ?? null,
      ...(bypass ? { gate: { confirmed: true } } : {}),
    })
  }

  const handleSubmit = async () => {
    setErr(null)
    if (!canSubmit) { setErr('标的代码、名称、买入论点与失效信号均为必填。'); return }
    const stop = posNum(stopLoss)
    try {
      const ev = await evalMut.mutateAsync({ mode: 'buy_new', payload: stop ? { stopLoss: stop } : {} })
      if (!ev.passed) { setGateFail(ev); return }
    } catch {
      return // request() 已 toast
    }
    doOpen(false)
  }

  const busy = evalMut.isPending || openMut.isPending

  return (
    <SectionCard title="新建仓" extra={<span className="text-[10px] text-muted">open 建档 → 计划中</span>}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">标的代码 *</span>
            <input value={symbol} onChange={e => setSymbol(e.target.value)} placeholder="600519" className={cn(INPUT, 'w-full font-mono')} />
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">名称 *</span>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="贵州茅台" className={cn(INPUT, 'w-full')} />
          </label>
          <label className="block md:col-span-2">
            <span className="mb-1 block text-[10px] text-muted">止损价(可选)</span>
            <input value={stopLoss} onChange={e => setStopLoss(e.target.value)} placeholder="未过门禁时止损必填" inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
          </label>
        </div>
        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">买入论点 thesis.text *</span>
          <textarea value={thesisText} onChange={e => setThesisText(e.target.value)} rows={2}
            placeholder="为什么买: 逻辑、催化、预期持有期"
            className={cn(INPUT, 'w-full resize-y')} />
        </label>
        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">失效信号 thesis.invalidation *</span>
          <textarea value={thesisInv} onChange={e => setThesisInv(e.target.value)} rows={2}
            placeholder="可观察的反证条件: 出现什么就必须退出"
            className={cn(INPUT, 'w-full resize-y')} />
        </label>

        <InlineError msg={err} />
        {gateFail && (
          <GateFailPanel
            evaluation={gateFail}
            pending={openMut.isPending}
            onConfirm={() => doOpen(true)}
            onCancel={() => setGateFail(null)}
          />
        )}

        <div className="flex justify-end">
          <button onClick={handleSubmit} disabled={!canSubmit || busy} className={BTN_PRIMARY}>
            <Plus className="h-3.5 w-3.5" />{busy ? '提交中…' : '建仓'}
          </button>
        </div>
      </div>
    </SectionCard>
  )
}

// ================================================================
// Tab 2: 单笔详情
// ================================================================

function DetailPanel({ tradeId }: { tradeId: string }) {
  const detailQuery = useQuery({
    queryKey: ['trading-trade', tradeId],
    queryFn: () => tradingGetTrade(tradeId),
  })

  if (detailQuery.isLoading) {
    return <div className="panel px-5 py-10 text-center text-sm text-muted">加载交易详情中…</div>
  }
  const trade = detailQuery.data?.trade
  if (!trade) {
    return <EmptyState icon={FileText} title="交易不存在" hint={`未找到 tradeId=${tradeId} 的记录, 可能已被清理。`} />
  }
  const events = detailQuery.data?.events ?? []

  return (
    <div className="space-y-4">
      <TradeFactsCard trade={trade} />
      <TimelineCard events={events} />
      <EventForm trade={trade} />
      <AutopsySection tradeId={tradeId} />
    </div>
  )
}

/** 当前事实卡 */
function TradeFactsCard({ trade }: { trade: Trade }) {
  const items: { label: string; value: React.ReactNode }[] = [
    { label: '状态', value: <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', STATUS_BADGE[trade.status])}>{trade.status}</span> },
    { label: '策略', value: trade.strategy ?? '—' },
    { label: '止损', value: <span className="font-mono">{fmtPrice(trade.stopLoss)}</span> },
    { label: '退出规则', value: trade.exitRule ?? '—' },
    { label: '持仓股数', value: <span className="font-mono">{fmtQty(trade.position.qty)}</span> },
    { label: '成本价', value: <span className="font-mono">{fmtPrice(trade.position.costPrice)}</span> },
    { label: '投入金额', value: <span className="font-mono">{fmtMoney(trade.position.invested)}</span> },
    { label: '已实现盈亏', value: <span className={cn('font-mono', priceColorClass(trade.realizedPnl))}>{fmtMoney(trade.realizedPnl)}</span> },
    { label: '建仓时间', value: trade.createdAt },
    { label: '平仓时间', value: trade.closedAt ?? '—' },
  ]
  if (trade.plan) {
    items.push({
      label: '建仓计划',
      value: (
        <span className="font-mono">
          {trade.plan.qty != null ? fmtQty(trade.plan.qty) : '—'} 股 @ {fmtPrice(trade.plan.price)}
        </span>
      ),
    })
  }
  return (
    <SectionCard
      title={`${trade.name} ${trade.symbol}`}
      extra={<span className="font-mono text-[10px] text-muted">{trade.tradeId}</span>}
    >
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 md:grid-cols-3 lg:grid-cols-5">
        {items.map(it => (
          <div key={it.label}>
            <div className="text-[10px] text-muted">{it.label}</div>
            <div className="mt-0.5 text-xs text-foreground">{it.value}</div>
          </div>
        ))}
      </div>
      <div className="mt-3 grid gap-3 border-t border-border/50 pt-3 md:grid-cols-2">
        <div>
          <div className="text-[10px] text-muted">买入论点</div>
          <p className="mt-0.5 text-xs leading-relaxed text-secondary">{trade.thesis.text}</p>
        </div>
        <div>
          <div className="text-[10px] text-muted">失效信号</div>
          <p className="mt-0.5 text-xs leading-relaxed text-secondary">{trade.thesis.invalidation}</p>
        </div>
      </div>
    </SectionCard>
  )
}

/** 事件时间线(append-only) */
function TimelineCard({ events }: { events: TradeEvent[] }) {
  return (
    <SectionCard title="事件时间线" extra={<span className="text-[10px] text-muted">{events.length} 条</span>}>
      {events.length === 0 ? (
        <p className="py-4 text-center text-xs text-muted">暂无事件。</p>
      ) : (
        <ul className="space-y-2.5">
          {events.map((e, i) => {
            // 事件 payload 摘要: k=v · k=v
            const summary = Object.entries(e.payload)
              .map(([k, v]) => `${k}=${v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
              .join(' · ')
            return (
            <li key={`${e.ts}-${i}`} className="flex gap-3">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', KIND_BADGE[e.kind])}>
                    {KIND_LABEL[e.kind]}
                  </span>
                  <span className="font-mono text-[10px] text-muted">{e.ts}</span>
                  {e.gateBypassed && (
                    <span className="rounded bg-danger/10 px-1.5 py-0.5 text-[10px] font-medium text-danger">
                      绕门执行
                    </span>
                  )}
                </div>
                {Object.keys(e.payload).length > 0 && (
                  <p className="mt-1 break-all font-mono text-[10px] leading-relaxed text-secondary">
                    {summary}
                  </p>
                )}
                {e.note && <p className="mt-0.5 text-xs text-secondary">{e.note}</p>}
              </div>
            </li>
            )
          })}
        </ul>
      )}
    </SectionCard>
  )
}

/** 事件录入: 按状态限定 kind, 字段随 kind 动态切换, 提交前门禁预检 */
function EventForm({ trade }: { trade: Trade }) {
  const qc = useQueryClient()
  const allowed = ALLOWED_KINDS[trade.status]
  const [kind, setKind] = useState<AppendKind | null>(allowed[0] ?? null)
  const [qty, setQty] = useState('')
  const [price, setPrice] = useState('')
  const [plannedQty, setPlannedQty] = useState('')
  const [plannedPrice, setPlannedPrice] = useState('')
  const [stopLoss, setStopLoss] = useState('')
  const [newStopLoss, setNewStopLoss] = useState('')
  const [newExitRule, setNewExitRule] = useState('')
  const [planOnly, setPlanOnly] = useState(false)
  const [note, setNote] = useState('')
  const [gateFail, setGateFail] = useState<GateEvaluation | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // 状态迁移后(如 fill 成功 → 持仓中)重置为当前状态允许的第一个 kind
  useEffect(() => {
    setKind(ALLOWED_KINDS[trade.status][0] ?? null)
    setGateFail(null)
    setErr(null)
  }, [trade.status])

  const resetFields = () => {
    setQty(''); setPrice(''); setPlannedQty(''); setPlannedPrice('')
    setStopLoss(''); setNewStopLoss(''); setNewExitRule('')
    setPlanOnly(false); setNote('')
  }

  const evalMut = useMutation({ mutationFn: tradingEvaluateGates })
  const appendMut = useMutation({
    mutationFn: (p: TradingAppendEventPayload) => tradingAppendEvent(trade.tradeId, p),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading-trade', trade.tradeId] })
      qc.invalidateQueries({ queryKey: ['trading-trades'] })
      qc.invalidateQueries({ queryKey: ['trading-portfolio'] })
      qc.invalidateQueries({ queryKey: ['trading-plan-deviation'] })
      toast('事件已录入', 'success')
      resetFields()
      setGateFail(null)
      setErr(null)
    },
  })

  if (allowed.length === 0) {
    return (
      <SectionCard title="事件录入">
        <p className="py-3 text-center text-xs text-muted">该笔交易已平仓归档, 拒绝任何后续写入。</p>
      </SectionCard>
    )
  }

  const buildPayload = (): Record<string, unknown> => {
    const p: Record<string, unknown> = {}
    if (kind === 'prepare' || kind === 'revise') {
      const q = posNum(plannedQty); const pr = posNum(plannedPrice); const s = posNum(stopLoss)
      if (q) p.plannedQty = q
      if (pr) p.plannedPrice = pr
      if (s) p.stopLoss = s
    } else if (kind === 'adjust') {
      const s = posNum(newStopLoss)
      if (s) p.newStopLoss = s
      if (newExitRule.trim()) p.newExitRule = newExitRule.trim()
    } else {
      const q = posNum(qty); const pr = posNum(price)
      if (q) p.qty = q
      if (pr) p.price = pr
      if (kind === 'add' && planOnly) p.planOnly = true
    }
    return p
  }

  const validate = (p: Record<string, unknown>): string | null => {
    if (kind === 'prepare' || kind === 'revise') {
      if (p.plannedQty == null && p.plannedPrice == null && p.stopLoss == null) {
        return '至少填写 计划股数 / 计划价格 / 止损 中的一项。'
      }
    } else if (kind === 'adjust') {
      if (p.newStopLoss == null && p.newExitRule == null) return 'adjust 必须提供 newStopLoss 或 newExitRule。'
    } else if (kind === 'close') {
      if (p.price == null) return '请填写平仓价格。'
    } else {
      if (p.qty == null || p.price == null) return '请填写数量与价格(正数)。'
    }
    return null
  }

  const doAppend = (bypass: boolean) => {
    if (!kind) return
    appendMut.mutate({
      kind,
      payload: buildPayload(),
      note: note.trim() || undefined,
      ...(bypass ? { gate: { confirmed: true } } : {}),
    })
  }

  const handleSubmit = async () => {
    if (!kind) return
    setErr(null)
    const payload = buildPayload()
    const v = validate(payload)
    if (v) { setErr(v); return }
    try {
      const ev = await evalMut.mutateAsync({
        mode: kind === 'fill' ? 'buy_new' : kind,
        tradeId: trade.tradeId,
        payload,
      })
      if (!ev.passed) { setGateFail(ev); return }
    } catch {
      return // request() 已 toast
    }
    doAppend(false)
  }

  const busy = evalMut.isPending || appendMut.isPending
  const showQtyPrice = kind === 'fill' || kind === 'add' || kind === 'tp' || kind === 'sl'
  const showPlanLeg = kind === 'prepare' || kind === 'revise'

  return (
    <SectionCard title="事件录入" extra={<span className="text-[10px] text-muted">{trade.status}可录入 {allowed.length} 类事件</span>}>
      <div className="space-y-3">
        {/* kind 选择(只显示当前状态允许的) */}
        <div className="flex flex-wrap items-center gap-1">
          {allowed.map(k => (
            <button
              key={k}
              onClick={() => { setKind(k); setGateFail(null); setErr(null) }}
              className={cn(
                'rounded-btn px-2.5 py-1 text-xs font-medium transition-colors cursor-pointer',
                kind === k ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
              )}
            >
              {KIND_LABEL[k]}
            </button>
          ))}
        </div>

        {/* 动态字段 */}
        {showPlanLeg && (
          <div className="grid grid-cols-3 gap-3">
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">计划股数 plannedQty</span>
              <input value={plannedQty} onChange={e => setPlannedQty(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">计划价格 plannedPrice</span>
              <input value={plannedPrice} onChange={e => setPlannedPrice(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">止损 stopLoss</span>
              <input value={stopLoss} onChange={e => setStopLoss(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
          </div>
        )}
        {showQtyPrice && (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">数量 qty *</span>
              <input value={qty} onChange={e => setQty(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">价格 price *</span>
              <input value={price} onChange={e => setPrice(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            {kind === 'add' && (
              <label className="col-span-2 flex items-end gap-2 pb-1.5">
                <input
                  type="checkbox"
                  checked={planOnly}
                  onChange={e => setPlanOnly(e.target.checked)}
                  className="h-3.5 w-3.5 accent-[hsl(var(--accent))]"
                />
                <span className="text-xs text-secondary">仅记录加仓计划(planOnly, 不改变仓位事实)</span>
              </label>
            )}
          </div>
        )}
        {kind === 'adjust' && (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">新止损 newStopLoss</span>
              <input value={newStopLoss} onChange={e => setNewStopLoss(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">新退出规则 newExitRule</span>
              <input value={newExitRule} onChange={e => setNewExitRule(e.target.value)} placeholder="如: 跌破20日线退出" className={cn(INPUT, 'w-full')} />
            </label>
          </div>
        )}
        {kind === 'close' && (
          <label className="block max-w-48">
            <span className="mb-1 block text-[10px] text-muted">平仓价格 price *</span>
            <input value={price} onChange={e => setPrice(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
          </label>
        )}

        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">备注 note(可选)</span>
          <input value={note} onChange={e => setNote(e.target.value)} placeholder="本次动作的理由" className={cn(INPUT, 'w-full')} />
        </label>

        <InlineError msg={err} />
        {gateFail && (
          <GateFailPanel
            evaluation={gateFail}
            pending={appendMut.isPending}
            onConfirm={() => doAppend(true)}
            onCancel={() => setGateFail(null)}
          />
        )}

        <div className="flex justify-end">
          <button onClick={handleSubmit} disabled={!kind || busy} className={BTN_PRIMARY}>
            {busy ? '提交中…' : `录入「${kind ? KIND_LABEL[kind] : ''}」`}
          </button>
        </div>
      </div>
    </SectionCard>
  )
}

/** AI 归因: 先读已有, 404/无数据再运行, 容错渲染 */
function AutopsySection({ tradeId }: { tradeId: string }) {
  const qc = useQueryClient()
  const [requested, setRequested] = useState(false)
  // P3: trading_autopsy 入口 profile 选择(ai_meta 由响应携带,optional,旧落盘记录兼容)
  const [profileId, setProfileId] = useState<string>()
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  const autopsyQuery = useQuery({
    queryKey: ['trading-autopsy', tradeId],
    queryFn: () => tradingGetAutopsy(tradeId),
    enabled: requested,
  })
  const runMut = useMutation({
    mutationFn: () => tradingRunAutopsy(
      tradeId,
      resolveEntryProfile('trading_autopsy', aiProfiles.data?.profiles ?? [], aiProfiles.data?.default_id ?? '') || profileId || undefined,
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading-autopsy', tradeId] })
      toast('AI 归因完成', 'success')
    },
  })

  const data: AutopsyResult | null | undefined = requested ? autopsyQuery.data : undefined

  return (
    <SectionCard
      title="AI 归因"
      extra={(
        <div className="flex items-center gap-1.5">
          <AiProviderSelector entry="trading_autopsy" value={profileId} onChange={setProfileId} compact />
          {requested && data === null && (
            <button
              onClick={() => runMut.mutate()}
              disabled={runMut.isPending}
              className={cn(BTN_PRIMARY, 'px-2.5 py-1 text-[11px]')}
            >
              <RefreshCw className={cn('h-3 w-3', runMut.isPending && 'animate-spin')} />
              {runMut.isPending ? '归因中…' : '运行归因'}
            </button>
          )}
          {!requested && (
            <button onClick={() => setRequested(true)} className={cn(BTN_GHOST, 'px-2.5 py-1 text-[11px]')}>
              <Bot className="h-3 w-3" />AI 归因
            </button>
          )}
        </div>
      )}
    >
      {!requested ? (
        <p className="py-3 text-center text-xs text-muted">基于事件流与红旗规则, 归因这笔交易的问题类别(A 策略不利 / B 执行偏离 / C 规则歧义 / D 数据问题)。</p>
      ) : autopsyQuery.isLoading ? (
        <p className="py-4 text-center text-xs text-muted">读取归因结果中…</p>
      ) : data == null ? (
        <p className="py-4 text-center text-xs text-muted">尚未生成归因, 点击右上角「运行归因」。</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
              {data.classification || '未分类'}
            </span>
            <span className="font-mono text-[10px] text-muted">{data.ts}</span>
            <AiExecutionMetaBadge meta={data.ai_meta} className="ml-auto" />
          </div>
          {data.reasoning && (
            <div>
              <div className="text-[10px] text-muted">归因理由</div>
              <p className="mt-0.5 whitespace-pre-wrap text-xs leading-relaxed text-secondary">{data.reasoning}</p>
            </div>
          )}
          {data.fix && (
            <div>
              <div className="text-[10px] text-muted">修复建议</div>
              <p className="mt-0.5 whitespace-pre-wrap text-xs leading-relaxed text-secondary">{data.fix}</p>
            </div>
          )}
          {data.redFlags.length > 0 && (
            <div>
              <div className="text-[10px] text-muted">机械红旗</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {data.redFlags.map((f, i) => (
                  <span
                    key={`${f.type}-${f.ts}-${i}`}
                    title={f.ts}
                    className="rounded bg-danger/10 px-1.5 py-0.5 text-[10px] font-medium text-danger"
                  >
                    {FLAG_LABEL[f.type] ?? f.type}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </SectionCard>
  )
}

// ================================================================
// Tab 3: 计划台
// ================================================================

function PlanPanel({ onSelectTrade }: { onSelectTrade: (id: string) => void }) {
  const qc = useQueryClient()
  const [date, setDate] = useState(todayCompact)
  const planQuery = useQuery({ queryKey: ['trading-plan', date], queryFn: () => tradingGetPlan(date) })
  const preferencesQuery = useQuery({ queryKey: ['preferences'], queryFn: api.preferences })
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  const featureEnabled = preferencesQuery.data?.structured_plan_check_enabled === true
  const [profileId, setProfileId] = useState<string>()
  const [checkingEntryId, setCheckingEntryId] = useState<string | null>(null)
  const [checkProgress, setCheckProgress] = useState('')
  const [checkError, setCheckError] = useState('')
  const [checkArtifact, setCheckArtifact] = useState<PlanCheckArtifact | null>(null)
  // M25: 连续性分析 opt-in, 默认关闭; 仅比较本地数据锚点, 不含执行语义。
  const [continuity, setContinuity] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const checksQuery = useQuery({
    queryKey: ['trading-plan-checks'],
    queryFn: () => tradingListPlanChecks(undefined, 20),
    enabled: featureEnabled,
    retry: false,
  })
  const toggleFeature = useMutation({
    mutationFn: (enabled: boolean) => api.updateStructuredPlanCheck(enabled),
    onSuccess: data => {
      qc.setQueryData(['preferences'], {
        ...(preferencesQuery.data ?? {}),
        structured_plan_check_enabled: data.structured_plan_check_enabled,
      })
      if (!data.structured_plan_check_enabled) {
        abortRef.current?.abort()
        setCheckArtifact(null)
        setContinuity(false)
      }
    },
  })
  const deviationQuery = useQuery({
    queryKey: ['trading-plan-deviation', date],
    queryFn: () => tradingGetPlanDeviation(date),
  })

  // 本地草稿: null = 未编辑, 直接展示查询结果; 编辑后覆盖, 保存/切换日期后归位
  const [entries, setEntries] = useState<PlanEntry[] | null>(null)
  const [notes, setNotes] = useState<string | null>(null)
  useEffect(() => {
    abortRef.current?.abort()
    setEntries(null)
    setNotes(null)
    setCheckArtifact(null)
    setCheckError('')
  }, [date])
  useEffect(() => () => abortRef.current?.abort(), [])

  const draftEntries = entries ?? planQuery.data?.entries ?? []
  const draftNotes = notes ?? planQuery.data?.actualNotes ?? ''
  const dirty = entries !== null || notes !== null

  const saveMut = useMutation({
    mutationFn: () => {
      const cleaned: PlanEntry[] = draftEntries
        .filter(e => e.symbol.trim() !== '')
        .map(e => ({
          ...e,
          symbol: e.symbol.trim(),
          trigger: e.trigger.trim(),
          reason: e.reason.trim(),
          strategyId: e.strategyId?.trim() || null,
          exitRule: e.exitRule?.trim() || '',
          invalidation: e.invalidation?.trim() || '',
          qty: e.qty != null && e.qty > 0 ? e.qty : null,
          plannedPrice: e.plannedPrice != null && e.plannedPrice > 0 ? e.plannedPrice : null,
          stopLoss: e.stopLoss != null && e.stopLoss > 0 ? e.stopLoss : null,
          thesisHorizonMonths: e.thesisHorizonMonths != null && e.thesisHorizonMonths > 0
            ? Math.trunc(e.thesisHorizonMonths)
            : null,
        }))
      // replace:true → 后端全量覆盖, 删除的条目生效
      const body = { schemaVersion: 1, date, entries: cleaned, actualNotes: draftNotes, replace: true }
      return tradingPutPlan(date, body)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading-plan', date] })
      qc.invalidateQueries({ queryKey: ['trading-plan-deviation', date] })
      toast('计划已保存', 'success')
      setEntries(null)
      setNotes(null)
    },
  })

  const updateEntry = (i: number, patch: Partial<PlanEntry>) => {
    setEntries(draftEntries.map((e, idx) => (idx === i ? { ...e, ...patch } : e)))
  }
  const addEntry = () => {
    // id: crypto.randomUUID(), 老环境退回时间戳+随机串
    const id = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `e-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    setEntries([
      ...draftEntries,
      {
        id, symbol: '', tradeId: null, action: 'buy_new', trigger: '', qty: null,
        reason: '', createdAt: nowStr(), strategyId: null, plannedPrice: null,
        stopLoss: null, exitRule: '', thesisHorizonMonths: null, invalidation: '',
      },
    ])
  }
  const removeEntry = (i: number) => {
    setEntries(draftEntries.filter((_, idx) => idx !== i))
  }

  const runPlanCheck = async (entry: PlanEntry) => {
    if (dirty) {
      toast('请先保存计划，再运行结构化检查')
      return
    }
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setCheckingEntryId(entry.id)
    setCheckProgress('正在准备 canonical K 线与程序门禁…')
    setCheckError('')
    setCheckArtifact(null)
    const selectedProfile = profileId
      || resolveEntryProfile('trading_plan_check', aiProfiles.data?.profiles ?? [], aiProfiles.data?.default_id ?? '')
      || undefined
    try {
      for await (const event of tradingCheckPlanStream(date, entry.id, selectedProfile, controller.signal, continuity)) {
        if (event.type === 'progress') {
          const stage = typeof event.stage === 'string' ? event.stage : ''
          setCheckProgress(stage === 'stage2' ? '正在检查已保存计划…' : stage === 'stage1' ? '正在诊断 K 线事实…' : '正在执行程序检查…')
        } else if (event.type === 'result') {
          setCheckArtifact(event)
          setCheckProgress('')
        } else if (event.type === 'error') {
          throw new Error(event.message)
        }
      }
      qc.invalidateQueries({ queryKey: ['trading-plan-checks'] })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        setCheckProgress('')
        toast('计划检查已取消')
      } else {
        const message = error instanceof Error ? error.message : '计划检查失败'
        setCheckError(message)
        setCheckProgress('')
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setCheckingEntryId(null)
    }
  }

  const loadPlanCheck = async (attemptId: string) => {
    setCheckError('')
    try {
      setCheckArtifact(await tradingGetPlanCheck(attemptId))
    } catch (error) {
      setCheckError(error instanceof Error ? error.message : '读取检查记录失败')
    }
  }

  const deviation = deviationQuery.data

  return (
    <div className="space-y-4">
      <SectionCard
        title="交易计划"
        extra={(
          <div className="flex items-center gap-2">
            <input
              type="date"
              value={`${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`}
              onChange={e => {
                // yyyy-mm-dd → yyyymmdd
                const c = e.target.value.replaceAll('-', '')
                if (/^\d{8}$/.test(c)) setDate(c)
              }}
              className={cn(INPUT, 'font-mono')}
            />
            {dirty && <span className="text-[10px] text-warning">未保存</span>}
            <button onClick={() => saveMut.mutate()} disabled={saveMut.isPending || !dirty} className={BTN_PRIMARY}>
              <Save className="h-3.5 w-3.5" />{saveMut.isPending ? '保存中…' : '保存计划'}
            </button>
          </div>
        )}
      >
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-elevated/30 px-3 py-2.5">
          <div>
            <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
              <ShieldCheck className="h-3.5 w-3.5 text-accent" />
              结构化计划检查
            </div>
            <p className="mt-1 max-w-2xl text-[10px] leading-relaxed text-muted">
              仅检查已保存计划的输入完整性、K 线事实与程序门禁。不会生成订单或买卖信号；“输入完整”也不代表建议交易。
            </p>
          </div>
          <div className="flex items-center gap-2">
            {featureEnabled && <AiProviderSelector entry="trading_plan_check" value={profileId} onChange={setProfileId} compact />}
            <button
              onClick={() => toggleFeature.mutate(!featureEnabled)}
              disabled={toggleFeature.isPending || preferencesQuery.isLoading}
              className={cn(
                BTN_GHOST,
                featureEnabled && 'border-accent/30 bg-accent/10 text-accent',
              )}
            >
              {featureEnabled ? '已启用' : '启用检查'}
            </button>
          </div>
        </div>
        {featureEnabled && (
          <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-border/60 bg-elevated/20 px-3 py-2">
            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={continuity}
                onChange={e => setContinuity(e.target.checked)}
                className="h-3.5 w-3.5 cursor-pointer rounded border-border accent-accent"
              />
              <GitBranch className="h-3 w-3 text-muted" />
              <span className="text-[11px] font-medium text-foreground">连续性分析</span>
            </label>
            <span className="text-[9px] leading-relaxed text-muted">
              仅比较本地数据锚点（数据截止日 / 策略配置 / 市场 / 复权），不含执行语义；锚点失配或跨度过大时自动回到全量分析，不生成交易建议。
            </span>
          </div>
        )}
        {planQuery.isLoading ? (
          <p className="py-6 text-center text-xs text-muted">加载计划中…</p>
        ) : (
          <div className="space-y-3">
            <div className="data-table-scroll">
              <div className="min-w-[860px] space-y-2">
                {/* 表头 */}
                <div className="grid grid-cols-[7rem_7rem_1fr_6rem_1fr_2rem] items-center gap-2 px-1">
                  {['标的', '动作', '触发条件', '数量', '理由', ''].map(h => (
                    <span key={h} className="text-[10px] font-medium uppercase tracking-wider text-muted">{h}</span>
                  ))}
                </div>
                {draftEntries.map((e, i) => (
                  <div key={e.id} className="space-y-2 rounded-lg border border-border/70 bg-base/40 p-2.5">
                    <div className="grid grid-cols-[7rem_7rem_1fr_6rem_1fr_2rem] items-center gap-2">
                      <input
                        value={e.symbol}
                        onChange={ev => updateEntry(i, { symbol: ev.target.value })}
                        placeholder="600519.SH"
                        className={cn(INPUT, 'font-mono')}
                      />
                      <select
                        value={e.action}
                        onChange={ev => updateEntry(i, { action: ev.target.value as PlanAction })}
                        className={cn(INPUT, 'cursor-pointer')}
                      >
                        {(Object.keys(ACTION_LABEL) as PlanAction[]).map(action => (
                          <option key={action} value={action}>{ACTION_LABEL[action]}</option>
                        ))}
                      </select>
                      <input
                        value={e.trigger}
                        onChange={ev => updateEntry(i, { trigger: ev.target.value })}
                        placeholder="如：放量突破后收盘确认"
                        className={INPUT}
                      />
                      <input
                        value={e.qty == null ? '' : String(e.qty)}
                        onChange={ev => updateEntry(i, { qty: posNum(ev.target.value) ?? null })}
                        placeholder="数量"
                        inputMode="decimal"
                        className={cn(INPUT, 'font-mono')}
                      />
                      <input
                        value={e.reason}
                        onChange={ev => updateEntry(i, { reason: ev.target.value })}
                        placeholder="计划依据"
                        className={INPUT}
                      />
                      <button
                        onClick={() => removeEntry(i)}
                        title="删除条目"
                        className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-lg text-muted transition-colors hover:bg-danger/10 hover:text-danger"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-6">
                      <label className="space-y-1">
                        <span className="text-[9px] text-muted">策略声明 ID</span>
                        <input
                          value={e.strategyId ?? ''}
                          onChange={ev => updateEntry(i, { strategyId: ev.target.value || null })}
                          placeholder="strategy_id"
                          className={cn(INPUT, 'w-full font-mono')}
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="text-[9px] text-muted">计划价格</span>
                        <input
                          value={e.plannedPrice == null ? '' : String(e.plannedPrice)}
                          onChange={ev => updateEntry(i, { plannedPrice: posNum(ev.target.value) ?? null })}
                          inputMode="decimal"
                          placeholder="plannedPrice"
                          className={cn(INPUT, 'w-full font-mono')}
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="text-[9px] text-muted">止损价格</span>
                        <input
                          value={e.stopLoss == null ? '' : String(e.stopLoss)}
                          onChange={ev => updateEntry(i, { stopLoss: posNum(ev.target.value) ?? null })}
                          inputMode="decimal"
                          placeholder="stopLoss"
                          className={cn(INPUT, 'w-full font-mono')}
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="text-[9px] text-muted">论点期限（月）</span>
                        <input
                          value={e.thesisHorizonMonths == null ? '' : String(e.thesisHorizonMonths)}
                          onChange={ev => updateEntry(i, { thesisHorizonMonths: posNum(ev.target.value) ?? null })}
                          inputMode="numeric"
                          placeholder="months"
                          className={cn(INPUT, 'w-full font-mono')}
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="text-[9px] text-muted">退出规则</span>
                        <input
                          value={e.exitRule ?? ''}
                          onChange={ev => updateEntry(i, { exitRule: ev.target.value })}
                          placeholder="可观察退出条件"
                          className={cn(INPUT, 'w-full')}
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="text-[9px] text-muted">失效条件</span>
                        <input
                          value={e.invalidation ?? ''}
                          onChange={ev => updateEntry(i, { invalidation: ev.target.value })}
                          placeholder="论点何时失效"
                          className={cn(INPUT, 'w-full')}
                        />
                      </label>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[9px] text-muted">
                        新建/加仓完整检查需数量、计划价格、策略声明、期限及止损/退出/失效条件。
                      </span>
                      {checkingEntryId === e.id ? (
                        <button
                          onClick={() => abortRef.current?.abort()}
                          className={cn(BTN_GHOST, 'border-warning/30 text-warning')}
                        >
                          <Square className="h-3 w-3" />取消检查
                        </button>
                      ) : (
                        <button
                          onClick={() => void runPlanCheck(e)}
                          disabled={!featureEnabled || dirty || !e.symbol.trim() || checkingEntryId !== null}
                          title={!featureEnabled ? '请先启用结构化计划检查' : dirty ? '请先保存计划' : undefined}
                          className={BTN_GHOST}
                        >
                          <Bot className="h-3 w-3" />检查已保存计划
                        </button>
                      )}
                    </div>
                  </div>
                ))}
                {draftEntries.length === 0 && (
                  <p className="py-4 text-center text-xs text-muted">当日暂无计划条目。</p>
                )}
              </div>
            </div>

            <button onClick={addEntry} className={BTN_GHOST}>
              <Plus className="h-3.5 w-3.5" />新增条目
            </button>

            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">盘后备注 actualNotes</span>
              <textarea
                value={draftNotes}
                onChange={e => setNotes(e.target.value)}
                rows={2}
                placeholder="收盘后补充: 执行情况的自由记录"
                className={cn(INPUT, 'w-full resize-y')}
              />
            </label>

            {checkProgress && (
              <div className="flex items-center gap-2 rounded-lg border border-accent/20 bg-accent/5 px-3 py-2 text-xs text-accent">
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                {checkProgress}
              </div>
            )}
            {checkError && (
              <div className="rounded-lg border border-danger/25 bg-danger/5 px-3 py-2 text-xs text-danger">
                {checkError}
              </div>
            )}
            {checkArtifact && <PlanCheckResultView artifact={checkArtifact} />}

            {featureEnabled && (checksQuery.data?.items.length ?? 0) > 0 && (
              <div className="rounded-lg border border-border bg-elevated/20 p-3">
                <div className="mb-2 flex items-center justify-between">
                  <h4 className="text-[11px] font-medium text-foreground">最近计划检查记录</h4>
                  <span className="text-[9px] text-muted">append-only artifact</span>
                </div>
                <div className="space-y-1">
                  {checksQuery.data?.items.slice(0, 8).map(item => (
                    <div key={item.attempt_id} className="flex flex-wrap items-center gap-2 rounded-md px-2 py-1.5 text-[10px] hover:bg-elevated/50">
                      <button
                        onClick={() => void loadPlanCheck(item.attempt_id)}
                        className="font-mono text-accent hover:underline"
                      >
                        {item.symbol ?? '未知标的'}
                      </button>
                      <span className="text-muted">{item.result_status === 'review_ready' ? '已生成审查' : '无可执行结果'}</span>
                      {item.continuity_mode && (
                        <span
                          className={cn(
                            'inline-flex items-center gap-0.5 rounded border px-1 py-px text-[9px] font-medium',
                            (CONTINUITY_MODE_META[item.continuity_mode] ?? CONTINUITY_MODE_META.unknown).badge,
                          )}
                          title={`连续性: ${(CONTINUITY_MODE_META[item.continuity_mode] ?? CONTINUITY_MODE_META.unknown).label}`}
                        >
                          <GitBranch className="h-2.5 w-2.5" />
                          {(CONTINUITY_MODE_META[item.continuity_mode] ?? CONTINUITY_MODE_META.unknown).label}
                        </span>
                      )}
                      <span className="ml-auto font-mono text-muted">{item.created_at?.slice(0, 19).replace('T', ' ') ?? '—'}</span>
                      <a
                        href={tradingPlanCheckExportUrl(item.attempt_id, 'markdown')}
                        className="inline-flex items-center gap-1 text-secondary hover:text-foreground"
                      >
                        <FileDown className="h-3 w-3" />Markdown
                      </a>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </SectionCard>

      {/* 计划/执行偏差 */}
      <div className="grid gap-3 md:grid-cols-3">
        <DeviationCard
          title="计划未执行"
          count={deviation?.planned_but_not_done.length}
          loading={deviationQuery.isLoading}
        >
          {(deviation?.planned_but_not_done ?? []).map(p => (
            <li key={p.id} className="text-xs text-secondary">
              <span className="font-mono text-foreground">{p.symbol}</span>
              <span className="ml-1.5">{ACTION_LABEL[p.action as PlanAction] ?? p.action}</span>
            </li>
          ))}
        </DeviationCard>
        <DeviationCard
          title="计划外执行"
          count={deviation?.done_but_not_planned.length}
          loading={deviationQuery.isLoading}
        >
          {(deviation?.done_but_not_planned ?? []).map((d, i) => (
            <li key={`${d.tradeId}-${d.kind}-${i}`}>
              <button
                onClick={() => onSelectTrade(d.tradeId)}
                className="text-left text-xs text-secondary transition-colors hover:text-accent cursor-pointer"
                title={`${d.tradeId} · ${d.ts}`}
              >
                <span className="font-mono text-foreground">{d.symbol}</span>
                <span className="ml-1.5">{KIND_LABEL[d.kind as TradeEventKind] ?? d.kind}</span>
                <span className="ml-1.5 font-mono text-[10px] text-muted">{d.ts}</span>
              </button>
            </li>
          ))}
        </DeviationCard>
        <DeviationCard
          title="已匹配"
          count={deviation?.matched.length}
          loading={deviationQuery.isLoading}
        >
          {(deviation?.matched ?? []).map(p => (
            <li key={p.id} className="text-xs text-secondary">
              <span className="font-mono text-foreground">{p.symbol}</span>
              <span className="ml-1.5">{ACTION_LABEL[p.action as PlanAction] ?? p.action}</span>
            </li>
          ))}
        </DeviationCard>
      </div>
    </div>
  )
}

const PLAN_GATE_COPY: Record<string, { label: string; detail: string; className: string }> = {
  proceed: {
    label: '输入完整，可进入审查',
    detail: '仅表示数据与程序门禁允许继续检查，不代表建议交易。',
    className: 'border-success/30 bg-success/5 text-success',
  },
  wait: {
    label: '暂缓，需补充或修正',
    detail: '程序门禁或数据充分性未通过，第二阶段未运行。',
    className: 'border-warning/30 bg-warning/5 text-warning',
  },
  unknown: {
    label: '信息不足，无法判断',
    detail: '缺少计划字段、策略声明或 canonical 数据，未调用后续审查。',
    className: 'border-border bg-elevated/40 text-muted',
  },
}

function PlanCheckResultView({ artifact }: { artifact: PlanCheckArtifact }) {
  const result = artifact.result
  const gate = PLAN_GATE_COPY[result.gate.status] ?? PLAN_GATE_COPY.unknown
  const failed = artifact.status === 'failed'
  const cancelled = artifact.status === 'cancelled'

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <div className="flex flex-wrap items-start gap-2">
        <div>
          <h4 className="text-xs font-medium text-foreground">结构化计划检查报告</h4>
          <p className="mt-0.5 font-mono text-[9px] text-muted">{artifact.attempt_id}</p>
        </div>
        <span className={cn('rounded-md border px-2 py-1 text-[10px] font-medium', gate.className)}>
          {failed ? '检查失败，未产生结论' : cancelled ? '检查已取消' : gate.label}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <AiExecutionMetaBadge meta={result.ai_meta} />
          <a
            href={tradingPlanCheckExportUrl(artifact.attempt_id, 'json')}
            className="inline-flex items-center gap-1 text-[10px] text-secondary hover:text-foreground"
          >
            <FileDown className="h-3 w-3" />JSON
          </a>
          <a
            href={tradingPlanCheckExportUrl(artifact.attempt_id, 'markdown')}
            className="inline-flex items-center gap-1 text-[10px] text-secondary hover:text-foreground"
          >
            <FileDown className="h-3 w-3" />Markdown
          </a>
        </div>
      </div>

      <p className="mt-2 text-[10px] leading-relaxed text-muted">{gate.detail}</p>
      {result.gate.reasons.length > 0 && (
        <ul className="mt-2 space-y-1 rounded-md bg-elevated/30 p-2 text-[10px] text-secondary">
          {result.gate.reasons.map((reason, index) => <li key={`${reason}-${index}`}>· {reason}</li>)}
        </ul>
      )}
      <ContinuitySection artifact={artifact} />

      {result.stage1 && (
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          {[
            ['趋势诊断', result.stage1.trend],
            ['波动诊断', result.stage1.volatility],
            ['流动性诊断', result.stage1.liquidity],
          ].map(([label, value]) => (
            <div key={label} className="rounded-md border border-border/60 bg-base/50 p-2">
              <div className="text-[9px] text-muted">{label}</div>
              <div className="mt-1 text-[11px] leading-relaxed text-secondary">{value}</div>
            </div>
          ))}
        </div>
      )}

      {result.review && (
        <div className="mt-3">
          <div className="mb-1.5 text-[10px] font-medium text-foreground">计划审查项</div>
          <div className="space-y-1.5">
            {result.review.checks.map((check, index) => (
              <div key={`${check.item}-${index}`} className="rounded-md border border-border/60 px-2.5 py-2">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-medium text-foreground">{check.item}</span>
                  <span className={cn(
                    'rounded px-1.5 py-0.5 text-[9px]',
                    check.conclusion === '满足'
                      ? 'bg-success/10 text-success'
                      : check.conclusion === '不满足'
                        ? 'bg-danger/10 text-danger'
                        : 'bg-warning/10 text-warning',
                  )}>
                    {check.conclusion}
                  </span>
                </div>
                <p className="mt-1 text-[10px] leading-relaxed text-secondary">{check.reason}</p>
              </div>
            ))}
          </div>
          {result.review.summary && <p className="mt-2 text-[10px] text-secondary">{result.review.summary}</p>}
        </div>
      )}

      {result.warnings.length > 0 && (
        <div className="mt-3 rounded-md border border-warning/20 bg-warning/5 p-2 text-[10px] text-warning">
          {result.warnings.map((warning, index) => <div key={`${warning}-${index}`}>{warning}</div>)}
        </div>
      )}

      <div className="mt-4">
        <div className="mb-2 text-[10px] font-medium text-foreground">可审计决策链</div>
        <DecisionTrace nodes={artifact.trace} />
      </div>
      <p className="mt-3 border-t border-border pt-2 text-[9px] leading-relaxed text-muted">
        {result.disclaimer}
      </p>
    </section>
  )
}

/**
 * M25: 计划检查连续性面板。
 *
 * 触发条件: artifact.parent_attempt_id 或 artifact.result.continuity 存在。
 * - 摘要: 当前 artifact 自身的连续性判定 (mode / reason / bars_delta / data_as_of)。
 * - 父链: 仅在存在 parent_attempt_id 时按需查询 tradingGetPlanCheckContinuity,
 *   展示 self → parent → ... 的 mode / reason / bars_delta / data_as_of / token usage。
 * 只读研究投影, 不含执行语义, 不生成交易建议。
 */
function ContinuitySection({ artifact }: { artifact: PlanCheckArtifact }) {
  const meta: PlanCheckContinuityMeta | undefined = artifact.result.continuity
  const hasParent = Boolean(artifact.parent_attempt_id) || Boolean(meta?.parent_attempt_id)
  // 链查询仅在确有父链时启用 (避免 fresh / 无 parent 的 artifact 发起无谓请求)。
  const chainQuery = useQuery({
    queryKey: QK.planCheckContinuity(artifact.attempt_id),
    queryFn: () => tradingGetPlanCheckContinuity(artifact.attempt_id),
    enabled: hasParent,
    retry: false,
  })

  // 既无 parent 也无 continuity 摘要 → 不渲染。
  if (!meta && !hasParent) return null

  const modeMeta = meta ? (CONTINUITY_MODE_META[meta.mode] ?? CONTINUITY_MODE_META.unknown) : null

  return (
    <div className="mt-3 rounded-md border border-border/60 bg-elevated/20 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1 text-[10px] font-medium text-foreground">
          <GitBranch className="h-3 w-3 text-muted" />连续性
        </span>
        {modeMeta && (
          <span className={cn('rounded border px-1.5 py-0.5 text-[9px] font-medium', modeMeta.badge)}>
            {modeMeta.label}
          </span>
        )}
        <span className="text-[9px] leading-relaxed text-muted">
          仅比较本地数据锚点，不含执行语义；锚点失配或跨度过大时回到全量分析。
        </span>
      </div>

      {meta && (
        <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <ContinuityField label="判定原因" value={meta.reason || '—'} span />
          <ContinuityField label="新增 bar" value={String(meta.bars_delta ?? 0)} mono />
          <ContinuityField label="父数据截止" value={meta.parent_data_as_of ?? '—'} mono />
          <ContinuityField label="当前数据截止" value={meta.self_data_as_of ?? '—'} mono />
        </div>
      )}

      {hasParent && (
        <div className="mt-2">
          <div className="mb-1 text-[9px] font-medium text-muted">
            父链{chainQuery.data ? `（深度 ${chainQuery.data.depth}）` : ''}
          </div>
          {chainQuery.isLoading ? (
            <p className="text-[10px] text-muted">加载父链中…</p>
          ) : chainQuery.isError ? (
            <p className="text-[10px] text-danger">父链查询失败</p>
          ) : chainQuery.data && chainQuery.data.chain.length > 0 ? (
            <div className="data-table-scroll">
              <table className="data-table min-w-full text-[10px]">
                <thead>
                  <tr>
                    {['节点', '模式', '新增 bar', '数据截止', 'tokens', '原因'].map(h => (
                      <th key={h} className="border-b border-border px-2 py-1 text-left text-[9px] font-medium uppercase tracking-wider text-muted">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {chainQuery.data.chain.map((node: PlanCheckContinuityChainNode, idx: number) => {
                    const nm = CONTINUITY_MODE_META[node.continuity_mode] ?? CONTINUITY_MODE_META.unknown
                    const isSelf = idx === 0
                    return (
                      <tr key={node.attempt_id} className={cn('align-top', isSelf && 'bg-accent/5')}>
                        <td className="border-b border-border/50 px-2 py-1 font-mono text-muted">
                          {isSelf ? '当前' : `↑ ${node.attempt_id.slice(0, 8)}`}
                        </td>
                        <td className="border-b border-border/50 px-2 py-1">
                          <span className={cn('rounded border px-1 py-px text-[9px] font-medium', nm.badge)}>{nm.label}</span>
                        </td>
                        <td className="border-b border-border/50 px-2 py-1 text-right font-mono tabular-nums">{node.bars_delta ?? 0}</td>
                        <td className="border-b border-border/50 px-2 py-1 font-mono text-muted">{node.data_as_of ?? '—'}</td>
                        <td className="border-b border-border/50 px-2 py-1 font-mono tabular-nums text-muted">{fmtUsageTokens(node.usage)}</td>
                        <td className="border-b border-border/50 px-2 py-1 text-secondary">{node.continuity_reason || '—'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}

/** 连续性摘要字段卡: value 横跨多列时 span=true。 */
function ContinuityField({ label, value, mono, span }: {
  label: string
  value: string
  mono?: boolean
  span?: boolean
}) {
  return (
    <div className={cn('rounded border border-border/50 bg-base/40 px-2 py-1.5', span && 'lg:col-span-2')}>
      <div className="text-[9px] text-muted">{label}</div>
      <div className={cn('mt-0.5 text-[10px] leading-relaxed text-secondary', mono && 'font-mono tabular-nums')}>{value}</div>
    </div>
  )
}

/** token 用量紧凑展示: 全 0 / 缺失 → '—'。与 AiExecutionMetaBadge 的计数口径一致。 */
function fmtUsageTokens(usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } | null): string {
  if (!usage) return '—'
  const total = usage.total_tokens ?? ((usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0))
  return total > 0 ? total.toLocaleString('en-US') : '—'
}

function DeviationCard({ title, count, loading, children }: {
  title: string
  count: number | undefined
  loading: boolean
  children: React.ReactNode
}) {
  return (
    <section className="panel p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-medium text-foreground">{title}</h4>
        <span className="rounded-md bg-elevated/50 px-1.5 py-0.5 text-[10px] font-medium text-muted">
          {loading ? '…' : count ?? '—'}
        </span>
      </div>
      <ul className="mt-2.5 space-y-1.5">
        {children}
      </ul>
      {!loading && count === 0 && <p className="mt-2.5 text-[10px] text-muted">无</p>}
    </section>
  )
}

// ================================================================
// Tab 4: 账户
// ================================================================

function AccountsPanel() {
  const qc = useQueryClient()
  const accountsQuery = useQuery({ queryKey: ['trading-accounts'], queryFn: tradingGetAccounts })
  const doc = accountsQuery.data
  const first: TradingAccount | undefined = doc?.accounts[0]

  const [capital, setCapital] = useState('')
  const [months, setMonths] = useState('')
  const [ratioPct, setRatioPct] = useState('')
  const [changeAmount, setChangeAmount] = useState('')
  const [changeReason, setChangeReason] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // 首次拿到数据时回填表单(保存成功后 loaded 复位, 重新回填服务端事实)
  useEffect(() => {
    if (first && !loaded) {
      setCapital(String(first.capital))
      setMonths(String(first.horizonFundMonths))
      setRatioPct(String(Math.round(first.maxSingleRatio * 10000) / 100))
      setLoaded(true)
    }
  }, [first, loaded])

  const saveMut = useMutation({
    mutationFn: (accounts: TradingAccount[]) => tradingPutAccounts({ accounts }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading-accounts'] })
      qc.invalidateQueries({ queryKey: ['trading-portfolio'] })
      toast('账户已保存', 'success')
      setChangeAmount('')
      setChangeReason('')
      setErr(null)
      setLoaded(false)
    },
  })

  const handleSave = () => {
    setErr(null)
    if (!first || !doc) return
    const cap = Number(capital)
    const m = Number(months)
    const ratio = Number(ratioPct) / 100
    if (!Number.isFinite(cap) || cap <= 0) { setErr('本金必须是正数。'); return }
    if (!Number.isInteger(m) || m <= 0) { setErr('资金期限必须是正整数(月)。'); return }
    if (!Number.isFinite(ratio) || ratio <= 0 || ratio > 1) { setErr('单标的比例必须在 0–100% 之间。'); return }

    // 追加资金变更: 只追加, 不删不改历史 changes
    const changes: AccountChange[] = [...first.changes]
    if (changeAmount.trim() !== '') {
      const amt = Number(changeAmount)
      if (!Number.isFinite(amt) || amt === 0) { setErr('资金变更金额必须是非零数字。'); return }
      if (changeReason.trim() === '') { setErr('资金变更必须填写原因。'); return }
      changes.push({ ts: nowStr(), amount: amt, reason: changeReason.trim() })
    }

    const next: TradingAccount[] = doc.accounts.map((a, i) => (
      i === 0 ? { ...a, capital: cap, horizonFundMonths: m, maxSingleRatio: ratio, changes } : a
    ))
    saveMut.mutate(next)
  }

  if (accountsQuery.isLoading) {
    return <div className="panel px-5 py-10 text-center text-sm text-muted">加载账户中…</div>
  }
  if (!first) {
    return <EmptyState icon={Wallet} title="暂无资金账户" hint="后端尚未配置资金账户(accounts.json)。" />
  }

  const changes = [...first.changes].sort((a, b) => (a.ts < b.ts ? 1 : -1))

  return (
    <div className="space-y-4">
      <SectionCard
        title={`资金账户 ${first.id}`}
        extra={<span className="text-[10px] text-muted">币种 {first.currency}{doc && doc.accounts.length > 1 ? ` · 共 ${doc.accounts.length} 个账户, 此处编辑第一个` : ''}</span>}
      >
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">本金 capital</span>
              <input value={capital} onChange={e => setCapital(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">资金期限(月) horizonFundMonths</span>
              <input value={months} onChange={e => setMonths(e.target.value)} inputMode="numeric" className={cn(INPUT, 'w-full font-mono')} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">单标的占比上限 maxSingleRatio(%)</span>
              <input value={ratioPct} onChange={e => setRatioPct(e.target.value)} inputMode="decimal" className={cn(INPUT, 'w-full font-mono')} />
            </label>
          </div>

          <div className="panel border-border/60 bg-base/40 p-3">
            <div className="text-[10px] text-muted">追加资金变更(随保存一并提交, 历史不可改)</div>
            <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-[10rem_1fr]">
              <input
                value={changeAmount}
                onChange={e => setChangeAmount(e.target.value)}
                placeholder="金额(正=追加/负=取出)"
                inputMode="decimal"
                className={cn(INPUT, 'font-mono')}
              />
              <input
                value={changeReason}
                onChange={e => setChangeReason(e.target.value)}
                placeholder="原因, 如: 年终奖追加 / 急用取出"
                className={INPUT}
              />
            </div>
          </div>

          <InlineError msg={err} />
          <div className="flex justify-end">
            <button onClick={handleSave} disabled={saveMut.isPending} className={BTN_PRIMARY}>
              <Save className="h-3.5 w-3.5" />{saveMut.isPending ? '保存中…' : '保存账户'}
            </button>
          </div>
        </div>
      </SectionCard>

      <SectionCard title="资金变更历史" extra={<span className="text-[10px] text-muted">{first.changes.length} 条 · 只读</span>}>
        {changes.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted">暂无资金变更记录。</p>
        ) : (
          <ul className="space-y-1.5">
            {changes.map((c, i) => (
              <li key={`${c.ts}-${i}`} className="flex items-baseline gap-3 text-xs">
                <span className="shrink-0 font-mono text-[10px] text-muted">{c.ts}</span>
                <span className={cn('shrink-0 font-mono tabular-nums', priceColorClass(c.amount))}>
                  {c.amount > 0 ? '+' : ''}{fmtMoney(c.amount)}
                </span>
                <span className="min-w-0 flex-1 text-secondary">{c.reason}</span>
              </li>
            ))}
          </ul>
        )}
      </SectionCard>
    </div>
  )
}

// ================================================================
// Tab 5: 桥接规划(占位保留)
// ================================================================

// 后续实现计划(本轮为占位):
//
// 一、信号 → 交易 的桥接
//   监控通知产生的买卖信号(StrategyAlert),通过可插拔的输出通道分发到
//   支持外部信号的交易软件。核心是在 alert_handler 层做多通道分发。
//
// 二、支持的交易软件(按接入难度)
//   1. QMT / miniQMT(迅投)—— 个人 A 股实盘首选。
//      XtQuant 的 xttrader.order_stock() 下单,信号来源不限(文件/HTTP)。
//   2. 掘金量化(MyQuant)—— 本地终端 + Python SDK,事件驱动接收信号。
//   3. Ptrade(恒生)—— 内置策略引擎,外部信号经 API/文件喂入。
//   4. vnpy(VeighNa)—— 开源框架,自写策略模块接收信号再调 Gateway 下单。
//
// 三、信号输出通道(可插拔)
//   alert_handler 分发:
//     ├─ SSE → 前端通知(已有)
//     ├─ 本地文件(JSON/CSV) → QMT 脚本轮询读取  ← 最简单,优先做
//     ├─ Webhook POST → 外部交易脚本
//     └─ 直连 xttrader(需本机装 QMT)
//
// 四、信号 → 交易指令 的字段补全
//   现有 StrategyAlert(symbol/type/strategy_id/price)是「信号层」,
//   下单还需补:volume(数量,A股100的倍数)、price_type(市价/限价)、account。
const PLAN: { title: string; desc: string }[] = [
  {
    title: 'QMT / miniQMT',
    desc: '个人 A 股实盘首选。XtQuant 的 xttrader 下单,信号经文件或 HTTP 喂入即可。国内个人量化实盘事实标准。',
  },
  {
    title: '掘金量化 (MyQuant)',
    desc: '本地终端 + Python SDK,事件驱动接收外部信号下单,本土化程度高。',
  },
  {
    title: 'Ptrade (恒生)',
    desc: '内置 Python 策略引擎,外部信号经 API/文件喂入,灵活性低于 QMT。',
  },
  {
    title: 'vnpy (VeighNa)',
    desc: '开源交易框架,Gateway 丰富(期货/股票/加密货币),需自建执行端,搭建成本较高。',
  },
  {
    title: '信号输出通道',
    desc: 'alert_handler 多通道分发:本地文件(最简,优先)、Webhook POST、直连 xttrader。与具体交易软件解耦。',
  },
]

function BridgePanel() {
  return (
    <div className="max-w-3xl">
      <section className="panel p-4">
        <h3 className="text-sm font-semibold text-foreground">信号自动下单桥接 · 后续实现规划</h3>
        <p className="mt-1 text-xs leading-relaxed text-muted">
          把监控产生的买卖信号, 自动推送给支持外部信号的交易软件(QMT/掘金/Ptrade 等)执行下单。
        </p>
        <ul className="mt-3 space-y-3">
          {PLAN.map((item) => (
            <li key={item.title} className="flex gap-3">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
              <div>
                <p className="text-sm font-medium text-foreground">{item.title}</p>
                <p className="mt-0.5 text-xs leading-relaxed text-secondary">{item.desc}</p>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
