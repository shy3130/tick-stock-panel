import { useQuery } from '@tanstack/react-query'
import { Bot } from 'lucide-react'
import { api, type AiExecutionMeta, type AiUsageMeta } from '@/lib/api'
import { cn } from '@/lib/cn'

/**
 * P3: AI 执行元信息紧凑展示 — 实际 profile / 模型 / fallback / token usage。
 *
 * 契约(字段全部 optional,旧响应兼容):
 * - meta 缺失 → 不渲染;
 * - profile 列表加载失败/为空 → 回退显示 profile_id 原始串,不崩;
 * - fallback_used=true → 明确标注「已切换备用配置」,title 带原因与原 profile;
 * - usage 至少一项计数 > 0 才展示用量;全 0 / 缺失 → 不展示伪数据。
 */

interface Props {
  meta?: AiExecutionMeta | null
  className?: string
}

function usageOf(usage?: AiUsageMeta | null): AiUsageMeta | null {
  if (!usage) return null
  const counts = [usage.prompt_tokens, usage.cached_prompt_tokens, usage.completion_tokens, usage.total_tokens]
  return counts.some(v => typeof v === 'number' && v > 0) ? usage : null
}

export function AiExecutionMetaBadge({ meta, className }: Props) {
  // 与 AiProviderSelector 同一 queryKey,命中缓存;失败时 profiles=[],回退 id 展示
  const profilesQuery = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  if (!meta) return null

  const profiles = profilesQuery.data?.profiles ?? []
  const actualId = meta.profile_id ?? ''
  const primaryId = meta.primary_profile_id ?? ''
  const actualName = actualId ? profiles.find(p => p.id === actualId)?.name : undefined
  const primaryName = primaryId ? profiles.find(p => p.id === primaryId)?.name : undefined
  const profileLabel = actualName || actualId || meta.provider || ''
  const usage = usageOf(meta.usage)
  const fallback = meta.fallback_used === true

  if (!profileLabel && !meta.model && !fallback && !usage) return null

  const usageTotal = usage
    ? (usage.total_tokens ?? ((usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0)))
    : 0

  const title = [
    actualName || actualId ? `配置: ${[actualName, actualId && `(${actualId})`].filter(Boolean).join(' ')}` : '',
    meta.provider ? `provider: ${meta.provider}` : '',
    meta.model ? `模型: ${meta.model}` : '',
    fallback
      ? `已切换备用配置${meta.fallback_reason ? `（${meta.fallback_reason}）` : ''}${primaryName || primaryId ? `，原配置: ${primaryName || primaryId}` : ''}`
      : '',
    usage
      ? `tokens: prompt ${(usage.prompt_tokens ?? 0).toLocaleString('en-US')}${usage.cached_prompt_tokens ? `（缓存命中 ${usage.cached_prompt_tokens.toLocaleString('en-US')}）` : ''} + completion ${(usage.completion_tokens ?? 0).toLocaleString('en-US')} = ${usageTotal.toLocaleString('en-US')}`
      : '',
  ].filter(Boolean).join('\n')

  return (
    <span
      className={cn('inline-flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] text-muted', className)}
      title={title}
    >
      <Bot className="h-3 w-3 shrink-0" />
      {(profileLabel || meta.model) && (
        <span className="truncate">
          {profileLabel}
          {profileLabel && meta.model ? ` · ${meta.model}` : meta.model ?? ''}
        </span>
      )}
      {fallback && (
        <span className="shrink-0 rounded bg-warning/10 px-1 py-px font-medium text-warning">
          已切换备用配置
        </span>
      )}
      {usage && (
        <span className="shrink-0 font-mono tabular-nums">
          tokens {usageTotal.toLocaleString('en-US')}
          {usage.cached_prompt_tokens ? ` · 缓存 ${usage.cached_prompt_tokens.toLocaleString('en-US')}` : ''}
        </span>
      )}
    </span>
  )
}
