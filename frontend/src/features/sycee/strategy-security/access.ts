type AuthStatus = {
  user?: { role: 'admin' | 'user' } | null
}

export function canAuthorStrategies(status: AuthStatus | null | undefined): boolean {
  return status?.user?.role === 'admin'
}
