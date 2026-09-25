/**
 * 模拟盘 (虚拟账户) — 虚拟资金 + 真实行情价格的模拟撮合, 多账户隔离。
 *
 * 口径提示: 持仓现价用最近日线收盘价 (收盘定版一致, 盘中估算见设计方案
 * docs/paper-trading-plan.md)。费用/滑点参数与回测引擎同名同默认值。
 * 多账户: 所有查询按账户隔离 (queryKey 前缀 'paper'), 切换即换一套数据。
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Banknote, CircleDollarSign, GitCompare, PieChart, Plus, Settings, TrendingUp, Wallet, X } from 'lucide-react'
import { api, type PaperCompareRow, type PaperFill, type PaperOrder } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'

const ACC_STORAGE_KEY = 'paper.account'

const ORDER_TYPE_LABEL: Record<string, string> = {
  market: '即时',
  next_open: '次日开盘',
  close: '当日收盘',
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待成交',
  filled: '已成交',
  cancelled: '已撤单',
  expired: '已过期',
}

function fmtMoney(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 账户 id: 字母数字开头短横线/下划线, 与后端校验一致 */
function genAccountId() {
  return `acc_${Date.now().toString(36)}${Math.floor(Math.random() * 36).toString(36)}`
}

/** 归一化到首日=100 (基准对比); 空洞(缺行情日)保留 null 不连线 */
function normalizeBase(values: Array<number | null | undefined>): Array<number | null> {
  const first = values.find((v): v is number => v != null && v > 0)
  if (!first) return values.map(() => null)
  return values.map(v => (v != null && v > 0 ? Number(((v / first) * 100).toFixed(2)) : null))
}

/** 净值曲线 (echarts line, 跟随主题色): 账户归一化 vs 沪深300 归一化 (首日=100) */
function NavChart({ nav }: { nav: Array<{ date: string; nav: number; benchmark_close?: number }> }) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!elRef.current) return
    chartRef.current = echarts.init(elRef.current, undefined, { renderer: 'canvas' })
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    // 跟随亮暗主题切换重设坐标轴配色
    const observer = new MutationObserver(() => {
      const darkNow = document.documentElement.classList.contains('dark')
      const axisColor = darkNow ? '#8E8E96' : '#52525B'
      const splitColor = darkNow ? '#353539' : '#E4E4E7'
      chartRef.current?.setOption({
        xAxis: { axisLine: { lineStyle: { color: splitColor } }, axisLabel: { color: axisColor } },
        yAxis: { axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: splitColor } } },
      })
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => {
      window.removeEventListener('resize', onResize)
      observer.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const dark = document.documentElement.classList.contains('dark')
    const axisColor = dark ? '#8E8E96' : '#52525B'
    const splitColor = dark ? '#353539' : '#E4E4E7'
    chart.setOption({
      grid: { left: 56, right: 16, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => fmtMoney(v),
      },
      xAxis: {
        type: 'category',
        data: nav.map(n => n.date),
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: axisColor, fontSize: 10, formatter: (v: number) => fmtMoney(v, 0) },
        splitLine: { lineStyle: { color: splitColor } },
      },
      series: [
        {
          name: '虚拟账户',
          type: 'line',
          data: normalizeBase(nav.map(n => n.nav)),
          showSymbol: false,
          lineStyle: { color: '#8B5CF6', width: 2 },
          areaStyle: { color: 'rgba(139, 92, 246, 0.08)' },
        },
        ...(nav.some(n => n.benchmark_close) ? [{
          name: '沪深300',
          type: 'line' as const,
          data: normalizeBase(nav.map(n => n.benchmark_close)),
          showSymbol: false,
          lineStyle: { color: dark ? '#8E8E96' : '#52525B', width: 1.5, type: 'dashed' as const },
        }] : []),
      ],
      legend: {
        top: 0,
        right: 0,
        textStyle: { color: axisColor, fontSize: 10 },
        itemWidth: 14,
      },
    })
  }, [nav])

  return <div ref={elRef} className="h-48 w-full" />
}

