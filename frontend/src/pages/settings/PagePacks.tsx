/**
 * 设置 → 页面插件: 运行时扩展页面的上传/试载/启停/删除。
 *
 * 生命周期: 上传(后端存 pending) → 前端立即动态 import 试载并做契约校验
 * (packToExtension) → 校验结果回填 manifest(成功即启用) → 刷新页面后在
 * 启动引导窗口正式挂载(main.tsx → loadRuntimePageExtensions)。
 * 启动期失败(路由冲突等)由注册表记入 loadErrors, 这里按 runtime: 前缀
 * 展示。打包方式见 frontend/page-pack-template 与 docs/page-sdk.md。
 */
import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Package, Trash2, Upload, AlertTriangle, CheckCircle2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { packToExtension, runtimeBundleUrl } from '@/extensions/runtime'
import { getFrontendExtensionLoadErrors } from '@/extensions/registry'

function fmtSize(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`
  return `${Math.max(1, Math.round(n / 1024))}KB`
}

function fmtTime(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function SettingsPagePacksPanel() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: QK.extPages, queryFn: api.extPagesList })
  const [idInput, setIdInput] = useState('')
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const invalidate = () => qc.invalidateQueries({ queryKey: QK.extPages })
  const enabledMut = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.extPagesPutEnabled(v.id, v.enabled),
    onSuccess: (_d, v) => { invalidate(); toast(v.enabled ? '已启用, 刷新页面后生效' : '已停用, 刷新页面后生效', 'success') },
  })
  const deleteMut = useMutation({
    mutationFn: (id: string) => api.extPagesDelete(id),
    onSuccess: invalidate,
  })

  async function handleUpload() {
    const file = fileRef.current?.files?.[0]
    const id = idInput.trim()
    if (!file) { toast('请选择页面插件产物 (.js / .mjs)', 'error'); return }
    if (!/^[a-zA-Z0-9_]{1,64}$/.test(id)) { toast('ID 仅允许字母/数字/下划线 (1-64 位)', 'error'); return }
    setBusy(true)
    try {
      await api.extPagesUpload(id, file)
      // 试载: 直接 import 刚上传的产物, 契约校验失败则停用并回填错误
      let label = '', path = '', error = ''
      try {
        const m = await import(/* @vite-ignore */ `${runtimeBundleUrl(id)}?trial=${Date.now()}`)
        const ext = packToExtension(m.default)
        label = ext.navigation?.[0]?.label ?? id
        path = ext.routes?.[0]?.path ?? ''
      } catch (e) {
        error = e instanceof Error ? e.message : String(e)
      }
      await api.extPagesPutManifest(id, { label, path, error })
      await invalidate()
      if (error) toast(`页面插件校验失败: ${error}`, 'error')
      else { toast('页面插件已启用, 刷新页面后生效', 'success'); setIdInput(''); if (fileRef.current) fileRef.current.value = '' }
    } finally {
      setBusy(false)
    }
  }

  const packs = list.data ?? []
  const bootErrors = getFrontendExtensionLoadErrors().filter(e => e.source.startsWith('runtime:'))

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-border bg-surface p-6">
        <div className="text-[11px] uppercase tracking-[0.2em] text-accent/80">页面插件</div>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">上传打包页面产物, 免重建挂载</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary">
          用 <span className="font-mono text-xs text-accent">frontend/page-pack-template</span> 打包页面
          (产物为单文件 ES 模块), 在此上传即成为完整页面 — 路由、侧栏入口、菜单管理自动注册,
          刷新后生效。契约与安全边界见 <span className="font-mono text-xs text-accent">docs/page-sdk.md</span>。
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            value={idInput}
            onChange={(e) => setIdInput(e.target.value)}
            placeholder="页面插件 ID (字母/数字/下划线)"
            className="h-9 w-56 rounded-btn border border-border bg-elevated px-3 text-sm text-foreground placeholder:text-muted/60 focus:border-accent/50 focus:outline-none"
          />
          <input
            ref={fileRef}
            type="file"
            accept=".js,.mjs"
            className="hidden"
          />
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 text-xs text-secondary hover:bg-elevated/80 hover:text-foreground transition-colors"
          >
            <Package className="h-3.5 w-3.5" />
            {fileRef.current?.files?.[0]?.name ?? '选择产物文件'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={handleUpload}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            <Upload className="h-3.5 w-3.5" />
            {busy ? '上传并校验中…' : '上传并校验'}
          </button>
        </div>
      </section>

      {bootErrors.length > 0 && (
        <section className="rounded-card border border-danger/30 bg-danger/5 p-4 text-xs leading-5 text-secondary">
          <div className="mb-1 flex items-center gap-1.5 font-medium text-danger">
            <AlertTriangle className="h-3.5 w-3.5" /> 本次启动有页面插件挂载失败
          </div>
          {bootErrors.map((e, i) => (
            <div key={i} className="font-mono">{e.source}: {e.error}</div>
          ))}
          <div className="mt-1 text-muted">多为路由与核心/其他页面冲突或产物损坏; 修正后重新上传, 或停用对应包。</div>
        </section>
      )}

      <section className="rounded-card border border-border bg-surface overflow-hidden">
        <div className="grid grid-cols-[1fr_8rem_6rem_5rem_4rem] items-center border-b border-border px-4 py-2 text-[11px] text-muted">
          <div>页面 / ID</div><div>路径</div><div>大小 / 时间</div><div className="text-center">启用</div><div className="text-center">操作</div>
        </div>
        {packs.length === 0 && (
          <div className="px-4 py-10 text-center text-sm text-muted">暂无页面插件，用上方表单上传第一个。</div>
        )}
        {packs.map(p => (
          <div key={p.id} className={cn(
            'grid grid-cols-[1fr_8rem_6rem_5rem_4rem] items-center border-b border-border/60 px-4 py-3 last:border-b-0',
            p.enabled ? '' : 'opacity-70',
          )}>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 text-sm text-foreground">
                {p.error ? <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-danger" /> : p.enabled ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" /> : <Package className="h-3.5 w-3.5 shrink-0 text-muted" />}
                <span className="truncate">{p.label || '(未验证)'}</span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-muted">{p.id}</div>
              {p.error && <div className="mt-1 text-[11px] leading-4 text-danger">{p.error}</div>}
            </div>
            <div className="truncate font-mono text-[11px] text-muted">{p.path || '—'}</div>
            <div className="text-[11px] leading-4 text-muted">
              <div>{fmtSize(p.size)}</div>
              <div>{fmtTime(p.uploaded_at)}</div>
            </div>
            <div className="flex justify-center">
              <button
                type="button"
                onClick={() => enabledMut.mutate({ id: p.id, enabled: !p.enabled })}
                disabled={!!p.error}
                className={cn(
                  'relative h-5 w-9 rounded-full transition-colors',
                  p.enabled ? 'bg-accent' : 'bg-elevated border border-border',
                  p.error && 'cursor-not-allowed opacity-40',
                )}
                title={p.error ? '校验失败的包不可启用' : p.enabled ? '停用 (刷新后生效)' : '启用 (刷新后生效)'}
              >
                <span className={cn(
                  'absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all',
                  p.enabled ? 'left-[1.125rem]' : 'left-0.5',
                )} />
              </button>
            </div>
            <div className="flex justify-center">
              <button
                type="button"
                onClick={() => { if (confirm(`删除页面插件 "${p.label || p.id}"?`)) deleteMut.mutate(p.id) }}
                className="rounded p-1.5 text-muted hover:bg-danger/10 hover:text-danger transition-colors"
                title="删除"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
