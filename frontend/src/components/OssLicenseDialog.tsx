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

/** 页面与弹窗共用的许可合规声明 — 界面常驻, 移除即构成违约 */
export function LicenseComplianceNotice({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border border-warning/30 bg-warning/5 ${compact ? 'p-3 mt-4' : 'p-3'}`}>
      <ShieldAlert className="h-4 w-4 text-warning shrink-0 mt-px" />
      <div className="min-w-0">
        <div className="text-xs font-medium text-warning">许可合规声明</div>
        <p className="mt-1 text-[11px] leading-relaxed text-secondary">
          本项目基于 MIT 协议开源, 协议要求分发本软件或其任何部分时
          <span className="text-warning font-medium">必须完整保留原始版权声明与许可文本</span>。
          未经版权所有者书面许可, 移除、遮蔽或篡改版权署名、许可声明及本声明,
          以及将本项目重新分发时隐匿来源的行为, 均构成协议违约与著作权侵权;
          版权所有者保留依法追究责任的权利。
        </p>
      </div>
    </div>
  )
}

/**
 * 开源许可对话框 — 设置→关于「查看许可」入口。
 * 展示本项目 LICENSE 全文与随应用分发的第三方组件许可清单。
 */
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
          <h2 id="oss-license-title" className="text-sm font-medium text-foreground">开源许可</h2>
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

        {/* 第三方组件 */}
        <LicenseSection title="前端组件" entries={FRONTEND_DEPENDENCIES} />
        <LicenseSection title="后端组件" entries={BACKEND_DEPENDENCIES} />

        <p className="text-[10px] leading-relaxed text-muted border-t border-border pt-3">
          清单口径: 前后端直接运行时依赖 (构建工具与开发依赖不随产物分发, 故不列于此);
          传递依赖由各自分发包携带许可。各组件的完整许可文本见其官方仓库。
        </p>

        {/* 弹窗内再放一份声明 — 页面声明被移除时这里仍是威慑 */}
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
