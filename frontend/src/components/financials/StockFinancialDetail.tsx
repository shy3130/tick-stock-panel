import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { CalendarDays, TrendingUp, FileText, Wallet, Activity, Sparkles, AlertTriangle, Loader2, ChartPie } from 'lucide-react'
import {
  useFinancialMetrics,
  useFinancialIncome,
  useFinancialBalanceSheet,
  useFinancialCashFlow,
  useFinancialShares,
} from '@/lib/useFinancials'
import { fmtPrice, fmtBigNum, fmtDate } from '@/lib/format'
import { Skeleton } from '@/components/data/Skeleton'
import { startAnalysis, findLatestHistoryReport, openHistoryReport } from '@/lib/aiReportStore'
import { toast } from '@/components/Toast'

interface Props {
  symbol: string
  name: string
}

type TabKey = 'metrics' | 'income' | 'balance_sheet' | 'cash_flow' | 'shares'

const TABS: { key: TabKey; label: string; icon: typeof TrendingUp }[] = [
  { key: 'metrics', label: '核心指标', icon: TrendingUp },
  { key: 'income', label: '利润表', icon: FileText },
  { key: 'balance_sheet', label: '资产负债表', icon: Wallet },
  { key: 'cash_flow', label: '现金流量表', icon: Activity },
  { key: 'shares', label: '股本', icon: ChartPie },
]

// 字段定义:键 → (中文名, 格式化类型)
// pct=百分点(存的是 12.3 表示 12.3%); amount=金额(元,转亿/万亿); perShare=每股; num=普通数值(保留2位)
type FmtType = 'pct' | 'amount' | 'perShare' | 'num'
type FieldDef = { label: string; fmt: FmtType; group?: string }

const FIELD_DEFS: Record<TabKey, FieldDef[]> = {
  metrics: [
    { label: '基本每股收益 EPS', fmt: 'perShare', key: 'eps_basic' } as any,
    { label: '稀释每股收益 EPS', fmt: 'perShare', key: 'eps_diluted' } as any,
    { label: '每股净资产 BPS', fmt: 'perShare', key: 'bps' } as any,
    { label: '每股经营现金流', fmt: 'perShare', key: 'ocfps' } as any,
    { label: '净资产收益率 ROE', fmt: 'pct', key: 'roe' } as any,
    { label: '稀释 ROE', fmt: 'pct', key: 'roe_diluted' } as any,
    { label: '总资产收益率 ROA', fmt: 'pct', key: 'roa' } as any,
    { label: '销售毛利率', fmt: 'pct', key: 'gross_margin' } as any,
    { label: '销售净利率', fmt: 'pct', key: 'net_margin' } as any,
    { label: '资产负债率', fmt: 'pct', key: 'debt_to_asset_ratio' } as any,
    { label: '营业收入同比增长', fmt: 'pct', key: 'revenue_yoy' } as any,
    { label: '净利润同比增长', fmt: 'pct', key: 'net_income_yoy' } as any,
    { label: '经营现金/营收', fmt: 'pct', key: 'operating_cash_to_revenue' } as any,
    { label: '存货周转率', fmt: 'num', key: 'inventory_turnover' } as any,
  ],
  income: [
    { label: '营业收入', fmt: 'amount', key: 'revenue' } as any,
    { label: '营业成本', fmt: 'amount', key: 'operating_cost' } as any,
    { label: '营业利润', fmt: 'amount', key: 'operating_profit' } as any,
    { label: '销售费用', fmt: 'amount', key: 'selling_expense' } as any,
    { label: '管理费用', fmt: 'amount', key: 'admin_expense' } as any,
    { label: '研发费用', fmt: 'amount', key: 'rd_expense' } as any,
    { label: '财务费用', fmt: 'amount', key: 'financial_expense' } as any,
    { label: '营业外收入', fmt: 'amount', key: 'non_operating_income' } as any,
    { label: '营业外支出', fmt: 'amount', key: 'non_operating_expense' } as any,
    { label: '利润总额', fmt: 'amount', key: 'total_profit' } as any,
    { label: '所得税', fmt: 'amount', key: 'income_tax' } as any,
    { label: '净利润', fmt: 'amount', key: 'net_income' } as any,
    { label: '归母净利润', fmt: 'amount', key: 'net_income_attributable' } as any,
    { label: '扣非净利润', fmt: 'amount', key: 'net_income_deducted' } as any,
    { label: '基本每股收益', fmt: 'perShare', key: 'basic_eps' } as any,
    { label: '稀释每股收益', fmt: 'perShare', key: 'diluted_eps' } as any,
  ],
  balance_sheet: [
    { label: '资产总计', fmt: 'amount', key: 'total_assets' } as any,
    { label: '流动资产合计', fmt: 'amount', key: 'total_current_assets' } as any,
    { label: '非流动资产合计', fmt: 'amount', key: 'total_non_current_assets' } as any,
    { label: '货币资金', fmt: 'amount', key: 'cash_and_equivalents' } as any,
    { label: '应收账款', fmt: 'amount', key: 'accounts_receivable' } as any,
    { label: '存货', fmt: 'amount', key: 'inventory' } as any,
    { label: '固定资产', fmt: 'amount', key: 'fixed_assets' } as any,
    { label: '无形资产', fmt: 'amount', key: 'intangible_assets' } as any,
    { label: '商誉', fmt: 'amount', key: 'goodwill' } as any,
    { label: '负债合计', fmt: 'amount', key: 'total_liabilities' } as any,
    { label: '流动负债合计', fmt: 'amount', key: 'total_current_liabilities' } as any,
    { label: '非流动负债合计', fmt: 'amount', key: 'total_non_current_liabilities' } as any,
    { label: '短期借款', fmt: 'amount', key: 'short_term_borrowing' } as any,
    { label: '长期借款', fmt: 'amount', key: 'long_term_borrowing' } as any,
    { label: '应付账款', fmt: 'amount', key: 'accounts_payable' } as any,
    { label: '所有者权益合计', fmt: 'amount', key: 'total_equity' } as any,
    { label: '归母所有者权益', fmt: 'amount', key: 'equity_attributable' } as any,
    { label: '未分配利润', fmt: 'amount', key: 'retained_earnings' } as any,
    { label: '少数股东权益', fmt: 'amount', key: 'minority_interest' } as any,
  ],
  cash_flow: [
    { label: '经营活动现金流净额', fmt: 'amount', key: 'net_operating_cash_flow' } as any,
    { label: '投资活动现金流净额', fmt: 'amount', key: 'net_investing_cash_flow' } as any,
    { label: '筹资活动现金流净额', fmt: 'amount', key: 'net_financing_cash_flow' } as any,
    { label: '固定资产/无形资产投资', fmt: 'amount', key: 'capex' } as any,
    { label: '现金及等价物净增加额', fmt: 'amount', key: 'net_cash_change' } as any,
  ],
  shares: [
    { label: '总股本', fmt: 'amount', key: 'total_shares' } as any,
    { label: '流通股本', fmt: 'amount', key: 'float_shares' } as any,
  ],
}

