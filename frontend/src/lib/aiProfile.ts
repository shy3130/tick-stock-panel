import type { AiProfileMasked } from './api'

const key = (entry: string) => `ai_profile:${entry}`

export function getRememberedProfile(entry: string): string | null {
  return localStorage.getItem(key(entry))
}

export function rememberProfile(entry: string, profileId: string): void {
  localStorage.setItem(key(entry), profileId)
}

export function resolveEntryProfile(entry: string, profiles: Pick<AiProfileMasked, 'id' | 'available'>[], defaultId: string): string {
  const remembered = getRememberedProfile(entry)
  if (remembered && profiles.some(p => p.id === remembered && p.available !== false)) return remembered
  if (defaultId && profiles.some(p => p.id === defaultId && p.available !== false)) return defaultId
  return profiles.find(p => p.available !== false)?.id || ''
}
