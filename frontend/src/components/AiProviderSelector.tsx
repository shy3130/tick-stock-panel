import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot } from 'lucide-react'
import { api, type AiProviderKind } from '@/lib/api'
import { rememberProfile, resolveEntryProfile } from '@/lib/aiProfile'
import { cn } from '@/lib/cn'

interface Props {
  entry: string
  value?: string
  onChange: (id: string) => void
  compact?: boolean
  providers?: readonly AiProviderKind[]
}

export function AiProviderSelector({ entry, value, onChange, compact = false, providers }: Props) {
  const q = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  const allProfiles = q.data?.profiles ?? []
  const profiles = providers?.length
    ? allProfiles.filter(profile => providers.some(kind => profile.provider === kind))
    : allProfiles
  const defaultId = q.data?.default_id ?? ''
  const resolved = resolveEntryProfile(entry, profiles, defaultId)
  const valueAllowed = Boolean(value && profiles.some(profile => profile.id === value))
  const current = (!providers || valueAllowed) ? (value || resolved) : resolved

  useEffect(() => {
    if (current === value) return
    if (current) onChange(current)
    else if (providers) onChange('')
  }, [current, onChange, value, providers])

  if (profiles.length <= 1) return null

  return (
    <label className="inline-flex items-center gap-1.5 text-[11px] text-muted">
      {!compact && <Bot className="h-3.5 w-3.5" />}
      <select
        value={current}
        onChange={e => {
          rememberProfile(entry, e.target.value)
          onChange(e.target.value)
        }}
        className={cn('control h-8 min-w-[150px] px-2 text-xs text-secondary')}
      >
        {profiles.map(profile => (
          <option key={profile.id} value={profile.id} disabled={profile.available === false}>
            {profile.name}{profile.is_default ? ' · 默认' : ''}{profile.available === false ? ' · 未检测到命令' : ''}
          </option>
        ))}
      </select>
    </label>
  )
}
