/**
 * 应用更新检查 — 单例 store (useSyncExternalStore)。
 *
 * 侧栏左下角版本号徽标与 设置→系统 的「检查更新」共用同一状态与网络请求。
 * 静默检查结果按天缓存 localStorage (GitHub 未认证 API 限额 60 次/时):
 * 同一版本 24h 内只发一次真实请求; 手动检查 (force) 绕过缓存立即刷新。
 * 检查失败不落缓存, 下次启动仍会重试, 但单次会话只静默检查一次。
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { useVersion } from '@/lib/useSharedQueries'

// 检查更新的目标仓库 (Release 清单来源) — 仓库已由 tickflow-stock-panel 改名,
// 用当前名可少依赖一次 GitHub 的改名重定向
const UPDATE_REPO = 'shy3130/tick-stock-panel'
const CACHE_KEY = 'update_check_cache'
const CACHE_TTL_MS = 24 * 60 * 60 * 1000

export type UpdateStatus = 'idle' | 'checking' | 'latest' | 'found' | 'error'

export interface UpdateInfo {
  latest: string
  url: string
}

interface UpdateState {
  status: UpdateStatus
  info: UpdateInfo | null
  checkedAt: number
}

let state: UpdateState = { status: 'idle', info: null, checkedAt: 0 }
const listeners = new Set<() => void>()

function setState(next: Partial<UpdateState>): void {
  state = { ...state, ...next }
  listeners.forEach((fn) => fn())
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

export function getUpdateState(): UpdateState {
  return state
}

/** 语义版本比较: a<b → -1, 相等 → 0, a>b → 1; 无法解析按 0.0.0 处理 */
export function compareVersion(a: string, b: string): number {
  const parse = (v: string) =>
    (v.trim().replace(/^v/i, '').match(/\d+/g) ?? []).slice(0, 3).map(Number)
  const pa = parse(a)
  const pb = parse(b)
  for (let i = 0; i < 3; i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (d !== 0) return Math.sign(d)
  }
  return 0
}

interface CacheBlob {
  current: string
  latest: string
  url: string
  found: boolean
  checkedAt: number
}

function readCache(): CacheBlob | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const v = JSON.parse(raw) as Partial<CacheBlob>
    if (
      typeof v.current !== 'string' ||
      typeof v.latest !== 'string' ||
      typeof v.url !== 'string' ||
      typeof v.found !== 'boolean' ||
      typeof v.checkedAt !== 'number'
    ) {
      return null
    }
    return v as CacheBlob
  } catch {
    return null
  }
}

function writeCache(blob: CacheBlob): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(blob))
  } catch {
    // 隐私模式/存储满: 只影响下次启动多一次请求, 可忽略
  }
}

// 优先 api.github.com (官方带 CORS, 浏览器可直连; 未认证限额 60 次/时);
// latest.json 清单 (release.yml 产物) 作兜底 — API 限流或字段变动时仍可取到版本号。
async function fetchLatest(): Promise<UpdateInfo> {
  let latest = ''
  let url = `https://github.com/${UPDATE_REPO}/releases/latest`
  const r = await fetch(`https://api.github.com/repos/${UPDATE_REPO}/releases/latest`, {
    cache: 'no-store',
  })
  if (r.ok) {
    const rel = await r.json()
    latest = String(rel.tag_name ?? '')
    url = rel.html_url || url
  }
  if (!latest) {
    const m = await fetch(
      `https://github.com/${UPDATE_REPO}/releases/latest/download/latest.json`,
      { cache: 'no-store' },
    )
    if (m.ok) {
      const manifest = await m.json()
      latest = String(manifest.tag ?? '')
      url = manifest.notes_url || url
    }
  }
  if (!latest) throw new Error('no release info')
  return { latest, url }
}

let inFlight = false

export async function checkForUpdate(
  current: string,
  opts: { force?: boolean } = {},
): Promise<void> {
  const cur = current.trim()
  if (!cur || inFlight) return
  if (!opts.force) {
    const c = readCache()
    if (c && c.current === cur && Date.now() - c.checkedAt < CACHE_TTL_MS) {
      setState({
        status: c.found ? 'found' : 'latest',
        info: { latest: c.latest, url: c.url },
        checkedAt: c.checkedAt,
      })
      return
    }
  }
  inFlight = true
  setState({ status: 'checking' })
  const checkedAt = Date.now()
  try {
    const info = await fetchLatest()
    const found = compareVersion(cur, info.latest) < 0
    setState({ status: found ? 'found' : 'latest', info, checkedAt })
    writeCache({ current: cur, latest: info.latest, url: info.url, found, checkedAt })
  } catch {
    // 不落缓存: 下次启动重试 (静默检查每次会话最多一次, 不会打爆限额)
    setState({ status: 'error', checkedAt })
  } finally {
    inFlight = false
  }
}

let silentStarted = false

/**
 * 侧栏与设置页共用的更新检查 hook。
 * 版本号就绪后自动做一次静默检查 (每次会话最多一次);
 * `check` 为手动入口, 绕过缓存立即刷新。
 */
export function useUpdateCheck(): {
  status: UpdateStatus
  info: UpdateInfo | null
  checkedAt: number
  check: () => void
} {
  const s = useSyncExternalStore(subscribe, getUpdateState)
  const { data: versionData } = useVersion()
  const current = (versionData?.version ?? '').trim()
  useEffect(() => {
    if (!current || silentStarted) return
    silentStarted = true
    void checkForUpdate(current)
  }, [current])
  const check = useCallback(() => {
    void checkForUpdate(current, { force: true })
  }, [current])
  return { status: s.status, info: s.info, checkedAt: s.checkedAt, check }
}
