import { useIsMutating } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '@/lib/api'
import {
  CURRENT_BUILD_ID,
  isNewBuild,
  mayAutoReload,
  type AppVersion,
} from '@/lib/appVersion'


export function AppVersionGuard({
  currentBuildId = CURRENT_BUILD_ID,
  loadVersion = api.appVersion,
  reload = () => window.location.reload(),
  pollIntervalMs = 60_000,
}: {
  currentBuildId?: string
  loadVersion?: () => Promise<AppVersion>
  reload?: () => void
  pollIntervalMs?: number
}) {
  const activeMutations = useIsMutating()
  const [remoteBuild, setRemoteBuild] = useState<AppVersion | null>(null)
  const [visibility, setVisibility] = useState<DocumentVisibilityState>(
    () => document.visibilityState,
  )
  const observedBuildRef = useRef<string | null>(null)

  const dialogOpen = () => Boolean(document.querySelector('[role="dialog"]'))
  const canReload = useCallback(
    (nextVisibility: DocumentVisibilityState) => mayAutoReload({
      visibility: nextVisibility,
      activeMutations,
      dialogOpen: dialogOpen(),
    }),
    [activeMutations],
  )

  const checkVersion = useCallback(async () => {
    try {
      const remote = await loadVersion()
      if (!isNewBuild(currentBuildId, remote)) return
      if (observedBuildRef.current !== remote.build_id) {
        observedBuildRef.current = remote.build_id
        setRemoteBuild(remote)
      }
      if (canReload(document.visibilityState)) reload()
    } catch {
      // Version discovery is diagnostic only and must not disturb live data.
    }
  }, [canReload, currentBuildId, loadVersion, reload])

  useEffect(() => {
    void checkVersion()
    const timer = window.setInterval(() => void checkVersion(), pollIntervalMs)
    return () => window.clearInterval(timer)
  }, [checkVersion, pollIntervalMs])

  useEffect(() => {
    const onVisibilityChange = () => {
      const next = document.visibilityState
      setVisibility(next)
      if (remoteBuild && canReload(next)) reload()
      else if (next === 'visible') void checkVersion()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [canReload, checkVersion, reload, remoteBuild])

  if (!remoteBuild || visibility === 'hidden') return null

  return (
    <div
      role="status"
      className="fixed inset-x-3 top-3 z-[100] flex items-center justify-between gap-3 rounded-card border border-accent/40 bg-surface px-3 py-2 text-xs shadow-xl sm:left-auto sm:max-w-md"
    >
      <span>发现新版本，为避免继续使用旧页面，请刷新。</span>
      <button
        type="button"
        onClick={reload}
        className="shrink-0 rounded-btn bg-accent px-3 py-1.5 font-medium text-white"
      >
        立即刷新
      </button>
    </div>
  )
}
