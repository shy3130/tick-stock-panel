// 数字 / 价格 / 涨跌幅 格式化(§6.0.2 等宽数字)

export function fmtPrice(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${(v * 100).toFixed(digits)}%`
}

export function fmtVolume(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (v >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(0)
}

// A 股语义色:红涨绿跌 → 仅用于价格相关元素
export function priceColorClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-bull' : 'text-bear'
}

export function fmtBigNum(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (v >= 1_000_000_000_000) return `${(v / 1_000_000_000_000).toFixed(2)}万亿`
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return v.toFixed(0)
}

export function fmtDate(s: string | Date | null | undefined): string {
  if (s == null) return '—'
  const d = typeof s === 'string' ? new Date(s) : s
  if (isNaN(d.getTime())) return String(s)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// ===== Data 页面工具函数 =====

export function formatNumber(n: number): string {
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(1)}亿`
  if (n >= 10_000) return `${(n / 10_000).toFixed(1)}万`
  return n.toLocaleString()
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`
}

export function formatScheduleDatePart(iso: string): string {
  const d = new Date(iso)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function formatScheduleTimePart(iso: string): string {
  const d = new Date(iso)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export function isToday(iso: string): boolean {
  const d = new Date(iso)
  const now = new Date()
  return d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate()
}

export function formatLogTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

/**
 * 扩展数据列数字格式化 — 千分位逗号 + 单位换算 + 小数位。
 * 供自选/策略列表的扩展数据 number 单元格统一调用。
 *
 * opts:
 *   - thousandSeparator: true 则加英文逗号(1,234,567)
 *   - unitConvert: 'none'(默认不换算) / 'wan'(÷1e4 加"万") / 'yi'(÷1e8 加"亿") / 'auto'(≥1e8用亿, ≥1e4用万)
 *   - unitDecimals: 换算后保留小数位(默认 2); unitConvert=none 时此值仅控制原值小数位
 */
export function formatExtNumber(
  val: number,
  opts: { thousandSeparator?: boolean; unitConvert?: 'none' | 'wan' | 'yi' | 'auto'; unitDecimals?: number } = {},
): string {
  const { thousandSeparator, unitConvert = 'none', unitDecimals } = opts
  if (!Number.isFinite(val)) return '—'

  let n = val
  let suffix = ''
  let decimals = unitDecimals ?? 2

  if (unitConvert === 'wan') {
    n = val / 1e4
    suffix = '万'
  } else if (unitConvert === 'yi') {
    n = val / 1e8
    suffix = '亿'
  } else if (unitConvert === 'auto') {
    const abs = Math.abs(val)
    if (abs >= 1e8) { n = val / 1e8; suffix = '亿' }
    else if (abs >= 1e4) { n = val / 1e4; suffix = '万' }
    else { decimals = unitDecimals ?? 0 }
  } else {
    // none: 整数不带小数, 小数保留原精度(最多 4 位去尾零)
    decimals = unitDecimals ?? (Number.isInteger(val) ? 0 : 4)
  }

  // toFixed 后去尾零(none 模式下小数原精度场景)
  let str = n.toFixed(decimals)
  if (unitConvert === 'none' && !unitDecimals && !Number.isInteger(val)) {
    // 去掉 toFixed(4) 产生的尾零, 如 1.2300 → 1.23
    str = String(Number(str))
  }

  // 千分位逗号(仅整数部分)
  if (thousandSeparator) {
    const [intPart, fracPart] = str.split('.')
    const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
    str = fracPart != null ? `${grouped}.${fracPart}` : grouped
  }

  return str + suffix
}
