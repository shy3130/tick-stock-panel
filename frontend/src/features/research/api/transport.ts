import { researchApiErrorFromBody, ResearchApiError } from '../model/errors'

export async function researchRequest<T>(
  path: string,
  init: RequestInit | undefined,
  parse: (json: unknown) => T,
  opts?: { nullOn404?: boolean },
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (init?.body && !(init.body instanceof FormData)) headers['Content-Type'] = 'application/json'
  if (init?.headers) Object.assign(headers, init.headers as Record<string, string>)

  let res: Response
  try {
    res = await fetch(path, { ...init, headers })
  } catch {
    throw new ResearchApiError({
      status: 0,
      code: 'network_error',
      message: '无法连接研究服务',
      retryable: true,
    })
  }

  if (res.status === 204) return parse(undefined)

  const text = await res.text()
  let json: unknown = null
  if (text) {
    try {
      json = JSON.parse(text)
    } catch {
      json = text
    }
  }

  if (!res.ok) {
    if (opts?.nullOn404 && res.status === 404) return parse(null)
    throw researchApiErrorFromBody(res.status, json)
  }
  return parse(json)
}

export function jsonBody(payload: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(payload) }
}
