/**
 * 系统设置面板 — 全局行为开关。
 *
 * 独立于实时监控, 放置影响整体应用行为的开关项。
 */
import { useState, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Settings2, Trash2, RefreshCw, Bell, Volume2, Info } from 'lucide-react'
import { usePreferences, useVersion } from '@/lib/useSharedQueries'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { refreshAlertToastConfig } from '@/components/AlertToast'
import { SOUND_OPTIONS, previewSound } from '@/lib/notificationSound'
import {
  SettingsPanel,
  SettingsSection,
  SettingsToggleRow,
  settingsSecondaryButtonClass,
} from './SettingsPrimitives'

export function SettingsSystemPanel() {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const { data: versionData } = useVersion()
  const [saving, setSaving] = useState(false)

  const screenerAutoRun = prefs?.screener_auto_run ?? true
  const [clearing, setClearing] = useState(false)
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
    <SettingsPanel
      icon={Settings2}
      title="系统设置"
      description="管理工作台的全局行为、通知方式与维护操作。"
      width="default"
    >
      <SettingsSection
        icon={Settings2}
        title="策略页"
      >
        <SettingsToggleRow
          label="进入策略页自动运行策略"
          description="开启后进入策略页自动跑所有策略获取命中数; 关闭则需手动点击"
          checked={screenerAutoRun}
          disabled={saving}
          onCheckedChange={(v) => save({ screener_auto_run: v })}
        />
      </SettingsSection>

      <SettingsSection
        icon={Bell}
        title="通知弹窗"
      >
        <SettingsToggleRow
          label="开启监控通知弹窗"
          description="收到监控告警时在右下角弹出通知卡片"
          checked={toastEnabled}
          disabled={saving}
          onCheckedChange={(v) => {
            localStorage.setItem('alert_toast_enabled', v ? '1' : '0')
            setToastEnabled(v)
            refreshAlertToastConfig()
          }}
        />

        <div className="flex items-center justify-between gap-4 py-2.5">
          <div className="min-w-0">
            <div className="text-sm text-foreground">最大弹窗个数</div>
            <div className="mt-0.5 text-[11px] leading-4 text-muted">同时显示的通知数量 (1-5), 超出丢弃最旧的</div>
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
            className="h-9 w-16 shrink-0 rounded-btn border border-border bg-base px-2 text-xs text-foreground outline-none transition-colors focus-visible:border-accent/50 focus-visible:ring-2 focus-visible:ring-accent/15 disabled:opacity-50"
          >
            {[1, 2, 3, 4, 5].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>

        <SettingsToggleRow
          label="通知声效"
          description="收到监控告警时播放提示音"
          checked={soundEnabled}
          disabled={!toastEnabled}
          onCheckedChange={(v) => {
            localStorage.setItem('alert_sound_enabled', v ? '1' : '0')
            setSoundEnabled(v)
            if (v) previewSound(soundType)
          }}
        />

        <div className="flex items-center justify-between gap-4 py-2.5">
          <div className="flex min-w-0 items-start gap-2.5">
            <Volume2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-secondary" />
            <div className="min-w-0">
              <div className="text-sm text-foreground">声效选择</div>
              <div className="mt-0.5 text-[11px] leading-4 text-muted">选择提示音风格</div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <select
              value={soundType}
              disabled={!toastEnabled || !soundEnabled}
              onChange={(e) => {
                const v = e.target.value
                localStorage.setItem('alert_sound', v)
                setSoundType(v)
                if (v !== 'none') previewSound(v)
              }}
              className="h-9 w-24 rounded-btn border border-border bg-base px-2 text-xs text-foreground outline-none transition-colors focus-visible:border-accent/50 focus-visible:ring-2 focus-visible:ring-accent/15 disabled:opacity-50"
            >
              {SOUND_OPTIONS.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
            <button
              onClick={() => previewSound(soundType)}
              disabled={!toastEnabled || !soundEnabled || soundType === 'none'}
              className={settingsSecondaryButtonClass}
            >
              试听
            </button>
          </div>
        </div>
      </SettingsSection>

      <SettingsSection
        icon={Trash2}
        title="缓存"
      >
        <div className="flex items-center justify-between gap-4 py-2.5">
          <div className="min-w-0">
            <div className="text-sm text-foreground">刷新前端缓存</div>
            <div className="mt-0.5 text-[11px] leading-4 text-muted">
              清除页面缓存并强制重新加载 (不影响个人配置和本地股票数据)
            </div>
          </div>
          <button
            onClick={handleClearCache}
            disabled={clearing}
            className={`${settingsSecondaryButtonClass} shrink-0`}
          >
            {clearing ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {clearing ? '清理中…' : '清理并刷新'}
          </button>
        </div>
      </SettingsSection>

      <SettingsSection
        icon={Info}
        title="关于"
      >
        <div className="flex items-center justify-between gap-4 py-2.5">
          <div className="min-w-0">
            <div className="text-sm text-foreground">版本</div>
            <div className="mt-0.5 text-[11px] leading-4 text-muted">当前安装的应用版本</div>
          </div>
          <span className="font-mono text-xs text-secondary shrink-0">
            {versionData?.version ?? '—'}
          </span>
        </div>

        <div className="flex items-center justify-between gap-4 py-2.5">
          <div className="min-w-0">
            <div className="text-sm text-foreground">检查更新</div>
            <div className="mt-0.5 text-[11px] leading-4 text-muted">前往 GitHub Releases 下载最新版本</div>
          </div>
          <a
            href="https://github.com/shy3130/tickflow-stock-panel/releases/latest"
            target="_blank"
            rel="noreferrer"
            className={`${settingsSecondaryButtonClass} shrink-0`}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            检查更新
          </a>
        </div>
      </SettingsSection>
    </SettingsPanel>
  )
}
