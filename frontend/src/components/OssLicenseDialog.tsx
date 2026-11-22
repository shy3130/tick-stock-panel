import { X, ScrollText, ShieldAlert } from 'lucide-react'
import { Modal } from '@/components/Modal'
import {
  PROJECT_COPYRIGHT,
  PROJECT_LICENSE_NAME,
  PROJECT_LICENSE_TEXT,
  PROJECT_REPO_URL,
  FRONTEND_DEPENDENCIES,
  BACKEND_DEPENDENCIES,
  type OssLicenseEntry,
} from '@/data/ossLicenses'

/** 页面与弹窗共用的 TSP 本体许可声明。 */
export function LicenseComplianceNotice({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border border-warning/30 bg-warning/5 ${compact ? 'p-3 mt-4' : 'p-3'}`}>
      <ShieldAlert className="h-4 w-4 text-warning shrink-0 mt-px" />
      <div className="min-w-0">
        <div className="text-xs font-medium text-warning">TSP 工作台分发须知</div>
        <p className="mt-1 text-[11px] leading-relaxed text-secondary">
          TSP 工作台采用 {PROJECT_LICENSE_NAME} 许可。分发软件的副本或实质部分时，须保留原始版权声明与许可文本。
        </p>
      </div>
    </div>
  )
}

export function TiveloLicenseNotice() {
  return (
    <section aria-label="Tivelo 组件许可" className="rounded-card border border-warning/30 bg-warning/5 p-4">
      <div className="flex items-center gap-2">
        <ShieldAlert className="h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
        <h3 className="text-sm font-semibold text-foreground">Tivelo 组件许可</h3>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-secondary">
        采用 PolyForm Noncommercial License 1.0.0 的 Tivelo 自有实现，
        <strong className="font-semibold text-warning">许可范围外的商用须另获权利人书面授权。</strong>
      </p>
      <p className="mt-2 break-words font-mono text-[11px] leading-relaxed text-secondary">
        tivelo-data · tivelo-backtest · tivelo-contracts · tivelo-market-cn · tivelo-extensions
      </p>
      <p className="mt-2 text-[11px] leading-relaxed text-muted">
        各组件的适用范围见随包附带的 LICENSE、LICENSE-MIT 和 NOTICE。已授予的 MIT 权利继续有效。
      </p>
      <a
        href="https://spdx.org/licenses/PolyForm-Noncommercial-1.0.0.html"
        target="_blank"
        rel="noreferrer"
        className="mt-2 inline-block text-[11px] text-accent underline underline-offset-2 hover:text-foreground"
      >
        查看 PolyForm 非商业许可全文
      </a>
    </section>
  )
}

/** 设置 → 关于「查看许可」入口，展示工作台和随应用分发的组件许可。 */
export function OssLicenseDialog({ onClose }: { onClose: () => void }) {
  return (
    <Modal
      onClose={onClose}
      labelledBy="oss-license-title"
      panelClassName="w-[92vw] max-w-2xl bg-surface border border-border rounded-card shadow-xl flex flex-col max-h-[86vh]"
    >
      {/* 标题栏 */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <ScrollText className="h-4 w-4 text-accent" />
          <h2 id="oss-license-title" className="text-sm font-medium text-foreground">软件许可</h2>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-elevated transition-colors text-secondary"
          aria-label="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* 内容区 */}
      <div className="overflow-y-auto px-5 py-4 flex flex-col gap-5">
        {/* 本项目 */}
        <section>
          <div className="flex items-center justify-between gap-3 mb-2">
            <div className="min-w-0">
              <div className="text-sm text-foreground">Tick Stock Panel</div>
              <div className="text-[11px] text-muted truncate">{PROJECT_COPYRIGHT}</div>
            </div>
            <span className="shrink-0 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
              {PROJECT_LICENSE_NAME}
            </span>
          </div>
          <pre className="max-h-44 overflow-y-auto rounded-lg bg-elevated p-3 text-[10px] leading-relaxed text-secondary font-mono whitespace-pre-wrap">
{PROJECT_LICENSE_TEXT}
          </pre>
          <a
            href={PROJECT_REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
          >
            {PROJECT_REPO_URL.replace('https://', '')}
          </a>
        </section>

        <TiveloLicenseNotice />

        {/* 其他依赖组件 */}
        <LicenseSection title="前端组件" entries={FRONTEND_DEPENDENCIES} />
        <LicenseSection title="后端组件" entries={BACKEND_DEPENDENCIES} />

        <p className="text-[10px] leading-relaxed text-muted border-t border-border pt-3">
          清单列出前后端直接运行时依赖。传递依赖的许可随各自的分发包提供。
        </p>

        <LicenseComplianceNotice />
      </div>
    </Modal>
  )
}

/** MIT 之外的协议高亮显示, 便于识别非 MIT 组件 */
function LicenseBadge({ license }: { license: string }) {
  const isMIT = license === 'MIT'
  return (
    <span
      className={`shrink-0 rounded-full border px-2 py-px font-mono text-[10px] ${
        isMIT
          ? 'border-border bg-elevated text-muted'
          : 'border-accent/30 bg-accent/10 text-accent'
      }`}
    >
      {license}
    </span>
  )
}

function LicenseSection({ title, entries }: { title: string; entries: OssLicenseEntry[] }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium text-secondary">
        {title}
        <span className="ml-1.5 text-[10px] font-normal text-muted">{entries.length} 项</span>
      </h3>
      <div className="rounded-lg border border-border overflow-hidden">
        <ul className="divide-y divide-border">
          {entries.map((e) => (
            <li key={e.name} className="flex items-center justify-between gap-3 px-3 py-1.5">
              <span className="min-w-0 truncate font-mono text-[11px] text-foreground">
                {e.name}
                <span className="ml-1.5 text-muted">@{e.version}</span>
              </span>
              <LicenseBadge license={e.license} />
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
