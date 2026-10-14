/**
 * 模拟盘 (虚拟账户) — 多账户对比主视图 + 行展开账户操作。
 *
 * 对比 (默认): 全账户收益降序大列表 (每行一个账户, 同本金同费率口径),
 * 盘中快照就绪时总资产/收益显示实时估算 (估 徽标), 归一化净值叠加;
 * 行内直达冻结/删除 (强确认弹窗, 有持仓时后端先拒绝再显式强删);
 * 批量创建一次构造一批同规格账户各绑一条跟单规则 (策略直接跟实时行情模拟)。
 *
 * 行展开 = 完整账户操作: 总览/净值/持仓/订单 (全状态全量)/成交台账 (可导出
 * CSV)/回合明细 (FIFO)/下单 (卖出带 T+1 可卖提示)/跟单规则/费用/冻结/删除。
 *
 * 口径提示: 定版字段按交易日收盘结算; 盘中实时估算与 /overview 同口径。
 * 费用/滑点参数与回测引擎同名同默认值。
 * 多账户: 所有查询按账户隔离 (queryKey 前缀 'paper'), 切换即换一套数据。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Banknote, ChevronDown, ChevronUp, CircleDollarSign, GitCompare, History, ListChecks, PieChart, Plus, RefreshCw, Settings, ShieldAlert, SlidersHorizontal, Snowflake, Trash2, TrendingUp, Wallet, X, Zap } from 'lucide-react'
import { createPortal } from 'react-dom'
import { api, type PaperCompareRow, type PaperFill, type PaperHolding, type PaperOrder, type PaperRound } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useQuoteStatus } from '@/lib/useSharedQueries'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import type { FrontendExtension } from '@/extensions/types'
import { IconPaper } from '@/components/BrandIcons'

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

/** 新建账户默认值 (localStorage), 字段级优先级: 基础配置 (用户显式设置) > 上次创建记忆 > 内置默认。 */
const NEW_ACCOUNT_DEFAULTS_KEY = 'paper.newAccountDefaults'
const PAPER_BASE_DEFAULTS_KEY = 'paper.baseDefaults.v1'
type NewAccountDefaults = {
  cash?: string; commissionWan?: string; stampQian?: string; slippageBps?: string
}

