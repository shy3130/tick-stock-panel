/**
 * 页面管理 — 原「菜单设置 / 扩展页面 / 页面插件」三个设置 tab 的三合一外壳。
 *
 * ?section=<menus|ext-pages|page-packs> 深链定位子区块; Settings.tsx 会把旧
 * ?tab=menus/ext-pages/page-packs 链接重写为 ?tab=pages&section=<旧值>, 历史
 * 链接与 router 内的 /analysis 重定向均不失效。
 */
import { useSearchParams } from 'react-router-dom'
import { cn } from '@/lib/cn'
import { SettingsMenuSettingsPanel } from './MenuSettings'
import { SettingsExtPagesPanel } from './ExtPages'
import { SettingsPagePacksPanel } from './PagePacks'

const SECTIONS = [
  { key: 'menus', label: '菜单与页面' },
  { key: 'ext-pages', label: '扩展页面' },
  { key: 'page-packs', label: '页面插件' },
] as const

type SectionKey = (typeof SECTIONS)[number]['key']

export function SettingsPageAdminPanel() {
  const [searchParams, setSearchParams] = useSearchParams()
  const sectionParam = searchParams.get('section') as SectionKey | null
  const section: SectionKey = SECTIONS.some(s => s.key === sectionParam) ? sectionParam! : 'menus'

  const switchSection = (key: SectionKey) => {
    const next = new URLSearchParams(searchParams)
    next.set('section', key)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="max-w-5xl space-y-6">
      <div className="flex flex-wrap gap-2">
        {SECTIONS.map(s => (
          <button
            key={s.key}
            type="button"
            onClick={() => switchSection(s.key)}
            className={cn(
              'rounded-lg border px-3 py-1.5 text-sm transition-colors',
              section === s.key
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-border text-secondary hover:bg-elevated/60',
            )}
          >
            {s.label}
          </button>
        ))}
      </div>

      {section === 'menus' && <SettingsMenuSettingsPanel />}
      {section === 'ext-pages' && <SettingsExtPagesPanel />}
      {section === 'page-packs' && <SettingsPagePacksPanel />}
    </div>
  )
}
