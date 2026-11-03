import { ExternalLink } from 'lucide-react'
import type { PluginDataSourceItem } from '@/lib/api'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'

/** 数据源与扩展管理页共享清单正文，链接不依赖 Key 配置状态。 */
export function PluginMetadata({ plugin }: { plugin: PluginDataSourceItem }) {
  const links = [
    { url: plugin.homepage, label: plugin.homepage_label?.trim() || `${plugin.display_name} 官网` },
    { url: plugin.key_manage_url, label: plugin.key_manage_label?.trim() || '管理 Key / 查看账户用量' },
  ].filter(link => link.url)

  return <div className="min-w-0 space-y-3">
    {plugin.description && <MarkdownRenderer content={plugin.description} compact />}
    {!!plugin.usage_notes?.length && <ul className="space-y-2">
      {plugin.usage_notes.map((note, i) => <li key={i} className="flex items-start gap-2">
        <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted/50" aria-hidden="true" />
        <div className="min-w-0 flex-1"><MarkdownRenderer content={note} compact /></div>
      </li>)}
    </ul>}
    {!!links.length && <div className="flex flex-wrap gap-x-5 gap-y-2">
      {links.map(({ url, label }, i) => <a key={i} href={url} target="_blank" rel="noopener noreferrer"
        className="inline-flex min-h-9 max-w-full items-center gap-1.5 rounded-sm text-xs text-accent underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
        <span className="min-w-0 break-words">{label}</span>
        <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" />
      </a>)}
    </div>}
  </div>
}
