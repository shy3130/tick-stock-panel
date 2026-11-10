import { useCallback, useEffect, useMemo, useRef } from 'react'
import { replaceEqualDeep, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api, type StrategyLibraryMeta, type StrategyPoolGroup, type StrategyPoolSnapshot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { invalidateStrategyLibrary, notifyStrategyLibraryChanged } from '@/lib/strategyLibrary'
import { toast } from '@/components/Toast'

// 同一个 QueryClient 下的多个宿主共享写队列, 保持用户操作顺序。
const queues = new WeakMap<QueryClient, Promise<void>>()
function enqueue<T>(qc: QueryClient, op: () => Promise<T>): Promise<T> {
  const next = (queues.get(qc) ?? Promise.resolve()).then(async () => {
    await qc.cancelQueries({ queryKey: QK.strategyPool })
    return op()
  })
  queues.set(qc, next.then(() => undefined, () => undefined))
  return next
}

function latestSnapshot(previous: unknown, next: unknown): StrategyPoolSnapshot {
  const old = previous as StrategyPoolSnapshot | undefined
  const incoming = next as StrategyPoolSnapshot
  if (old?.revision != null && incoming.revision != null && old.revision > incoming.revision) return old
  return replaceEqualDeep(old, incoming)
}

// 旧版按周期隔离的双池 ('strategy-pool' 日线 + 'strategy-pool-1m' 分钟) 已合并为
// 统一池; v2 起池与策略库元数据 (分组/归档/加入时间) 迁至后端 /api/strategy-pool,
// localStorage 'strategy-pool' 退化为纯 ID 影子副本 (回滚到旧版本时池不丢)。
// 后端在写池时同步旧 ui_prefs 影子; 本地池先于偏好同步的边角由迁移上载兜底。
function loadLegacyLocalPool(): string[] {
  const savedMinute = storage.strategyPoolMinute.get([])
  const savedDaily = storage.strategyPool.get([])
  const minute = Array.isArray(savedMinute) ? savedMinute.filter(id => typeof id === 'string') : []
  const daily = Array.isArray(savedDaily) ? savedDaily.filter(id => typeof id === 'string') : []
  if (minute.length === 0) {
    storage.strategyPoolMinute.remove()
    return daily
  }
  const merged = [...daily]
  for (const id of minute) {
    if (!merged.includes(id)) merged.push(id)
  }
  storage.strategyPool.set(merged)
  storage.strategyPoolMinute.remove()
  return merged
}

/**
 * 策略池 v2 — 后端权威, 全部操作实时生效 (无草稿/保存语义)。
 *
 * - 池 (pool): 参与扫描的纯 ID 有序列表; 分组/归档是策略库级属性, 与池无关。
 * - 归档可逆: 策略移入档案库并自动移出池; 恢复只回库, 不自动回池。
 * - 每次变更向旧偏好键 'strategy-pool' 影子写入纯 ID 数组, 保证降级回滚可用。
 * - prune 语义与 v1 一致: 策略列表拉取成功且非空时清掉池内已不存在的 ID。
 */
export function useStrategyPool() {
  const qc = useQueryClient()
  const snapshot = useQuery({
    queryKey: QK.strategyPool,
    queryFn: ({ signal }) => api.strategyPoolSnapshot(signal),
    staleTime: 30_000,
    retry: 1,
    structuralSharing: latestSnapshot,
  })

  const migrated = useRef(false)
  const latest = useRef<StrategyPoolSnapshot | undefined>(undefined)
  latest.current = snapshot.data

  const shadowSync = useCallback((ids: string[]) => {
    storage.strategyPool.set(ids)
  }, [])

  const settle = useCallback((next: StrategyPoolSnapshot) => {
    const accepted = qc.setQueryData<StrategyPoolSnapshot>(QK.strategyPool, previous => latestSnapshot(previous, next))
    if (accepted) shadowSync(accepted.pool)
    notifyStrategyLibraryChanged()
  }, [qc, shadowSync])

  const mutate = useCallback(<T extends StrategyPoolSnapshot,>(op: () => Promise<T>, refreshLibrary = false) => (
    enqueue(qc, async () => {
      const next = await op()
      settle(next)
      if (refreshLibrary) await invalidateStrategyLibrary(qc, false)
      return next
    })
  ), [qc, settle])

  const run = useCallback(async (op: () => Promise<StrategyPoolSnapshot>, errorHint: string, successMsg?: string, refreshLibrary = false) => {
    try {
      await mutate(op, refreshLibrary)
      if (successMsg) toast(successMsg, 'success')
    } catch (error) {
      toast(`${errorHint}：${error instanceof Error ? error.message : '请重试'}`, 'error')
      await qc.invalidateQueries({ queryKey: QK.strategyPool })
    }
  }, [qc, mutate])

  // 一次性 v2 迁移: 后端为空但本地旧池非空 (离线期间维护过池) → 上载本地顺序。
  // 正常升级路径是后端从 ui_prefs 偏好种子, 此处只为本地新于远端的边角兜底。
  const maybeUploadLegacy = useCallback(() => {
    if (migrated.current) return
    migrated.current = true
    if (storage.strategyPoolV2Migrated.get(false)) {
      if (latest.current) shadowSync(latest.current.pool)
      return
    }
    const local = loadLegacyLocalPool()
    void enqueue(qc, async () => {
      const server = qc.getQueryData<StrategyPoolSnapshot>(QK.strategyPool)
      if (!server) return
      // 服务端再检查一次空池: 多窗口同时升级时不覆盖另一个窗口的新选择。
      const next = local.length > 0 && server.pool.length === 0
        ? await api.strategyPoolSetEntries(local, true)
        : server
      settle(next)
      storage.strategyPoolV2Migrated.set(true)
    }).catch(error => {
      // 不标记完成、不改旧池。刷新/重新进入时可重试, 避免网络抖动变成永久丢池。
      toast(`策略池迁移失败：${error instanceof Error ? error.message : '请重试'}`, 'error')
    })
  }, [qc, settle, shadowSync])

  useEffect(() => {
    if (snapshot.isSuccess) maybeUploadLegacy()
  }, [snapshot.isSuccess, maybeUploadLegacy])

  const groups = useMemo(() => snapshot.data?.groups ?? [], [snapshot.data])
  const pool = useMemo(() => snapshot.data?.pool ?? [], [snapshot.data])
  const metaMap = useMemo(
    () => new Map(Object.entries(snapshot.data?.meta ?? {})) as Map<string, StrategyLibraryMeta>,
    [snapshot.data],
  )
  /** 档案库条目 (库级 meta 中 archived=true 的记录, 供档案视图遍历) */
  const archivedIds = useMemo(
    () => [...metaMap.entries()].filter(([, meta]) => meta.archived).map(([id]) => id),
    [metaMap],
  )
  const groupNames = useMemo(() => new Map(groups.map(g => [g.id, g.name])), [groups])

  const addToPool = useCallback((id: string) => {
    void run(() => api.strategyPoolAdd(id), '加入策略池失败')
  }, [run])

  const removeFromPool = useCallback((id: string) => {
    void run(() => api.strategyPoolRemove(id), '移出策略池失败')
  }, [run])

  const reorderPool = useCallback((newOrder: string[]) => {
    void run(() => {
      const current = qc.getQueryData<StrategyPoolSnapshot>(QK.strategyPool)
      // 此入口还用于整组入/出池、清空和撤销, 必须保留完整替换语义。
      return api.strategyPoolSetEntries(newOrder, false, current?.revision)
    }, '调整策略池顺序失败')
  }, [qc, run])

  const archiveStrategy = useCallback((id: string) => {
    void run(() => api.strategyPoolArchiveStrategy(id), '归档失败', '已归档，可在左侧「档案库」恢复', true)
  }, [run])

  const unarchiveStrategy = useCallback((id: string) => {
    void run(() => api.strategyPoolUnarchiveStrategy(id), '恢复失败', undefined, true)
  }, [run])

  const discardRecord = useCallback((id: string) => {
    void run(() => api.strategyPoolDiscardRecord(id), '移出档案库失败', undefined, true)
  }, [run])

  const setStrategyGroup = useCallback((id: string, group: string | null) => {
    void run(() => api.strategyPoolSetStrategyGroup(id, group), '调整分组失败', undefined, true)
  }, [run])

  const createGroup = useCallback(async (name: string) => {
    const data = await mutate(() => api.strategyPoolCreateGroup(name), true)
    return data.group
  }, [mutate])

  const renameGroup = useCallback(async (groupId: string, name: string) => {
    await mutate(() => api.strategyPoolRenameGroup(groupId, name), true)
  }, [mutate])

  const reorderGroups = useCallback(async (orderedIds: string[]) => {
    await mutate(() => api.strategyPoolReorderGroups(orderedIds), true)
  }, [mutate])

  /** 删除分组: 组内策略保留并转为未分组 (与自选分组语义一致) */
  const deleteGroup = useCallback(async (groupId: string) => {
    await mutate(() => api.strategyPoolDeleteGroup(groupId), true)
  }, [mutate])

  // 清除池中失效策略 ID (与 v1 相同的守卫: 调用方保证仅在策略列表拉取成功且
  // 非空时调用)。库级 meta 记录不动 —— 档案库里失效项由用户在档案库清理。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    if (!latest.current?.pool.some(id => !validSet.has(id))) return
    void run(async () => {
      const current = qc.getQueryData<StrategyPoolSnapshot>(QK.strategyPool)!
      const next = current.pool.filter(id => validSet.has(id))
      return next.length === current.pool.length ? current : api.strategyPoolSetEntries(next, false, current.revision)
    }, '清理失效策略失败')
  }, [qc, run])

  /** 策略被删除后的级联清理: 移出池并删除库级记录 (尽力而为, 不报错打扰)。 */
  const handleStrategyDeleted = useCallback((id: string) => {
    void enqueue(qc, async () => {
      try {
        if (qc.getQueryData<StrategyPoolSnapshot>(QK.strategyPool)?.pool.includes(id)) {
          settle(await api.strategyPoolRemove(id))
        }
        if (qc.getQueryData<StrategyPoolSnapshot>(QK.strategyPool)?.meta[id]) {
          settle(await api.strategyPoolDiscardRecord(id))
        }
      } catch { /* 级联清理失败不影响删除流程, 失效记录由 prune 自愈 */ }
      await invalidateStrategyLibrary(qc, false)
    })
  }, [qc, settle])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return {
    pool,
    groups,
    groupNames,
    metaMap,
    archivedIds,
    isLoading: snapshot.isLoading,
    isError: snapshot.isError,
    addToPool,
    removeFromPool,
    reorderPool,
    archiveStrategy,
    unarchiveStrategy,
    discardRecord,
    setStrategyGroup,
    createGroup,
    renameGroup,
    reorderGroups,
    deleteGroup,
    prune,
    handleStrategyDeleted,
    isInPool,
  }
}

export type StrategyPool = ReturnType<typeof useStrategyPool>
export type { StrategyLibraryMeta, StrategyPoolGroup }
