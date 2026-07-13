import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Key,
  Eye,
  EyeOff,
  Trash2,
  CheckCircle2,
  AlertCircle,
  RefreshCw,
  Activity,
  ExternalLink,
  Loader2,
  Save,
  Check,
  HelpCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { useCapabilities, useSettings } from '@/lib/useSharedQueries'
import { QK } from '@/lib/queryKeys'
import { CAP_LABELS, tierTextStyle, tierStyle, tierBaseName, ALL_TIERS, TierTag } from '@/lib/capability-labels'
import { cn } from '@/lib/cn'
import {
  SettingsPanel,
  SettingsSection,
  settingsControlClass,
  settingsIconButtonClass,
  settingsPrimaryButtonClass,
  settingsSecondaryButtonClass,
} from './SettingsPrimitives'

// ===== 导出为 Panel 组件 (由 Settings.tsx 嵌入) =====

export function SettingsKeysPanel() {
  const qc = useQueryClient()

  const settings = useSettings()
  const caps = useCapabilities()

  const [keyInput, setKeyInput] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = useMutation({
    mutationFn: () => api.saveTickflowKey(keyInput.trim()),
    onSuccess: (data) => {
      setKeyInput('')
      qc.invalidateQueries({ queryKey: QK.settings })
      qc.invalidateQueries({ queryKey: QK.capabilities })
      if (data.ok) {
        setSaved(true)
        setTimeout(() => setSaved(false), 2000)
      }
      // ok=false 由 save.data 在下方渲染提示(reason=invalid),无需额外处理
    },
  })

  const clear = useMutation({
    mutationFn: () => api.clearTickflowKey(),
    onSuccess: () => {
      setConfirmClear(false)
      qc.invalidateQueries({ queryKey: QK.settings })
      qc.invalidateQueries({ queryKey: QK.capabilities })
    },
  })

  const redetect = useMutation({
    mutationFn: api.redetectCapabilities,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.settings })
      qc.invalidateQueries({ queryKey: QK.capabilities })
    },
  })

  const mode = settings.data?.mode
  const masked = settings.data?.tickflow_api_key_masked
  const capCount = caps.data ? Object.keys(caps.data.capabilities).length : 0

  return (
    <>
      <SettingsPanel
        icon={Key}
        title="API 与订阅"
        description="管理数据服务密钥、当前订阅档位和系统已检测到的可用能力。"
        width="default"
      >
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_1.3fr]">
          {/* ========== 左列: Key 配置 ========== */}
          <div className="space-y-5">
            <SettingsSection icon={Key} title="TickFlow API Key">
            <p className="text-sm text-secondary leading-relaxed mb-4">
              在{' '}
              <a
                href="https://tickflow.org/auth/register?ref=V3KDKGXPEA"
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline inline-flex items-baseline gap-0.5"
              >
                tickflow.org
                <ExternalLink className="h-3 w-3 self-center" />
              </a>{' '}
              注册获取。API Key 存放为本地文件,不会上传任何第三方,请妥善保管。
            </p>

            {/* 当前状态 */}
            <div className="flex items-center justify-between mb-4">
              <div className="min-w-0">
                <div className="text-[10px] uppercase tracking-widest text-muted">状态</div>
                <div className="mt-1 flex items-center gap-2 min-w-0">
                  {mode === 'api_key' ? (
                    <>
                      <CheckCircle2 className="h-4 w-4 text-bear shrink-0" />
                      <span className="text-sm font-medium shrink-0">已配置</span>
                      <span className="font-mono text-xs text-secondary truncate">{masked}</span>
                    </>
                  ) : mode === 'free' ? (
                    <>
                      <CheckCircle2 className="h-4 w-4 text-bear shrink-0" />
                      <span className="text-sm font-medium shrink-0">免费 Key</span>
                      <span className="font-mono text-xs text-secondary truncate">{masked}</span>
                    </>
                  ) : (
                    <>
                      <AlertCircle className="h-4 w-4 text-muted shrink-0" />
                      <span className="text-sm font-medium text-muted">未配置</span>
                    </>
                  )}
                </div>
              </div>
              {(mode === 'api_key' || mode === 'free') && (
                <button
                  type="button"
                  onClick={() => setConfirmClear(true)}
                  disabled={clear.isPending}
                  className={cn(settingsSecondaryButtonClass, 'h-11 shrink-0 border-transparent bg-elevated px-2.5 hover:text-danger sm:h-8')}
                >
                  <Trash2 className="h-3 w-3" />
                  清除
                </button>
              )}
            </div>

            {/* 输入 */}
            <form
              onSubmit={(e) => {
                e.preventDefault()
                if (keyInput.trim()) save.mutate()
              }}
              className="space-y-2"
            >
              <div className="relative">
                <input
                  type={revealing ? 'text' : 'password'}
                  placeholder={mode === 'none' ? '粘贴 TickFlow API Key' : '粘贴新 Key 替换当前'}
                  value={keyInput}
                  onChange={(e) => { setKeyInput(e.target.value); if (saved) setSaved(false) }}
                  className={cn(settingsControlClass, 'pr-10 font-mono')}
                />
                <button
                  type="button"
                  onClick={() => setRevealing((v) => !v)}
                  className={cn(settingsIconButtonClass, 'absolute right-0.5 top-1/2 -translate-y-1/2')}
                  tabIndex={-1}
                  aria-label={revealing ? '隐藏' : '显示'}
                >
                  {revealing ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
              <button
                type="submit"
                disabled={save.isPending || (!keyInput.trim() && !saved)}
                className={cn(settingsPrimaryButtonClass, 'w-full')}
              >
                {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
                {save.isPending ? '保存中...' : saved ? '已保存' : '保存并检测'}
              </button>

              {/* 检测中提示 —— 成功/失败后自动消失 */}
              {save.isPending && (
                <div className="flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] leading-snug text-warning">
                  <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
                  <span>
                    验证通过前请不要离开当前页面 · 如遇网络问题请点击
                    <button
                      type="button"
                      onClick={() => { save.reset(); redetect.mutate() }}
                      disabled={redetect.isPending}
                      className="font-semibold underline underline-offset-2 hover:text-warning/80 disabled:opacity-50"
                    >
                      {redetect.isPending ? '重新检测中…' : '重新检测'}
                    </button>
                  </span>
                </div>
              )}
            </form>

            {save.isError && (
              <div className="mt-3 text-xs text-danger">
                保存失败:{String((save.error as any).message)}
              </div>
            )}
            {/* 无效 key —— 先探后存:探测失败(key 无效/乱填)时不存储,提示用户 */}
            {save.data && !save.data.ok && (
              <div className="mt-3 text-xs text-danger flex items-center gap-1.5">
                <AlertCircle className="h-3 w-3 shrink-0" />
                {save.data.reason === 'invalid'
                  ? 'Key 无效或已过期,请检查后重试(未保存该 Key)'
                  : save.data.error ?? '保存失败'}
              </div>
            )}
            {save.data?.ok && (
              <div className="mt-3 text-xs text-bear flex items-center gap-1.5">
                <CheckCircle2 className="h-3 w-3" />
                保存成功 — 档位 {save.data.tier_label}
                {save.data.mode === 'free' && '(免费档 · 历史日K + 自选实时监控)'}
              </div>
            )}
            </SettingsSection>
          </div>

          {/* ========== 右列: 档位 + 能力 ========== */}
          <div className="space-y-5">
            <SettingsSection
              icon={Activity}
              title="订阅档位"
              className="overflow-visible"
              action={
                <button
                  type="button"
                  onClick={() => redetect.mutate()}
                  disabled={redetect.isPending}
                  className={cn(settingsSecondaryButtonClass, 'h-11 px-2.5 sm:h-8')}
                >
                  <RefreshCw className={`h-3 w-3 ${redetect.isPending ? 'animate-spin' : ''}`} />
                  重新检测
                </button>
              }
            >
            {caps.data ? (
              <>
                <div className="flex items-center gap-1.5">
                  <div className="font-mono text-3xl font-bold tracking-tight" style={tierTextStyle(caps.data.label)}>
                    {caps.data.label}
                  </div>
                  <TierHelpPopover currentLabel={caps.data.label} />
                </div>
                <div className="mt-1 text-xs text-muted">
                  根据 API Key 自动检测 · 拥有"代表性 capability"任一即认为该档
                </div>

                {settings.data?.missing_caps && settings.data.missing_caps.length > 0 && (
                  <div className="mt-3 rounded-btn border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                    <div className="font-medium text-warning mb-1">
                      本档应有但未探测到({settings.data.missing_caps.length} 项)
                    </div>
                    <div className="text-secondary space-y-0.5">
                      {settings.data.missing_caps.map((c) => (
                        <div key={c} className="font-mono">
                          {CAP_LABELS[c]?.name ?? c}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div className="text-sm text-muted">加载中…</div>
            )}
            </SettingsSection>

            <SettingsSection icon={CheckCircle2} title="可用功能" badge={`${capCount} 项`}>
            {caps.data && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
                className="-mx-4 -mb-4"
              >
                <div className="border-t border-border">
                  {Object.entries(caps.data.capabilities).map(([cap, lim]) => {
                    const meta = CAP_LABELS[cap]
                    return (
                      <div
                        key={cap}
                        className="flex items-baseline justify-between gap-4 border-b border-border px-4 py-3 last:border-b-0"
                      >
                        <div className="min-w-0">
                          <div className="text-sm text-foreground truncate">
                            {meta?.name ?? cap}
                          </div>
                          {meta?.hint && (
                            <div className="mt-0.5 text-[11px] text-muted truncate">
                              {meta.hint}
                            </div>
                          )}
                        </div>
                        <div className="text-right shrink-0 text-xs">
                          <div className="font-mono text-foreground">
                            {lim.rpm ? `${lim.rpm}/min` : lim.subscribe ? `${lim.subscribe} 订阅` : '—'}
                          </div>
                          {lim.batch && (
                            <div className="font-mono text-muted">{lim.batch} 只/次</div>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </motion.div>
            )}

            {settings.data?.probe_log && settings.data.probe_log.length > 0 && (
              <details className="-mx-4 -mb-4 mt-4 border-t border-border">
                <summary className="cursor-pointer select-none px-4 py-3 text-xs text-muted transition-colors duration-150 ease-smooth hover:text-secondary">
                  查看检测日志
                </summary>
                <div className="space-y-0.5 px-4 pb-4 font-mono text-[11px] text-secondary">
                  {settings.data.probe_log.map((line, i) => (
                    <div key={i}>{line}</div>
                  ))}
                </div>
              </details>
            )}
            </SettingsSection>
          </div>
        </div>
      </SettingsPanel>

      <ConfirmDialog
        open={confirmClear}
        title="清除 API Key"
        message="清除后将退回 None 档(仅历史日K),需要重新输入 Key 才能恢复。"
        confirmText="确认清除"
        danger
        pending={clear.isPending}
        onCancel={() => setConfirmClear(false)}
        onConfirm={() => clear.mutate()}
      />
    </>
  )
}

// ===== 档位说明弹窗 =====

function TierHelpPopover({ currentLabel }: { currentLabel: string }) {
  const [open, setOpen] = useState(false)
  const currentBase = tierBaseName(currentLabel)

  return (
    <div className="relative inline-flex items-center">
      <HelpCircle
        className="h-4 w-4 text-muted/60 cursor-help hover:text-muted transition-colors"
        onClick={() => setOpen(v => !v)}
      />
      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, y: -4, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.95 }}
              transition={{ duration: 0.15 }}
              className="absolute top-full left-0 mt-1 z-50 w-72 bg-surface border border-border rounded-lg shadow-xl p-3.5 text-[11px] leading-relaxed"
              onClick={e => e.stopPropagation()}
            >
              {/* 档位 tag 横排 */}
              <div className="flex items-center gap-1.5 mb-3">
                {ALL_TIERS.map(t => (
                  <div key={t} className={`flex flex-col items-center gap-1 ${t === currentBase ? '' : 'opacity-60'}`}>
                    <TierTag label={t} />
                  </div>
                ))}
              </div>

              {/* 每档说明 */}
              <div className="space-y-1 mb-3 pb-3 border-b border-border">
                {ALL_TIERS.map(t => {
                  const s = tierStyle(t)
                  return (
                    <div key={t} className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full shrink-0" style={s.dotStyle} />
                      <span className="font-mono font-bold w-12 shrink-0" style={s.labelTextStyle}>{t === 'none' ? 'None' : t}</span>
                      <span className="text-secondary">{s.desc}</span>
                    </div>
                  )
                })}
              </div>

              <div className="mb-3 rounded-btn border border-warning/30 bg-warning/10 px-2.5 py-1.5 text-[11px] font-medium text-warning">
                高等档位包含较低档位的全部权益。
              </div>

              {/* 检测说明 */}
              <div className="text-secondary space-y-1.5">
                <div className="font-medium text-foreground">档位检测说明</div>
                <p>保存 Key 后系统会在付费端点逐一试探数据能力:连单只日K都拿不到则判为「None」(不存 Key);有日K但无复权因子则判为「Free」;有复权因子再按代表能力判定 Starter/Pro/Expert。</p>
                <p className="text-muted">None 档与 Free 档运行时都走免费数据通道(仅历史日K),区别仅在于是否保存了 Key。付费档走付费端点,享有实时行情等完整能力。</p>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}