function loadNewAccountDefaults(): Required<NewAccountDefaults> {
  let base: NewAccountDefaults = {}
  let last: NewAccountDefaults = {}
  try { base = JSON.parse(localStorage.getItem(PAPER_BASE_DEFAULTS_KEY) || '{}') } catch { /* 忽略坏数据 */ }
  try { last = JSON.parse(localStorage.getItem(NEW_ACCOUNT_DEFAULTS_KEY) || '{}') } catch { /* 忽略坏数据 */ }
  const src = { ...last, ...base }
  return {
    cash: src.cash ?? '1000000',
    commissionWan: src.commissionWan ?? '2.5',   // 万2.5
    stampQian: src.stampQian ?? '1',             // 千1 (仅卖出)
    slippageBps: src.slippageBps ?? '5',         // 5bps
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
  // 自动执行策略 (可选): 创建后立即绑一条自动跟单规则, 策略/监控规则触发信号时按下方参数自动下单
  const [autoKind, setAutoKind] = useState<'strategy' | 'rule'>('strategy')
  const [autoId, setAutoId] = useState('')
  const [autoName, setAutoName] = useState('')
  const [autoSearch, setAutoSearch] = useState('')   // 未选中时的搜索词 (选中后显示中文名)
  const [side, setSide] = useState<'buy' | 'sell' | 'auto'>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('pct_equity')
  const [sizeValue, setSizeValue] = useState('10')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('10')
  // 账户已建但规则绑定失败: 不静默吞掉 (账户已存在无法重提交), 行内警告 + 手动完成
  const [ruleWarn, setRuleWarn] = useState<string | null>(null)

  const strategiesQ = useQuery({
    queryKey: QK.screenerStrategies('any', 'all'),
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList, staleTime: 30_000 })

  const m = useMutation({
    mutationFn: async () => {
      // 账户名未填且已选策略/规则 → 自动用其名称 (不落到 "账户 acc_xxx")
      const autoLabel = autoId.trim() ? (autoName || autoId.trim()) : ''
      const r = await api.paperCreateAccount({
        initial_cash: Number(cash),
        account_id: accId,
        name: name.trim() || autoLabel || undefined,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
      })
      if (autoId.trim()) {
        try {
          await api.paperAutoRuleCreate({
            name: `跟${autoKind === 'strategy' ? '策略' : '规则'}·${autoName || autoId.trim()}`,
            match_kind: autoKind,
            match_id: autoId.trim(),
            side,
            size_mode: sizeMode,
            size_value: Number(sizeValue),
            order_type: orderType,
            cooldown_days: Number(cooldown),
            max_positions: Math.max(0, Math.round(Number(maxPositions) || 0)),
            enabled: true,
          }, r.account.id)
        } catch (err) {
          return { r, ruleError: String((err as Error).message || err) }
        }
      }
      return { r, ruleError: null as string | null }
    },
    onSuccess: ({ r, ruleError }) => {
      // 记住本次取值, 作为下次新建账户的默认
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({
          cash, commissionWan, stampQian, slippageBps,
        }))
      } catch { /* 存储不可用时静默 */ }
      if (ruleError) { setRuleWarn(ruleError); return }
      onDone(r.account.id)
    },
  })

  const autoQuery = (autoId ? (autoName || autoId) : autoSearch).trim().toLowerCase()
  const autoOptions: SuggestOption[] = autoKind === 'strategy'
    ? (strategiesQ.data?.presets ?? [])
        .filter(s => !autoQuery || s.name.toLowerCase().includes(autoQuery) || s.id.toLowerCase().includes(autoQuery) || (s.tags ?? []).some(t => t.toLowerCase().includes(autoQuery)))
        .map(s => ({
          value: s.id,
          label: s.name || s.id,
          desc: s.description || undefined,
          badges: [
            ...(s.timeframes ?? []).map(tf => tf === '1d' ? '日线' : tf === '1m' ? '分钟' : tf),
            ...(s.tags ?? []).slice(0, 2),
          ].slice(0, 3),
        }))
    : (rulesQ.data?.rules ?? [])
        .filter(r => !autoQuery || r.name.toLowerCase().includes(autoQuery) || r.id.toLowerCase().includes(autoQuery))
        .map(r => ({ value: r.id, label: r.name || r.id }))

  const valid = Number(cash) > 0
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0
    && (!autoId.trim() || Number(sizeValue) > 0)
  const segBtn = (active: boolean) => cn(
    'whitespace-nowrap rounded-btn border px-2 py-1 text-[11px] transition-colors',
    active ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary',
  )
  return (
    <div className="p-5">
      <div className="flex items-center gap-2">
        <CircleDollarSign className="h-5 w-5 text-accent" />
        <h2 id="setup-card-title" className="text-[16px] leading-6 font-semibold">创建虚拟账户</h2>
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
        placeholder={autoId.trim() && autoName ? `默认用${autoKind === 'strategy' ? '策略' : '规则'}名: ${autoName}` : (accId === 'default' ? '默认账户' : `账户 ${accId}`)}
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

      {/* 自动执行策略 (可选): 创建即绑定, 信号触发自动下单 */}
      <div className="mt-4 rounded-card border border-border bg-base/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Zap className="h-3.5 w-3.5 text-warning" />
          <span className="text-xs font-medium text-secondary">自动执行策略 (可选)</span>
          <span className="text-[10px] text-muted">创建后立即生效 · 信号触发按参数自动下单</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <div className="flex gap-1">
            <button onClick={() => { setAutoKind('strategy'); setAutoId(''); setAutoName(''); setAutoSearch(''); setSide('auto') }} className={segBtn(autoKind === 'strategy')}>策略</button>
            <button onClick={() => { setAutoKind('rule'); setAutoId(''); setAutoName(''); setAutoSearch(''); setSide(s => (s === 'auto' ? 'buy' : s)) }} className={segBtn(autoKind === 'rule')}>监控规则</button>
          </div>
          <div className="min-w-44 flex-1">
            {/* 选中后显示中文名 (id 只做匹配键); 重新输入则回到搜索态 */}
            <SuggestInput
              value={autoId ? (autoName || autoId) : autoSearch}
              onChange={v => { setAutoSearch(v); setAutoId(''); setAutoName('') }}
              options={autoOptions}
              onSelect={opt => { setAutoId(opt.value); setAutoName(opt.label === opt.value ? '' : opt.label); setAutoSearch('') }}
              loading={autoKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
              placeholder={autoKind === 'strategy' ? '搜索策略 (中文名 / ID)' : '搜索监控规则'}
              className="w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50"
            />
          </div>
          {autoId.trim() && (
            <button onClick={() => { setAutoId(''); setAutoName(''); setAutoSearch('') }} className="rounded-btn border border-border px-2 py-1 text-[10px] text-muted hover:text-danger" title="不绑定策略">清除</button>
          )}
        </div>
        {/* 两行栅格: 5 组控件一行放不下会被压成竖排文字并相互叠压。
            跟策略无方向概念 (固定 auto: 入场买/出场全平), 跟监控规则才显示方向。
            输入搜索词即展开 (选中或搜索中都可见参数) */}
        {(autoId || autoSearch).trim() && (
          <div className="mt-2 grid grid-cols-3 gap-2">
            {autoKind === 'strategy' ? (
              // 策略执行语义说明: 双向自动, 无需选择
              <div className="col-span-3 flex items-center gap-1.5 text-[10px] text-muted">
                <Zap className="h-3 w-3 text-accent" />
                入场信号自动买入 · 出场信号自动全平 — 完整执行策略周期
              </div>
            ) : (
              <div>
                <label className="block text-[10px] text-muted">方向</label>
                <div className="mt-1 flex flex-wrap gap-1">
                  <button onClick={() => setSide('buy')} className={segBtn(side === 'buy')}>买入</button>
                  <button onClick={() => setSide('sell')} className={segBtn(side === 'sell')}>卖出</button>
                </div>
              </div>
            )}
            <div>
              <label className="block text-[10px] text-muted">{sizeMode === 'pct_equity' ? '仓位 (权益 %)' : '仓位 (元)'}</label>
              <div className="mt-1 flex gap-1">
                <input type="number" min="0" step={sizeMode === 'pct_equity' ? 1 : 1000} value={sizeValue}
                  onChange={e => setSizeValue(e.target.value)}
                  className="w-16 shrink-0 rounded-btn border border-border bg-base px-1.5 py-1 font-mono text-xs outline-none focus:border-accent/50" />
                <button onClick={() => setSizeMode(m => m === 'pct_equity' ? 'fixed_amount' : 'pct_equity')}
                  className="whitespace-nowrap rounded-btn border border-border px-1.5 py-1 text-[10px] text-muted hover:text-accent" title="切换仓位模式">
                  {sizeMode === 'pct_equity' ? '按比例' : '按金额'}
                </button>
              </div>
            </div>
            <div>
              <label className="block text-[10px] text-muted">最大持仓</label>
              <input type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)}
                className="mt-1 w-full rounded-btn border border-border bg-base px-1.5 py-1 font-mono text-xs outline-none focus:border-accent/50"
                title="最多同时持有的标的数 (买入开新仓受限, 加仓/卖出不受限, 0 = 不限) — 与回测 max_positions 同口径" />
            </div>
            <div className="col-span-2">
              <label className="block text-[10px] text-muted">订单类型</label>
              <div className="mt-1 flex flex-wrap gap-1">
                {([['market', '即时'], ['next_open', '次日开盘'], ['close', '当日收盘']] as const).map(([v, t]) => (
                  <button key={v} onClick={() => setOrderType(v)} className={segBtn(orderType === v)}>{t}</button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-[10px] text-muted">冷却 (天)</label>
              <input type="number" min="0" step="1" value={cooldown} onChange={e => setCooldown(e.target.value)}
                className="mt-1 w-full rounded-btn border border-border bg-base px-1.5 py-1 font-mono text-xs outline-none focus:border-accent/50"
                title="同标的触发后 N 个交易日内不重复下单" />
            </div>
          </div>
        )}
      </div>

      <div className="mt-4 flex gap-2">
        <button
          onClick={() => valid && m.mutate()}
          disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
        >
          {m.isPending ? '创建中…' : ruleWarn ? '创建账户' : autoId.trim() ? '创建并绑定策略' : '创建账户'}
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
      {ruleWarn && (
        <div className="mt-2 rounded-btn bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
          账户已创建, 但策略绑定失败: {ruleWarn} — 可在账户面板「自动跟单规则」里手动补建。
          <button onClick={() => onDone(accId)} className="ml-1 underline underline-offset-2">完成</button>
        </div>
      )}
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
  /** 可选副行描述 (策略简介), 有则渲染两行式选项 */
  desc?: string
  /** 可选名称右侧徽标组 (tags / 周期) */
  badges?: string[]
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
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // 下拉用 portal + fixed 渲染: 祖先链 (创建向导滚动容器 / Modal 的 max-h overflow)
  // 会硬裁剪 absolute 下拉, 脱离文档流才能完整浮出; 位置实时贴输入框。
  const [dropStyle, setDropStyle] = useState<CSSProperties | null>(null)
  const syncDropPos = useCallback(() => {
    const r = inputRef.current?.getBoundingClientRect()
    if (!r) return
    const below = window.innerHeight - r.bottom - 8
    const above = r.top > 300 && below < 200
    setDropStyle(above
      ? { position: 'fixed', left: r.left, top: r.top - 6, width: Math.max(r.width, 280), transform: 'translateY(-100%)', maxHeight: Math.max(160, Math.min(320, r.top - 8)) }
      : { position: 'fixed', left: r.left, top: r.bottom + 6, width: Math.max(r.width, 280), maxHeight: Math.max(160, Math.min(320, below)) })
  }, [])
  useEffect(() => {
    if (!open) { setDropStyle(null); return }
    syncDropPos()
    // 滚动可来自任意祖先容器 (capture), 滚动/缩放时保持贴位
    window.addEventListener('scroll', syncDropPos, true)
    window.addEventListener('resize', syncDropPos)
    return () => {
      window.removeEventListener('scroll', syncDropPos, true)
      window.removeEventListener('resize', syncDropPos)
    }
  }, [open, syncDropPos])

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      const t = e.target as Element
      // portal 下拉不在 containerRef 内, 须一并排除, 否则点选项先被判定外点而关闭
      if (t.closest?.('[data-suggest-drop]')) return
      if (containerRef.current && !containerRef.current.contains(t)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  // 键盘 active 项始终滚入可视区 (列表可长, 66+ 项)
  useEffect(() => {
    if (activeIdx < 0 || !listRef.current) return
    listRef.current.querySelectorAll('button[data-idx]').forEach(btn => {
      if (Number(btn.getAttribute('data-idx')) === activeIdx) btn.scrollIntoView({ block: 'nearest' })
    })
  }, [activeIdx])

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
      setActiveIdx(i => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const pick = options[activeIdx >= 0 ? activeIdx : 0]
      if (pick) handlePick(pick)
    }
  }

  const showList = open && dropStyle && (loading || options.length > 0 || value.trim().length > 0)
  return (
    <div ref={containerRef} className="relative">
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={e => { onChange(e.target.value); setOpen(true); setActiveIdx(0) }}
        onFocus={() => { setOpen(true); syncDropPos(); setActiveIdx(0) }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className={className}
      />
      {showList && createPortal(
        <div
          data-suggest-drop
          style={dropStyle}
          className="z-[70] flex animate-[sg-drop-in_120ms_ease-out] flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-2xl shadow-black/60 ring-1 ring-black/30"
        >
          {/* 结果计数 + 键盘操作提示 */}
          <div className="flex shrink-0 items-center justify-between border-b border-border/60 bg-elevated/40 px-3 py-1.5 text-[10px] text-muted">
            <span>{loading ? '搜索中…' : `${options.length} 个候选${value.trim() ? ` · “${value.trim()}”` : ''}`}</span>
            <span className="font-mono tracking-tight">↑↓ 导航 · ↵ 选中 · esc 关闭</span>
          </div>
          <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1 [&::-webkit-scrollbar]:w-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border">
            {loading && options.length === 0 ? (
              <div className="space-y-1 px-2 py-1.5">
                {[0, 1, 2].map(i => <div key={i} className="h-9 animate-pulse rounded-lg bg-elevated/60" style={{ animationDelay: `${i * 120}ms` }} />)}
              </div>
            ) : options.length === 0 ? (
              <div className="px-3 py-6 text-center text-[11px] text-muted">
                无匹配候选 · 换个关键词或检查输入
              </div>
            ) : (
              options.map((opt, i) => (
                <button
                  key={opt.value}
                  data-idx={i}
                  type="button"
                  onClick={() => handlePick(opt)}
                  className={cn(
                    'relative mx-1 flex w-[calc(100%-8px)] flex-col gap-0.5 rounded-lg px-2.5 py-1.5 text-left transition-colors duration-75',
                    i === activeIdx ? 'bg-accent/10' : 'hover:bg-elevated/70',
                  )}
                >
                  {i === activeIdx && <span className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-accent" />}
                  <div className="flex min-w-0 items-center gap-1.5">
                    {opt.left && <span className="shrink-0 font-mono text-[11px] text-secondary">{opt.left}</span>}
                    <span className={cn('min-w-0 truncate text-xs', i === activeIdx ? 'text-accent' : 'text-foreground')}>{opt.label}</span>
                    {(opt.badges ?? []).slice(0, 3).map(b => (
                      <span key={b} className="shrink-0 rounded bg-elevated px-1 py-px text-[9px] leading-4 text-muted">{b}</span>
                    ))}
                    {opt.hint}
                  </div>
                  {(opt.desc || opt.value !== opt.label) && (
                    <div className="flex min-w-0 items-baseline gap-2">
                      {opt.desc && <span className="min-w-0 flex-1 truncate text-[10px] leading-4 text-muted">{opt.desc}</span>}
                      <span className="ml-auto shrink-0 font-mono text-[9px] text-muted/70">{opt.value}</span>
                    </div>
                  )}
                </button>
              ))
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}

/** 下单面板 */
function OrderForm({ acc, holdings = [], onDone }: { acc: string; holdings?: PaperHolding[]; onDone: () => void }) {
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('100')
  const [amount, setAmount] = useState('10000')
  const [qtyMode, setQtyMode] = useState<'qty' | 'amount'>('qty')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('market')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // 卖出时的持仓可卖提示 (T+1): 按代码匹配持仓行
  const sellPos = side === 'sell' ? holdings.find(h => h.symbol === symbol.trim().toUpperCase()) : undefined

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
          {side === 'sell' && (
            <div className="mt-1 text-[10px] text-muted">
              {sellPos
                ? `持有 ${sellPos.qty} 股 · T+1 可卖 ${sellPos.available_qty} 股 (含费用成本 ${fmtMoney(sellPos.avg_cost, 3)})`
                : symbol.trim() ? '该账户无此标的持仓' : '卖出前先选择持仓标的'}
            </div>
          )}
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

/** 模拟盘基础配置: 新建账户的默认初始资金与费率 (localStorage, 不落盘后端)。
 *  设置后创建表单 (单账户/批量) 优先加载它, 取代「记住上次取值」; 账户级费率
 *  仍可在各账户「费用与撮合设置」单独调整。 */
function BaseDefaultsModal({ onClose }: { onClose: () => void }) {
  const defaults = loadNewAccountDefaults()
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaults.stampQian)
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [saved, setSaved] = useState(false)
  const hasBase = (() => { try { return !!localStorage.getItem(PAPER_BASE_DEFAULTS_KEY) } catch { return false } })()
  const valid = Number(cash) > 0
    && Number(commissionWan) >= 0 && Number(commissionWan) <= 100
    && Number(stampQian) >= 0 && Number(stampQian) <= 50
    && Number(slippageBps) >= 0 && Number(slippageBps) <= 200
  const save = () => {
    try {
      localStorage.setItem(PAPER_BASE_DEFAULTS_KEY, JSON.stringify({ cash, commissionWan, stampQian, slippageBps }))
      setSaved(true)
      setTimeout(onClose, 600)
    } catch { /* 存储不可用时静默 */ }
  }
  const reset = () => {
    try { localStorage.removeItem(PAPER_BASE_DEFAULTS_KEY) } catch { /* 静默 */ }
    setCash('1000000'); setCommissionWan('2.5'); setStampQian('1'); setSlippageBps('5')
  }
  const inputCls = 'mt-1 w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  return (
    <Modal onClose={onClose} labelledBy="base-defaults-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="h-4 w-4 text-accent" />
          <h3 id="base-defaults-title" className="text-sm font-semibold text-foreground">基础配置 · 新账户默认</h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          新建账户 (单账户 / 批量) 自动加载以下默认值, 优先于「记住上次取值」;
          已有账户不受影响, 各账户费率仍可单独调整。
        </p>
        <label className="mt-4 block text-[11px] text-muted">初始虚拟资金 (元)</label>
        <input type="number" min="1" value={cash} onChange={e => setCash(e.target.value)} className={inputCls} />
        <div className="mt-3 grid grid-cols-3 gap-2">
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
        <div className="mt-4 flex gap-2">
          <button
            onClick={save}
            disabled={!valid}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {saved ? '已保存 ✓' : '保存为基础默认'}
          </button>
          {hasBase && (
            <button
              onClick={reset}
              className="rounded-btn border border-border px-3 text-xs text-muted transition-colors hover:border-warning/40 hover:text-warning"
              title="清除自定义基础配置, 回落内置默认 (100万 / 万2.5 / 千1 / 5bps)"
            >
              恢复内置默认
            </button>
          )}
          <button onClick={onClose} className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ================================================================
// 删除账户强确认弹窗 (破坏性操作, per 设计规范必须二次确认):
// 展示将丢失的数据统计 + 显式勾选; 有持仓/待成交时后端先拒绝一次,
// 弹窗内呈现拒绝明细后再给「强制删除」(force) — 默认安全, 强删需两步。
// ================================================================
function DeleteAccountModal({ acc, name, holdingsCount, onClose, onDeleted }: {
  acc: string
  name?: string
  holdingsCount: number
  onClose: () => void
  onDeleted: () => void
}) {
  const [ack, setAck] = useState(false)
  const [forceHint, setForceHint] = useState<string | null>(null) // 后端拒绝明细 (有持仓/待成交)
  const [forceMode, setForceMode] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const m = useMutation({
    mutationFn: (force: boolean) => api.paperDeleteAccount(force, acc),
    onSuccess: onDeleted,
    onError: e => {
      const msg = String((e as Error).message)
      if (msg.includes('force')) {
        // 后端 fail-closed 拒绝: 显示明细并解锁强制删除路径
        setForceHint(msg)
      } else {
        setErr(msg)
      }
    },
  })
  return (
    <Modal onClose={onClose} labelledBy="del-acc-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-danger" />
          <h3 id="del-acc-title" className="text-sm font-semibold text-foreground">
            删除账户 <span className="font-medium">{name || acc}</span>
          </h3>
          <span className="font-mono text-[10px] text-muted">{acc}</span>
        </div>
        <p className="mt-2 rounded-btn bg-danger/[0.06] px-2.5 py-2 text-[11px] leading-relaxed text-danger">
          将永久删除该账户的全部数据: 成交台账、持仓、净值曲线与跟单规则, <b>不可恢复</b>。
          {holdingsCount > 0 && <> 当前还有 {holdingsCount} 个持仓标的。</>}
        </p>
        <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs text-secondary">
          <input
            type="checkbox"
            checked={ack}
            onChange={e => setAck(e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 shrink-0"
          />
          我已知晓该账户数据将被永久删除且无法找回
        </label>
        {forceHint && (
          <div className="mt-3 rounded-btn border border-warning/30 bg-warning/[0.08] px-2.5 py-2 text-[11px] leading-relaxed text-warning">
            {forceHint}
            {!forceMode && (
              <button onClick={() => setForceMode(true)} className="ml-1 underline underline-offset-2">
                我仍要删除全部数据 →
              </button>
            )}
          </div>
        )}
        <div className="mt-4 flex gap-2">
          <button
            onClick={() => m.mutate(forceMode)}
            disabled={!ack || m.isPending || (!!forceHint && !forceMode)}
            className={cn(
              'flex-1 rounded-btn py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-40',
              forceMode ? 'bg-danger hover:bg-danger/85' : 'bg-danger/70 hover:bg-danger',
            )}
          >
            {m.isPending ? '删除中…' : forceMode ? '强制删除 (含持仓)' : '删除账户'}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">
            取消
          </button>
        </div>
        {err && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{err}</div>}
      </div>
    </Modal>
  )
}

// ================================================================
// 多账户横向对比: 归一化净值叠加图。
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
        // 仅 1 个定版点时无线段可画, 显示为圆点 (否则曲线"消失")
        showSymbol: r.nav.length <= 1,
        symbolSize: 7,
        // 归一化: 定版净值 / 首日净值 (不同本金也能公平对比)
        data: r.nav.map(n => [n.date, Number((n.nav / (r.nav[0]?.nav || 1)).toFixed(4))]),
        lineStyle: { color: COMPARE_COLORS[i % COMPARE_COLORS.length], width: 2 },
        itemStyle: { color: COMPARE_COLORS[i % COMPARE_COLORS.length] },
      })),
    })
  }, [rows])

  return <div ref={elRef} className="h-56 w-full" />
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
              <span className={cn('w-8 shrink-0 font-medium', r.side === 'buy' ? 'text-bull' : r.side === 'sell' ? 'text-bear' : 'text-accent')} title={r.side === 'auto' ? '自动: 入场信号买入 · 出场信号全平' : undefined}>{r.side === 'auto' ? '自动' : r.side === 'buy' ? '买' : '卖'}</span>
              <span className="w-24 shrink-0 font-mono text-[11px] text-muted">
                {r.size_mode === 'fixed_amount' ? fmtMoney(r.size_value, 0) : `${r.size_value}% 权益`}
              </span>
              <span className="w-16 shrink-0 text-[11px] text-muted">{ORDER_TYPE_LABEL[r.order_type]} · 冷却{r.cooldown_days}天</span>
              {r.max_positions != null && r.max_positions > 0 && (
                <span className="shrink-0 text-[11px] text-muted" title="最大同时持仓标的数 (0 = 不限)">≤{r.max_positions}仓</span>
              )}
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
  const [side, setSide] = useState<'buy' | 'sell' | 'auto'>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('fixed_amount')
  const [sizeValue, setSizeValue] = useState('10000')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('10')
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
        max_positions: Math.max(0, Math.round(Number(maxPositions) || 0)),
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
              <button key={k} onClick={() => { setMatchKind(k); setMatchId(''); setSide(s => (k === 'strategy' ? 'auto' : s === 'auto' ? 'buy' : s)) }}
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
        {matchKind === 'strategy' ? (
          <div className="self-end pb-1.5 text-[10px] leading-4 text-muted">
            入场信号买入 · 出场信号全平
          </div>
        ) : (
          <div>
            <label className="text-[11px] text-muted">方向</label>
            <div className="mt-1 flex gap-1.5">
              {(['buy', 'sell'] as const).map(sd => (
                <button key={sd} onClick={() => setSide(sd)}
                  className={cn('flex-1 whitespace-nowrap rounded-btn border py-1 text-[11px] font-medium transition-colors',
                    side === sd ? (sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted')}>
                  {sd === 'buy' ? '买入' : '卖出'}
                </button>
              ))}
            </div>
          </div>
        )}
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
        <div>
          <label className="text-[11px] text-muted" title="最多同时持有的标的数 (买入开新仓受限, 0 = 不限) — 与回测 max_positions 同口径">最大持仓 (标的数)</label>
          <input type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)}
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

// ================================================================
// 多账户对比 (V3): 模拟盘默认主视图 — 对比优先, 收益降序。
// ================================================================

/** 净值迷你走势 (纯 SVG polyline, 最近 ~30 个定版点; 红涨绿跌与全局口径一致) */
function NavSpark({ nav }: { nav: Array<{ date: string; nav: number }> }) {
  const pts = nav.map(n => n.nav).filter(v => v > 0).slice(-30)
  if (pts.length < 2) return <span className="block text-center text-[11px] text-muted">—</span>
  const w = 84, h = 24, pad = 2
  const min = Math.min(...pts)
  const max = Math.max(...pts)
  const span = max - min || 1
  const step = (w - pad * 2) / (pts.length - 1)
  const up = pts[pts.length - 1] >= pts[0]
  const d = pts
    .map((v, i) => `${(pad + i * step).toFixed(1)},${(h - pad - ((v - min) / span) * (h - pad * 2)).toFixed(1)}`)
    .join(' ')
  return (
    <svg width={w} height={h} className="mx-auto block" aria-hidden>
      <polyline points={d} fill="none" stroke={up ? '#F04438' : '#12B76A'} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function RankBadge({ rank, accent }: { rank: number; accent?: boolean }) {
  return (
    <span className={cn(
      'inline-flex h-5 w-5 items-center justify-center rounded-full font-mono text-[11px] font-semibold',
      accent && rank === 1 ? 'bg-accent/20 text-accent' : accent && rank <= 3 ? 'bg-elevated text-secondary' : 'text-muted',
    )}>
      {rank}
    </span>
  )
}

/** 对比榜单可排序列 (表头点击切换) */
type CmpSortKey = 'name' | 'total' | 'pnl_pct' | 'day_change_pct' | 'win_rate' | 'profit_loss_ratio' | 'rounds' | 'holdings_count' | 'last_nav_date' | 'realized_pnl' | 'max_drawdown'

/** 可空列: 无值行排序时恒垫底 (与方向无关), 见 sorted 比较器 */
const CMP_NULLABLE: Partial<Record<CmpSortKey, (r: PaperCompareRow) => boolean>> = {
  pnl_pct: r => r.pnl_pct == null,
  day_change_pct: r => r.day_change_pct == null,
  profit_loss_ratio: r => r.profit_loss_ratio == null,
  last_nav_date: r => r.last_nav_date == null,
  max_drawdown: r => r.max_drawdown == null,
}

function cmpSortVal(k: CmpSortKey, r: PaperCompareRow): string | number {
  switch (k) {
    case 'name': return r.name
    case 'total': return r.total ?? 0
    case 'pnl_pct': return r.pnl_pct ?? 0
    case 'day_change_pct': return r.day_change_pct ?? 0
    case 'win_rate': return r.win_rate ?? 0
    case 'profit_loss_ratio': return r.profit_loss_ratio ?? 0
    case 'rounds': return r.rounds ?? 0
    case 'holdings_count': return r.holdings_count ?? 0
    case 'last_nav_date': return r.last_nav_date ?? ''
    case 'realized_pnl': return r.realized_pnl ?? 0
    case 'max_drawdown': return -(r.max_drawdown ?? 0) // 回撤绝对值越小越优 → 取负后降序=回撤小在前
  }
}

/** 可排序表头: 点击切换该列升降序, 活动列显方向箭头, 非活动列 hover 显淡箭头 (样式随 Review._DtTh) */
function SortTh({ label, sortKey, sort, onSort, align = 'left', title }: {
  label: string
  sortKey: CmpSortKey
  sort: { key: CmpSortKey; desc: boolean }
  onSort: (k: CmpSortKey) => void
  align?: 'left' | 'right'
  title?: string
}) {
  const active = sort.key === sortKey
  return (
    <th className={cn('group/th py-1.5 font-medium', align === 'right' && 'text-right')}>
      <button
        type="button"
        title={title}
        onClick={() => onSort(sortKey)}
        className={cn('inline-flex items-center gap-0.5 transition-colors hover:text-foreground', active && 'text-foreground')}
      >
        {label}
        {active ? (
          sort.desc
            ? <ChevronDown className="h-2.5 w-2.5 opacity-80" />
            : <ChevronUp className="h-2.5 w-2.5 opacity-80" />
        ) : (
          <ChevronDown className="h-2.5 w-2.5 opacity-0 transition-opacity group-hover/th:opacity-40" />
        )}
      </button>
    </th>
  )
}

function CompareView({ onCreateSingle }: { onCreateSingle: () => void }) {
  const [createOpen, setCreateOpen] = useState(false)
  const [baseOpen, setBaseOpen] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<PaperCompareRow | null>(null)
  const qc = useQueryClient()
  // 表头排序: 默认累计收益降序; 同列再点翻转方向, 换列时数值列默认降序、账户名默认升序
  const [sort, setSort] = useState<{ key: CmpSortKey; desc: boolean }>({ key: 'pnl_pct', desc: true })
  const onSortCmp = (k: CmpSortKey) =>
    setSort(s => (s.key === k ? { key: k, desc: !s.desc } : { key: k, desc: k !== 'name' }))
  // 行内冻结/解冻 (与展开面板同一入口, 免展开直接切)
  const freezeM = useMutation({
    mutationFn: (args: { acc: string; frozen: boolean }) => api.paperFreeze(args.frozen, args.acc),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.paperAll }),
  })
  // 60s 轻轮询: 盘中自动跟单下单 / 盘后结算后榜单自然刷新 (保留旧数据防闪烁)。
  // 非交易时段榜单不变, 降为 5 分钟兜底 (字段缺失保持 60s)
  const { data: quoteStatus } = useQuoteStatus()
  const cmpQ = useQuery({
    queryKey: QK.paperCompare,
    queryFn: api.paperCompare,
    refetchInterval: () => (quoteStatus?.is_trading_hours === false ? 300_000 : 60_000),
    placeholderData: (prev: any) => prev,
  })

  const rows = cmpQ.data?.accounts ?? []
  // 收益口径固定排序: 统计卡「收益最高」与净值叠加「收益前 8 名」不受表头排序影响
  const byPnl = useMemo(() => [...rows].sort((a, b) => (b.pnl_pct ?? -1e9) - (a.pnl_pct ?? -1e9)), [rows])
  const sorted = useMemo(() => {
    const dir = sort.desc ? -1 : 1
    const isNull = CMP_NULLABLE[sort.key]
    return [...rows].sort((a, b) => {
      if (isNull) {
        const na = isNull(a), nb = isNull(b)
        if (na || nb) return na === nb ? 0 : na ? 1 : -1 // 无值行恒垫底, 不随方向翻到顶部
      }
      const va = cmpSortVal(sort.key, a)
      const vb = cmpSortVal(sort.key, b)
      const c = typeof va === 'string' || typeof vb === 'string'
        ? String(va).localeCompare(String(vb), 'zh-Hans-CN')
        : va - vb
      return c * dir
    })
  }, [rows, sort])
  const chartRows = byPnl.filter(r => r.nav.length > 0).slice(0, 8)
  const avgPct = rows.length > 0
    ? rows.reduce((s, r) => s + (r.pnl_pct ?? 0), 0) / rows.length
    : null
  const totalAssets = rows.reduce((s, r) => s + (r.total ?? 0), 0)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="模拟盘"
        subtitle="多账户并行对比 — 同本金 · 同费率 · 点表头排序, 点行展开完整账户操作"
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCreateOpen(true)}
              className="flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:bg-accent/90"
              title="同本金同费率批量创建一批账户, 各绑一条策略/监控规则跟单, 保证对比口径一致"
            >
              <Plus className="h-3 w-3" />
              批量创建账户
            </button>
            <button
              onClick={onCreateSingle}
              className="flex items-center gap-0.5 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title="新建单个虚拟账户 (原流程)"
            >
              <Plus className="h-3 w-3" />
              单账户
            </button>
            <button
              onClick={() => setBaseOpen(true)}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title="新建账户的默认初始资金与费率 (佣金/印花税/滑点), 创建时自动加载"
            >
              <SlidersHorizontal className="h-3 w-3" />
              基础配置
            </button>
            <button
              onClick={() => cmpQ.refetch()}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
            >
              <RefreshCw className={cn('h-3 w-3', cmpQ.isFetching && 'animate-spin')} />
              刷新
            </button>
          </div>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {cmpQ.isLoading ? (
          <div className="py-16 text-center text-sm text-muted">加载中…</div>
        ) : cmpQ.isError ? (
          <div className="py-16 text-center text-sm text-danger">
            对比数据加载失败
            <button onClick={() => cmpQ.refetch()} className="ml-2 rounded-btn border border-border px-2 py-0.5 text-xs text-secondary hover:text-foreground">重试</button>
          </div>
        ) : rows.length === 0 ? (
          <div className="mx-auto mt-16 max-w-md rounded-card border border-dashed border-border bg-surface/50 px-6 py-14 text-center">
            <GitCompare className="mx-auto h-8 w-8 text-muted/50" />
            <div className="mt-3 text-sm font-medium">还没有对比账户</div>
            <div className="mt-1.5 text-xs leading-relaxed text-muted">
              同本金、同费率批量创建一批账户, 各绑一个策略或监控规则,
              信号自动跟单 — 同口径的并行对比才有参考意义。
            </div>
            <div className="mt-4 flex justify-center gap-2">
              <button
                onClick={() => setCreateOpen(true)}
                className="flex items-center gap-1 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:bg-accent/90"
              >
                <Plus className="h-3.5 w-3.5" />
                批量创建账户
              </button>
              <button
                onClick={onCreateSingle}
                className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              >
                先建一个单账户
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatCard label="对比账户" value={String(rows.length)} icon={Wallet} iconCls="text-accent" />
              <StatCard label="合计虚拟资产" value={fmtMoney(totalAssets, 0)} icon={Banknote} iconCls="text-muted" />
              <StatCard
                label="平均累计收益"
                value={avgPct == null ? '—' : fmtPct(avgPct / 100)}
                valueClass={priceColorClass((avgPct ?? 0) / 100)}
                icon={TrendingUp}
                iconCls={priceColorClass((avgPct ?? 0) / 100)}
              />
              <StatCard
                label="收益最高"
                value={byPnl[0].name}
                sub={byPnl.length > 1 ? `+${(byPnl[0].pnl_pct ?? 0).toFixed(2)}% 高于第2名 ${((byPnl[0].pnl_pct ?? 0) - (byPnl[1].pnl_pct ?? 0)).toFixed(2)}pct` : undefined}
                valueClass="text-accent"
                icon={TrendingUp}
                iconCls="text-accent"
              />
            </div>

            {chartRows.length > 1 && (
              <div className="rounded-card border border-border bg-surface p-4">
                <div className="text-sm font-medium">归一化净值叠加 <span className="ml-1 text-[10px] text-muted">起点 = 1, 收益前 8 名 · 不同本金公平对比</span></div>
                <CompareChart rows={chartRows} />
              </div>
            )}

            <div className="rounded-card border border-border bg-surface p-4">
              <div className="flex items-center gap-2">
                <div className="text-sm font-medium">收益对比</div>
                <span className="text-[10px] text-muted">点表头排序 · 点行展开完整账户操作</span>
                <span className="ml-auto text-[10px] text-muted" title="定版字段结算后更新; 盘中快照就绪时总资产/收益显示实时估算 (估 徽标)">
                  {rows.some(r => r.estimating) ? '总资产 / 收益为盘中实时估算 · 其余截至最近结算日' : '数据截至最近结算日'}
                </span>
              </div>
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[1100px] text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-[10px] text-muted">
                      <th className="py-1.5 pr-1 font-medium">#</th>
                      <SortTh label="账户" sortKey="name" sort={sort} onSort={onSortCmp} />
                      <SortTh label="总资产" sortKey="total" sort={sort} onSort={onSortCmp} align="right" title="定版口径; 盘中快照就绪时显示实时估算" />
                      <SortTh label="累计收益" sortKey="pnl_pct" sort={sort} onSort={onSortCmp} align="right" />
                      <SortTh label="最新日" sortKey="day_change_pct" sort={sort} onSort={onSortCmp} align="right" title="最近两个定版净值的涨跌" />
                      <SortTh label="已实现" sortKey="realized_pnl" sort={sort} onSort={onSortCmp} align="right" title="FIFO 回合已落袋盈亏 (含费用)" />
                      <SortTh label="回撤" sortKey="max_drawdown" sort={sort} onSort={onSortCmp} align="right" title="定版净值序列最大回撤" />
                      <SortTh label="胜率" sortKey="win_rate" sort={sort} onSort={onSortCmp} align="right" />
                      <SortTh label="盈亏比" sortKey="profit_loss_ratio" sort={sort} onSort={onSortCmp} align="right" title="平均盈利回合 / 平均亏损回合, 无亏损回合时为空" />
                      <SortTh label="回合" sortKey="rounds" sort={sort} onSort={onSortCmp} align="right" title="FIFO 配对的完整买卖回合" />
                      <SortTh label="持仓" sortKey="holdings_count" sort={sort} onSort={onSortCmp} align="right" />
                      <th className="py-1.5 text-center font-medium">净值趋势</th>
                      <SortTh label="最近结算" sortKey="last_nav_date" sort={sort} onSort={onSortCmp} align="right" />
                      {/* 操作列 sticky 贴右: 14 列宽表在窄视口横向滚动时, 冻结/删除入口始终可见。
                          box-shadow 在 border-collapse 表格上不渲染, 分界用 1px 左边框。 */}
                      <th className="sticky right-0 z-10 w-16 border-l border-border/60 bg-surface/95 py-1.5 text-center font-medium backdrop-blur-sm" title="冻结切换 / 删除账户">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.map((r, i) => {
                      const open = expanded === r.account
                      return (
                        <Fragment key={r.account}>
                          <tr
                            className={cn(
                              'cursor-pointer border-t border-border/50 transition-colors hover:bg-elevated/40',
                              open && 'bg-elevated/30',
                              r.status === 'frozen' && !open && 'opacity-60',
                            )}
                            onClick={() => setExpanded(open ? null : r.account)}
                          >
                            <td className="py-2 pr-1" title="当前排序列位 (仅按累计收益降序时高亮为收益名次)"><RankBadge rank={i + 1} accent={sort.key === 'pnl_pct' && sort.desc} /></td>
                            <td className="py-2">
                              <div className="flex items-center gap-1.5">
                                <span className="max-w-44 truncate text-xs font-medium">{r.name}</span>
                                {r.status === 'frozen' && <span className="rounded-full bg-warning/15 px-1.5 py-px text-[9px] leading-4 text-warning">冻结</span>}
                                {(r.auto_rules ?? []).slice(0, 2).map((ar, j) => (
                                  <span
                                    key={`${ar.match_id}-${j}`}
                                    className={cn('max-w-32 truncate rounded-full px-1.5 py-px text-[9px] leading-4', ar.enabled ? 'bg-accent/10 text-accent' : 'bg-elevated text-muted')}
                                    title={`${ar.name} · ${ar.match_kind === 'strategy' ? '跟策略' : '跟规则'} ${ar.match_id}${ar.side === 'sell' ? ' · 卖出方向' : ''}`}
                                  >
                                    {ar.name}{ar.side === 'sell' ? '·卖' : ''}
                                  </span>
                                ))}
                                {(r.auto_rules?.length ?? 0) > 2 && (
                                  <span className="shrink-0 text-[9px] text-muted" title={r.auto_rules!.slice(2).map(a => a.name).join(' / ')}>+{r.auto_rules!.length - 2}</span>
                                )}
                              </div>
                              <div className="font-mono text-[10px] text-muted">{r.account}</div>
                            </td>
                            <td className="py-2 text-right font-mono tabular">
                              <span className="inline-flex items-baseline justify-end gap-1">
                                {fmtMoney(r.estimating ? (r.live_total ?? r.total) : r.total, 0)}
                                {r.estimating && <span className="text-[9px] font-sans text-accent" title="盘中快照价实时估算, 收盘后定版">估</span>}
                              </span>
                            </td>
                            <td className="py-2 text-right font-mono font-semibold tabular">
                              <span className={priceColorClass((r.estimating ? (r.live_pnl_pct ?? r.pnl_pct ?? 0) : (r.pnl_pct ?? 0)) / 100)}>
                                {fmtPct(((r.estimating ? (r.live_pnl_pct ?? r.pnl_pct ?? 0) : (r.pnl_pct ?? 0))) / 100)}
                              </span>
                            </td>
                            <td className="py-2 text-right font-mono tabular">
                              {r.day_change_pct != null
                                ? <span className={priceColorClass(r.day_change_pct / 100)}>{fmtPct(r.day_change_pct / 100)}</span>
                                : <span className="text-muted">—</span>}
                            </td>
                            <td className={cn('py-2 text-right font-mono tabular', priceColorClass(r.realized_pnl ?? 0))}>
                              {(r.realized_pnl ?? 0) >= 0 ? '+' : ''}{fmtMoney(r.realized_pnl ?? 0, 0)}
                            </td>
                            <td className="py-2 text-right font-mono tabular text-secondary">
                              {r.max_drawdown != null
                                ? r.max_drawdown > 0 ? `-${r.max_drawdown.toFixed(1)}%` : '0.0%'
                                : <span className="text-muted">—</span>}
                            </td>
                            <td className="py-2 text-right font-mono tabular text-secondary">{r.win_rate.toFixed(1)}%</td>
                            <td className="py-2 text-right font-mono tabular text-secondary">
                              {r.profit_loss_ratio != null ? r.profit_loss_ratio : <span className="text-muted">—</span>}
                            </td>
                            <td className="py-2 text-right font-mono tabular text-muted">{r.rounds}</td>
                            <td className="py-2 text-right font-mono tabular text-secondary">{r.holdings_count ?? 0}</td>
                            <td className="py-1.5"><NavSpark nav={r.nav} /></td>
                            <td className="py-2 text-right font-mono text-[11px] text-muted">{r.last_nav_date ?? '—'}</td>
                            <td className="sticky right-0 z-10 border-l border-border/60 bg-surface/95 py-2 text-right backdrop-blur-sm" onClick={e => e.stopPropagation()}>
                              <div className="flex items-center justify-end gap-0.5">
                                <button
                                  onClick={() => freezeM.mutate({ acc: r.account, frozen: r.status !== 'frozen' })}
                                  disabled={freezeM.isPending}
                                  className={cn(
                                    'rounded p-1 transition-colors',
                                    r.status === 'frozen' ? 'text-warning hover:bg-warning/10' : 'text-muted hover:bg-elevated hover:text-warning',
                                  )}
                                  title={r.status === 'frozen' ? '解冻账户 (恢复下单与跟单)' : '冻结账户 (拒绝新订单, 持仓只读)'}
                                >
                                  <Snowflake className="h-3.5 w-3.5" />
                                </button>
                                <button
                                  onClick={() => setDeleting(r)}
                                  className="rounded p-1 text-muted transition-colors hover:bg-danger/10 hover:text-danger"
                                  title="删除账户 (含全部数据, 需二次确认)"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </button>
                                <ChevronDown className={cn('ml-0.5 inline h-3.5 w-3.5 text-muted transition-transform duration-150', open && 'rotate-180')} />
                              </div>
                            </td>
                          </tr>
                          {open && (
                            <tr className="border-t border-border/50 bg-base/40">
                              <td colSpan={14} className="px-4 py-4">
                                <AccountPanel acc={r.account} name={r.name} onDeleted={() => setExpanded(null)} />
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>
      {createOpen && <BatchCreateModal onClose={() => setCreateOpen(false)} onCreated={() => setCreateOpen(false)} />}
      {baseOpen && <BaseDefaultsModal onClose={() => setBaseOpen(false)} />}
      {deleting && (
        <DeleteAccountModal
          acc={deleting.account}
          name={deleting.name}
          holdingsCount={deleting.holdings_count ?? 0}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null)
            setExpanded(null)
            qc.invalidateQueries({ queryKey: QK.paperAll })
          }}
        />
      )}
    </div>
  )
}

// ================================================================
// 批量创建对比账户弹窗: 同口径由构造保证 (同本金/同费率/同跟单参数)。
// 来源行只带 名称+匹配目标; 方向/仓位/订单类型/冷却为全表共享参数。
// ================================================================
type BatchSourceRow = { name: string; kind: 'strategy' | 'rule'; matchId: string }

function BatchCreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (count: number) => void }) {
  const qc = useQueryClient()
  const defaults = loadNewAccountDefaults()
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaults.stampQian)
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [prefix, setPrefix] = useState('')
  const [rows, setRows] = useState<BatchSourceRow[]>([])
  const [quickKind, setQuickKind] = useState<'strategy' | 'rule'>('strategy')
  const [quickId, setQuickId] = useState('')
  // 共享跟单参数 (应用到全部来源 → 同规格对比)
  const [side, setSide] = useState<'buy' | 'sell' | 'auto'>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('pct_equity')
  const [sizeValue, setSizeValue] = useState('10')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('10')

  const strategiesQ = useQuery({
    queryKey: QK.screenerStrategies('any', 'all'),
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList, staleTime: 30_000 })

  const m = useMutation({
    mutationFn: () =>
      api.paperArenaCreate({
        initial_cash: Number(cash),
        name_prefix: prefix.trim() || undefined,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
        sources: rows.map(r => ({
          name: r.name.trim() || undefined,
          match_kind: r.kind,
          match_id: r.matchId.trim(),
          side,
          size_mode: sizeMode,
          size_value: Number(sizeValue),
          order_type: orderType,
          cooldown_days: Number(cooldown),
          max_positions: Math.max(0, Math.round(Number(maxPositions) || 0)),
        })),
      }),
    onSuccess: r => {
      qc.invalidateQueries({ queryKey: QK.paperAll })
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({ cash, commissionWan, stampQian, slippageBps }))
      } catch { /* 存储不可用时静默 */ }
      onCreated(r.created.length)
    },
  })

  const addRow = (row: BatchSourceRow) => {
    setRows(prev => (prev.some(r => r.matchId === row.matchId) ? prev : [...prev, row]))
    // 混入监控规则来源后 auto 不再合法 (后端校验拒绝), 落回买入
    if (row.kind === 'rule') setSide(s => (s === 'auto' ? 'buy' : s))
  }
  const addAllStrategies = () => {
    const presets = (strategiesQ.data?.presets ?? []).slice(0, 20)
    setRows(prev => {
      const seen = new Set(prev.map(r => r.matchId))
      return [...prev, ...presets.filter(p => !seen.has(p.id)).map(p => ({ name: p.name || p.id, kind: 'strategy' as const, matchId: p.id }))]
    })
  }

  const valid = rows.length >= 1
    && rows.every(r => r.matchId.trim().length > 0)
    && Number(cash) > 0
    && Number(sizeValue) > 0
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0

  const quickQuery = quickId.trim().toLowerCase()
  const quickOptions: SuggestOption[] = quickKind === 'strategy'
    ? (strategiesQ.data?.presets ?? [])
        .filter(s => !quickQuery || s.name.toLowerCase().includes(quickQuery) || s.id.toLowerCase().includes(quickQuery) || (s.tags ?? []).some(t => t.toLowerCase().includes(quickQuery)))
        .map(s => ({
          value: s.id,
          label: s.name || s.id,
          desc: s.description || undefined,
          badges: [
            ...(s.timeframes ?? []).map(tf => tf === '1d' ? '日线' : tf === '1m' ? '分钟' : tf),
            ...(s.tags ?? []).slice(0, 2),
          ].slice(0, 3),
        }))
    : (rulesQ.data?.rules ?? [])
        .filter(r => !quickQuery || r.name.toLowerCase().includes(quickQuery) || r.id.toLowerCase().includes(quickQuery))
        .map(r => ({ value: r.id, label: r.name || r.id }))

  const inputCls = 'w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  const labelCls = 'block text-[11px] text-muted'

  return (
    <Modal onClose={onClose} labelledBy="arena-create-title" panelClassName="w-[94vw] max-w-3xl bg-surface border border-border rounded-card shadow-xl">
      <div className="max-h-[86vh] overflow-y-auto p-5">
        <div className="flex items-center gap-2">
          <GitCompare className="h-4 w-4 text-accent" />
          <h3 id="arena-create-title" className="text-sm font-semibold text-foreground">批量创建对比账户</h3>
          <span className="text-[10px] text-muted">同本金 · 同费率 · 同跟单参数 — 口径一致由构造保证</span>
        </div>

        {/* 对比来源 */}
        <div className="mt-4 rounded-card border border-border bg-base/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex gap-1">
              {(['strategy', 'rule'] as const).map(k => (
                <button key={k} onClick={() => { setQuickKind(k); setQuickId('') }}
                  className={cn('rounded-btn border px-2 py-1 text-[11px] transition-colors',
                    quickKind === k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                  {k === 'strategy' ? '策略' : '监控规则'}
                </button>
              ))}
            </div>
            <div className="min-w-52 flex-1">
              <SuggestInput
                value={quickId}
                onChange={setQuickId}
                options={quickOptions}
                onSelect={opt => {
                  addRow({ name: opt.label, kind: quickKind, matchId: opt.value })
                  setQuickId('')
                }}
                loading={quickKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
                placeholder={quickKind === 'strategy' ? '搜索策略加入对比 (中文名 / ID)' : '搜索监控规则加入对比'}
                className="w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50"
              />
            </div>
            <button
              onClick={() => { const opt = quickOptions[0]; if (opt) { addRow({ name: opt.label, kind: quickKind, matchId: opt.value }); setQuickId('') } }}
              disabled={quickOptions.length === 0}
              className="rounded-btn bg-accent/10 px-2.5 py-1 text-[11px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-40"
            >
              添加
            </button>
            {quickKind === 'strategy' && (
              <button
                onClick={addAllStrategies}
                disabled={(strategiesQ.data?.presets ?? []).length === 0}
                className="rounded-btn border border-border px-2.5 py-1 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-40"
                title="把现有策略各开一个账户 (最多 20 个)"
              >
                全部策略一键加入 ({Math.min((strategiesQ.data?.presets ?? []).length, 20)})
              </button>
            )}
          </div>

          {rows.length === 0 ? (
            <div className="py-6 text-center text-[11px] text-muted">还没有来源 — 上面搜索添加, 或「全部策略一键加入」</div>
          ) : (
            <div className="mt-2 max-h-52 space-y-1 overflow-y-auto">
              {rows.map((r, i) => (
                <div key={r.matchId} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs hover:bg-elevated/40">
                  <span className="w-16 shrink-0 text-[10px] text-muted">{r.kind === 'strategy' ? '跟策略' : '跟规则'}</span>
                  <input
                    value={r.name}
                    onChange={e => setRows(prev => prev.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                    placeholder="账户名 (缺省用 ID)"
                    className="min-w-0 flex-1 rounded-btn border border-border bg-base px-2 py-1 text-xs outline-none focus:border-accent/50"
                  />
                  <span className="w-44 shrink-0 truncate font-mono text-[11px] text-muted" title={r.matchId}>{r.matchId}</span>
                  <button onClick={() => setRows(prev => prev.filter((_, j) => j !== i))} className="shrink-0 rounded p-0.5 text-muted hover:text-danger" title="移除">
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 共享规格 */}
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
          <div>
            <label className={labelCls}>每户初始资金 (元)</label>
            <input type="number" value={cash} onChange={e => setCash(e.target.value)} className={cn(inputCls, 'mt-1')} placeholder="200000" />
          </div>
          <div>
            <label className={labelCls}>账户名前缀 (可选)</label>
            <input value={prefix} onChange={e => setPrefix(e.target.value)} className={cn(inputCls, 'mt-1 font-sans')} placeholder="如: 第一期-" />
          </div>
          {rows.length > 0 && rows.every(r => r.kind === 'strategy') ? (
            <div className="self-end pb-1.5 text-[10px] leading-4 text-muted">
              入场信号买入 · 出场信号全平
            </div>
          ) : (
            <div>
              <label className={labelCls}>方向 (监控规则来源)</label>
              <div className="mt-1 flex gap-1.5">
                {(['buy', 'sell'] as const).map(sd => (
                  <button key={sd} onClick={() => setSide(sd)}
                    className={cn('flex-1 whitespace-nowrap rounded-btn border py-1 text-[11px] font-medium transition-colors',
                      side === sd ? (sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted')}>
                    {sd === 'buy' ? '买入' : '卖出'}
                  </button>
                ))}
              </div>
            </div>
          )}
          <div>
            <label className={labelCls}>仓位模式 (全部来源)</label>
            <div className="mt-1 flex gap-1.5">
              <button onClick={() => setSizeMode('fixed_amount')}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'fixed_amount' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>固定金额</button>
              <button onClick={() => setSizeMode('pct_equity')}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'pct_equity' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>权益 %</button>
            </div>
          </div>
          <div>
            <label className={labelCls}>{sizeMode === 'fixed_amount' ? '每笔金额 (元)' : '每笔占权益 %'}</label>
            <input type="number" value={sizeValue} onChange={e => setSizeValue(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div>
            <label className={labelCls}>订单类型</label>
            <div className="mt-1 flex gap-1">
              {(['next_open', 'close', 'market'] as const).map(t => (
                <button key={t} onClick={() => setOrderType(t)}
                  className={cn('flex-1 rounded-btn border py-1 text-[10px] transition-colors',
                    orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                  {ORDER_TYPE_LABEL[t]}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className={labelCls}>冷却天数 (同标的)</label>
            <input type="number" value={cooldown} onChange={e => setCooldown(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div>
            <label className={labelCls} title="最多同时持有的标的数 (买入开新仓受限, 0 = 不限) — 与回测 max_positions 同口径">最大持仓 (标的数)</label>
            <input type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div className="grid grid-cols-3 gap-2 md:col-span-2">
            <div>
              <label className={labelCls}>佣金 (万)</label>
              <input type="number" step="0.1" min="0" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
            <div>
              <label className={labelCls}>印花税 (千)</label>
              <input type="number" step="0.1" min="0" value={stampQian} onChange={e => setStampQian(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
            <div>
              <label className={labelCls}>滑点 (bps)</label>
              <input type="number" step="1" min="0" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
          </div>
        </div>

        <div className="mt-4 flex items-center gap-2">
          <button
            onClick={() => valid && m.mutate()}
            disabled={!valid || m.isPending}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-40"
          >
            {m.isPending ? '创建中…' : `创建 ${rows.length || ''} 个账户`.replace('  ', ' ')}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">取消</button>
        </div>
        {m.isError && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{String((m.error as Error).message)}</div>}
        <div className="mt-2 text-[10px] leading-relaxed text-muted">
          创建后各账户立即参与盘中自动跟单 (规则触发 → 下单 → T+1 约束), 每交易日盘后管道统一结算定版净值, 对比榜单自动更新。
        </div>
      </div>
    </Modal>
  )
}

export function Paper() {
  const qc = useQueryClient()
  // 新建账户草稿 id: 打开弹窗时生成, 关闭即丢弃, 不写任何持久状态。
  const [draftId, setDraftId] = useState<string | null>(null)
  const [setupOpen, setSetupOpen] = useState(false)
  const close = () => { setSetupOpen(false); setDraftId(null) }
  return (
    <>
      <CompareView onCreateSingle={() => { setDraftId(genAccountId()); setSetupOpen(true) }} />
      {setupOpen && (
        <Modal onClose={close} labelledBy="setup-card-title" panelClassName="w-[94vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
          <SetupCard
            accId={draftId ?? 'default'}
            onDone={() => {
              qc.invalidateQueries({ queryKey: QK.paperAll })
              close()
            }}
            onCancel={close}
          />
        </Modal>
      )}
    </>
  )
}

/** 账户操作面板 (原单账户视图主体): 在对比榜单行展开内渲染, 免跳转。
 *  含总览卡/净值曲线/持仓/订单与成交/回合明细/下单/跟单规则/费用设置/冻结/删除。 */
function AccountPanel({ acc, name, onDeleted }: { acc: string; name?: string; onDeleted?: () => void }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'orders' | 'trades' | 'rounds'>('orders')
  const [feeOpen, setFeeOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [showAllOrders, setShowAllOrders] = useState(false)
  const [showAllTrades, setShowAllTrades] = useState(false)

  const overviewQ = useQuery({ queryKey: QK.paperOverview(acc), queryFn: () => api.paperOverview(acc) })
  const ordersQ = useQuery({ queryKey: QK.paperOrders(acc), queryFn: () => api.paperOrders(undefined, acc) })
  const tradesQ = useQuery({ queryKey: QK.paperTrades(acc), queryFn: () => api.paperTrades(acc) })
  const navQ = useQuery({ queryKey: QK.paperNav(acc), queryFn: () => api.paperNav(acc) })
  const statsQ = useQuery({ queryKey: QK.paperStats(acc), queryFn: () => api.paperStats(acc) })
  const roundsQ = useQuery({ queryKey: QK.paperRounds(acc), queryFn: () => api.paperRounds(acc) })
  const rounds: PaperRound[] = [...(roundsQ.data?.rounds ?? [])].reverse() // 最新回合在前

  // 持仓+回合代码→名称 (#454): 就地显示名称, 不用切页查询。股票/ETF/指数都覆盖。
  // 回合标的可能已清仓 (不在持仓), 合并去重一起查
  const holdingSymbols = Array.from(new Set([
    ...(overviewQ.data?.holdings ?? []).map(h => h.symbol),
    ...rounds.map(r => r.symbol),
  ]))
  const namesQ = useQuery({
    queryKey: ['instrument-names', holdingSymbols.join(',')],
    queryFn: () => api.instrumentNames(holdingSymbols),
    enabled: holdingSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = namesQ.data?.names ?? {}

  // 'paper' 前缀兜底失效: 覆盖全部账户的全部查询 (订单变动可能影响净值/统计)
  const invalidateAll = () => qc.invalidateQueries({ queryKey: QK.paperAll })

  const cancelM = useMutation({
    mutationFn: (id: string) => api.paperOrderCancel(id, acc),
    onSuccess: invalidateAll,
  })
  const queueM = useMutation({
    mutationFn: (on: boolean) => api.paperSettings({ queue_limit_orders: on }, acc),
    onSuccess: invalidateAll,
  })

  if (overviewQ.isLoading) {
    return <div className="py-8 text-center text-xs text-muted">加载中…</div>
  }
  // 请求失败与「未开户」分开呈现: 失败给错误态 + 重试, 不伪装成未初始化
  if (overviewQ.isError || !overviewQ.data) {
    return (
      <div className="flex flex-col items-center gap-2 py-8">
        <span className="text-xs text-secondary">账户数据加载失败，请重试</span>
        <button
          type="button"
          onClick={() => overviewQ.refetch()}
          disabled={overviewQ.isFetching}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-accent transition-colors hover:bg-elevated disabled:opacity-50"
        >
          重试
        </button>
      </div>
    )
  }
  const ov = overviewQ.data
  if (!ov?.initialized) {
    return <div className="py-8 text-center text-xs text-muted">账户未初始化</div>
  }

  const holdings: PaperHolding[] = ov.holdings ?? []
  // 全状态订单流水 (含已成交), 默认收最近 20 条, 可展开全部
  const allOrders: PaperOrder[] = ordersQ.data?.orders ?? []
  const orders = showAllOrders ? allOrders : allOrders.slice(0, 20)
  const allFills: PaperFill[] = tradesQ.data?.fills ?? []
  const trades = showAllTrades ? allFills : allFills.slice(0, 30)
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
    a.download = `模拟盘成交台账_${acc}_${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      {/* 面板头: 账户标识 + 状态徽章 + 费用 + 冻结 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{name || acc}</span>
        <span className="font-mono text-[10px] text-muted">{acc}</span>
        {ov.status === 'frozen' && <span className="rounded-full bg-warning/15 px-2 py-0.5 text-[10px] text-warning">已冻结</span>}
        {ov.queue_limit_orders && (
          <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[10px] text-accent" title="触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)">
            涨跌停排队
          </span>
        )}
        <button
          onClick={() => setFeeOpen(true)}
          className="ml-auto flex items-center gap-1 rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
          title={`佣金 ${((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ (最低5元) · 印花税 ${((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰ 仅卖出 · 滑点 ${ov.fees?.slippage_bps ?? 0}bps — 点击调整`}
        >
          <Settings className="h-3 w-3" />
          佣金 {((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ · 印花税 {((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰ · 滑点 {ov.fees?.slippage_bps ?? 0}bps
        </button>
        <button
          onClick={() => api.paperFreeze(ov.status !== 'frozen', acc).then(invalidateAll)}
          className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-warning/40 hover:text-warning"
        >
          {ov.status === 'frozen' ? '解冻账户' : '冻结账户'}
        </button>
        <button
          onClick={() => setDeleteOpen(true)}
          className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger"
          title="删除账户及其全部数据 (不可恢复)"
        >
          删除账户
        </button>
      </div>
      <div>
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
                        <td className="py-1.5 font-sans">
                          {h.symbol}
                          {symbolNames[h.symbol] && (
                            <span className="ml-1.5 text-[10px] text-muted">{symbolNames[h.symbol]}</span>
                          )}
                        </td>
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

            {/* 订单 / 成交 / 回合明细 */}
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="flex flex-wrap items-center gap-2">
                {(['orders', 'trades', 'rounds'] as const).map(t => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={cn('flex items-center gap-1 rounded-btn px-2.5 py-1 text-xs transition-colors', tab === t ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                  >
                    {t === 'orders' && <><History className="h-3 w-3" />订单 {allOrders.length > 0 && <span className="text-[10px] text-muted">{allOrders.length}</span>}</>}
                    {t === 'trades' && <>成交台账 {allFills.length > 0 && <span className="text-[10px] text-muted">{allFills.length}</span>}</>}
                    {t === 'rounds' && <><ListChecks className="h-3 w-3" />回合明细 {rounds.length > 0 && <span className="text-[10px] text-muted">{rounds.length}</span>}</>}
                  </button>
                ))}
                {tab === 'orders' && allOrders.length > 20 && (
                  <button
                    onClick={() => setShowAllOrders(v => !v)}
                    className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent"
                  >
                    {showAllOrders ? '收起' : `显示全部 ${allOrders.length} 条`}
                  </button>
                )}
                {tab === 'trades' && allFills.length > 30 && (
                  <button
                    onClick={() => setShowAllTrades(v => !v)}
                    className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent"
                  >
                    {showAllTrades ? '收起' : `显示全部 ${allFills.length} 条`}
                  </button>
                )}
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
                    {stats.max_drawdown != null ? (stats.max_drawdown > 0 ? `-${stats.max_drawdown}%` : '0%') : '--'} ·{' '}
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
              ) : tab === 'trades' ? (
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
              ) : (
                <div className="mt-2">
                  {rounds.length === 0 ? (
                    <div className="py-6 text-center text-xs text-muted">暂无完整回合 — FIFO 配对买卖后在此展示每笔回合的盈亏明细</div>
                  ) : (
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-[10px] text-muted">
                          <th className="py-1.5 font-normal">标的</th>
                          <th className="py-1.5 text-right font-normal">数量</th>
                          <th className="py-1.5 text-right font-normal">开仓日</th>
                          <th className="py-1.5 text-right font-normal">平仓日</th>
                          <th className="py-1.5 text-right font-normal">持有天数</th>
                          <th className="py-1.5 text-right font-normal">回合盈亏</th>
                          <th className="py-1.5 text-right font-normal">收益率</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {rounds.map((rt, i) => (
                          <tr key={`${rt.symbol}-${rt.open_date}-${rt.close_date}-${i}`} className="border-t border-border/50 transition-colors hover:bg-elevated/40">
                            <td className="py-1.5 font-sans">
                              {rt.symbol}
                              {symbolNames[rt.symbol] && <span className="ml-1.5 text-[10px] text-muted">{symbolNames[rt.symbol]}</span>}
                            </td>
                            <td className="py-1.5 text-right">{rt.qty}</td>
                            <td className="py-1.5 text-right text-muted">{rt.open_date}</td>
                            <td className="py-1.5 text-right text-muted">{rt.close_date}</td>
                            <td className="py-1.5 text-right text-secondary">{rt.holding_days}天</td>
                            <td className={cn('py-1.5 text-right', priceColorClass(rt.pnl))}>
                              {(rt.pnl >= 0 ? '+' : '') + fmtMoney(rt.pnl, 0)}
                            </td>
                            <td className={cn('py-1.5 text-right', priceColorClass(rt.pnl_pct))}>
                              {fmtPct(rt.pnl_pct / 100)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* 右列: 下单 + 自动跟单 + 策略对比 */}
          <div className="space-y-4">
            <OrderForm acc={acc} holdings={holdings} onDone={invalidateAll} />
            <AutoRulesPanel acc={acc} />
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
          accId={acc}
          fees={ov.fees}
          queue={!!ov.queue_limit_orders}
          onSaved={() => setFeeOpen(false)}
          onClose={() => setFeeOpen(false)}
        />
      )}
      {deleteOpen && (
        <DeleteAccountModal
          acc={acc}
          name={name}
          holdingsCount={holdings.length}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => {
            setDeleteOpen(false)
            qc.invalidateQueries({ queryKey: QK.paperAll })
            onDeleted?.()
          }}
        />
      )}
    </div>
  )
}

// ── 扩展注册清单 — 本文件即完整扩展(组件+路由+导航), 单文件可整体摘除/导入 ──
const extension: FrontendExtension = {
  id: 'official.paper',
  apiVersion: 1,
  routes: [
    { id: 'paper', path: '/paper', component: Paper },
  ],
  navigation: [
    { id: 'paper', routeId: 'paper', label: '模拟盘', icon: IconPaper },
  ],
}

export default extension
