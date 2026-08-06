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
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot, Briefcase, Cable, CalendarDays, FileText, Plus, RefreshCw, Save,
  ShieldAlert, Trash2, Wallet, type LucideIcon,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import {
  tradingAppendEvent,
  tradingEvaluateGates,
  tradingGetAccounts,
  tradingGetAutopsy,
  tradingGetPlan,
  tradingGetPlanDeviation,
  tradingGetPortfolio,
  tradingGetTrade,
  tradingListTrades,
  tradingOpenTrade,
  tradingPutAccounts,
  tradingPutPlan,
  tradingRunAutopsy,
  type AccountChange,
  type AutopsyResult,
  type GateEvaluation,
  type PlanAction,
  type PlanEntry,
  type PortfolioSnapshot,
  type Trade,
  type TradeEvent,
  type TradeEventKind,
  type TradeStatus,
  type TradingAccount,
  type TradingAppendEventPayload,
} from '@/lib/api'

// ===== 样式(沿用项目 tokens) =====

const INPUT =
  'rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/50'
const BTN_PRIMARY =
  'inline-flex items-center gap-1.5 rounded-btn bg-accent px-3.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50'
const BTN_GHOST =
  'inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50'
const BTN_DANGER =
  'inline-flex items-center gap-1.5 rounded-btn bg-danger px-3.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-danger/90 disabled:opacity-50'

const TH = 'px-3 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-muted border-b border-border'
const TD = 'px-3 py-2 text-xs border-b border-border/50'
const TD_NUM = `${TD} text-right font-mono tabular-nums`

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
    <div className="flex flex-col h-full">
      <PageHeader title="交易" subtitle="生命周期 · 门禁 · 计划与账户" />

      {/* tab 栏 */}
      <div className="flex items-center gap-1 overflow-x-auto border-b border-border px-5 pt-3 pb-2">
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

      <div className="flex-1 min-h-0 overflow-auto px-5 py-4">
        <div className="mx-auto max-w-6xl">
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
    <section className="rounded-card border border-border bg-surface overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        {extra}
      </div>
      <div className="p-4">{children}</div>
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
    <div className="space-y-3 rounded-card border border-danger/40 bg-danger/5 p-4">
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
        <div className="rounded-card border border-border bg-surface px-5 py-8 text-center text-sm text-muted">加载组合快照中…</div>
      ) : pf ? (
        <PortfolioHeader pf={pf} />
      ) : (
        <div className="rounded-card border border-border bg-surface px-5 py-8 text-center text-sm text-muted">组合快照不可用。</div>
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
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px]">
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
          <div key={c.label} className="rounded-card border border-border bg-surface p-4">
            <div className="text-xs text-muted">{c.label}</div>
            <div className={cn('mt-1 font-mono text-lg font-medium tabular-nums text-foreground', c.cls)}>
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
        <div className="rounded-card border border-warning/40 bg-warning/5 px-4 py-3 text-xs text-warning">
          fhold 券商持仓暂不可用(未接入或读取失败), 当前仅展示生命周期记录与账户快照。
        </div>
      ) : pf.fhold.positions.length === 0 ? (
        <p className="py-4 text-center text-xs text-muted">券商账户当前无持仓。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px]">
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
    return <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted">加载交易详情中…</div>
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
  const autopsyQuery = useQuery({
    queryKey: ['trading-autopsy', tradeId],
    queryFn: () => tradingGetAutopsy(tradeId),
    enabled: requested,
  })
  const runMut = useMutation({
    mutationFn: () => tradingRunAutopsy(tradeId),
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
  const deviationQuery = useQuery({
    queryKey: ['trading-plan-deviation', date],
    queryFn: () => tradingGetPlanDeviation(date),
  })

  // 本地草稿: null = 未编辑, 直接展示查询结果; 编辑后覆盖, 保存/切换日期后归位
  const [entries, setEntries] = useState<PlanEntry[] | null>(null)
  const [notes, setNotes] = useState<string | null>(null)
  useEffect(() => {
    setEntries(null)
    setNotes(null)
  }, [date])

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
          qty: e.qty != null && e.qty > 0 ? e.qty : null,
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
      { id, symbol: '', tradeId: null, action: 'buy_new', trigger: '', qty: null, reason: '', createdAt: nowStr() },
    ])
  }
  const removeEntry = (i: number) => {
    setEntries(draftEntries.filter((_, idx) => idx !== i))
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
        {planQuery.isLoading ? (
          <p className="py-6 text-center text-xs text-muted">加载计划中…</p>
        ) : (
          <div className="space-y-3">
            <div className="overflow-x-auto">
              <div className="min-w-[860px] space-y-2">
                {/* 表头 */}
                <div className="grid grid-cols-[7rem_7rem_1fr_6rem_1fr_2rem] items-center gap-2 px-1">
                  {['标的', '动作', '触发条件', '数量', '理由', ''].map(h => (
                    <span key={h} className="text-[10px] font-medium uppercase tracking-wider text-muted">{h}</span>
                  ))}
                </div>
                {draftEntries.map((e, i) => (
                  <div key={e.id} className="grid grid-cols-[7rem_7rem_1fr_6rem_1fr_2rem] items-center gap-2">
                    <input
                      value={e.symbol}
                      onChange={ev => updateEntry(i, { symbol: ev.target.value })}
                      placeholder="600519"
                      className={cn(INPUT, 'font-mono')}
                    />
                    <select
                      value={e.action}
                      onChange={ev => updateEntry(i, { action: ev.target.value as PlanAction })}
                      className={cn(INPUT, 'cursor-pointer')}
                    >
                      {(Object.keys(ACTION_LABEL) as PlanAction[]).map(a => (
                        <option key={a} value={a}>{ACTION_LABEL[a]}</option>
                      ))}
                    </select>
                    <input
                      value={e.trigger}
                      onChange={ev => updateEntry(i, { trigger: ev.target.value })}
                      placeholder="如: 跌破 12.5 或 放量突破"
                      className={INPUT}
                    />
                    <input
                      value={e.qty == null ? '' : String(e.qty)}
                      onChange={ev => updateEntry(i, { qty: posNum(ev.target.value) ?? null })}
                      placeholder="—"
                      inputMode="decimal"
                      className={cn(INPUT, 'font-mono')}
                    />
                    <input
                      value={e.reason}
                      onChange={ev => updateEntry(i, { reason: ev.target.value })}
                      placeholder="为什么"
                      className={INPUT}
                    />
                    <button
                      onClick={() => removeEntry(i)}
                      title="删除条目"
                      className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-muted transition-colors hover:bg-danger/10 hover:text-danger cursor-pointer"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
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

function DeviationCard({ title, count, loading, children }: {
  title: string
  count: number | undefined
  loading: boolean
  children: React.ReactNode
}) {
  return (
    <section className="rounded-card border border-border bg-surface p-4">
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
    return <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted">加载账户中…</div>
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

          <div className="rounded-card border border-border/60 bg-base/40 p-3">
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
      <section className="rounded-card border border-border bg-surface p-5">
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
