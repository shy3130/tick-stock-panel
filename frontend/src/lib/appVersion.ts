export interface AppVersion {
  build_id: string
  published_at: string | null
}

export interface AutoReloadState {
  visibility: DocumentVisibilityState
  activeMutations: number
  dialogOpen: boolean
}

export const CURRENT_BUILD_ID = import.meta.env.VITE_BUILD_ID || 'dev'

export function isNewBuild(
  currentBuildId: string,
  remote: AppVersion,
): boolean {
  return Boolean(remote.build_id && remote.build_id !== currentBuildId)
}

export function mayAutoReload({
  visibility,
  activeMutations,
  dialogOpen,
}: AutoReloadState): boolean {
  return visibility === 'hidden' && activeMutations === 0 && !dialogOpen
}
