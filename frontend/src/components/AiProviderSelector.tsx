import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot } from 'lucide-react'
import { api } from '@/lib/api'
import { rememberProfile, resolveEntryProfile } from '@/lib/aiProfile'

interface Props {
  entry: string
  value?: string
  onChange: (id: string) => void
  compact?: boolean
}

export function AiProviderSelector({ entry, value, onChange, compact = false }: Props) {
  const q = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  const profiles = q.data?.profiles ?? []
  const defaultId = q.data?.default_id ?? ''
  const current = value || resolveEntryProfile(entry, profiles, defaultId)

  useEffect(() => {
    if (current && current !== value) onChange(current)
  }, [current, onChange, value])

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
        className="h-8 min-w-[150px] rounded-lg border border-border/50 bg-base px-2 text-xs text-secondary outline-none transition-colors hover:border-accent/40 focus:border-accent/60"
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
