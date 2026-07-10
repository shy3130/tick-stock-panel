export type PublicEntryStatus = {
  authenticated: boolean
}

export type PublicEntryTarget = 'loading' | 'landing' | 'workspace' | 'login'

export function resolvePublicEntry(
  status: PublicEntryStatus | null | undefined,
  pathnameWithSearch: string,
): PublicEntryTarget {
  if (!status) return 'loading'
  if (status.authenticated) return 'workspace'

  const pathname = pathnameWithSearch.split('?')[0] || '/'
  return pathname === '/' ? 'landing' : 'login'
}
