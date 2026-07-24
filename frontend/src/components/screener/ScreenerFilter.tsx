import { X, RotateCcw, Filter } from 'lucide-react'
import { BOARDS, getBoardType } from '@/lib/board'

// ===== 筛选类型 =====

export interface ScreenerFilter {
  priceMin: string
  priceMax: string
  changePctMin: string
  changePctMax: string
  momentum5dMin: string
  momentum5dMax: string
  amountMin: string      // 成交额最小(亿)
  marketCapMin: string   // 市值最小(亿)
  marketCapMax: string   // 市值最大(亿)
  floatCapMin: string    // 流通市值最小(亿)
  floatCapMax: string    // 流通市值最大(亿)
  volRatioMin: string    // 量比最小
  rsiMin: string
  rsiMax: string
  boards: string[]       // 板块筛选: 空数组=不筛选, 否则只保留选中的板块
  excludeST: boolean     // 是否排除 ST/*ST/退市股
}

export const defaultFilter: ScreenerFilter = {
  priceMin: '', priceMax: '',
  changePctMin: '', changePctMax: '',
  momentum5dMin: '', momentum5dMax: '',
  amountMin: '',
  marketCapMin: '', marketCapMax: '',
  floatCapMin: '', floatCapMax: '',
  volRatioMin: '',
  rsiMin: '', rsiMax: '',
  boards: [],
  excludeST: false,
}

export function filterActive(f: ScreenerFilter): boolean {
  if (f.boards.length > 0) return true
  if (f.excludeST) return true
  return Object.entries(f).some(([k, v]) =>
    k !== 'boards' && k !== 'excludeST' && v !== '' && v !== false,
  )
}

export function countActiveFilters(f: ScreenerFilter): number {
  let n = 0
  if (f.priceMin || f.priceMax) n++
  if (f.changePctMin || f.changePctMax) n++
  if (f.momentum5dMin || f.momentum5dMax) n++
  if (f.amountMin) n++
  if (f.marketCapMin || f.marketCapMax) n++
  if (f.floatCapMin || f.floatCapMax) n++
  if (f.volRatioMin) n++
  if (f.rsiMin || f.rsiMax) n++
  if (f.boards.length > 0) n++
  if (f.excludeST) n++
  return n
}

export function applyFilter(rows: any[], f: ScreenerFilter): any[] {
  if (!filterActive(f)) return rows
  const num = (v: string) => v === '' ? null : Number(v)
  return rows.filter((r) => {
    // 板块: 用 symbol 判定板块, 必须在选中列表里
    // 全选 5 个板块 = 不过滤 (等价于 boards:[]), 避免 getBoardType 返回 null 的边缘品种被误删
    if (f.boards.length > 0 && f.boards.length < BOARDS.length) {
      const board = getBoardType(r.symbol)
      if (!board || !f.boards.includes(board)) return false
    }
    // ST: name 含 ST/*ST/退 的排除 (对齐后端口径 (?i)ST|退)
    if (f.excludeST && /ST|退/i.test(String(r.name ?? ''))) return false
    const close = Number(r.close ?? 0)
    const v = (field: string) => num(field)
    // 现价
    if (v(f.priceMin) != null && close < v(f.priceMin)!) return false
    if (v(f.priceMax) != null && close > v(f.priceMax)!) return false
    // 涨跌幅(%)
    const chg = (r.change_pct ?? 0) * 100
    if (v(f.changePctMin) != null && chg < v(f.changePctMin)!) return false
    if (v(f.changePctMax) != null && chg > v(f.changePctMax)!) return false
    // 5日涨幅(%)
    const m5 = (r.momentum_5d ?? 0) * 100
    if (v(f.momentum5dMin) != null && m5 < v(f.momentum5dMin)!) return false
    if (v(f.momentum5dMax) != null && m5 > v(f.momentum5dMax)!) return false
    // 成交额(亿)
    const amount = (r.amount ?? 0) / 1e8
    if (v(f.amountMin) != null && amount < v(f.amountMin)!) return false
    // 市值(亿)
    const cap = close * (r.total_shares ?? 0) / 1e8
    if (v(f.marketCapMin) != null && cap < v(f.marketCapMin)!) return false
    if (v(f.marketCapMax) != null && cap > v(f.marketCapMax)!) return false
    // 流通市值(亿)
    const fcap = close * (r.float_shares ?? 0) / 1e8
    if (v(f.floatCapMin) != null && fcap < v(f.floatCapMin)!) return false
    if (v(f.floatCapMax) != null && fcap > v(f.floatCapMax)!) return false
    // 量比
    if (v(f.volRatioMin) != null && (r.vol_ratio_5d ?? 0) < v(f.volRatioMin)!) return false
    // RSI
    const rsi = r.rsi_14 ?? 0
    if (v(f.rsiMin) != null && rsi < v(f.rsiMin)!) return false
    if (v(f.rsiMax) != null && rsi > v(f.rsiMax)!) return false
    return true
  })
}

// ===== 筛选面板 =====

