import { extractPoolCard } from './agentPoolCard.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

assert(extractPoolCard(null) == null, 'null 应 fail-closed')
assert(extractPoolCard({ status: 'error', pool_id: 'x' }) == null, '非 success 应拒绝')
assert(extractPoolCard({ status: 'success', pool_id: '', as_of: '2026-08-20', total: 3 }) == null, '空 pool_id 应拒绝')
assert(extractPoolCard({ status: 'success', pool_id: 'p1', as_of: '20260820', total: 3 }) == null, '非法 as_of 应拒绝')
assert(extractPoolCard({ status: 'success', pool_id: 'p1', as_of: '2026-08-20', total: 0 }) == null, 'total<=0 应拒绝')

const card = extractPoolCard({
  status: 'success',
  pool_id: '  abcd  ',
  as_of: '2026-08-20',
  total: 12.9,
  preview: [{ symbol: 'SH600000', name: '浦发' }, { symbol: '  ' }, null, { symbol: 'SZ000001' }],
})
assert(card != null, '合法 payload 应抽出卡片')
assert(card.pool_id === 'abcd', 'pool_id 应 trim')
assert(card.total === 12, 'total 应向下取整')
assert(card.previewSymbols.join(',') === 'SH600000,SZ000001', '预览只保留非空 symbol')

console.log('agentPoolCard.test.ts ok')
