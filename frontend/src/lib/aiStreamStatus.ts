/**
 * F9: AI 流式生成连接态 —— 三个 store(个股/财务/复盘)共用的连接状态与文案。
 *
 * 生命周期: start → connecting;首个 delta → open;done/error/cancelled/abort → closed。
 * cancelled 是终态, 后续 chunk 不得把状态改回 open/done(各 store 已有守卫)。
 *
 * 纯函数, 无副作用, 供 bun 测试直接覆盖。
 */

export type AiConnection = 'connecting' | 'open' | 'closed'

export interface AiStreamStatusInput {
  phase: 'idle' | 'loading' | 'streaming' | 'done' | 'error' | 'cancelled'
  connection: AiConnection
}

export type AiStatusTone = 'active' | 'error' | 'muted'

export interface AiStreamStatus {
  label: string
  tone: AiStatusTone
}

/**
 * F9: 由 phase 推导下一连接态。
 * streaming → open;done/error/cancelled → closed;其余保持。
 * 终态(closed)不会因后续 chunk 回到 open —— 配合各 store 的 cancelled 终态守卫。
 */
export function nextConnection(phase: AiStreamStatusInput['phase'], prev: AiConnection): AiConnection {
  if (phase === 'streaming') return 'open'
  if (phase === 'done' || phase === 'error' || phase === 'cancelled' || phase === 'idle') return 'closed'
  return prev
}

/**
 * 连接状态条文案:
 * - connecting → 连接中
 * - open       → 生成中
 * - error      → 已断开
 * - cancelled  → 已取消
 * - done/idle  → null(无需展示, 避免与「已完成」等既有文案重复)
 */
export function aiStreamStatus({ phase, connection }: AiStreamStatusInput): AiStreamStatus | null {
  if (phase === 'cancelled') return { label: '已取消', tone: 'muted' }
  if (phase === 'error') return { label: '已断开', tone: 'error' }
  if (connection === 'connecting') return { label: '连接中', tone: 'active' }
  if (connection === 'open') return { label: '生成中', tone: 'active' }
  return null
}