export function FilterPanel({ value, onChange, onClose, onReset }: {
  value: ScreenerFilter
  onChange: (f: ScreenerFilter) => void
  onClose: () => void
  onReset: () => void
}) {
  const set = (key: keyof ScreenerFilter, v: string) => onChange({ ...value, [key]: v })

  const toggleBoard = (board: string) => {
    const next = value.boards.includes(board)
      ? value.boards.filter(b => b !== board)
      : [...value.boards, board]
    onChange({ ...value, boards: next })
  }

  // 数值字段只引用 string 类型的 key (排除 boards/excludeST), 避免类型混乱
  type NumKey = keyof Pick<ScreenerFilter,
    'priceMin' | 'priceMax' | 'changePctMin' | 'changePctMax' |
    'momentum5dMin' | 'momentum5dMax' | 'amountMin' |
    'marketCapMin' | 'marketCapMax' | 'floatCapMin' | 'floatCapMax' |
    'volRatioMin' | 'rsiMin' | 'rsiMax'>
  const fields: { label: string; min: NumKey; max: NumKey; unit: string; step?: string }[] = [
    { label: '现价',      min: 'priceMin',      max: 'priceMax',      unit: '元', step: '0.1' },
    { label: '涨跌幅',    min: 'changePctMin',   max: 'changePctMax',  unit: '%' },
    { label: '5日涨幅',   min: 'momentum5dMin',  max: 'momentum5dMax', unit: '%' },
    { label: '成交额',    min: 'amountMin',      max: 'amountMin',     unit: '亿', step: '0.5' },
    { label: '总市值',    min: 'marketCapMin',   max: 'marketCapMax',  unit: '亿', step: '10' },
    { label: '流通市值',  min: 'floatCapMin',    max: 'floatCapMax',   unit: '亿', step: '10' },
    { label: '量比',      min: 'volRatioMin',    max: 'volRatioMin',   unit: '', step: '0.1' },
    { label: 'RSI14',     min: 'rsiMin',         max: 'rsiMax',        unit: '', step: '1' },
  ]

  return (
    <div className="rounded-card border border-accent/30 bg-accent/[0.03] p-4 space-y-3">
      {/* 标题栏: 左侧标题 + 激活计数, 右侧重置 + 关闭 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Filter className="h-3.5 w-3.5 text-accent" />
          <span className="text-xs font-medium text-accent">筛选条件</span>
          {filterActive(value) && (
            <span className="bg-accent/15 text-accent rounded-full px-1.5 h-4 inline-flex items-center text-[10px] font-bold leading-none">
              {countActiveFilters(value)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {filterActive(value) && (
            <button
              onClick={onReset}
              title="清空全部筛选"
              className="inline-flex items-center gap-1 px-1.5 h-6 rounded text-[11px]
                text-muted hover:text-danger hover:bg-danger/10 transition-colors"
            >
              <RotateCcw className="h-3 w-3" />
              清空
            </button>
          )}
          <button
            onClick={onClose}
            title="收起筛选"
            className="p-1 rounded text-secondary hover:text-foreground hover:bg-elevated transition-colors"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* 板块 + ST 快速筛选 (按钮组) */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] text-secondary shrink-0 w-10">市场</span>
        {BOARDS.map(board => {
          const active = value.boards.includes(board)
          return (
            <button
              key={board}
              onClick={() => toggleBoard(board)}
              className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                active
                  ? 'bg-accent/15 text-accent'
                  : 'bg-elevated text-secondary hover:text-foreground hover:bg-elevated/80'
              }`}
            >
              {board}
            </button>
          )
        })}
        <span className="w-px h-4 bg-border mx-1" />
        <button
          onClick={() => onChange({ ...value, excludeST: !value.excludeST })}
          className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
            value.excludeST
              ? 'bg-accent/15 text-accent'
              : 'bg-elevated text-secondary hover:text-foreground hover:bg-elevated/80'
          }`}
        >
          排除ST
        </button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2.5">
        {fields.map((f) => {
          const isRange = f.min !== f.max
          return (
            <div key={f.label} className="flex items-center gap-1.5">
              <span className="text-[11px] text-secondary shrink-0 w-14 text-right">{f.label}</span>
              <input
                type="number"
                placeholder="最小"
                value={value[f.min]}
                onChange={(e) => set(f.min, e.target.value)}
                step={f.step}
                className="w-16 px-1.5 py-1 rounded-btn bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
              />
              {isRange && (
                <>
                  <span className="text-[10px] text-muted">~</span>
                  <input
                    type="number"
                    placeholder="最大"
                    value={value[f.max]}
                    onChange={(e) => set(f.max, e.target.value)}
                    step={f.step}
                    className="w-16 px-1.5 py-1 rounded-btn bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
                  />
                </>
              )}
              {f.unit && <span className="text-[10px] text-muted shrink-0">{f.unit}</span>}
            </div>
          )
        })}
      </div>
      <div className="text-[10px] text-muted/70 pl-1">输入即生效 · 点击市场/ST 按钮切换</div>
    </div>
  )
}
