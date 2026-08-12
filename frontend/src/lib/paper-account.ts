import type { AdvisorActionState } from './advisor'

export type PaperTradeSide = 'BUY' | 'SELL'

export interface PaperFeeAssumptions {
  commission_rate: number
  commission_rate_label: string
  minimum_commission: number
  sell_stamp_tax_rate: number
  sell_stamp_tax_rate_label: string
  slippage: string
  disclaimer: string
}

export interface PaperPosition {
  symbol: string
  name: string
  quantity: number
  sellable_quantity: number
  average_cost: number
  cost_basis: number
  mark_price: number
  marked_value: number
  market_value: number
  unrealized_pnl: number
  mark_source: 'STRATEGY_CACHE' | 'COST_FALLBACK'
  portfolio_weight_pct: number
  invested_weight_pct: number
}

export type PaperConcentrationLevel = 'NONE' | 'LOW' | 'MODERATE' | 'HIGH' | 'EXTREME'

export interface PaperPortfolioRiskWarning {
  code: string
  message: string
}

export interface PaperPortfolioRisk {
  position_count: number
  cash_pct: number
  invested_pct: number
  largest_position_pct: number
  largest_invested_position_pct: number
  concentration_hhi: number
  concentration_level: PaperConcentrationLevel
  warnings: PaperPortfolioRiskWarning[]
}

export interface PaperValuationWarning {
  code: string
  symbol: string
  message: string
}

export interface PaperJournalEntry {
  id: string
  timestamp: string
  side: PaperTradeSide
  symbol: string
  name: string
  trade_date: string
  quantity: number
  plan_note: string
  invalidation_note: string
  price: number
  gross_amount: number
  cash_before: number
  cash_after: number
  commission: number
  stamp_tax: number
  total_fees: number
  fifo_cost_basis: number
  realized_pnl: number
}

export interface PaperAccountResponse {
  schema_version: number
  valuation_date: string
  initial_cash: number
  cash: number
  cost_basis: number
  marked_value: number
  market_value: number
  total_equity: number
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  positions: PaperPosition[]
  portfolio_risk: PaperPortfolioRisk
  fee_assumptions: PaperFeeAssumptions
  valuation_warnings: PaperValuationWarning[]
  journal: PaperJournalEntry[]
}

export interface PaperResetRequest {
  initial_cash: 5000 | 10000
  confirmation: 'RESET'
}

export interface PaperTradeRequest {
  symbol: string
  name: string
  side: PaperTradeSide
  quantity: number
  price: number
  trade_date: string
  plan_note: string
  invalidation_note: string
}

export interface PaperTradeDraft {
  symbol: string
  name: string
  side: PaperTradeSide
  quantity: string
  price: string
  trade_date: string
  plan_note: string
  invalidation_note: string
}

export type PaperMutationOperation = 'reset' | 'trade'

export interface PaperPortfolioRiskPresentation {
  label: string
  tone: 'neutral' | 'safe' | 'warning' | 'danger'
  detail: string
}

const STOCK_SYMBOL = /^(?:(?:600|601|603|605|688|689)\d{3}\.SH|(?:000|001|002|003|300|301)\d{3}\.SZ|(?:4\d{5}|8\d{5}|92\d{4})\.BJ)$/

const CONCENTRATION_PRESENTATION: Record<PaperConcentrationLevel, {
  label: string
  tone: PaperPortfolioRiskPresentation['tone']
}> = {
  NONE: { label: '暂无持仓', tone: 'neutral' },
  LOW: { label: '集中度较低', tone: 'safe' },
  MODERATE: { label: '集中度需关注', tone: 'warning' },
  HIGH: { label: '高度集中', tone: 'warning' },
  EXTREME: { label: '极高集中', tone: 'danger' },
}

export function portfolioRiskPresentation(
  risk: PaperPortfolioRisk,
): PaperPortfolioRiskPresentation {
  const presentation = CONCENTRATION_PRESENTATION[risk.concentration_level]
  const detail = risk.position_count === 0
    ? `已用资金 ${risk.invested_pct.toFixed(2)}% · 现金 ${risk.cash_pct.toFixed(2)}%`
    : `已用资金 ${risk.invested_pct.toFixed(2)}% · 最大单票占已投资部分 ${risk.largest_invested_position_pct.toFixed(2)}%`
  return {
    ...presentation,
    detail,
  }
}

