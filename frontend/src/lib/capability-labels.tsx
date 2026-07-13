// capability 内部名 → 用户能理解的中文标签
export const CAP_LABELS: Record<string, { name: string; hint: string }> = {
  'quote.by_symbol':         { name: '自选股实时监控', hint: 'Free 可按标的查询实时行情,用于少量自选股监控' },
  'quote.batch':             { name: '实时行情(批量)',   hint: '一次拿多只股票的价' },
  'quote.pool':              { name: '标的池查询',        hint: '按沪深300等池子拿行情' },
  'kline.daily.by_symbol':   { name: '日 K(按标的)',    hint: '单只股票历史日 K' },
  'kline.daily.batch':       { name: '日 K(批量)',      hint: '一次拿多只股票的日 K — 选股 / 信号扫描 必需' },
  'kline.minute.by_symbol':  { name: '分钟 K(按标的)',  hint: '单股 1m/5m/15m/30m/60m K 线' },
  'kline.minute.batch':      { name: '分钟 K(批量)',    hint: '多股分钟 K' },

  'depth5':                  { name: '五档盘口',          hint: '买卖五档报价' },
  'websocket':               { name: '实时推送(WS)',    hint: '免轮询的实时行情订阅' },
  'financial':               { name: '财务数据',          hint: '利润表 / 资负表 / 现金流 / 关键指标' },
  'adj_factor':              { name: '复权因子',          hint: '让 MA/MACD 等指标在分红送转日不失真' },
}

// 套餐等级 —— 用于按档位门控功能(如专线端点 / 按月扩展分钟K)。
// 基础档提取与后端 quote_service.py 一致:取 label 第一个词("Pro +" → "pro")。
// none = None 档(无 key / 无效 key),低于 free,仅历史日K无实时行情。
export const TIER_RANK: Record<string, number> = { none: -1, free: 0, starter: 1, pro: 2, expert: 3 }
export const EXPERT_RANK = TIER_RANK.expert

export function tierRank(label: string): number {
  const base = (label.split(' ')[0] ?? '').split('+')[0].trim().toLowerCase()
  return TIER_RANK[base] ?? -1
}

export function isExpertOrAbove(label: string): boolean {
  return tierRank(label) >= EXPERT_RANK
}

/** 档位完整样式(tag 背景 + 圆点 + 文字渐变),供设置页统一展示。 */
export interface TierStyle {
  tagBg: { background: string }
  dotStyle: { background: string }
  labelTextStyle: { color?: string; background?: string; WebkitBackgroundClip?: string; backgroundClip?: string }
  desc: string
}

const TIER_STYLE: Record<string, TierStyle> = {
  none: {
    desc: '未配置 Key · 仅历史日K',
    tagBg: { background: 'rgba(113,113,122,0.15)' },
    dotStyle: { background: '#52525b' },
    labelTextStyle: { color: '#71717a' },
  },
  free: {
    desc: '历史日K · 自选实时',
    tagBg: { background: 'rgba(113,113,122,0.3)' },
    dotStyle: { background: '#71717a' },
    labelTextStyle: { color: '#a1a1aa' },
  },
  starter: {
    desc: '除权因子 · 全市场实时',
    tagBg: { background: 'rgba(59,130,246,0.2)' },
    dotStyle: { background: '#3b82f6' },
    labelTextStyle: { color: '#60a5fa' },
  },
  pro: {
    desc: '分钟K · 盘口',
    tagBg: { background: 'linear-gradient(135deg, rgba(168,85,247,0.2), rgba(124,58,237,0.15))' },
    dotStyle: { background: 'linear-gradient(135deg, #a855f7, #7c3aed)' },
    labelTextStyle: { background: 'linear-gradient(135deg, #c084fc, #a855f7)', WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' },
  },
  expert: {
    desc: 'WebSocket · 财务数据',
    tagBg: { background: 'linear-gradient(135deg, rgba(59,130,246,0.2), rgba(168,85,247,0.2), rgba(245,158,11,0.2))' },
    dotStyle: { background: 'linear-gradient(135deg, #3b82f6, #a855f7, #f59e0b)' },
    labelTextStyle: { background: 'linear-gradient(135deg, #60a5fa, #c084fc, #fbbf24)', WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' },
  },
}

/** 从档位 label 提取基础档位名(小写): "Expert +" → "expert" */
export function tierBaseName(label: string): string {
  return (label.split(' ')[0] ?? '').split('+')[0].trim().toLowerCase()
}

/** 返回档位完整样式 */
export function tierStyle(label: string): TierStyle {
  return TIER_STYLE[tierBaseName(label)] ?? TIER_STYLE.free
}

/** 所有档位(有序, 供档位列表渲染) */
export const ALL_TIERS = ['none', 'free', 'starter', 'pro', 'expert'] as const

/** 返回档位标签的渐变文字样式(用于大字显示, 如 Keys 页档位) */
export function tierTextStyle(label: string): { color?: string; background?: string; WebkitBackgroundClip?: string; backgroundClip?: string } {
  return tierStyle(label).labelTextStyle
}

/** 渲染档位 tag。 */
export function TierTag({ label, className = '' }: { label: string; className?: string }) {
  const t = tierStyle(label)
  const base = tierBaseName(label)
  // none 档显示英文「None」,其余档显示英文档名
  const display = base === 'none' ? 'None' : base
  return (
    <span
      className={`inline-flex h-[18px] max-w-[80px] shrink-0 items-center overflow-hidden rounded px-1.5 text-[10px] font-bold font-mono leading-none ${className}`}
      style={t.tagBg}
    >
      <span className="truncate capitalize" style={t.labelTextStyle}>{display}</span>
    </span>
  )
}
