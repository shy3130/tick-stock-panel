/**
 * 关于面板 — 版权、更新、打赏三个区块。
 */
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ScrollText, ExternalLink, CheckCircle2, Download, RefreshCw, Sparkles, Scale, Heart, Rocket, X } from 'lucide-react'
import { useVersion } from '@/lib/useSharedQueries'
import { useUpdateCheck } from '@/lib/updateCheck'
import { Modal } from '@/components/Modal'
import { OssLicenseDialog, LicenseComplianceNotice } from '@/components/OssLicenseDialog'
import { PROJECT_LICENSE_NAME, PROJECT_REPO_URL } from '@/data/ossLicenses'

export function SettingsAboutPanel() {
  const { data: versionData } = useVersion()
  const [licenseOpen, setLicenseOpen] = useState(false)
  const [qrZoom, setQrZoom] = useState<{ src: string; alt: string; caption: string } | null>(null)

  // ===== 检查更新 (共享单例 store: 与侧栏左下角 NEW 徽标同源, 手动检查绕过缓存) =====
  const {
    status: updateState,
    info: updateInfo,
    check: checkUpdate,
  } = useUpdateCheck()

  // ===== 侧栏下载图标深链 (?autoupdate=1): 滚动定位到本行并自动执行一次检查 =====
  const [searchParams, setSearchParams] = useSearchParams()
  const updateRowRef = useRef<HTMLDivElement>(null)
  const currentVersion = (versionData?.version ?? '').trim()
  useEffect(() => {
    if (!currentVersion || searchParams.get('autoupdate') !== '1') return
    updateRowRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    checkUpdate()
    const next = new URLSearchParams(searchParams)
    next.delete('autoupdate')
    setSearchParams(next, { replace: true })
  }, [currentVersion, searchParams, checkUpdate, setSearchParams])

  return (
    <div className="pb-8 flex flex-col gap-6">
      {/* ===== 版权 ===== */}
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center justify-between gap-3 mb-4">
          <div className="flex items-center gap-2">
            <Scale className="h-4 w-4 text-accent" />
            <h3 className="text-sm font-medium text-foreground">版权</h3>
          </div>
          <span className="shrink-0 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
            {PROJECT_LICENSE_NAME}
          </span>
        </div>

        {/* 版权署名 — 界面常驻, 避免仅存在于 LICENSE 文件时被整包删除 */}
        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">© 2026 tick-stock-panel contributors</div>
            <div className="text-[11px] text-muted truncate">基于 MIT 协议开源 · 遵照协议分发时须保留版权与许可声明</div>
          </div>
          <a
            href={PROJECT_REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex shrink-0 items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                       bg-elevated text-secondary hover:text-foreground transition-colors"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            仓库
          </a>
        </div>

        {/* 开源许可 — 点击弹窗查看完整内容 */}
        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">开源许可</div>
            <div className="text-[11px] text-muted truncate">本项目协议全文与第三方组件许可清单</div>
          </div>
          <button
            onClick={() => setLicenseOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                       bg-elevated text-secondary hover:text-foreground transition-colors"
          >
            <ScrollText className="h-3.5 w-3.5" />
            查看许可
          </button>
        </div>

        {/* 许可合规声明 — 醒目警示 */}
        <LicenseComplianceNotice compact />
      </section>

      {/* ===== 更新 ===== */}
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center gap-2 mb-4">
          <Rocket className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">更新</h3>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">当前版本</div>
            <div className="text-[11px] text-muted truncate">已安装的应用版本</div>
          </div>
          <span className="font-mono text-xs text-secondary shrink-0">
            {versionData?.version ?? '—'}
          </span>
        </div>

        <div ref={updateRowRef} className="flex scroll-mt-24 items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">检查更新</div>
            {updateState === 'found' && updateInfo ? (
              <div className="mt-0.5 inline-flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] text-accent">
                <Sparkles className="h-3 w-3" />
                发现新版本
                <span className="font-semibold">v{updateInfo.latest.replace(/^v/, '')}</span>
                <span className="text-accent/60">· 当前 v{versionData?.version?.replace(/^v/, '') ?? '—'}</span>
              </div>
            ) : (
              <div className="text-[11px] text-muted truncate">
                {updateState === 'latest'
                  ? `已是最新版本 (v${versionData?.version?.replace(/^v/, '') ?? '—'})`
                  : updateState === 'error'
                    ? '检查失败 (网络受限?), 可直接前往 Releases'
                    : '对比 GitHub Releases 最新 Release, 提示新版本'}
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <a
              href="https://tsp.shy313.com/"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                         bg-elevated text-secondary hover:text-foreground transition-colors"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              官网
            </a>
            {updateState === 'found' && updateInfo && (
              <a
                href={updateInfo.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                           bg-accent text-white hover:bg-accent/90 transition-colors"
              >
                <Download className="h-3.5 w-3.5" />
                前往下载 v{updateInfo.latest.replace(/^v/, '')}
              </a>
            )}
            <button
              onClick={checkUpdate}
              disabled={updateState === 'checking'}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                         bg-elevated text-secondary hover:text-foreground transition-colors
                         disabled:opacity-50"
            >
              {updateState === 'checking' ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              ) : updateState === 'latest' ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )}
              {updateState === 'checking' ? '检查中…' : updateState === 'latest' ? '已检查' : '检查更新'}
            </button>
          </div>
        </div>
      </section>

      {/* ===== 打赏 ===== */}
      <section className="rounded-card border border-accent/25 bg-gradient-to-b from-accent/[0.10] to-transparent p-5">
        <div className="flex items-center justify-between gap-3 mb-4">
          <div className="flex items-center gap-2">
            <Heart className="h-4 w-4 text-accent" />
            <h3 className="text-sm font-medium text-foreground">打赏</h3>
          </div>
          <span className="shrink-0 rounded-full border border-border bg-elevated px-2 py-0.5 text-[10px] text-muted">
            完全自愿 · 不影响任何功能
          </span>
        </div>

        <div className="flex items-center justify-between gap-6">
          {/* 左: 文案与入口 */}
          <div className="min-w-0 flex-1">
            <div className="text-sm text-foreground">支持这个项目</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
              如果 TSP 对你的交易与研究有帮助, 欢迎请作者喝杯咖啡,
              扫描右侧微信赞赏码即可支持项目持续维护。
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                onClick={() => setQrZoom({ src: '/donate-wechat.jpg', alt: '微信赞赏码', caption: '微信扫一扫 · 赞赏支持' })}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                           bg-accent text-white hover:bg-accent/90 transition-colors"
              >
                <Heart className="h-3.5 w-3.5" />
                赞赏支持
              </button>
            </div>
            <p className="mt-3 text-[10px] leading-relaxed text-muted">
              项目基于 MIT 协议永久免费, 打赏不会解锁任何功能; 数据源等第三方服务费用与本站无关。
            </p>
          </div>

          {/* 右: 微信赞赏码 (白底保证二维码可扫) */}
          <button
            onClick={() => setQrZoom({ src: '/donate-wechat.jpg', alt: '微信赞赏码', caption: '微信扫一扫 · 赞赏支持' })}
            title="点击放大"
            className="group shrink-0 rounded-lg border border-border bg-white p-2
                       hover:border-accent/40 transition-colors"
          >
            <img
              src="/donate-wechat.jpg"
              alt="微信赞赏码"
              className="h-28 w-28 object-contain"
            />
            <div className="mt-1 text-center text-[10px] text-neutral-500 group-hover:text-neutral-700">
              微信赞赏
            </div>
          </button>
        </div>
      </section>

      {licenseOpen && <OssLicenseDialog onClose={() => setLicenseOpen(false)} />}

      {/* 收款码大图 — 桌面端无法直接扫码时方便手机扫 */}
      {qrZoom && (
        <Modal
          onClose={() => setQrZoom(null)}
          ariaLabel={qrZoom.alt}
          panelClassName="w-auto bg-white rounded-card shadow-xl p-4"
        >
          <div className="flex flex-col items-center">
            <img
              src={qrZoom.src}
              alt={qrZoom.alt}
              className="max-w-[70vw] max-h-[70vh] object-contain"
            />
            <div className="mt-2 flex w-full items-center justify-between gap-4">
              <span className="text-xs text-neutral-600">{qrZoom.caption}</span>
              <button
                onClick={() => setQrZoom(null)}
                className="p-1 rounded hover:bg-neutral-100 transition-colors text-neutral-500"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
