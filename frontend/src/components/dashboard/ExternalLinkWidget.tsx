/**
 * 看板外部链接组件 — 在网格内嵌入任意 http(s) 页面。
 *
 * 安全约定(与 layout.sanitizeExtUrl 配合):
 * - URL 仅 http(s) 且拒绝与本站同源 — 同源内容配 allow-scripts+allow-same-origin
 *   等于绕过 sandbox, 一律不嵌;
 * - sandbox 显式放行 脚本/同源(目标站自身存储)/弹窗/表单, 其余(下载/顶层跳转等)默认禁;
 * - referrerPolicy=no-referrer, 不带本站 referer;
 * - 部分站点以 X-Frame-Options/CSP 拒绝嵌入 → 白屏兜底: 常驻「新窗口打开」+ 提示文案。
 */
import { useMemo } from 'react'
import { ExternalLink, Link2 } from 'lucide-react'

export function ExternalLinkWidget({ title, url }: { title: string; url: string }) {
  const host = useMemo(() => {
    try {
      return new URL(url).hostname
    } catch {
      return ''
    }
  }, [url])

  return (
    <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface/80 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/60 px-2 py-1">
        <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-foreground">
          <Link2 className="h-3 w-3 shrink-0 text-accent" />
          <span className="truncate" title={title}>{title}</span>
          {host && <span className="shrink-0 truncate font-mono text-[10px] text-muted">{host}</span>}
        </span>
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center gap-1 rounded-btn px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          title="在新窗口打开"
        >
          <ExternalLink className="h-3 w-3" />新窗口
        </a>
      </div>
      <iframe
        src={url}
        title={title}
        sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-forms"
        referrerPolicy="no-referrer"
        loading="lazy"
        className="min-h-0 w-full flex-1 border-0 bg-white"
      />
      <div className="shrink-0 px-2 py-0.5 text-[9px] text-muted/60">
        若长时间空白, 该站点可能禁止嵌入, 请用右上角新窗口打开
      </div>
    </section>
  )
}