function localCalendarDate(now: Date): string {
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function createPaperTradeDraft(now = new Date()): PaperTradeDraft {
  return {
    symbol: '',
    name: '',
    side: 'BUY',
    quantity: '',
    price: '',
    trade_date: localCalendarDate(now),
    plan_note: '',
    invalidation_note: '',
  }
}

export function preparePaperTradeDraftForSubmit(
  draft: PaperTradeDraft,
  tradeDateEdited: boolean,
  now = new Date(),
): PaperTradeDraft {
  return {
    ...draft,
    trade_date: tradeDateEdited ? draft.trade_date : localCalendarDate(now),
  }
}

export function paperMutationErrorMessage(
  error: unknown,
  operation: PaperMutationOperation,
): string {
  const message = error instanceof Error ? error.message.trim() : ''
  if (/[\u3400-\u9fff]/u.test(message)) {
    if (message.includes('下一步')) {
      return message
    }
    const separator = /[。！？；.!?;]$/u.test(message) ? '' : '。'
    const nextAction = operation === 'reset'
      ? '下一步：请刷新账户确认当前状态，确认尚未重置后再重新提交。'
      : '下一步：请刷新账户及最近记录确认结果，确认尚未记录后再重试。'
    return `${message}${separator}${nextAction}`
  }
  return operation === 'reset'
    ? '无法连接本地服务，账户重置尚未确认。下一步：检查应用服务是否运行，再重新提交。'
    : '无法连接本地服务，模拟成交尚未确认记录。下一步：检查应用服务是否运行，刷新账户后再重试。'
}

export function lotGuidance(symbol: string, side: PaperTradeSide): string {
  if (side === 'SELL') {
    return '模拟卖出以账户显示的可卖数量为准（T+1）'
  }
  if (symbol.trim().toUpperCase().startsWith('688')
    || symbol.trim().toUpperCase().startsWith('689')) {
    return '科创板：至少 200 股，之后按 100 股递增'
  }
  return '普通股票：100 股的整数倍'
}

export function canRecordPaperTrade(
  actionState: AdvisorActionState | undefined,
  side: PaperTradeSide = 'BUY',
): boolean {
  return side === 'SELL' || actionState === 'SIMULATE_ONLY'
}

export function validatePaperTradeDraft(
  draft: PaperTradeDraft,
  actionState: AdvisorActionState | undefined,
): string[] {
  if (draft.side === 'BUY') {
    if (!actionState) {
      return [
        '今日行动尚未读取完成，不能记录模拟买入。下一步：请先刷新日报。',
      ]
    }
    if (actionState === 'OBSERVE_ONLY') {
      return [
        '今日安全检查未通过，不能记录模拟买入。下一步：先处理页面显示的数据或市场问题。',
      ]
    }
    if (actionState === 'RESEARCH_ONLY') {
      return [
        '候选只完成第 1 个确认日，不能记录模拟买入。下一步：等待下一可信交易日复核。',
      ]
    }
    if (actionState === 'NO_CANDIDATE') {
      return [
        '本批没有可模拟候选，不能记录模拟买入。下一步：查看淘汰原因并等待新结果。',
      ]
    }
    if (actionState === 'MODEL_WARNING') {
      return [
        '模型正在校准，不能记录新的模拟买入。下一步：先完成最近 10 个交易日回放。',
      ]
    }
  }

  const errors: string[] = []
  const symbol = draft.symbol.trim().toUpperCase()
  if (!symbol) {
    errors.push('请填写带交易所后缀的六位股票代码，例如 600000.SH。')
  } else if (!STOCK_SYMBOL.test(symbol)) {
    errors.push('当前仅支持带 .SH、.SZ 或 .BJ 后缀的中国内地股票。')
  }
  if (!draft.name.trim()) {
    errors.push('请填写股票名称。')
  } else if (draft.name.length > 80) {
    errors.push('股票名称不能超过 80 个字符。')
  }

  const quantity = Number(draft.quantity)
  const validQuantity = /^\d+$/.test(draft.quantity.trim())
    && Number.isSafeInteger(quantity)
    && quantity > 0
  if (!validQuantity) {
    errors.push('模拟数量必须是大于 0 的整数股数。')
  } else if (draft.side === 'BUY') {
    if (symbol.startsWith('688') || symbol.startsWith('689')) {
      if (quantity < 200) {
        errors.push('科创板模拟数量至少为 200 股。')
      } else if (quantity % 100 !== 0) {
        errors.push('科创板模拟数量在 200 股起按 100 股递增。')
      }
    } else if (quantity % 100 !== 0) {
      errors.push('普通股票的模拟数量必须是 100 股的整数倍。')
    }
  }

  const price = Number(draft.price)
  if (!draft.price.trim() || !Number.isFinite(price) || price <= 0) {
    errors.push('模拟成交价必须是大于 0 的有限数值。')
  }
  if (!draft.trade_date.trim()) {
    errors.push('请填写模拟成交日期。')
  }
  if (draft.plan_note.length > 500) {
    errors.push('模拟计划不能超过 500 个字符。')
  }
  if (draft.invalidation_note.length > 500) {
    errors.push('失效条件不能超过 500 个字符。')
  }
  return errors
}

export function toPaperTradeRequest(draft: PaperTradeDraft): PaperTradeRequest {
  return {
    symbol: draft.symbol.trim().toUpperCase(),
    name: draft.name,
    side: draft.side,
    quantity: Number(draft.quantity),
    price: Number(draft.price),
    trade_date: draft.trade_date,
    plan_note: draft.plan_note,
    invalidation_note: draft.invalidation_note,
  }
}
