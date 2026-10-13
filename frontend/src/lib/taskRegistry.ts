/**
 * 进行中任务注册表 (服务端) — 长任务跨设备接回。
 *
 * 上报一律 fire-and-forget 且失败静默: 注册表缺失/旧后端/网络断 → 功能自动
 * 退化为原来的单机 localStorage 行为, 绝不阻塞或打断任务本身。
 * id = kind + 参数指纹 (djb2 of payload JSON): 任意设备对同一任务幂等更新。
 * 调用频率: 每个任务生命周期 2 次写 (running + 终态), 页面挂载 1 次读, 可忽略。
 */
export type TaskRegistryKind = 'backtest' | 'mining' | 'optimizer' | 'walkforward'
export type TaskRegistryStatus = 'running' | 'done' | 'error'

export interface RegistryTask {
  id: string
  kind: TaskRegistryKind
  label: string
  payload: Record<string, unknown>
  status: TaskRegistryStatus
  started_at: number
  updated_at: number
}

function payloadFingerprint(payload: Record<string, unknown>): string {
  // djb2: 短字符串哈希, 只用于幂等 id, 不求密码学强度
  const s = JSON.stringify(payload)
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0
  return h.toString(36)
}

function taskId(kind: TaskRegistryKind, payload: Record<string, unknown>): string {
  return `${kind}:${payloadFingerprint(payload)}`
}

async function report(
  kind: TaskRegistryKind,
  payload: Record<string, unknown>,
  status: TaskRegistryStatus,
  label = '',
): Promise<void> {
  try {
    await fetch(`/api/task-registry/${encodeURIComponent(taskId(kind, payload))}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, status, label, payload }),
    })
  } catch { /* 静默: 注册表不可用时退化为单机行为 */ }
}

/** 任务启动时上报 running (fire-and-forget, 不 await)。 */
export function reportTaskRunning(
  kind: TaskRegistryKind,
  payload: Record<string, unknown>,
  label = '',
): void {
  void report(kind, payload, 'running', label)
}

/** 任务终态 (完成/失败/取消) 上报 (fire-and-forget, 不 await)。 */
export function reportTaskFinished(
  kind: TaskRegistryKind,
  payload: Record<string, unknown>,
): void {
  void report(kind, payload, 'done')
}

/** 取该类型最近一个 running 任务; 无/不可达返回 null (页面挂载时最多调一次)。 */
export async function fetchRunningTask(
  kind: TaskRegistryKind,
): Promise<RegistryTask | null> {
  try {
    const resp = await fetch(`/api/task-registry?kind=${kind}`)
    if (!resp.ok) return null
    const data = await resp.json() as { items?: RegistryTask[] }
    const items = (data.items ?? []).filter(i => i?.kind === kind && i?.status === 'running')
    // 后端已按 updated_at 倒序; running 且 payload 完整的第一条即目标
    return items.find(i => i.payload && Object.keys(i.payload).length > 0) ?? null
  } catch {
    return null
  }
}
