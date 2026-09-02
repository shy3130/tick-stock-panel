import { asArray, asBoolean, asRecord, asString } from './parse'

export class ResearchApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly field: string | null
  readonly details: Record<string, unknown>

  constructor(input: {
    status: number
    code: string
    message: string
    retryable?: boolean
    field?: string | null
    details?: Record<string, unknown>
  }) {
    super(input.message)
    this.name = 'ResearchApiError'
    this.status = input.status
    this.code = input.code
    this.retryable = input.retryable ?? (input.status >= 500)
    this.field = input.field ?? null
    this.details = input.details ?? {}
  }

  get isPreflightBlocked(): boolean {
    return this.status === 409 && (this.code === 'preflight_blocked' || this.code === 'conflict')
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  get isQueueBusy(): boolean {
    return this.status === 429 || this.code === 'full_market_queue_busy'
  }
}

export function isResearchApiError(error: unknown): error is ResearchApiError {
  return error instanceof ResearchApiError
}

export function researchErrorMessage(error: unknown): string {
  if (isResearchApiError(error)) return error.message
  if (error instanceof Error && error.message) return error.message
  return '研究请求未完成，请稍后重试。'
}

function formatFastapiDetail(detail: unknown): { message: string; field: string | null; details: Record<string, unknown> } {
  if (typeof detail === 'string' && detail.trim()) {
    return { message: detail, field: null, details: {} }
  }
  const issues = asArray(detail)
  if (issues.length > 0) {
    const messages = issues.map((item) => {
      const rec = asRecord(item)
      if (!rec) return String(item)
      const loc = asArray(rec.loc).map(String).filter((part) => part !== 'body').join('.')
      const msg = asString(rec.msg) ?? '字段无效'
      return loc ? `${loc}: ${msg}` : msg
    })
    const first = asRecord(issues[0])
    const loc = first ? asArray(first.loc).map(String).filter((part) => part !== 'body') : []
    return {
      message: messages.join('；'),
      field: loc.length ? loc[loc.length - 1] ?? null : null,
      details: { issues },
    }
  }
  const rec = asRecord(detail)
  if (rec) {
    return {
      message: asString(rec.message) ?? asString(rec.msg) ?? '研究请求失败',
      field: asString(rec.field),
      details: rec,
    }
  }
  return { message: '研究请求失败', field: null, details: {} }
}

export function researchApiErrorFromBody(status: number, body: unknown): ResearchApiError {
  const envelope = unwrapResearchError(body)
  if (envelope) {
    return new ResearchApiError({
      status,
      code: asString(envelope.code) ?? statusToCode(status),
      message: asString(envelope.message) ?? statusText(status),
      retryable: asBoolean(envelope.retryable) ?? status >= 500,
      field: asString(envelope.field),
      details: asRecord(envelope.details) ?? {},
    })
  }
  const rec = asRecord(body)
  if (rec && rec.detail !== undefined) {
    const parsed = formatFastapiDetail(rec.detail)
    return new ResearchApiError({
      status,
      code: status === 422 ? 'validation_error' : statusToCode(status),
      message: parsed.message,
      retryable: status >= 500,
      field: parsed.field,
      details: parsed.details,
    })
  }
  const message = rec
    ? asString(rec.message) ?? asString(rec.msg) ?? statusText(status)
    : typeof body === 'string' && body.trim()
      ? body
      : statusText(status)
  return new ResearchApiError({
    status,
    code: statusToCode(status),
    message,
    retryable: status >= 500,
  })
}

function unwrapResearchError(body: unknown): Record<string, unknown> | null {
  const rec = asRecord(body)
  if (!rec) return null
  const direct = asRecord(rec.error)
  if (direct && (direct.code != null || direct.message != null)) return direct
  const detail = asRecord(rec.detail)
  if (!detail) return null
  const nested = asRecord(detail.error)
  if (nested && (nested.code != null || nested.message != null)) return nested
  if (detail.code != null || detail.message != null) return detail
  return null
}

function statusToCode(status: number): string {
  switch (status) {
    case 400: return 'bad_request'
    case 404: return 'not_found'
    case 409: return 'conflict'
    case 422: return 'validation_error'
    case 429: return 'full_market_queue_busy'
    case 500: return 'internal_error'
    case 503: return 'service_unavailable'
    default: return `http_${status}`
  }
}

function statusText(status: number): string {
  switch (status) {
    case 400: return '研究请求语义错误'
    case 404: return '因子或运行不存在'
    case 409: return '当前状态不允许该操作'
    case 422: return '参数未通过校验'
    case 429: return '全市场研究队列已满'
    case 500: return '研究服务内部错误'
    case 503: return '研究基础设施暂不可用'
    default: return `研究请求失败（${status}）`
  }
}
