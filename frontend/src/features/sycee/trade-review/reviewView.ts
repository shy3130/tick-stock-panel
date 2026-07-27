import type { MistakeTag, PnlResult, TradeReviewItem } from './api.ts'

export const MISTAKE_TAG_OPTIONS: Array<{ value: MistakeTag; label: string }> = [
  { value: 'plan_deviation', label: '偏离计划' },
  { value: 'late_entry', label: '入场过晚' },
  { value: 'early_exit', label: '过早退出' },
  { value: 'late_exit', label: '止损拖延' },
  { value: 'oversize', label: '仓位过重' },
  { value: 'thesis_error', label: '逻辑错误' },
  { value: 'execution', label: '执行问题' },
  { value: 'emotional', label: '情绪化' },
]

export type ReviewPnlFilter = 'all' | PnlResult

export interface ReviewFilters {
  strategyId: string
  mistakeTag: '' | MistakeTag
  pnlResult: ReviewPnlFilter
}

export function filterTradeReviewItems(
  items: TradeReviewItem[],
  filters: ReviewFilters,
): TradeReviewItem[] {
  return items.filter(item => {
    if (filters.strategyId && item.review?.strategy_id !== filters.strategyId) return false
    if (filters.mistakeTag && !item.review?.mistake_tags.includes(filters.mistakeTag)) return false
    if (filters.pnlResult !== 'all' && item.attribution?.pnl_result !== filters.pnlResult) return false
    return true
  })
}

export function mistakeTagLabel(tag: MistakeTag): string {
  return MISTAKE_TAG_OPTIONS.find(option => option.value === tag)?.label ?? tag
}
