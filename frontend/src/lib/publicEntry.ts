export type PublicEntryStatus = {
  authenticated: boolean
  invite_enabled?: boolean
}

export type PublicEntryTarget = 'loading' | 'landing' | 'workspace' | 'login' | 'invite'

export function resolvePublicEntry(
  status: PublicEntryStatus | null | undefined,
  pathnameWithSearch: string,
): PublicEntryTarget {
  if (!status) return 'loading'
  if (status.authenticated) return 'workspace'
  if (status.invite_enabled) return 'invite'

  const pathname = pathnameWithSearch.split('?')[0] || '/'
  return pathname === '/' ? 'landing' : 'login'
}