function StatCard({ label, value, sub, valueClass, icon: Icon, iconCls }: {
  label: string; value: string; sub?: string; valueClass?: string
  icon?: React.ComponentType<{ className?: string }>; iconCls?: string
}) {
  return (
    <div className="rounded-card border border-border bg-surface px-4 py-3 transition-colors duration-150 hover:border-accent/25 hover:bg-elevated/30">
      <div className="flex items-center gap-1.5 text-[11px] text-muted">
        {Icon && <Icon className={cn('h-3.5 w-3.5', iconCls)} />}
        {label}
      </div>
      <div className={cn('mt-1 font-mono text-xl font-semibold tabular', valueClass)}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

/** 新建账户默认值 (localStorage): 上次创建用的初始资金/费用, 作为下次预填默认。 */
const NEW_ACCOUNT_DEFAULTS_KEY = 'paper.newAccountDefaults'
type NewAccountDefaults = {
  cash?: string; commissionWan?: string; stampQian?: string; slippageBps?: string
}

function loadNewAccountDefaults(): Required<NewAccountDefaults> {
  let saved: NewAccountDefaults = {}
  try { saved = JSON.parse(localStorage.getItem(NEW_ACCOUNT_DEFAULTS_KEY) || '{}') } catch { /* 忽略坏数据 */ }
  return {
    cash: saved.cash ?? '1000000',
    commissionWan: saved.commissionWan ?? '2.5',   // 万2.5
    stampQian: saved.stampQian ?? '1',             // 千1 (仅卖出)
    slippageBps: saved.slippageBps ?? '5',         // 5bps
  }
}

/** 初始化向导: 选中账户不存在或正在新建时展示。
 * onCancel 由外层按「是否已有其他账户」传入 —— 一个账户都没有时必须先建一个, 不给取消。
 * 费用三参数可设置, 且记住为下次新建的默认值。 */
function SetupCard({ accId, onDone, onCancel }: { accId: string; onDone: (createdId: string) => void; onCancel?: () => void }) {
  const defaults = loadNewAccountDefaults()
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaults.stampQian)
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [name, setName] = useState('')
  const m = useMutation({
    mutationFn: () =>
      api.paperCreateAccount({
        initial_cash: Number(cash),
        account_id: accId,
        name: name.trim() || undefined,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
      }),
    onSuccess: r => {
      // 记住本次取值, 作为下次新建账户的默认
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({
          cash, commissionWan, stampQian, slippageBps,
        }))
      } catch { /* 存储不可用时静默 */ }
      onDone(r.account.id)
    },
  })
  const valid = Number(cash) > 0
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0
  return (
    <div className="mx-auto mt-16 max-w-md rounded-card border border-border bg-surface p-6">
      <div className="flex items-center gap-2">
        <CircleDollarSign className="h-5 w-5 text-accent" />
        <h2 className="text-[16px] leading-6 font-semibold">创建虚拟账户</h2>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        虚拟资金 + 真实行情价格模拟撮合, 不涉及任何真实资金。费用口径与回测一致,
        创建后仍可在「费用」里调整; 本次取值会记住, 作为下次新建的默认。
      </p>
      <label className="mt-4 block text-xs text-secondary">账户名称 (可选)</label>
      <input
        value={name}
        onChange={e => setName(e.target.value)}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent/50"
        placeholder={accId === 'default' ? '默认账户' : `账户 ${accId}`}
      />
      <label className="mt-3 block text-xs text-secondary">初始虚拟资金 (元)</label>
      <input
        type="number"
        value={cash}
        onChange={e => setCash(e.target.value)}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 font-mono text-sm outline-none focus:border-accent/50"
        placeholder="1000000"
      />
      <div className="mt-3 grid grid-cols-3 gap-2">
        <div>
          <label className="block text-[11px] text-muted">佣金 (万)</label>
          <input type="number" step="0.1" min="0" value={commissionWan} onChange={e => setCommissionWan(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="block text-[11px] text-muted">印花税 (千)</label>
          <input type="number" step="0.1" min="0" value={stampQian} onChange={e => setStampQian(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="block text-[11px] text-muted">滑点 (bps)</label>
          <input type="number" step="1" min="0" value={slippageBps} onChange={e => setSlippageBps(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
      </div>
      <div className="mt-4 flex gap-2">
        <button
          onClick={() => valid && m.mutate()}
          disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
        >
          {m.isPending ? '创建中…' : '创建账户'}
        </button>
        {onCancel && (
          <button
            onClick={onCancel}
            disabled={m.isPending}
            className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
            title="放弃创建, 返回原账户"
          >
            取消
          </button>
        )}
      </div>
      {m.isError && <div className="mt-2 text-xs text-danger">{String((m.error as Error).message)}</div>}
    </div>
  )
}

// ================================================================
// 通用联想输入 (紧凑版): 受控输入 + 选项下拉 + 键盘导航 + 外点关闭。
// 数据获取由父组件负责 (instrumentSearch 异步搜索 / 策略列表本地过滤),
// 本组件只管交互与渲染; 模式对齐 Watchlist.StockSearchBox 一族搜索框。
// ================================================================
interface SuggestOption {
  /** 选中后写入输入框的值 (完整代码 / 策略 id) */
  value: string
  /** 主文本 (截断) */
  label: string
  /** 可选等宽左槽 (股票代码) */
  left?: string
  /** 可选右槽 (板性徽标 / 来源等) */
  hint?: ReactNode
}

function SuggestInput({
  value,
  onChange,
  options,
  onSelect,
  placeholder,
  className,
  loading,
}: {
  value: string
  onChange: (v: string) => void
  options: SuggestOption[]
  onSelect: (opt: SuggestOption) => void
  placeholder?: string
  className?: string
  loading?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function handlePick(opt: SuggestOption) {
    onSelect(opt)
    setOpen(false)
    setActiveIdx(-1)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || options.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, options.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const pick = options[activeIdx >= 0 ? activeIdx : 0]
      if (pick) handlePick(pick)
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <input
        type="text"
        value={value}
        onChange={e => { onChange(e.target.value); setOpen(true); setActiveIdx(-1) }}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className={className}
      />
      {open && (loading || options.length > 0) && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 max-h-56 overflow-y-auto rounded-card border border-border bg-base shadow-xl">
          {loading && options.length === 0 ? (
            <div className="px-3 py-3 text-center text-[11px] text-muted">搜索中…</div>
          ) : (
            options.map((opt, i) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => handlePick(opt)}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors duration-100',
                  i === activeIdx ? 'bg-accent/10 text-accent' : 'hover:bg-elevated text-foreground',
                )}
              >
                {opt.left && <span className="shrink-0 font-mono text-[11px]">{opt.left}</span>}
                <span className="min-w-0 flex-1 truncate">{opt.label}</span>
                {opt.hint}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}

/** 下单面板 */
function OrderForm({ acc, onDone }: { acc: string; onDone: () => void }) {
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('100')
  const [amount, setAmount] = useState('10000')
  const [qtyMode, setQtyMode] = useState<'qty' | 'amount'>('qty')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('market')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const m = useMutation({
    mutationFn: () =>
      api.paperOrderCreate({
        symbol: symbol.trim().toUpperCase(),
        side,
        ...(qtyMode === 'qty' ? { qty: Number(qty) } : { amount: Number(amount) }),
        order_type: orderType,
      }, acc),
    onSuccess: r => {
      setMsg({ ok: true, text: `已提交 ${ORDER_TYPE_LABEL[r.order.order_type]}单 ${r.order.side === 'buy' ? '买入' : '卖出'} ${r.order.symbol} x ${r.order.qty}` })
      onDone()
    },
    onError: e => setMsg({ ok: false, text: String((e as Error).message) }),
  })

  const canSubmit = symbol.trim().length >= 6
    && (qtyMode === 'qty' ? Number(qty) > 0 && Number(qty) % 100 === 0 : Number(amount) > 0)

  // 标的联想: 代码 / 中文名 / 拼音首字母, 与自选搜索同一后端 (ETF 可一并搜出)
  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbol.trim(), 'stock,etf'),
    queryFn: () => api.instrumentSearch(symbol.trim(), 20, 'stock,etf'),
    enabled: symbol.trim().length > 0,
    staleTime: 30_000,
  })
  const symbolOptions: SuggestOption[] = (symbolSearch.data?.results ?? []).map(r => {
    const b = boardTag(r.symbol)
    return {
      value: r.symbol,
      label: r.name || r.code || r.symbol,
      left: r.symbol,
      hint: b ? <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] leading-none', b.color)}>{b.label}</span> : null,
    }
  })

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-sm font-medium">下单</div>
      <div className="mt-3 space-y-3">
        <div>
          <label className="text-[11px] text-muted">代码 (代码 / 中文名 / 拼音首字母联想)</label>
          <SuggestInput
            value={symbol}
            onChange={setSymbol}
            options={symbolOptions}
            onSelect={opt => setSymbol(opt.value)}
            loading={symbolSearch.isFetching}
            placeholder="600519.SH / 茅台 / MZ"
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm uppercase outline-none focus:border-accent/50"
          />
        </div>
        <div className="flex gap-2">
          {(['buy', 'sell'] as const).map(s => (
            <button
              key={s}
              onClick={() => setSide(s)}
              className={cn(
                'flex-1 rounded-btn border py-1.5 text-xs font-medium transition-all duration-150 ease-smooth',
                side === s
                  ? s === 'buy'
                    ? 'border-bull/50 bg-bull/10 text-bull shadow-[0_0_0_1px_rgba(240,68,56,0.08)]'
                    : 'border-bear/50 bg-bear/10 text-bear shadow-[0_0_0_1px_rgba(18,183,106,0.08)]'
                  : 'border-border text-muted hover:border-border hover:bg-elevated/60 hover:text-secondary',
              )}
            >
              {s === 'buy' ? '买入' : '卖出'}
            </button>
          ))}
        </div>
        <div>
          <div className="flex items-center justify-between">
            <label className="text-[11px] text-muted">{qtyMode === 'qty' ? '数量 (股, 100 的整数倍)' : '金额 (元, 按最近收盘价折算)'}</label>
            <div className="flex gap-1 text-[10px]">
              {(['qty', 'amount'] as const).map(mo => (
                <button
                  key={mo}
                  onClick={() => setQtyMode(mo)}
                  className={cn('rounded px-1.5 py-px transition-colors', qtyMode === mo ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                >
                  {mo === 'qty' ? '按数量' : '按金额'}
                </button>
              ))}
            </div>
          </div>
          <input
            type="number"
            value={qtyMode === 'qty' ? qty : amount}
            onChange={e => (qtyMode === 'qty' ? setQty(e.target.value) : setAmount(e.target.value))}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50"
          />
        </div>
        <div>
          <label className="text-[11px] text-muted">订单类型</label>
          <div className="mt-1 flex gap-1.5">
            {(Object.keys(ORDER_TYPE_LABEL) as Array<'market' | 'next_open' | 'close'>).map(t => (
              <button
                key={t}
                onClick={() => setOrderType(t)}
                title={t === 'market' ? '盘中按最新快照价成交; ETF 即时单自动转次日开盘' : undefined}
                className={cn(
                  'flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary',
                )}
              >
                {ORDER_TYPE_LABEL[t]}
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={() => canSubmit && m.mutate()}
          disabled={!canSubmit || m.isPending}
          className={cn(
            'w-full rounded-btn py-2 text-sm font-medium text-white transition-all duration-150 ease-smooth disabled:opacity-40 disabled:cursor-not-allowed',
            side === 'buy' ? 'bg-bull hover:bg-bull/85' : 'bg-bear hover:bg-bear/85',
          )}
        >
          {m.isPending ? '提交中…' : `${side === 'buy' ? '买入' : '卖出'} ${symbol.trim().toUpperCase() || ''}`}
        </button>
        {msg && (
          <div className={cn(
            'rounded-btn border px-2 py-1.5 text-[11px]',
            msg.ok
              ? side === 'buy' ? 'border-bull/20 bg-bull/[0.06] text-bull' : 'border-bear/20 bg-bear/[0.06] text-bear'
              : 'border-danger/20 bg-danger/[0.06] text-danger',
          )}>
            {msg.text}
          </div>
        )}
      </div>
    </div>
  )
}

/** 策略候选对比 (V3): 模拟盘统计 vs 回测候选 (回测报告的持久化标量摘要, 口径与回测对齐)。
 * 单位口径: 候选 metrics 为小数 (fmtPct 渲染), 模拟盘统计为百分数原值 — 各自按己方单位渲染, 不做数值换算。 */
interface PaperCompareStats {
  total_return_pct: number | null
  annual_pct: number | null
  max_drawdown: number | null
  win_rate: number | null
  n_trades: number | null
  profit_loss_ratio: number | null
}

function CandidateCompareCard({ paper }: { paper: PaperCompareStats }) {
  const candsQ = useQuery({ queryKey: QK.backtestCandidates, queryFn: api.backtestCandidates })
  const candidates = (candsQ.data?.items ?? []).filter(c => c.kind === 'strategy')
  const [selId, setSelId] = useState('')
  const sel = candidates.find(c => c.id === selId) ?? candidates[0]
  const m = sel?.metrics ?? {}
  const paperPct = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
  const candPct = (v: number | null | undefined) => (v == null ? '—' : fmtPct(v))
  const num = (v: number | null | undefined, digits = 0) => (v == null ? '—' : v.toFixed(digits))
  const rows: Array<[string, string, string]> = [
    ['累计收益', paperPct(paper.total_return_pct), candPct(m.total_return)],
    ['年化收益', paperPct(paper.annual_pct), candPct(m.annual_return)],
    ['最大回撤', paper.max_drawdown != null ? `-${paper.max_drawdown.toFixed(2)}%` : '—', candPct(m.max_drawdown)],
    ['胜率', paper.win_rate != null ? `${paper.win_rate.toFixed(1)}%` : '—', candPct(m.win_rate)],
    ['交易次数', num(paper.n_trades), num(m.n_trades)],
    ['盈亏比', num(paper.profit_loss_ratio, 2), num(m.profit_factor, 2)],
    ['夏普比率', '—', num(m.sharpe, 2)],
  ]
  return (
    <div className="rounded-card border border-border/60 bg-surface p-3">
      <div className="flex items-center gap-2">
        <div className="shrink-0 text-sm font-medium">策略对比</div>
        {candidates.length > 0 ? (
          <select
            value={sel?.id ?? ''}
            onChange={e => setSelId(e.target.value)}
            className="min-w-0 flex-1 rounded-btn border border-border bg-base px-1.5 py-1 text-xs text-secondary"
            title="选择要对比的策略候选"
          >
            {candidates.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        ) : (
          <span className="text-[10px] text-muted">暂无策略候选</span>
        )}
      </div>
      {sel ? (
        <table className="mt-2 w-full text-[11px]">
          <thead>
            <tr className="text-muted">
              <th className="py-0.5 text-left font-normal">指标</th>
              <th className="py-0.5 text-right font-normal">模拟盘</th>
              <th className="max-w-0 truncate py-0.5 text-right font-normal" title={sel.name}>{sel.name} (回测)</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map(([label, p, c]) => (
              <tr key={label} className="border-t border-border/40">
                <td className="py-1 font-sans text-muted">{label}</td>
                <td className="py-1 text-right text-secondary">{p}</td>
                <td className="py-1 text-right text-secondary">{c}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="mt-2 text-[11px] leading-relaxed text-muted">
          在策略页保存回测候选后, 可与模拟盘同口径对比 (累计/年化/回撤/胜率等)。
        </div>
      )}
    </div>
  )
}

/** 自动跟单规则列表 + 新建表单 (V2): 监控事件 → 自动下单 (按账户隔离) */
// ================================================================
// 费用与撮合设置 (当前账户弹窗): 佣金/印花税/滑点 + 涨跌停排队。
// 只影响之后的新成交, 不重算历史台账 (口径与回测费用模型一致)。
// ================================================================
function FeeSettingsModal({ accId, fees, queue, onSaved, onClose }: {
  accId: string
  fees: { commission_pct: number; stamp_tax_pct: number; slippage_bps: number }
  queue: boolean
  onSaved: () => void
  onClose: () => void
}) {
  const [commissionWan, setCommissionWan] = useState((fees.commission_pct * 10000).toFixed(2))
  const [stampQian, setStampQian] = useState((fees.stamp_tax_pct * 1000).toFixed(2))
  const [slippageBps, setSlippageBps] = useState(String(fees.slippage_bps))
  const [queueOn, setQueueOn] = useState(queue)
  const [err, setErr] = useState<string | null>(null)
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: () => api.paperSettings({
      commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
      stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
      slippage_bps: Number(slippageBps),
      queue_limit_orders: queueOn,
    }, accId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.paperAll })
      onSaved()
    },
    onError: e => setErr(String((e as Error).message)),
  })
  // 与后端 update_settings 的范围校验一致 (前端先行提示)
  const valid = Number(commissionWan) >= 0 && Number(commissionWan) <= 100
    && Number(stampQian) >= 0 && Number(stampQian) <= 50
    && Number(slippageBps) >= 0 && Number(slippageBps) <= 200
  const inputCls = 'mt-1 w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  return (
    <Modal onClose={onClose} labelledBy="fee-settings-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <Settings className="h-4 w-4 text-accent" />
          <h3 id="fee-settings-title" className="text-sm font-semibold text-foreground">
            费用与撮合设置
            <span className="ml-1.5 font-mono text-[10px] text-muted">{accId}</span>
          </h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          仅影响之后的新成交, 已有台账与统计不重算。口径与回测费用模型一致。
        </p>
        <div className="mt-4 grid grid-cols-3 gap-2">
          <div>
            <label className="block text-[11px] text-muted">佣金 (万)</label>
            <input type="number" step="0.1" min="0" max="100" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label className="block text-[11px] text-muted">印花税 (千, 卖出)</label>
            <input type="number" step="0.1" min="0" max="50" value={stampQian} onChange={e => setStampQian(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label className="block text-[11px] text-muted">滑点 (bps)</label>
            <input type="number" step="1" min="0" max="200" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={inputCls} />
          </div>
        </div>
        <label className="mt-4 flex cursor-pointer items-center justify-between rounded-btn border border-border bg-base px-3 py-2">
          <span className="text-xs text-secondary">
            涨跌停排队
            <span className="ml-1 text-[10px] text-muted">触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)</span>
          </span>
          <input type="checkbox" checked={queueOn} onChange={e => setQueueOn(e.target.checked)} className="h-4 w-4 shrink-0" />
        </label>
        <div className="mt-4 flex gap-2">
          <button
            onClick={() => m.mutate()}
            disabled={!valid || m.isPending}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {m.isPending ? '保存中…' : '保存'}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">
            取消
          </button>
        </div>
        {err && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{err}</div>}
      </div>
    </Modal>
  )
}

// ================================================================
// 多账户横向对比弹窗: 归一化净值叠加图 + 指标对比表 (行=指标, 列=账户)。
// ================================================================
const COMPARE_COLORS = ['#3B82F6', '#F59E0B', '#10B981', '#EF4444', '#8B5CF6', '#06B6D4', '#EC4899', '#84CC16']

function CompareChart({ rows }: { rows: PaperCompareRow[] }) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!elRef.current) return
    chartRef.current = echarts.init(elRef.current, undefined, { renderer: 'canvas' })
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    const observer = new MutationObserver(() => {
      const darkNow = document.documentElement.classList.contains('dark')
      const axisColor = darkNow ? '#8E8E96' : '#52525B'
      const splitColor = darkNow ? '#353539' : '#E4E4E7'
      chartRef.current?.setOption({
        xAxis: { axisLine: { lineStyle: { color: splitColor } }, axisLabel: { color: axisColor } },
        yAxis: { axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: splitColor } } },
      })
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => {
      window.removeEventListener('resize', onResize)
      observer.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const dark = document.documentElement.classList.contains('dark')
    const axisColor = dark ? '#8E8E96' : '#52525B'
    const splitColor = dark ? '#353539' : '#E4E4E7'
    chart.setOption({
      grid: { left: 52, right: 16, top: 30, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => v?.toFixed(4),
      },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: axisColor, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
        splitLine: { lineStyle: { color: splitColor } },
      },
      legend: { top: 0, right: 0, textStyle: { color: axisColor, fontSize: 10 }, itemWidth: 14 },
      series: rows.map((r, i) => ({
        name: r.name,
        type: 'line' as const,
        showSymbol: false,
        // 归一化: 定版净值 / 首日净值 (不同本金也能公平对比)
        data: r.nav.map(n => [n.date, Number((n.nav / (r.nav[0]?.nav || 1)).toFixed(4))]),
        lineStyle: { color: COMPARE_COLORS[i % COMPARE_COLORS.length], width: 2 },
      })),
    })
  }, [rows])

  return <div ref={elRef} className="h-56 w-full" />
}

function AccountCompareModal({ onClose }: { onClose: () => void }) {
  const cmpQ = useQuery({ queryKey: QK.paperCompare, queryFn: api.paperCompare })
  const rows = cmpQ.data?.accounts ?? []
  const chartRows = rows.filter(r => r.nav.length > 0)
  // 收益率最高的账户列头标记「领先」(单账户不标)
  const bestAccount = rows.length > 1
    ? rows.reduce((a, b) => ((b.pnl_pct ?? -1e9) > (a.pnl_pct ?? -1e9) ? b : a))
    : null
  const metrics: Array<{ label: string; render: (r: PaperCompareRow) => ReactNode }> = [
    {
      label: '累计收益率',
      render: r => <span className={cn('font-semibold', priceColorClass((r.pnl_pct ?? 0) / 100))}>{fmtPct((r.pnl_pct ?? 0) / 100)}</span>,
    },
    { label: '总资产', render: r => fmtMoney(r.total) },
    {
      label: '累计盈亏',
      render: r => <span className={priceColorClass((r.total_pnl ?? 0) / (r.initial_cash || 1))}>{fmtMoney(r.total_pnl)}</span>,
    },
    { label: '最大回撤', render: r => (r.max_drawdown != null ? <span className="text-warning">{fmtPct(r.max_drawdown / 100)}</span> : '—') },
    { label: '胜率', render: r => `${r.win_rate.toFixed(1)}%` },
    { label: '盈亏比', render: r => (r.profit_loss_ratio != null ? r.profit_loss_ratio.toFixed(2) : '—') },
    { label: '回合数', render: r => String(r.rounds) },
    { label: '平均持有', render: r => `${r.avg_holding_days}天` },
    { label: '现金', render: r => fmtMoney(r.cash) },
    { label: '持仓市值', render: r => fmtMoney(r.market_value) },
    { label: '初始资金', render: r => fmtMoney(r.initial_cash, 0) },
    {
      label: '费用',
      render: r => `${(r.fees.commission_pct * 10000).toFixed(1)}‱ · ${(r.fees.stamp_tax_pct * 1000).toFixed(1)}‰ · ${r.fees.slippage_bps}bps`,
    },
  ]
  return (
    <Modal onClose={onClose} labelledBy="compare-title" panelClassName="w-[94vw] max-w-4xl bg-surface border border-border rounded-card shadow-xl">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <GitCompare className="h-4 w-4 text-accent" />
          <h3 id="compare-title" className="text-sm font-semibold text-foreground">账户横向对比</h3>
          <button
            onClick={() => cmpQ.refetch()}
            className="ml-auto rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
          >
            {cmpQ.isFetching ? '刷新中…' : '刷新'}
          </button>
        </div>
        {cmpQ.isLoading ? (
          <div className="py-12 text-center text-xs text-muted">加载中…</div>
        ) : rows.length === 0 ? (
          <div className="py-12 text-center text-xs text-muted">创建账户后即可在此横向对比</div>
        ) : (
          <div className="mt-3 space-y-3">
            {chartRows.length > 0 && (
              <div>
                <div className="text-[11px] text-muted">归一化净值 (起点 = 1, 不同本金公平对比)</div>
                <CompareChart rows={chartRows} />
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-left text-[11px] text-muted">
                    <th className="px-2 py-1.5 font-medium">指标</th>
                    {rows.map(r => (
                      <th key={r.account} className="px-2 py-1.5 font-medium">
                        <span className={cn(r === bestAccount ? 'text-accent' : 'text-foreground')}>{r.name}</span>
                        {r === bestAccount && <span className="ml-1 rounded bg-accent/15 px-1 py-px text-[9px] text-accent">领先</span>}
                        {r.status === 'frozen' && <span className="ml-1 rounded-full bg-warning/15 px-1 py-px text-[9px] text-warning">冻结</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {metrics.map(mrow => (
                    <tr key={mrow.label} className="border-b border-border/50">
                      <td className="px-2 py-1.5 text-muted">{mrow.label}</td>
                      {rows.map(r => (
                        <td key={r.account} className="px-2 py-1.5 font-mono tabular text-foreground">{mrow.render(r)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}

function AutoRulesPanel({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const rulesQ = useQuery({ queryKey: QK.paperAutoRules(acc), queryFn: () => api.paperAutoRules(acc) })
  const rules = rulesQ.data?.rules ?? []
  const [creating, setCreating] = useState(false)

  const invalidate = () => qc.invalidateQueries({ queryKey: QK.paperAutoRules(acc) })

  const toggleM = useMutation({
    mutationFn: (args: { id: string; enabled: boolean }) => api.paperAutoRuleSetEnabled(args.id, args.enabled, acc),
    onSuccess: invalidate,
  })
  const deleteM = useMutation({
    mutationFn: (id: string) => api.paperAutoRuleDelete(id, acc),
    onSuccess: invalidate,
  })

  if (creating) {
    return <AutoRuleForm acc={acc} onDone={() => { setCreating(false); invalidate() }} onCancel={() => setCreating(false)} />
  }
  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">自动跟单规则</div>
        <button onClick={() => setCreating(true)} className="flex items-center gap-1 rounded-btn bg-accent/10 px-2.5 py-1 text-xs text-accent transition-colors hover:bg-accent/20">
          <Plus className="h-3 w-3" /> 新建规则
        </button>
      </div>
      <div className="mt-1 text-[11px] leading-relaxed text-muted">
        监控规则/策略触发时自动按规则下单 (默认次日开盘成交), 同标的 {`冷却 N 天`}内不重复触发
      </div>
      {rules.length === 0 ? (
        <div className="py-8 text-center text-xs text-muted">暂无规则 — 新建一条, 让策略信号自动进入模拟盘</div>
      ) : (
        <div className="mt-2 space-y-1">
          {rules.map(r => (
            <div key={r.id} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs transition-colors hover:bg-elevated/40">
              <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', r.enabled ? 'bg-bear' : 'bg-muted')} />
              <span className="w-28 shrink-0 truncate font-medium">{r.name}</span>
              <span className="w-20 shrink-0 text-muted">{r.match_kind === 'strategy' ? '跟策略' : '跟规则'}</span>
              <span className="w-32 shrink-0 truncate font-mono text-[11px] text-muted" title={r.match_id}>{r.match_id}</span>
              <span className={cn('w-8 shrink-0 font-medium', r.side === 'buy' ? 'text-bull' : 'text-bear')}>{r.side === 'buy' ? '买' : '卖'}</span>
              <span className="w-24 shrink-0 font-mono text-[11px] text-muted">
                {r.size_mode === 'fixed_amount' ? fmtMoney(r.size_value, 0) : `${r.size_value}% 权益`}
              </span>
              <span className="w-16 shrink-0 text-[11px] text-muted">{ORDER_TYPE_LABEL[r.order_type]} · 冷却{r.cooldown_days}天</span>
              <button
                onClick={() => toggleM.mutate({ id: r.id, enabled: !r.enabled })}
                className={cn('ml-auto shrink-0 rounded-btn px-2 py-0.5 text-[10px] transition-colors',
                  r.enabled ? 'bg-bear/10 text-bear hover:bg-bear/20' : 'bg-elevated text-muted hover:text-secondary')}
              >
                {r.enabled ? '停用' : '启用'}
              </button>
              <button onClick={() => deleteM.mutate(r.id)} className="shrink-0 rounded p-0.5 text-muted hover:text-danger" title="删除规则">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function AutoRuleForm({ acc, onDone, onCancel }: { acc: string; onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState('')
  const [matchKind, setMatchKind] = useState<'strategy' | 'rule'>('strategy')
  const [matchId, setMatchId] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('fixed_amount')
  const [sizeValue, setSizeValue] = useState('10000')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [msg, setMsg] = useState<string | null>(null)

  const m = useMutation({
    mutationFn: () =>
      api.paperAutoRuleCreate({
        name: name.trim(),
        match_kind: matchKind,
        match_id: matchId.trim(),
        side,
        size_mode: sizeMode,
        size_value: Number(sizeValue),
        order_type: orderType,
        cooldown_days: Number(cooldown),
        enabled: true,
      }, acc),
    onSuccess: onDone,
    onError: e => setMsg(String((e as Error).message)),
  })

  const valid = name.trim() && matchId.trim() && Number(sizeValue) > 0

  // ID 联想: 跟策略 → 策略引擎列表; 跟监控规则 → 监控规则列表。本地按中文名/ID 过滤。
  const strategiesQ = useQuery({
    queryKey: QK.screenerStrategies('any', 'all'),
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({
    queryKey: QK.monitorRules,
    queryFn: api.monitorRulesList,
    staleTime: 30_000,
  })
  const idQuery = matchId.trim().toLowerCase()
  const idOptions: SuggestOption[] = (() => {
    if (matchKind === 'strategy') {
      return (strategiesQ.data?.presets ?? [])
        .filter(s => !idQuery || s.name.toLowerCase().includes(idQuery) || s.id.toLowerCase().includes(idQuery))
        .slice(0, 8)
        .map(s => ({
          value: s.id,
          label: s.name || s.id,
          hint: <span className="shrink-0 font-mono text-[10px] text-muted">{s.id}</span>,
        }))
    }
    return (rulesQ.data?.rules ?? [])
      .filter(r => !idQuery || r.name.toLowerCase().includes(idQuery) || r.id.toLowerCase().includes(idQuery))
      .slice(0, 8)
      .map(r => ({
        value: r.id,
        label: r.name || r.id,
        hint: <span className={cn('shrink-0 font-mono text-[10px]', r.enabled ? 'text-muted' : 'text-warning/70')}>{r.id}</span>,
      }))
  })()

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-sm font-medium">新建自动跟单规则</div>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <label className="text-[11px] text-muted">规则名称</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="如: 跟单前高突破"
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="text-[11px] text-muted">跟什么</label>
          <div className="mt-1 flex gap-1.5">
            {(['strategy', 'rule'] as const).map(k => (
              <button key={k} onClick={() => { setMatchKind(k); setMatchId('') }}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  matchKind === k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                {k === 'strategy' ? '跟策略' : '跟监控规则'}
              </button>
            ))}
          </div>
        </div>
        <div className="md:col-span-2">
          <label className="text-[11px] text-muted">{matchKind === 'strategy' ? '策略 (输入中文名 / ID 联想, 留空聚焦看全部)' : '监控规则 (输入名称 / ID 联想)'}</label>
          <SuggestInput
            value={matchId}
            onChange={setMatchId}
            options={idOptions}
            onSelect={opt => setMatchId(opt.value)}
            loading={matchKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
            placeholder={matchKind === 'strategy' ? '如: 前高突破 / sg_...' : '如: 高位放量 / mrule_...'}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50"
          />
        </div>
        <div>
          <label className="text-[11px] text-muted">方向</label>
          <div className="mt-1 flex gap-1.5">
            {(['buy', 'sell'] as const).map(sd => (
              <button key={sd} onClick={() => setSide(sd)}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] font-medium transition-colors',
                  side === sd ? (sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted')}>
                {sd === 'buy' ? '买入' : '卖出'}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="text-[11px] text-muted">仓位</label>
          <div className="mt-1 flex gap-1.5">
            <button onClick={() => setSizeMode('fixed_amount')}
              className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'fixed_amount' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>固定金额</button>
            <button onClick={() => setSizeMode('pct_equity')}
              className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'pct_equity' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>权益 %</button>
          </div>
        </div>
        <div>
          <label className="text-[11px] text-muted">{sizeMode === 'fixed_amount' ? '每笔金额 (元)' : '每笔占权益 %'}</label>
          <input type="number" value={sizeValue} onChange={e => setSizeValue(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="text-[11px] text-muted">订单类型</label>
          <div className="mt-1 flex gap-1.5">
            {(['next_open', 'close', 'market'] as const).map(t => (
              <button key={t} onClick={() => setOrderType(t)}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                {ORDER_TYPE_LABEL[t]}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="text-[11px] text-muted">冷却天数 (同标的)</label>
          <input type="number" value={cooldown} onChange={e => setCooldown(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
      </div>
      <div className="mt-3 flex gap-2">
        <button onClick={() => valid && m.mutate()} disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity disabled:opacity-40">
          {m.isPending ? '创建中…' : '创建规则'}
        </button>
        <button onClick={onCancel} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:text-foreground">取消</button>
      </div>
      {msg && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{msg}</div>}
    </div>
  )
}

export function Paper() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'orders' | 'trades'>('orders')
  const [accId, setAccIdState] = useState(() => localStorage.getItem(ACC_STORAGE_KEY) || 'default')
  // 新建账户草稿 id: 非空 = 正在创建。点「+」只进入草稿态, 不动 accId / localStorage,
  // 取消即丢弃; 旧实现直接把生成的 id 写进 accId, 刷新后卡在无主向导上无法退出。
  const [draftId, setDraftId] = useState<string | null>(null)
  const [feeOpen, setFeeOpen] = useState(false)
  const [compareOpen, setCompareOpen] = useState(false)
  const setAccId = (id: string) => {
    localStorage.setItem(ACC_STORAGE_KEY, id)
    setAccIdState(id)
  }

  const accountsQ = useQuery({ queryKey: QK.paperAccounts, queryFn: api.paperAccounts })
  const overviewQ = useQuery({ queryKey: QK.paperOverview(accId), queryFn: () => api.paperOverview(accId) })
  const ordersQ = useQuery({ queryKey: QK.paperOrders(accId), queryFn: () => api.paperOrders(undefined, accId) })
  const tradesQ = useQuery({ queryKey: QK.paperTrades(accId), queryFn: () => api.paperTrades(accId) })
  const navQ = useQuery({ queryKey: QK.paperNav(accId), queryFn: () => api.paperNav(accId) })
  const statsQ = useQuery({ queryKey: QK.paperStats(accId), queryFn: () => api.paperStats(accId) })

  // 'paper' 前缀兜底失效: 覆盖全部账户的全部查询 (订单变动可能影响净值/统计)
  const invalidateAll = () => qc.invalidateQueries({ queryKey: QK.paperAll })

  const cancelM = useMutation({
    mutationFn: (id: string) => api.paperOrderCancel(id, accId),
    onSuccess: invalidateAll,
  })
  const queueM = useMutation({
    mutationFn: (on: boolean) => api.paperSettings({ queue_limit_orders: on }, accId),
    onSuccess: invalidateAll,
  })

  if (overviewQ.isLoading) {
    return <div className="p-5 text-sm text-muted">加载中…</div>
  }
  const ov = overviewQ.data
  const accounts = accountsQ.data?.accounts ?? []
  if (draftId !== null || !ov?.initialized) {
    // 已有其他账户 → 顶部保留账户切换 (切走即放弃创建) + 可取消; 一个账户都没有 → 纯向导
    const cancellable = accounts.length > 0
    const selectedId = accounts.some(a => a.id === accId) ? accId : accounts[0]?.id ?? accId
    const cancelCreate = () => {
      setDraftId(null)
      // 选中态是残留 id 时回到第一个既有账户 (正常新建流程 accId 本就有效, 此行不触发)
      if (!accounts.some(a => a.id === accId)) setAccId(accounts[0].id)
    }
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <PageHeader
          title="模拟盘"
          subtitle="虚拟账户 · 用假钱验证你的策略"
          right={cancellable ? (
            <div className="flex items-center gap-1.5">
              <select
                value={selectedId}
                onChange={e => { setDraftId(null); setAccId(e.target.value) }}
                className="max-w-36 rounded-btn border border-border bg-surface px-2 py-1 text-xs outline-none focus:border-accent/50"
                title="切换虚拟账户 (切换即放弃本次创建)"
              >
                {accounts.map(a => (
                  <option key={a.id} value={a.id}>{a.name || a.id}</option>
                ))}
              </select>
              <button
                onClick={cancelCreate}
                className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger"
                title="放弃创建, 返回原账户"
              >
                取消创建
              </button>
            </div>
          ) : undefined}
        />
        <div className="flex-1 overflow-y-auto">
          <SetupCard
            accId={draftId ?? accId}
            onDone={createdId => {
              setDraftId(null)
              setAccId(createdId)
              invalidateAll()
            }}
            onCancel={cancellable ? cancelCreate : undefined}
          />
        </div>
      </div>
    )
  }

  const holdings = ov.holdings ?? []
  const orders = (ordersQ.data?.orders ?? []).filter(o => o.status === 'pending' || o.status === 'expired' || o.status === 'cancelled').slice(0, 20)
  const trades: PaperFill[] = (tradesQ.data?.fills ?? []).slice(0, 30)
  const allFills: PaperFill[] = tradesQ.data?.fills ?? []
  const nav = navQ.data?.nav ?? []
  const stats = statsQ.data
  const pnlPct = ov.initial_cash && ov.initial_cash > 0 ? ((ov.total_pnl ?? 0) / ov.initial_cash) * 100 : 0

  /** 导出全部成交台账 CSV (带 BOM, Excel 可直接打开); 口径与页面「成交台账」一致 */
  const exportTradesCsv = () => {
    if (allFills.length === 0) return
    const esc = (v: string | number | null | undefined) => {
      const s = v == null ? '' : String(v)
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
    }
    const lines = ['日期,代码,方向,数量,成交价,费用,类型,订单号']
    for (const f of allFills) {
      lines.push([
        f.date, f.symbol,
        f.kind === 'corp_action' ? '除权' : f.side === 'buy' ? '买入' : '卖出',
        f.qty ?? '', f.price ?? '', f.fee ?? '',
        f.kind ?? 'fill', f.order_id ?? '',
      ].map(esc).join(','))
    }
    const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `模拟盘成交台账_${accId}_${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="模拟盘"
        subtitle="虚拟账户 · 用假钱验证你的策略"
        titleExtra={
          <>
            {ov.status === 'frozen' && <span className="rounded-full bg-warning/15 px-2 py-0.5 text-[10px] text-warning">已冻结</span>}
            {ov.queue_limit_orders && (
              <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[10px] text-accent" title="触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)">
                涨跌停排队
              </span>
            )}
          </>
        }
        right={
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1.5">
              <select
                value={accId}
                onChange={e => setAccId(e.target.value)}
                className="max-w-36 rounded-btn border border-border bg-surface px-2 py-1 text-xs outline-none focus:border-accent/50"
                title="切换虚拟账户 (数据相互隔离)"
              >
                {(accounts.some(a => a.id === accId) ? accounts : [...accounts, { id: accId, name: accId }]).map(a => (
                  <option key={a.id} value={a.id}>{a.name || a.id}</option>
                ))}
              </select>
              <button
                onClick={() => setDraftId(genAccountId())}
                className="flex items-center gap-0.5 rounded-btn border border-border px-1.5 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
                title="新建虚拟账户"
              >
                <Plus className="h-3 w-3" />
              </button>
            </div>
            <button
              onClick={() => setFeeOpen(true)}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title={`佣金 ${((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ (最低5元) · 印花税 ${((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰ 仅卖出 · 滑点 ${ov.fees?.slippage_bps ?? 0}bps — 点击调整`}
            >
              <Settings className="h-3 w-3" />
              佣金 {((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ · 印花税 {((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰ · 滑点 {ov.fees?.slippage_bps ?? 0}bps
            </button>
            <button
              onClick={() => setCompareOpen(true)}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title="多账户指标横向对比 + 归一化净值叠加"
            >
              <GitCompare className="h-3 w-3" />
              账户对比
            </button>
            <button
              onClick={() => api.paperFreeze(ov.status !== 'frozen', accId).then(invalidateAll)}
              className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-warning/40 hover:text-warning"
            >
              {ov.status === 'frozen' ? '解冻账户' : '冻结账户'}
            </button>
          </div>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {/* 总览卡片 */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="总资产 (虚拟)" value={fmtMoney(ov.total)} icon={Wallet} iconCls="text-accent" />
          <StatCard label="现金" value={fmtMoney(ov.cash)} icon={Banknote} iconCls="text-muted" />
          <StatCard label="持仓市值" value={fmtMoney(ov.market_value)} icon={PieChart} iconCls="text-muted" />
          <StatCard
            label="累计盈亏"
            value={`${(ov.total_pnl ?? 0) >= 0 ? '+' : ''}${fmtMoney(ov.total_pnl)} (${fmtPct(pnlPct / 100)})`}
            valueClass={priceColorClass((ov.total_pnl ?? 0) / (ov.initial_cash || 1))}
            icon={TrendingUp}
            iconCls={priceColorClass((ov.total_pnl ?? 0) / (ov.initial_cash || 1))}
          />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[1fr_320px]">
          {/* 左列: 净值 + 持仓 + 流水 */}
          <div className="min-w-0 space-y-4">
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="text-sm font-medium">净值曲线 <span className="ml-1 text-[10px] text-muted">按交易日收盘定版</span></div>
              {nav.length > 0 ? <NavChart nav={nav} /> : <div className="py-10 text-center text-xs text-muted">暂无定版净值 — 交易日盘后管道自动结算</div>}
            </div>

            <div className="rounded-card border border-border bg-surface p-4">
              <div className="text-sm font-medium">当前持仓</div>
              {holdings.length === 0 ? (
                <div className="py-8 text-center text-xs text-muted">暂无持仓 — 右侧下单或等自动跟单触发</div>
              ) : (
                <table className="mt-2 w-full text-xs">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="text-left text-[10px] text-muted">
                      <th className="py-1.5 font-normal">代码</th>
                      <th className="py-1.5 text-right font-normal">数量</th>
                      <th className="py-1.5 text-right font-normal">可卖(T+1)</th>
                      <th className="py-1.5 text-right font-normal">成本</th>
                      <th className="py-1.5 text-right font-normal">现价</th>
                      <th className="py-1.5 text-right font-normal">市值</th>
                      <th className="py-1.5 text-right font-normal">盈亏</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {holdings.map(h => (
                      <tr key={h.symbol} className="border-t border-border/50 transition-colors hover:bg-elevated/40">
                        <td className="py-1.5 font-sans">{h.symbol}</td>
                        <td className="py-1.5 text-right">{h.qty}</td>
                        <td className="py-1.5 text-right text-muted">{h.available_qty}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.avg_cost, 3)}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.last_price, 3)}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.market_value, 0)}</td>
                        <td className={cn('py-1.5 text-right', priceColorClass(h.pnl))}>
                          {(h.pnl >= 0 ? '+' : '') + fmtMoney(h.pnl, 0)} ({fmtPct(h.pnl_pct / 100)})
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* 订单 / 成交流水 */}
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="flex items-center gap-2">
                {(['orders', 'trades'] as const).map(t => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={cn('rounded-btn px-2.5 py-1 text-xs transition-colors', tab === t ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                  >
                    {t === 'orders' ? '订单' : '成交台账'}
                  </button>
                ))}
                {tab === 'trades' && (
                  <button
                    onClick={exportTradesCsv}
                    disabled={allFills.length === 0}
                    className="ml-auto rounded-btn border border-border px-2 py-1 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
                    title="导出全部成交台账 (CSV, Excel 可直接打开)"
                  >
                    导出 CSV
                  </button>
                )}
                {stats && (
                  <span className="ml-auto text-[11px] text-muted" title="回合为 FIFO 配对的完整买卖; 回撤按定版净值序列">
                    回合 {stats.rounds} · 胜率 {stats.win_rate}% · 盈亏比 {stats.profit_loss_ratio ?? '--'} · 均持 {stats.avg_holding_days}天 · 回撤{' '}
                    {stats.max_drawdown != null ? `-${stats.max_drawdown}%` : '--'} ·{' '}
                    <span className={cn('font-mono', priceColorClass(stats.realized_pnl))}>
                      已实现 {stats.realized_pnl >= 0 ? '+' : ''}{fmtMoney(stats.realized_pnl, 0)}
                    </span>
                  </span>
                )}
              </div>
              {tab === 'orders' ? (
                <div className="mt-2 space-y-1">
                  {orders.length === 0 && <div className="py-6 text-center text-xs text-muted">暂无订单</div>}
                  {orders.map((o: PaperOrder) => (
                    <div key={o.id} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs hover:bg-elevated/50">
                      <span className={cn('w-8 shrink-0 font-medium', o.side === 'buy' ? 'text-bull' : 'text-bear')}>
                        {o.side === 'buy' ? '买入' : '卖出'}
                      </span>
                      <span className="w-24 shrink-0 font-mono">{o.symbol}</span>
                      <span className="w-16 shrink-0 font-mono text-right">{o.qty}</span>
                      <span className="w-16 shrink-0 text-muted">{ORDER_TYPE_LABEL[o.order_type]}</span>
                      <span className={cn(
                        'w-16 shrink-0 rounded-full px-1.5 py-px text-center text-[10px] leading-4',
                        o.status === 'filled' ? 'bg-bear/10 text-bear'
                          : o.status === 'expired' ? 'bg-warning/10 text-warning'
                          : o.status === 'cancelled' ? 'bg-elevated text-muted'
                          : 'bg-accent/10 text-accent',
                      )}>
                        {STATUS_LABEL[o.status]}
                      </span>
                      {o.status === 'pending' && o.postponed > 0 && (
                        <span className="shrink-0 rounded-full bg-accent/10 px-1.5 py-px text-[10px] leading-4 text-accent" title={`涨跌停排队重试中, 已顺延 ${o.postponed}/${3} 日`}>
                          排队 {o.postponed}/3
                        </span>
                      )}
                      <span className="min-w-0 flex-1 truncate text-[11px] text-muted" title={o.reason ?? undefined}>
                        {o.status === 'filled' && o.fill_price != null ? `@ ${fmtMoney(o.fill_price, 3)}` : o.reason ?? ''}
                      </span>
                      {o.status === 'pending' && (
                        <button onClick={() => cancelM.mutate(o.id)} className="shrink-0 rounded p-0.5 text-muted hover:text-foreground" title="撤单">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2 space-y-1">
                  {trades.length === 0 && <div className="py-6 text-center text-xs text-muted">暂无成交</div>}
                  {trades.map((f: PaperFill) => (
                    <div key={`${f.seq}-${f.order_id ?? 'corp'}`} className="flex items-center gap-2 rounded-btn px-2 py-1.5 font-mono text-xs hover:bg-elevated/50">
                      <span className="w-14 shrink-0 text-muted">{f.date}</span>
                      {f.kind === 'corp_action' ? (
                        <span className="text-accent">除权 ×{f.factor} ({f.symbol}: {f.qty_before} → )</span>
                      ) : (
                        <>
                          <span className={cn('w-8 shrink-0 font-sans font-medium', f.side === 'buy' ? 'text-bull' : 'text-bear')}>
                            {f.side === 'buy' ? '买入' : '卖出'}
                          </span>
                          <span className="w-24 shrink-0">{f.symbol}</span>
                          <span className="w-16 shrink-0 text-right">{f.qty}</span>
                          <span className="w-20 shrink-0 text-right">@ {fmtMoney(f.price, 3)}</span>
                          <span className="w-16 shrink-0 text-right text-muted">费 {fmtMoney(f.fee)}</span>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* 右列: 下单 + 自动跟单 + 策略对比 */}
          <div className="space-y-4">
            <OrderForm acc={accId} onDone={invalidateAll} />
            <AutoRulesPanel acc={accId} />
            <CandidateCompareCard paper={{
              total_return_pct: nav.length >= 2 && nav[0].nav > 0 ? (nav[nav.length - 1].nav / nav[0].nav - 1) * 100 : null,
              annual_pct: nav.length >= 2 && nav[0].nav > 0
                ? (Math.pow(nav[nav.length - 1].nav / nav[0].nav, 252 / nav.length) - 1) * 100
                : null,
              max_drawdown: stats?.max_drawdown ?? null,
              win_rate: stats?.win_rate ?? null,
              n_trades: allFills.filter(f => f.kind !== 'corp_action').length,
              profit_loss_ratio: stats?.profit_loss_ratio ?? null,
            }} />
            <div className="rounded-card border border-border/60 bg-base/40 p-3 text-[11px] leading-relaxed text-muted">
              <div className="font-medium text-secondary">撮合口径</div>
              <div className="mt-1">· 即时单: 交易时段按最新快照价 + 滑点成交</div>
              <div>· 次日开盘 / 当日收盘单: 盘后管道按真实开盘/收盘价成交</div>
              <div>· T+1: 当日买入次一交易日可卖</div>
              <div>· 停牌顺延 3 日自动过期; 除权按因子调整数量与成本, 台账留痕</div>
              <div className="mt-2 flex items-center justify-between border-t border-border/40 pt-2">
                <span title="开启后, 触及涨停的买入/触及跌停的卖出不再直接过期, 转次日开盘重试 (最多顺延 3 日)">
                  涨跌停排队次日重试
                </span>
                <button
                  onClick={() => queueM.mutate(!ov.queue_limit_orders)}
                  disabled={queueM.isPending}
                  className={cn(
                    'relative h-4 w-8 shrink-0 rounded-full transition-colors',
                    ov.queue_limit_orders ? 'bg-accent' : 'bg-border',
                  )}
                  aria-label="涨跌停排队次日重试开关"
                >
                  <span className={cn(
                    'absolute top-0.5 h-3 w-3 rounded-full bg-white shadow transition-all',
                    ov.queue_limit_orders ? 'left-[18px]' : 'left-0.5',
                  )} />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
      {feeOpen && ov.fees && (
        <FeeSettingsModal
          accId={accId}
          fees={ov.fees}
          queue={!!ov.queue_limit_orders}
          onSaved={() => setFeeOpen(false)}
          onClose={() => setFeeOpen(false)}
        />
      )}
      {compareOpen && <AccountCompareModal onClose={() => setCompareOpen(false)} />}
    </div>
  )
}
