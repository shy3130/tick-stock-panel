/**
 * 系统设置面板 — 全局行为开关。
 *
 * 独立于实时监控, 放置影响整体应用行为的开关项。
 */
import { useState, useCallback, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Settings2, Trash2, RefreshCw, Bell, Volume2, ExternalLink, ZoomIn, Minus, Plus, ShieldCheck, RotateCcw, ChevronDown } from 'lucide-react'
import { usePreferences } from '@/lib/useSharedQueries'
import { api } from '@/lib/api'
import type { UpgradeGuardCategory } from '@/lib/api'
import { toast } from '@/components/Toast'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { ExtensionSlot } from '@/extensions/ExtensionSlot'
import { refreshAlertToastConfig } from '@/components/AlertToast'
import { SOUND_OPTIONS, previewSound } from '@/lib/notificationSound'
import {
  listZhVoices, previewVoice, activateVoice, getCurrentVoiceURI,
} from '@/lib/voiceBroadcast'
import { loadStockExternalTemplate, saveStockExternalTemplate } from '@/lib/stock-external-link'
import { ZOOM_MIN, ZOOM_MAX, ZOOM_STEP, getPageZoom, setPageZoom } from '@/lib/pageZoom'

export function SettingsSystemPanel() {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const [saving, setSaving] = useState(false)

  const screenerAutoRun = prefs?.screener_auto_run ?? true
  const [extTpl, setExtTpl] = useState(() => loadStockExternalTemplate())
  const [clearing, setClearing] = useState(false)

  // ===== 界面缩放 (桌面客户端无浏览器缩放快捷键, CSS zoom 作用于 <html>) =====
  const [pageZoom, setPageZoomState] = useState(() => getPageZoom())
  const changeZoom = useCallback((delta: number) => {
    setPageZoomState(setPageZoom(getPageZoom() + delta))
  }, [])

  const [toastEnabled, setToastEnabled] = useState(() => {
    try { return localStorage.getItem('alert_toast_enabled') !== '0' } catch { return true }
  })
  const [toastMax, setToastMax] = useState(() => {
    try {
      const v = parseInt(localStorage.getItem('alert_toast_max') || '', 10)
      return v >= 1 && v <= 5 ? v : 3
    } catch { return 3 }
  })
  const [soundEnabled, setSoundEnabled] = useState(() => {
    try { return localStorage.getItem('alert_sound_enabled') !== '0' } catch { return true }
  })
  const [soundType, setSoundType] = useState(() => {
    try { return localStorage.getItem('alert_sound') || 'ding' } catch { return 'ding' }
  })
  const [voiceEnabled, setVoiceEnabled] = useState(() => {
    try { return localStorage.getItem('voice_broadcast_enabled') === '1' } catch { return false }
  })
  const [voices, setVoices] = useState(listZhVoices())
  // 用户手选值 (空=走默认偏好 Google 中国大陆)
  const [voiceConfigured, setVoiceConfigured] = useState(() => {
    try { return localStorage.getItem('voice_broadcast_voice') || '' } catch { return '' }
  })
  // 下拉回显值: 用户手选优先, 否则显示当前解析到的语音
  const [voiceURI, setVoiceURI] = useState(() => {
    try { return localStorage.getItem('voice_broadcast_voice') || getCurrentVoiceURI() } catch { return getCurrentVoiceURI() }
  })
  const [voiceRate, setVoiceRate] = useState(() => {
    try {
      const v = parseFloat(localStorage.getItem('voice_broadcast_rate') || '')
      return v >= 0.5 && v <= 2 ? v : 1
    } catch { return 1 }
  })

  // 语音包异步加载 (Google 云语音为 Chrome 联网注入, 比本地晚到), 监听刷新
  useEffect(() => {
    const h = () => {
      setVoices(listZhVoices())
      // 用户未手选时, 跟随默认偏好 (Google CN 到货后自动同步回显)
      if (!voiceConfigured) setVoiceURI(getCurrentVoiceURI())
    }
    if ('speechSynthesis' in window) {
      window.speechSynthesis.addEventListener('voiceschanged', h)
      // 部分浏览器首次需主动触发一次
      h()
    }
    return () => {
      if ('speechSynthesis' in window) window.speechSynthesis.removeEventListener('voiceschanged', h)
    }
  }, [voiceConfigured])

  const save = useCallback(async (cfg: Record<string, unknown>) => {
    setSaving(true)
    try {
      await api.updateRealtimeMonitorConfig(cfg)
      qc.invalidateQueries({ queryKey: QK.preferences })
    } finally {
      setSaving(false)
    }
  }, [qc])

  // 刷新前端缓存: 清除 react-query 缓存 + 强制重载 (绕过浏览器缓存)
  // 不动 localStorage (用户列配置/策略池等偏好保留), 也不影响后端的本地股票数据
  const handleClearCache = useCallback(() => {
    setClearing(true)
    qc.clear()
    // 加时间戳参数强制浏览器重新下载所有静态资源
    setTimeout(() => {
      window.location.href = window.location.pathname + '?_t=' + Date.now()
    }, 300)
  }, [qc])

  return (
    <>
      <PageHeader
        title="系统设置"
        subtitle="全局行为开关"
      />

      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center gap-2 mb-4">
          <Settings2 className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">策略页</h3>
        </div>

        <ToggleRow
          label="进入策略页自动运行策略"
          desc="开启后进入策略页自动跑所有策略获取命中数; 关闭则需手动点击"
          checked={screenerAutoRun}
          disabled={saving}
          onChange={(v) => save({ screener_auto_run: v })}
        />
      </section>

      <section className="rounded-card border border-border bg-surface p-5 mt-6">
        <div className="flex items-center gap-2 mb-4">
          <Bell className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">通知弹窗</h3>
        </div>

        <ToggleRow
          label="开启监控通知弹窗"
          desc="收到监控告警时在右下角弹出通知卡片"
          checked={toastEnabled}
          disabled={saving}
          onChange={(v) => {
            localStorage.setItem('alert_toast_enabled', v ? '1' : '0')
            setToastEnabled(v)
            refreshAlertToastConfig()
          }}
        />

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">最大弹窗个数</div>
            <div className="text-[11px] text-muted truncate">同时显示的通知数量 (1-5), 超出丢弃最旧的</div>
          </div>
          <select
            value={toastMax}
            disabled={!toastEnabled}
            onChange={(e) => {
              const v = Number(e.target.value)
              localStorage.setItem('alert_toast_max', String(v))
              setToastMax(v)
              refreshAlertToastConfig()
            }}
            className="w-16 h-8 px-1.5 rounded-btn border border-border bg-base text-xs text-foreground disabled:opacity-50"
          >
            {[1, 2, 3, 4, 5].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>

        <ToggleRow
          label="通知声效"
          desc="收到监控告警时播放提示音"
          checked={soundEnabled}
          disabled={!toastEnabled}
          onChange={(v) => {
            localStorage.setItem('alert_sound_enabled', v ? '1' : '0')
            setSoundEnabled(v)
            if (v) previewSound(soundType)
          }}
        />

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0 flex items-center gap-1.5">
            <Volume2 className="h-3.5 w-3.5 text-muted" />
            <div>
              <div className="text-sm text-foreground">声效选择</div>
              <div className="text-[11px] text-muted truncate">选择提示音风格</div>
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <select
              value={soundType}
              disabled={!toastEnabled || !soundEnabled}
              onChange={(e) => {
                const v = e.target.value
                localStorage.setItem('alert_sound', v)
                setSoundType(v)
                if (v !== 'none') previewSound(v)
              }}
              className="w-20 h-8 px-1.5 rounded-btn border border-border bg-base text-xs text-foreground disabled:opacity-50"
            >
              {SOUND_OPTIONS.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
            <button
              onClick={() => previewSound(soundType)}
              disabled={!toastEnabled || !soundEnabled || soundType === 'none'}
              className="px-2 h-8 rounded-btn border border-border bg-base text-xs text-secondary hover:text-foreground hover:border-accent/30 disabled:opacity-50 transition-colors cursor-pointer"
            >
              试听
            </button>
          </div>
        </div>
      </section>

      <section className="rounded-card border border-border bg-surface p-5 mt-6">
        <div className="flex items-center gap-2 mb-4">
          <Volume2 className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">语音播报</h3>
        </div>

        <ToggleRow
          label="监控告警语音播报"
          desc="收到告警时用中文语音播报内容 (需浏览器支持, 默认关闭)"
          checked={voiceEnabled}
          disabled={!toastEnabled}
          onChange={(v) => {
            localStorage.setItem('voice_broadcast_enabled', v ? '1' : '0')
            setVoiceEnabled(v)
            if (v) { activateVoice(); previewVoice() }   // 开启即激活 + 试听一句
          }}
        />

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0 flex items-center gap-1.5">
            <Volume2 className="h-3.5 w-3.5 text-muted" />
            <div>
              <div className="text-sm text-foreground">语音音色</div>
              <div className="text-[11px] text-muted truncate">
                {voices.length === 0
                  ? '未检测到中文语音, 将用系统默认'
                  : '默认优先 Google 中国大陆 (音质最佳)'}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <select
              value={voiceURI}
              disabled={!toastEnabled || !voiceEnabled}
              onChange={(e) => {
                const v = e.target.value
                if (v) {
                  // 手选某一语音包
                  localStorage.setItem('voice_broadcast_voice', v)
                  setVoiceConfigured(v)
                  setVoiceURI(v)
                } else {
                  // 选"默认偏好": 清空手选, 走 Google 中国大陆偏好
                  localStorage.removeItem('voice_broadcast_voice')
                  setVoiceConfigured('')
                  setVoiceURI(getCurrentVoiceURI())
                }
              }}
              className="w-32 h-8 px-1.5 rounded-btn border border-border bg-base text-xs text-foreground disabled:opacity-50"
            >
              <option value={getCurrentVoiceURI()}>默认偏好</option>
              {voices
                .filter(v => v.voiceURI !== getCurrentVoiceURI())
                .map(v => <option key={v.voiceURI} value={v.voiceURI}>{v.name}</option>)
              }
            </select>
            <button
              onClick={() => previewVoice()}
              disabled={!toastEnabled || !voiceEnabled}
              className="px-2 h-8 rounded-btn border border-border bg-base text-xs text-secondary hover:text-foreground hover:border-accent/30 disabled:opacity-50 transition-colors cursor-pointer"
            >
              试听
            </button>
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">语速</div>
            <div className="text-[11px] text-muted truncate">0.5 慢 — 2.0 快</div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <input
              type="range" min={0.5} max={2} step={0.1} value={voiceRate}
              disabled={!toastEnabled || !voiceEnabled}
              onChange={(e) => {
                const v = parseFloat(e.target.value)
                localStorage.setItem('voice_broadcast_rate', String(v))
                setVoiceRate(v)
              }}
              className="w-32 disabled:opacity-50"
            />
            <span className="text-xs text-muted w-8 text-right">{voiceRate.toFixed(1)}</span>
          </div>
        </div>
      </section>

      <section className="rounded-card border border-border bg-surface p-5 mt-6">
        <div className="flex items-center gap-2 mb-4">
          <ExternalLink className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">个股详情外链</h3>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">详情页 URL 模板</div>
            <div className="text-[11px] text-muted truncate">{"支持 {code} {market} {symbol} · 留空关闭外链"}</div>
          </div>
          <input
            value={extTpl}
            onChange={(e) => {
              setExtTpl(e.target.value)
              saveStockExternalTemplate(e.target.value)
            }}
            placeholder="https://..."
            spellCheck={false}
            className="w-[26rem] max-w-[60%] h-8 px-2.5 rounded-btn border border-border bg-base text-xs font-mono text-foreground focus:border-accent/50 focus:outline-none"
          />
        </div>
      </section>

      <section className="rounded-card border border-border bg-surface p-5 mt-6">
        <div className="flex items-center gap-2 mb-4">
          <Trash2 className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">缓存</h3>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">刷新前端缓存</div>
            <div className="text-[11px] text-muted truncate">
              清除页面缓存并强制重新加载 (不影响个人配置和本地股票数据)
            </div>
          </div>
          <button
            onClick={handleClearCache}
            disabled={clearing}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                       bg-elevated text-secondary hover:text-foreground transition-colors
                       disabled:opacity-50 shrink-0"
          >
            {clearing ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {clearing ? '清理中…' : '清理并刷新'}
          </button>
        </div>
      </section>

      <section className="rounded-card border border-border bg-surface p-5 mt-6">
        <div className="flex items-center gap-2 mb-4">
          <ZoomIn className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">界面</h3>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">界面缩放</div>
            <div className="text-[11px] text-muted truncate">
              整个界面等比缩放, 立即生效并记忆 (80%–150%)
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {pageZoom !== 1 && (
              <button
                onClick={() => setPageZoomState(setPageZoom(1))}
                className="px-2.5 py-1.5 rounded-btn text-xs bg-elevated text-secondary
                           hover:text-foreground transition-colors"
              >
                重置
              </button>
            )}
            <button
              onClick={() => changeZoom(-ZOOM_STEP)}
              disabled={pageZoom <= ZOOM_MIN}
              aria-label="缩小界面"
              className="inline-flex items-center justify-center h-7 w-7 rounded-btn
                         bg-elevated text-secondary hover:text-foreground transition-colors
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Minus className="h-3.5 w-3.5" />
            </button>
            <span className="font-mono text-xs text-secondary w-11 text-center select-none">
              {Math.round(pageZoom * 100)}%
            </span>
            <button
              onClick={() => changeZoom(ZOOM_STEP)}
              disabled={pageZoom >= ZOOM_MAX}
              aria-label="放大界面"
              className="inline-flex items-center justify-center h-7 w-7 rounded-btn
                         bg-elevated text-secondary hover:text-foreground transition-colors
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        <ExtensionSlot name="settings.system.section" context={{}} compact />
      </section>

      <UpgradeGuardCard />
    </>
  )
}

// ===== 升级保护 (二次开发) =====
// 用户修改/新增的程序文件在每次启动时备份到 data/upgrade_guard/, 升级不丢;
// 检测到版本变化时给出对比报告: 可恢复项一键还原, 冲突项保留三方材料手工合并。

const UPGRADE_CATEGORY_LABEL: Record<UpgradeGuardCategory, string> = {
  restorable: '可恢复',
  conflict: '需手工合并',
  match: '已一致',
  added_lost: '新增文件可恢复',
}

function UpgradeGuardCard() {
  const qc = useQueryClient()
  const { data: st, isLoading } = useQuery({
    queryKey: ['upgrade-guard', 'status'],
    queryFn: api.upgradeGuardStatus,
    staleTime: 30_000,
    retry: 1,
  })
  const [busy, setBusy] = useState<'scan' | 'restore-all' | string | null>(null)
  const [listOpen, setListOpen] = useState(false)

  const refresh = () => qc.invalidateQueries({ queryKey: ['upgrade-guard', 'status'] })

  const scan = async () => {
    setBusy('scan')
    try {
      await api.upgradeGuardScan()
      await refresh()
    } catch (e: any) {
      toast(`扫描失败: ${e?.message ?? e}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  const restore = async (reportId: string, relpath: string) => {
    setBusy(relpath)
    try {
      await api.upgradeGuardRestore(reportId, relpath)
      toast(`已恢复 ${relpath}`, 'success')
      await refresh()
    } catch (e: any) {
      toast(`恢复失败: ${e?.message ?? e}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  const restoreAll = async (reportId: string, files: { relpath: string }[]) => {
    setBusy('restore-all')
    let ok = 0
    for (const f of files) {
      try {
        await api.upgradeGuardRestore(reportId, f.relpath)
        ok += 1
      } catch { /* 单个失败继续其余 */ }
    }
    toast(`已恢复 ${ok}/${files.length} 个文件`, ok === files.length ? 'success' : 'error')
    await refresh()
    setBusy(null)
  }

  const dismiss = async () => {
    setBusy('dismiss')
    try {
      await api.upgradeGuardDismiss()
      await refresh()
    } finally {
      setBusy(null)
    }
  }

  const report = st?.pending_report ?? null
  const reportFiles = report?.files ?? []
  const restorableFiles = reportFiles.filter(f => f.category === 'restorable' || f.category === 'added_lost')

  return (
    <section className="rounded-card border border-border bg-surface p-5 mt-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-secondary" />
            <h3 className="text-sm font-medium text-foreground">升级保护（二次开发）</h3>
          </div>
          <div className="mt-1.5 text-[11px] leading-relaxed text-muted">
            {isLoading ? '加载中…' : st?.enabled
              ? <>修改/新增的程序文件每次启动自动备份到 <code className="text-secondary">{st?.backup_dir}</code>，升级不会丢失</>
              : '当前安装未携带版本清单（源码运行）— 请使用 Git 管理二次开发改动，配合 scripts/upgrade_check.py 做升级预检'}
          </div>
        </div>
        {st?.enabled && (
          <div className="flex shrink-0 items-center gap-2">
            <span className="text-[10px] text-muted">清单 {st?.manifest_version ?? '—'} · 当前 {st?.current_version}</span>
            <button onClick={scan} disabled={busy === 'scan' || !!st?.scanning}
              title="重新扫描程序目录, 刷新常备备份 (修改程序文件后可手动执行)"
              className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50">
              <RefreshCw className={`h-3 w-3 ${busy === 'scan' || st?.scanning ? 'animate-spin' : ''}`} />
              {busy === 'scan' || st?.scanning ? '扫描中' : '重新扫描'}
            </button>
          </div>
        )}
      </div>

      {st?.enabled && (
        <div className="mt-3 border-t border-border/60 pt-3">
          <button onClick={() => setListOpen(v => !v)} className="flex w-full items-center gap-1.5 text-left text-xs text-secondary hover:text-foreground">
            <ChevronDown className={`h-3 w-3 transition-transform ${listOpen ? '' : '-rotate-90'}`} />
            当前检测到 <b className="text-foreground">{st?.modified?.length ?? 0}</b> 个修改文件、<b className="text-foreground">{st?.added?.length ?? 0}</b> 个新增文件
            <span className="text-[10px] text-muted">（上次扫描 {st?.last_scan?.replace('T', ' ').slice(0, 19) ?? '—'}）</span>
          </button>
          {listOpen && (
            <div className="mt-2 max-h-48 overflow-y-auto rounded-lg border border-border/60 bg-base/40 p-2 font-mono text-[10px] leading-5">
              {st?.modified?.map(f => <div key={f} className="truncate text-warning">✎ {f}</div>)}
              {st?.added?.map(f => <div key={f} className="truncate text-bear">＋ {f}</div>)}
              {(st?.modified?.length ?? 0) + (st?.added?.length ?? 0) === 0 && (
                <div className="text-muted">无二次开发改动</div>
              )}
            </div>
          )}
        </div>
      )}

      {report && (
        <div className="mt-3 rounded-lg border border-warning/25 bg-warning/[0.05] p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 text-xs">
              <div className="font-medium text-foreground">
                升级检测: {report.from_version} → {report.to_version}
              </div>
              <div className="mt-1 text-[11px] leading-relaxed text-muted">
                你的 {reportFiles.length} 个二次开发文件已归档比对 —
                可恢复 <b className="text-bear">{report.counts.restorable + report.counts.added_lost}</b> ·
                需手工合并 <b className="text-warning">{report.counts.conflict}</b> ·
                已一致 <b className="text-muted">{report.counts.match}</b>
              </div>
            </div>
            <div className="flex shrink-0 gap-2">
              {restorableFiles.length > 0 && (
                <button onClick={() => restoreAll(report.id, restorableFiles)} disabled={!!busy}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1 text-[11px] text-white hover:bg-accent/90 disabled:opacity-50">
                  <RotateCcw className="h-3 w-3" />
                  恢复可恢复项 ({restorableFiles.length})
                </button>
              )}
              <button onClick={dismiss} disabled={busy === 'dismiss'}
                className="rounded-lg px-2.5 py-1 text-[11px] text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50">
                知道了
              </button>
            </div>
          </div>
          <div className="mt-2 max-h-56 overflow-y-auto rounded-md border border-border/40 bg-base/40 p-2">
            {reportFiles.map(f => (
              <div key={f.relpath} className="flex items-center justify-between gap-2 py-0.5">
                <span className="min-w-0 truncate font-mono text-[10px] text-secondary" title={f.relpath}>{f.relpath}</span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <span className={`text-[10px] ${f.category === 'conflict' ? 'text-warning' : f.category === 'match' ? 'text-muted' : 'text-bear'}`}>
                    {UPGRADE_CATEGORY_LABEL[f.category]}
                  </span>
                  {(f.category === 'restorable' || f.category === 'added_lost') && (
                    <button onClick={() => restore(report.id, f.relpath)} disabled={busy === f.relpath}
                      className="rounded px-1.5 py-0.5 text-[10px] text-accent hover:bg-accent/10 disabled:opacity-50">
                      {busy === f.relpath ? '恢复中' : '恢复'}
                    </button>
                  )}
                </span>
              </div>
            ))}
          </div>
          {report.counts.conflict > 0 && (
            <div className="mt-2 text-[10px] leading-relaxed text-muted">
              冲突项不自动合并: 你的版本已归档在 <code>{report.dir}</code>, 磁盘上是新版文件, 请对照手动合并后重新扫描。
            </div>
          )}
        </div>
      )}
    </section>
  )
}


// ===== ToggleRow =====

function ToggleRow({
  label,
  desc,
  checked,
  disabled,
  onChange,
}: {
  label: string
  desc: string
  checked: boolean
  disabled?: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0">
        <div className="text-sm text-foreground">{label}</div>
        <div className="text-[11px] text-muted truncate">{desc}</div>
      </div>
      <button
        onClick={() => onChange(!checked)}
        disabled={disabled}
        className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 disabled:opacity-50 ${
          checked ? 'bg-accent' : 'bg-elevated'
        }`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[18px]' : 'translate-x-[3px]'
          }`}
        />
      </button>
    </div>
  )
}
