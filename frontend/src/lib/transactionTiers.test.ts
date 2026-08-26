import { transactionAmountTier } from './transactionTiers.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

const cases: Array<[number | null | undefined, string]> = [
  [1_000_000, '≥100万'],
  [999_999, '20-100万'],
  [200_000, '20-100万'],
  [199_999, '4-20万'],
  [40_000, '4-20万'],
  [39_999, '<4万'],
  [0, '<4万'],
  [null, '<4万'],
  [undefined, '<4万'],
  [Number.NaN, '<4万'],
]

for (const [amount, label] of cases) {
  assert(
    transactionAmountTier(amount).label === label,
    `amount=${String(amount)} should be ${label}`,
  )
}

console.log(`${cases.length}/${cases.length} transaction amount tier cases passed`)