function formatValue(v: number | null | undefined, fmt: FmtType): string {
  if (v == null || Number.isNaN(v)) return '—'
  switch (fmt) {
    case 'pct':
      // 存储的是百分点(12.3 表示 12.3%),直接保留2位 + %
      return `${v.toFixed(2)}%`
    case 'amount':
      // 金额(元)→ 亿/万亿;保留负号
      return fmtBigNum(v)
    case 'perShare':
      return fmtPrice(v, 2)
    case 'num':
    default:
      return v.toFixed(2)
  }
}

export function StockFinancialDetail({ symbol, name }: Props) {
  const [tab, setTab] = useState<TabKey>('metrics')
  // AI 分析:点击时检查历史,若已有同标的报告则二次确认
  const [checking, setChecking] = useState(false)
  const [confirmReport, setConfirmReport] = useState<{ id: string; created_at: string; focus: string } | null>(null)

  const handleAiClick = async () => {
    if (checking) return
    setChecking(true)
    try {
      const latest = await findLatestHistoryReport(symbol)
      if (latest) {
        // 有历史报告 → 弹二次确认
        setConfirmReport({ id: latest.id, created_at: latest.created_at, focus: latest.focus })
      } else {
        // 无历史 → 直接分析
        await doAnalysis()
      }
    } catch {
      // 查询失败不阻塞,直接分析
      await doAnalysis()
    } finally {
      setChecking(false)
    }
  }

  const doAnalysis = async () => {
    const r = await startAnalysis(symbol, name)
    if (r.error) toast(r.error, 'error')
  }

  const metrics = useFinancialMetrics(symbol)
  const income = useFinancialIncome(symbol)
  const balance = useFinancialBalanceSheet(symbol)
  const cashFlow = useFinancialCashFlow(symbol)
  const shares = useFinancialShares(symbol)

  const queryMap = {
    metrics: metrics,
    income: income,
    balance_sheet: balance,
    cash_flow: cashFlow,
    shares: shares,
  } as const

  const current = queryMap[tab]
  // 按 period_end 降序(最新在前);同步默认 latest_only,通常只有1期
  const rows = (current.data?.data ?? []).slice().sort((a, b) =>
    (b.period_end ?? '').localeCompare(a.period_end ?? '')
  )
  const fieldDefs = FIELD_DEFS[tab]

  // 头部报告期信息取最新一期(优先用当前 tab,兜底用 metrics)
  const latestPeriod = rows[0]?.period_end ?? metrics.data?.data?.[0]?.period_end ?? null
  const latestAnnounce = rows[0]?.announce_date ?? metrics.data?.data?.[0]?.announce_date ?? null

  return (
    <div className="rounded-card border border-border bg-surface overflow-hidden">
      {/* 头部:标的 + 报告期 */}
      <div className="px-5 py-4 border-b border-border flex items-center gap-3 flex-wrap">
        <div className="flex items-baseline gap-2 min-w-0">
          <span className="text-lg font-semibold text-foreground">{name}</span>
          <span className="text-xs font-mono text-muted">{symbol}</span>
        </div>
        <div className="flex items-center gap-2 ml-auto">
          <button
            onClick={handleAiClick}
            disabled={checking}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-btn text-[11px] font-medium border border-purple-400/30 bg-purple-400/10 text-purple-300 hover:bg-purple-400/20 hover:border-purple-400/40 transition-all shrink-0 disabled:opacity-50"
            title="AI 财务分析"
          >
            {checking ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}
            AI 财务分析
          </button>
          {latestPeriod && (
            <div className="flex items-center gap-1.5 text-xs text-secondary">
              <CalendarDays className="h-3.5 w-3.5" />
              <span>报告期 <span className="font-mono">{latestPeriod}</span></span>
              {latestAnnounce && (
                <span className="text-muted">· 披露 {fmtDate(latestAnnounce)}</span>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 标签页 */}
      <div className="flex items-center gap-1 px-3 pt-2 border-b border-border/60 overflow-x-auto">
        {TABS.map(t => {
          const Icon = t.icon
          const isActive = tab === t.key
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors ${
                isActive
                  ? 'border-accent text-accent'
                  : 'border-transparent text-muted hover:text-secondary'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          )
        })}
      </div>

      {/* 表格内容 */}
      <div className="p-4">
        {current.isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="flex justify-between">
                <Skeleton w="w-32" h="h-4" />
                <Skeleton w="w-20" h="h-4" />
              </div>
            ))}
          </div>
        ) : rows.length === 0 ? (
          <div className="py-10 text-center text-xs text-muted">
            暂无{TABS.find(t => t.key === tab)?.label}数据 — 可点击顶部「全部同步」拉取
          </div>
        ) : (
          <div className="space-y-5">
            {/* 多期时为每期渲染一组;单期时只有一组 */}
            {rows.map((row, ri) => (
              <div key={row.period_end ?? ri}>
                {rows.length > 1 && (
                  <div className="text-[11px] text-muted mb-2 flex items-center gap-1.5">
                    <CalendarDays className="h-3 w-3" />
                    报告期 <span className="font-mono text-secondary">{row.period_end}</span>
                  </div>
                )}
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-0">
                  {fieldDefs.map((def: any) => {
                    const val = row[def.key]
                    return (
                      <div
                        key={def.key}
                        className="flex items-baseline justify-between gap-3 py-2 border-b border-border/40"
                      >
                        <span className="text-xs text-secondary shrink-0">{def.label}</span>
                        <span className="text-sm font-mono tabular-nums text-foreground text-right">
                          {formatValue(val, def.fmt)}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* AI 分析二次确认:已有该标的历史报告 */}
      <AnimatePresence>
        {confirmReport && (
          <div className="fixed inset-0 z-[70] flex items-center justify-center">
            <motion.div
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="absolute inset-0 bg-black/50 backdrop-blur-sm"
              onClick={() => setConfirmReport(null)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 12 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97, y: 8 }}
              transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-[90vw] max-w-[400px] rounded-card border border-border bg-base shadow-2xl p-6"
            >
              <div className="flex items-start gap-3">
                <div className="shrink-0 h-10 w-10 rounded-full bg-purple-400/12 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-purple-300" />
                </div>
                <div className="min-w-0 flex-1">
                  <h3 className="text-sm font-semibold text-foreground mb-1.5">该个股已有分析报告</h3>
                  <p className="text-xs text-secondary leading-relaxed">
                    <span className="font-medium text-foreground">{name}</span>
                    <span className="font-mono text-muted"> {symbol}</span> 在
                    <span className="text-purple-300 font-medium"> {fmtReportTime(confirmReport.created_at)} </span>
                    已生成过 AI 财务分析报告。
                  </p>
                  <p className="mt-2 text-[11px] text-muted">
                    您可以查看历史报告,或基于最新数据重新生成一份。
                  </p>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 mt-5">
                <button
                  onClick={() => setConfirmReport(null)}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-xs transition-colors"
                >
                  取消
                </button>
                <button
                  onClick={() => { if (confirmReport) openHistoryReport(confirmReport.id); setConfirmReport(null) }}
                  className="px-3 py-1.5 rounded-btn border border-border text-secondary hover:text-foreground text-xs font-medium transition-colors"
                >
                  查看历史报告
                </button>
                <button
                  onClick={() => { doAnalysis(); setConfirmReport(null) }}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-purple-500/80 to-fuchsia-500/80 text-white text-xs font-medium hover:from-purple-500 hover:to-fuchsia-500 transition-all"
                >
                  <Sparkles className="h-3.5 w-3.5" />
                  重新分析
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  )
}

// 历史报告时间友好显示
function fmtReportTime(iso: string): string {
  try {
    const t = new Date(iso).getTime()
    const diff = Date.now() - t
    if (diff < 60_000) return '刚刚'
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
    if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
    if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
    return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch { return '' }
}
