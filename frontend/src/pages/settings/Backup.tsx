/**
 * 配置备份面板 — 导出/导入用户配置, 解决切换设备配置丢失。
 *
 * 导出: 按类别勾选 (默认全选、密钥除外), 服务端打包 ZIP 直链下载。
 * 导入: 上传 → inspect 预览 (类别条目数/冲突) → 按类别勾选 + 二次确认 →
 *       apply → 失效受影响的前端查询 (后端已同步刷新引擎与数据源)。
 */
import { useCallback, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Download, FileArchive, FlaskConical, RotateCcw, ShieldAlert, TriangleAlert, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import type { ConfigBackupApplyResult, ConfigBackupInspect } from '@/lib/api'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { QK } from '@/lib/queryKeys'

/** 类别选项 — key 与后端 config_backup._CATEGORY_LABELS 一一对应 */
const CATEGORY_OPTIONS: { key: string; label: string; desc: string; sensitive?: boolean }[] = [
  { key: 'strategies',         label: '策略源码',       desc: '自定义 / AI / 组合策略' },
  { key: 'strategy_overrides', label: '策略调参',       desc: '参数覆盖' },
  { key: 'custom_signals',     label: '自定义信号',     desc: '信号库中的自定义信号' },
  { key: 'custom_factors',     label: '自定义因子',     desc: '自定义与组合因子' },
  { key: 'monitor_rules',      label: '监控规则',       desc: '监控中心规则' },
  { key: 'watchlist',          label: '自选股与分组',   desc: '自选列表与分组定义' },
  { key: 'lots',               label: '批量持仓',       desc: '批量交易持仓记录' },
  { key: 'paper',              label: '模拟盘账户',     desc: '账户 / 持仓 / 成交台账' },
  { key: 'research_candidates', label: '挖掘候选',      desc: '挖掘入选清单' },
  { key: 'data_sources',       label: '自定义数据源',   desc: '数据源连接配置' },
  { key: 'analysis_menus',     label: '分析菜单',       desc: '自定义分析入口' },
  { key: 'ext_pages',          label: '扩展页面',       desc: '自定义页面插件 (含可执行代码)' },
  { key: 'ext_data',           label: '扩展数据集配置', desc: '不含数据本体与 Key' },
  { key: 'sound_packs',        label: '语音包',         desc: '自定义提示音' },
  { key: 'history',            label: '历史记录',       desc: '告警历史与助手会话' },
  { key: 'preferences',        label: '偏好设置',       desc: '界面与通知偏好 (脱敏)' },
  { key: 'secrets',            label: '密钥',           desc: 'AI Key / 数据源 Key / Webhook Secret', sensitive: true },
  { key: 'auth',               label: '访问凭证',       desc: '访问密码与会话 — 导入后原密码在本机生效', sensitive: true },
]

/** 导出默认勾选: 除密钥外全选 (安全默认)。 */
const DEFAULT_EXPORT_KEYS = CATEGORY_OPTIONS.filter((c) => !c.sensitive).map((c) => c.key)

const SCOPE_EXCLUDED = '行情数据、K 线缓存、回测矩阵、任务记录等可重新同步或派生的运行数据不在备份范围内'

/** 导入成功后需要失效的查询 — 与备份类别覆盖的领域对应。 */
const INVALIDATE_KEYS: readonly (readonly unknown[])[] = [
  QK.preferences,
  QK.settings,
  QK.monitorRules,
  QK.customSignals,
  QK.customSignalsOptions,
  QK.watchlist,
  QK.watchlistGroups,
  QK.dataSources,
  QK.lots,
  QK.paperAll,
  QK.soundPacks,
  ['alerts'],
  // 参数化键按前缀失效 (与 Screener/FactorEditor 的既有做法一致)
  ['screener-strategies'],
  ['factors-library'],
  ['ext-pages'],
]

function toggleSet(set: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(set)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return next
}

/** 卡片头: 图标底板 + 标题 + 说明, 右侧放主操作。 */
function CardHeader({ icon, title, subtitle, children }: {
  icon: React.ReactNode
  title: string
  subtitle: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-accent/10 text-accent">
          {icon}
        </div>
        <div className="min-w-0">
          <div className="text-sm font-medium text-foreground">{title}</div>
          <div className="truncate text-[11px] text-muted">{subtitle}</div>
        </div>
      </div>
      {children && <div className="shrink-0">{children}</div>}
    </div>
  )
}

/** 类别勾选行 — 选中态着色 (敏感类别用警示色)。 */
function CategoryCheck({ label, desc, checked, onChange, sensitive }: {
  label: string
  desc: string
  checked: boolean
  onChange: () => void
  sensitive?: boolean
}) {
  return (
    <label
      className={`flex cursor-pointer items-start gap-2.5 rounded-btn border px-2.5 py-2 transition-colors ${
        sensitive
          ? checked ? 'border-warning/30 bg-warning/[0.06]' : 'border-border/60 hover:border-warning/30'
          : checked ? 'border-accent/25 bg-accent/[0.05]' : 'border-transparent hover:bg-elevated/60'
      }`}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="mt-0.5 rounded border-border"
      />
      <div className="min-w-0">
        <div className="flex items-center gap-1 text-xs text-foreground">
          {sensitive && <ShieldAlert className="h-3 w-3 shrink-0 text-warning" />}
          {label}
        </div>
        {desc && <div className="mt-0.5 text-[11px] leading-snug text-muted">{desc}</div>}
      </div>
    </label>
  )
}

export function SettingsBackupPanel() {
  const qc = useQueryClient()

  // ── 导出 ──
  const [exportSel, setExportSel] = useState<ReadonlySet<string>>(
    () => new Set(DEFAULT_EXPORT_KEYS),
  )
  const exportSecrets = exportSel.has('secrets') || exportSel.has('auth')

  // ── 导入 ──
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [inspect, setInspect] = useState<ConfigBackupInspect | null>(null)
  const [importSel, setImportSel] = useState<ReadonlySet<string>>(new Set())
  const [applyResult, setApplyResult] = useState<ConfigBackupApplyResult | null>(null)
  const [allowOverwrite, setAllowOverwrite] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const inspectM = useMutation({
    mutationFn: (f: File) => api.configBackupInspect(f),
    onSuccess: (r) => {
      setInspect(r)
      // 新备份默认全选 (含密钥 — 导出方已决定打包它)
      setImportSel(new Set(r.categories.map((c) => c.key)))
      setApplyResult(null)
    },
    onError: () => setFile(null),  // 错误提示由 request 层统一 toast
  })

  const onFilePicked = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    e.target.value = ''  // 同名重选也能触发 onChange
    if (!f) return
    setFile(f)
    setInspect(null)
    setApplyResult(null)
    inspectM.mutate(f)
  }, [inspectM])

  const applyM = useMutation({
    mutationFn: () => api.configBackupApply(file!, allowOverwrite, [...importSel]),
    onSuccess: (r) => {
      setApplyResult(r)
      setConfirming(false)
      const imported = r.categories.reduce((s, c) => s + c.imported, 0)
      const failed = r.categories.reduce((s, c) => s + c.failed.length, 0)
      toast(failed > 0 ? `导入完成: ${imported} 项成功, ${failed} 项失败` : `导入完成: ${imported} 项`, failed > 0 ? 'error' : 'success')
      INVALIDATE_KEYS.forEach((k) => qc.invalidateQueries({ queryKey: k }))
    },
    onError: () => setConfirming(false),
  })

  const closeConfirm = () => {
    if (!applyM.isPending) setConfirming(false)
  }

  const fmtTime = (iso: string) => {
    try {
      return iso ? new Date(iso).toLocaleString() : '—'
    } catch {
      return iso || '—'
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      {/* ── 导出 ── */}
      <div className="rounded-card border border-border bg-surface p-4">
        <CardHeader
          icon={<Download className="h-4 w-4" />}
          title="导出配置备份"
          subtitle="打包为单个 ZIP 文件, 拷贝到新设备后即可导入恢复"
        >
          <a
            href={exportSel.size > 0 ? api.configBackupExportUrl([...exportSel]) : undefined}
            download
            aria-disabled={exportSel.size === 0}
            onClick={(e) => { if (exportSel.size === 0) e.preventDefault() }}
            className={`inline-flex items-center gap-1.5 rounded-btn px-3.5 py-1.5 text-xs font-medium transition-colors ${
              exportSel.size > 0
                ? 'bg-accent text-white hover:bg-accent/90'
                : 'pointer-events-none bg-accent text-white opacity-40'
            }`}
          >
            <Download className="h-3.5 w-3.5" />
            导出 .zip
          </a>
        </CardHeader>

        {/* 备份内容选择 */}
        <div className="mt-3 rounded-btn border border-border/70 bg-elevated/25 p-2.5">
          <div className="mb-1.5 flex items-center justify-between px-0.5">
            <div className="flex items-center gap-2 text-[11px] font-medium text-secondary">
              备份内容
              <span className={`rounded-full px-1.5 py-px text-[10px] ${
                exportSel.size > 0 ? 'bg-accent/10 text-accent' : 'bg-danger/10 text-danger'
              }`}>
                已选 {exportSel.size} / {CATEGORY_OPTIONS.length}
              </span>
            </div>
            <div className="flex items-center gap-2 text-[11px]">
              <button
                onClick={() => setExportSel(new Set(DEFAULT_EXPORT_KEYS))}
                className="text-muted transition-colors hover:text-accent"
              >
                全选
              </button>
              <span className="text-border">·</span>
              <button
                onClick={() => setExportSel(new Set())}
                className="text-muted transition-colors hover:text-accent"
              >
               清空
              </button>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
            {CATEGORY_OPTIONS.filter((c) => !c.sensitive).map((c) => (
              <CategoryCheck
                key={c.key}
                label={c.label}
                desc={c.desc}
                checked={exportSel.has(c.key)}
                onChange={() => setExportSel((s) => toggleSet(s, c.key))}
              />
            ))}
            {/* 密钥独立成行, 与普通类别视觉分隔 */}
            <div className="col-span-full mt-1 border-t border-border/60 pt-1">
              {CATEGORY_OPTIONS.filter((c) => c.sensitive).map((c) => (
                <CategoryCheck
                  key={c.key}
                  label={c.label}
                  desc={c.desc}
                  sensitive
                  checked={exportSel.has(c.key)}
                  onChange={() => setExportSel((s) => toggleSet(s, c.key))}
                />
              ))}
            </div>
          </div>
        </div>

        {exportSecrets && (
          <div className="mt-2 flex items-start gap-2 rounded-btn border border-warning/25 bg-warning/[0.07] px-3 py-2 text-[11px] leading-relaxed text-warning">
            <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
            <span>
              已勾选敏感内容: 备份文件将包含明文密钥 (AI Key、数据源 Key、Webhook Secret)
              和/或访问凭证 (密码哈希与会话), 新设备导入后无需重新填写。
              请妥善保管, 切勿分享或上传到公开位置。
            </span>
          </div>
        )}
        <div className="mt-2.5 px-0.5 text-[11px] text-muted">{SCOPE_EXCLUDED}</div>
      </div>

      {/* ── 导入 ── */}
      <div className="rounded-card border border-border bg-surface p-4">
        <CardHeader
          icon={<Upload className="h-4 w-4" />}
          title="导入配置备份"
          subtitle="选择导出的备份文件, 预览确认后恢复到本机"
        />

        <input
          ref={fileRef}
          type="file"
          accept=".zip"
          className="hidden"
          onChange={onFilePicked}
        />

        {/* 选择文件: 未选时为放置区, 已选后显示文件条 */}
        {!inspect ? (
          <button
            onClick={() => fileRef.current?.click()}
            disabled={inspectM.isPending}
            className="mt-3 flex w-full flex-col items-center justify-center gap-1.5 rounded-btn border border-dashed border-border bg-elevated/40 px-4 py-6 transition-colors hover:bg-elevated/70 disabled:opacity-50"
          >
            {inspectM.isPending ? (
              <>
                <RotateCcw className="h-5 w-5 animate-spin text-accent" />
                <span className="text-xs text-secondary">解析备份内容…</span>
              </>
            ) : (
              <>
                <FileArchive className="h-5 w-5 text-accent" />
                <span className="text-xs text-secondary">点击选择备份文件 (.zip)</span>
                <span className="text-[11px] text-muted">导入前会先解析预览, 不会直接写入</span>
              </>
            )}
          </button>
        ) : (
          <div className="mt-3 flex items-center gap-2 rounded-btn border border-border/70 bg-elevated/30 px-3 py-2">
            <CheckCircle2 className="h-4 w-4 shrink-0 text-accent" />
            <span className="min-w-0 flex-1 truncate text-xs text-foreground">{file?.name}</span>
            <button
              onClick={() => fileRef.current?.click()}
              className="shrink-0 text-[11px] text-muted transition-colors hover:text-accent"
            >
              重新选择
            </button>
          </div>
        )}
        {!inspect && (
          <div className="mt-2 px-0.5 text-[11px] leading-relaxed text-muted">
            同名文件默认跳过 (可选择覆盖); 偏好与密钥按合并写入, 不会清掉本机已有内容
          </div>
        )}

        {/* 预览 + 类别勾选 */}
        {inspect && (
          <div className="mt-3 space-y-2.5 rounded-btn border border-border/70 bg-elevated/25 p-3">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-secondary">
                备份时间 {fmtTime(inspect.created_at)}
              </span>
              {inspect.app_version && (
                <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-secondary">
                  来源 v{inspect.app_version}
                </span>
              )}
              <span className={`rounded-full border px-2 py-0.5 ${
                inspect.has_secrets
                  ? 'border-warning/30 bg-warning/[0.08] text-warning'
                  : 'border-border bg-surface text-muted'
              }`}>
                {inspect.has_secrets ? '包含密钥' : '不含密钥'}
              </span>
              <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-secondary">
                共 {inspect.total_files} 个文件
              </span>
            </div>

            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-muted">
                  <th className="w-8 py-1 font-normal" />
                  <th className="py-1 text-left font-normal">导入类别</th>
                  <th className="py-1 text-right font-normal">条目</th>
                  <th className="py-1 text-right font-normal">冲突</th>
                </tr>
              </thead>
              <tbody>
                {inspect.categories.map((c) => (
                  <tr key={c.key} className="border-t border-border/60">
                    <td className="py-1.5">
                      <input
                        type="checkbox"
                        checked={importSel.has(c.key)}
                        onChange={() => setImportSel((s) => toggleSet(s, c.key))}
                        className="rounded border-border"
                        aria-label={`导入 ${c.label}`}
                      />
                    </td>
                    <td className="py-1.5 text-foreground">
                      {c.key === 'secrets' && <ShieldAlert className="mr-1 inline h-3 w-3 text-warning" />}
                      {c.label}
                    </td>
                    <td className="py-1.5 text-right text-secondary">{c.count}</td>
                    <td className="py-1.5 text-right">
                      {c.conflicts > 0
                        ? <span className="rounded-full bg-warning/10 px-1.5 py-px text-warning">{c.conflicts}</span>
                        : <span className="text-muted">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {inspect.conflict_files > 0 && (
              <div className="flex items-center justify-between gap-4 rounded-btn bg-elevated/70 px-2.5 py-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 text-xs text-foreground">
                    <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-warning" />
                    覆盖已存在的同名项
                  </div>
                  <div className="text-[11px] text-muted">
                    {inspect.conflict_files} 个同名文件默认跳过; 开启后用备份内容覆盖本机同名文件
                  </div>
                </div>
                <button
                  onClick={() => setAllowOverwrite((v) => !v)}
                  aria-pressed={allowOverwrite}
                  className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors duration-200 ${
                    allowOverwrite ? 'bg-warning' : 'bg-elevated'
                  }`}
                >
                  <span
                    className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                      allowOverwrite ? 'translate-x-[18px]' : 'translate-x-[3px]'
                    }`}
                  />
                </button>
              </div>
            )}

            <div className="flex items-center justify-between gap-3 pt-0.5">
              <div className="text-[11px] text-muted">
                {importSel.size === 0 ? '请至少勾选一个类别' : '导入前请确认备份文件来源可信'}
              </div>
              <button
                onClick={() => setConfirming(true)}
                disabled={importSel.size === 0 || applyM.isPending}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-btn border border-danger/30 bg-danger/10 px-3.5 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20 disabled:opacity-50"
              >
                <Upload className="h-3.5 w-3.5" />
                导入所选 {importSel.size} 个类别
              </button>
            </div>
          </div>
        )}

        {/* 导入结果 */}
        {applyResult && (
          <div className="mt-3 space-y-2 rounded-btn border border-border/70 bg-elevated/25 p-3">
            <div className="flex items-center gap-2 text-sm text-foreground">
              <CheckCircle2 className={`h-4 w-4 ${applyResult.categories.some((c) => c.failed.length > 0) ? 'text-warning' : 'text-accent'}`} />
              导入结果
            </div>
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-muted">
                  <th className="py-1 text-left font-normal">类别</th>
                  <th className="py-1 text-right font-normal">成功</th>
                  <th className="py-1 text-right font-normal">跳过冲突</th>
                  <th className="py-1 text-right font-normal">失败</th>
                </tr>
              </thead>
              <tbody>
                {applyResult.categories.map((c) => (
                  <tr key={c.key} className="border-t border-border/60">
                    <td className="py-1.5 text-foreground">{c.label}</td>
                    <td className="py-1.5 text-right text-secondary">{c.imported}</td>
                    <td className="py-1.5 text-right text-muted">{c.skipped_conflict || '—'}</td>
                    <td className="py-1.5 text-right">
                      {c.failed.length > 0
                        ? <span className="rounded-full bg-danger/10 px-1.5 py-px text-danger">{c.failed.length}</span>
                        : <span className="text-muted">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {applyResult.categories.some((c) => c.failed.length > 0) && (
              <div className="space-y-1">
                {applyResult.categories.flatMap((c) =>
                  c.failed.map((f) => (
                    <div key={`${c.key}:${f.file}`} className="truncate text-[11px] text-danger" title={f.error}>
                      {f.file}: {f.error}
                    </div>
                  )),
                )}
              </div>
            )}
            {/* 导入即冒烟: 信号/因子逐条 校验→编译→依赖可解析 结果 */}
            {applyResult.categories.some((c) => (c.smoke?.length ?? 0) > 0) && (
              <div className="space-y-1.5 border-t border-border/60 pt-2">
                <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
                  <FlaskConical className="h-3.5 w-3.5 text-accent" />
                  导入冒烟
                  <span className="font-normal text-muted">校验 → 编译 → 依赖可解析 (不跑回测)</span>
                </div>
                {applyResult.categories.flatMap((c) =>
                  (c.smoke ?? []).map((sm) => (
                    <div key={`${c.key}:${sm.file}`} className="rounded-btn bg-elevated/50 px-2.5 py-1.5">
                      <div className="flex items-center gap-1.5 text-[11px]">
                        {sm.ok
                          ? <CheckCircle2 className="h-3 w-3 shrink-0 text-accent" />
                          : <TriangleAlert className="h-3 w-3 shrink-0 text-danger" />}
                        <span className="truncate font-mono text-muted" title={sm.file}>{sm.file}</span>
                        {sm.ok && sm.warmup_bars != null && (
                          <span className="ml-auto shrink-0 text-muted">预热 {sm.warmup_bars} bars</span>
                        )}
                      </div>
                      {!sm.ok && sm.issues.map((i, idx) => (
                        <div key={idx} className="mt-0.5 pl-4.5 text-[11px] text-danger">
                          <span className="rounded bg-danger/10 px-1 py-px font-mono text-[10px]">{i.code}</span> {i.message}
                        </div>
                      ))}
                    </div>
                  )),
                )}
              </div>
            )}
            {(applyResult.skipped_unselected > 0 || applyResult.out_of_scope_files > 0) && (
              <div className="space-y-0.5 text-[11px] text-muted">
                {applyResult.skipped_unselected > 0 && (
                  <div>{applyResult.skipped_unselected} 个条目属于未勾选的类别, 已跳过</div>
                )}
                {applyResult.out_of_scope_files > 0 && (
                  <div>{applyResult.out_of_scope_files} 个条目不在当前版本支持的备份范围内, 已忽略</div>
                )}
              </div>
            )}
            <div className="text-[11px] text-muted">
              偏好与密钥按合并写入 (备份内容覆盖同名项, 本机新增项保留)
            </div>
          </div>
        )}
      </div>

      {/* 二次确认 */}
      {confirming && inspect && (
        <Modal onClose={closeConfirm} ariaLabel="确认导入配置备份">
          <div className="p-5">
            <div className="flex items-center gap-2">
              <TriangleAlert className="h-4 w-4 text-warning" />
              <div className="text-sm font-medium">确认导入配置备份?</div>
            </div>
            <div className="mt-2 space-y-1 text-[12px] text-muted">
              <div>文件: {file?.name}</div>
              <div>
                将导入 {importSel.size} 个类别
                {allowOverwrite ? ', 并覆盖已存在的同名项' : ''}。
              </div>
              {(importSel.has('secrets') || importSel.has('auth')) && (
                <div className="text-warning">已勾选敏感内容, 导入后将覆盖本机同名密钥/访问凭证。</div>
              )}
              <div>策略源码会在导入时执行安全校验, 无法通过校验的条目将被跳过。</div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={closeConfirm}
                disabled={applyM.isPending}
                className="rounded-btn border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:text-foreground disabled:opacity-50"
              >
                取消
              </button>
              <button
                onClick={() => applyM.mutate()}
                disabled={applyM.isPending}
                className="inline-flex items-center gap-1.5 rounded-btn border border-danger/30 bg-danger/10 px-3 py-1.5 text-xs text-danger transition-colors hover:bg-danger/20 disabled:opacity-50"
              >
                <Upload className="h-3.5 w-3.5" />
                {applyM.isPending ? '导入中…' : '确认导入'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
