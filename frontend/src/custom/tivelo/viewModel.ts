import { ApiError } from '@/lib/api'
import type { EngineJobRecord, EngineJobStatus, EngineResearchReport } from '@/lib/engineJobTypes'

export const STATUS: Record<EngineJobStatus, string> = {
  queued: '排队中',
  running: '运行中',
  publishing: '发布中',
  cancelling: '正在取消',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
}
export const active = (status: EngineJobStatus) =>
  ['queued', 'running', 'publishing', 'cancelling'].includes(status)
export const retryable = (status: EngineJobStatus) => ['failed', 'cancelled', 'interrupted'].includes(status)
export const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
export const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : [])
export const text = (value: unknown, fallback = '未提供') => (typeof value === 'string' ? value : fallback)

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return '登录已过期，请重新登录后继续。'
    if (error.status === 403) return '当前账号没有此操作的权限，请联系管理员。'
    if (error.status === 503) return '研究任务服务暂时不可用，请稍后刷新。'
    if (error.status === 404) return '该任务或输入不存在，请刷新列表重新选择。'
  }
  return error instanceof Error ? error.message : '操作未完成，请重试。'
}

export function stageLabel(id: string) {
  const [node, stage = ''] = id.split('.')
  const [kind, index] = stage.split('_')
  const labels: Record<string, string> = {
    sync: '同步数据',
    publish: '发布数据',
    snapshot: '固定快照',
    prepare: '准备回测',
    validate: '校验输入',
    execute: '执行研究',
    report: '生成报告',
  }
  const label = labels[kind]
  return `${node} · ${label ?? stage}${index === undefined ? '' : ` ${Number(index) + 1}`}`
}

export function orderedStages(job: EngineJobRecord) {
  const order = (id: string) => {
    const [, stage = ''] = id.split('.')
    const [kind, index] = stage.split('_')
    if (kind === 'sync' || kind === 'publish') return Number(index) * 2 + (kind === 'publish' ? 1 : 0)
    const positions: Record<string, number> = {
      snapshot: 100,
      prepare: 101,
      validate: 102,
      execute: 103,
      report: 104,
    }
    return positions[kind] ?? 105
  }
  return Object.entries(job.nodes).sort(
    ([a], [b]) => a.split('.')[0].localeCompare(b.split('.')[0]) || order(a) - order(b),
  )
}

export function taskLabel(job: EngineJobRecord) {
  const stages = Object.keys(job.nodes)
  return stages.some(id => id.endsWith('.snapshot')) && !stages.some(id => id.endsWith('.report'))
    ? '数据快照'
    : '研究任务'
}

export const ERROR_HINTS: Record<string, string> = {
  RUNTIME_MISMATCH: '运行环境与锁定版本不同。恢复原环境后重试，或用当前环境另建任务。',
  DATA_CONFLICT: '锁定输入已发生变化。请恢复原输入或另建任务。',
  ARTIFACT_CORRUPT: '输入或结果校验未通过，请检查归档完整性。',
  DATA_COVERAGE_INSUFFICIENT: '数据覆盖不足，请补齐所需日期和标的。',
  RESOURCE_LIMIT: '任务超过资源或重试限制，请检查预算并另建任务。',
  UNAUTHORIZED_FEATURE: '此功能未获得运行授权，请检查扩展状态。',
  RULES_UNAVAILABLE: '所选日期缺少可用市场规则，请检查规则覆盖。',
}

/** Display exact decimal strings, including exponent notation, without a float conversion. */
export function decimalLabel(value: unknown, percent = false): string {
  if (typeof value !== 'string' || value.length > 128) return '未提供'
  const match = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value)
  if (!match) return '未提供'
  const exponent = Number(match[4] ?? 0) + (percent ? 2 : 0)
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 128) return '未提供'
  const digits = match[2] + (match[3] ?? '')
  const point = match[2].length + exponent
  let whole = point <= 0 ? '0' : digits.slice(0, point).padEnd(point, '0')
  let fraction = point < 0 ? '0'.repeat(-point) + digits : digits.slice(point)
  whole = whole.replace(/^0+(?=\d)/, '')
  fraction = fraction.replace(/0+$/, '')
  const negative = match[1] === '-' && /[1-9]/.test(whole + fraction)
  return `${negative ? '-' : ''}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction ? '.' + fraction : ''}${percent ? '%' : ''}`
}

export const priceLabel = (mode: unknown, provider?: unknown) => {
  if (mode === 'extension' && provider === 'tivelo.pro.dynamic') return '动态复权'
  if (mode === 'extension' && typeof provider === 'string' && provider) return provider
  const labels: Record<string, string> = {
    raw: '不复权',
    qfq: '前复权',
    hfq: '后复权',
    extension: '扩展价格视图',
  }
  return labels[text(mode)] ?? '未知价格口径'
}
export const timeLabel = (value: string, options?: Intl.DateTimeFormatOptions) =>
  Number.isNaN(Date.parse(value)) ? '时间未知' : new Date(value).toLocaleString('zh-CN', { hour12: false, ...options })
export const newRequestKey = () =>
  `workbench:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`

export function downloadFile(file: Blob, filename: string) {
  const url = URL.createObjectURL(file)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadReport(report: EngineResearchReport) {
  downloadFile(
    new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }),
    `research-${report.plan_id.slice(0, 12)}-${report.node_id}.json`,
  )
}
