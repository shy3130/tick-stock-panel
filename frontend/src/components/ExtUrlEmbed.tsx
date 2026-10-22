/**
 * URL 嵌入型扩展页面 — 整页内嵌外部 http(s) 页面。
 *
 * 安全约定与看板 ExternalLinkWidget 一致 (配合 dashboard/layout.ts sanitizeExtUrl):
 * - URL 仅 http(s) 且拒绝与本站同源 — 同源内容配 allow-scripts+allow-same-origin
 *   等于绕过 sandbox, 一律不嵌;
 * - sandbox 显式放行 脚本/同源(目标站自身存储)/弹窗/表单, 其余默认禁;
 * - referrerPolicy=no-referrer; 部分站点以 X-Frame-Options/CSP 拒绝嵌入 →
 *   白屏兜底: 常驻「新窗口打开」+ 提示文案。
 */
import { ExternalLink, Link2, ShieldAlert } from 'lucide-react'
import { sanitizeExtUrl } from '@/components/dashboard/layout'

export function ExtUrlEmbed({ label, url }: { label: string; url: string }) {
  const safeUrl = sanitizeExtUrl(url)

  if (!safeUrl) {
    return (
      <div className="mx-auto max-w-xl rounded-card border border-border bg-surface p-8 text-center">
        <ShieldAlert className="mx-auto h-6 w-6 text-accent" />
        <h2 className="mt-3 text-lg font-semibold text-foreground">无法嵌入该地址</h2>
        <p className="mt-2 text-sm leading-6 text-secondary">
          嵌入地址仅支持 http(s) 且不能是本站自身。请到「设置 → 页面管理 → 扩展页面」修改该页面的 URL。
        </p>
        <p className="mt-1 break-all font-mono text-xs text-muted">{url}</p>
      </div>
    )
  }

  let host = ''
  try { host = new URL(safeUrl).hostname } catch { /* sanitizeExtUrl 已保证可解析 */ }

  return (
    <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/60 px-3 py-1.5">
        <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-foreground">
          <Link2 className="h-3.5 w-3.5 shrink-0 text-accent" />
          <span className="truncate" title={label}>{label}</span>
          {host && <span className="shrink-0 truncate font-mono text-[10px] text-muted">{host}</span>}
        </span>
        <a
          href={safeUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center gap-1 rounded-btn px-2 py-1 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          title="在新窗口打开"
        >
          <ExternalLink className="h-3.5 w-3.5" />新窗口打开
        </a>
      </div>
      <iframe
        src={safeUrl}
        title={label}
        sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-forms"
        referrerPolicy="no-referrer"
        loading="lazy"
        className="min-h-0 w-full flex-1 border-0 bg-white"
      />
      <div className="shrink-0 px-3 py-1 text-[10px] text-muted/60">
        若长时间空白, 该站点可能禁止被嵌入 (X-Frame-Options), 请用右上角新窗口打开
      </div>
    </section>
  )
}
