/**
 * 会话合并 (纯函数) — 本地 localStorage 与服务端会话历史的启动合并规则。
 *
 * 规则: 按 id 去重, 同 id 取消息更多的一份 (会话编辑只追加消息, 消息数近似
 * 新旧); 合并后按 createdAt 倒序取前 maxSessions。activeId 优先本地有效值,
 * 其次远端, 兜底第一个。抽成独立纯函数便于单测与未来规则演进。
 */
export interface MergeableSession {
  id: string
  createdAt: number
  messages: unknown[]
}

export function mergeSessions<T extends MergeableSession>(
  local: T[],
  localActiveId: string,
  remote: T[],
  remoteActiveId: string,
  maxSessions: number,
): { sessions: T[]; activeId: string } {
  const byId = new Map<string, T>()
  // 先远端后本地 + >= : 引用相同时本地胜出 (用户当前正在看的保持稳定)
  for (const s of [...remote, ...local]) {
    const existing = byId.get(s.id)
    if (!existing || s.messages.length >= existing.messages.length) byId.set(s.id, s)
  }
  const sessions = [...byId.values()]
    .sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0))
    .slice(0, Math.max(1, maxSessions))
  const activeId = sessions.some(s => s.id === localActiveId)
    ? localActiveId
    : sessions.some(s => s.id === remoteActiveId)
      ? remoteActiveId
      : sessions[0]?.id ?? ''
  return { sessions, activeId }
}
