/**
 * 逐笔成交金额分档（元）。
 *
 * 只按已验证可靠的单笔 amount 分类；不依赖 direction/side，避免在
 * 上游编码语义未统一时把字段解释成主动买卖。
 */
export const TRANSACTION_AMOUNT_TIERS = [
  { min: 1_000_000, size: 13, color: '#EF4444', label: '≥100万' },
  { min: 200_000, size: 8, color: '#F59E0B', label: '20-100万' },
  { min: 40_000, size: 5, color: '#60A5FA', label: '4-20万' },
  { min: 0, size: 3, color: 'rgba(161,161,161,0.45)', label: '<4万' },
] as const

export type TransactionAmountTier = (typeof TRANSACTION_AMOUNT_TIERS)[number]

export function transactionAmountTier(amount: number | null | undefined): TransactionAmountTier {
  const value = typeof amount === 'number' && Number.isFinite(amount) ? amount : 0
  return TRANSACTION_AMOUNT_TIERS.find(tier => value >= tier.min)
    ?? TRANSACTION_AMOUNT_TIERS[TRANSACTION_AMOUNT_TIERS.length - 1]
}
