/**
 * 系统设置面板 — 全局行为开关。
 *
 * 独立于实时监控, 放置影响整体应用行为的开关项。
 */
import { useState, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Settings2, Trash2, RefreshCw, Bell, Volume2, Info, Database } from 'lucide-react'
import { usePreferences, useVersion } from '@/lib/useSharedQueries'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { refreshAlertToastConfig } from '@/components/AlertToast'
import { SOUND_OPTIONS, previewSound } from '@/lib/notificationSound'

export function SettingsSystemPanel() {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const { data: versionData } = useVersion()
  const [saving, setSaving] = useState(false)

  const screenerAutoRun = prefs?.screener_auto_run ?? true
  const currentProvider = prefs?.effective_data_provider ?? prefs?.data_provider ?? 'fquant_local'
  const providerEnvOverride = prefs?.data_provider_env_override ?? false
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

  const saveDataProvider = useCallback(async (provider: 'fquant' | 'fquant_local') => {
    setSaving(true)
    try {
      await api.updateDataProvider(provider)
      qc.invalidateQueries()
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

      <section className="panel">
        <div className="flex items-center gap-2 mb-4">
          <Database className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">数据源</h3>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">行情数据源</div>
            <div className="text-[11px] text-muted truncate">
              {providerEnvOverride ? '当前由 DATA_PROVIDER 环境变量锁定' : '切换后重新检测能力并刷新缓存'}
            </div>
          </div>
          <select
            value={currentProvider}
            disabled={saving || providerEnvOverride}
            onChange={(e) => saveDataProvider(e.target.value as 'fquant' | 'fquant_local')}
            className="control w-32 h-8 px-2 text-xs text-foreground disabled:opacity-50"
          >
            <option value="fquant">FQuant 本地</option>
            <option value="fquant_local">duckdb</option>
          </select>
        </div>
      </section>

      <section className="panel mt-6">
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

      <section className="panel mt-6">
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
            className="control w-16 h-8 px-1.5 text-xs text-foreground disabled:opacity-50"
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
              className="control w-20 h-8 px-1.5 text-xs text-foreground disabled:opacity-50"
            >
              {SOUND_OPTIONS.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
            <button
              onClick={() => previewSound(soundType)}
              disabled={!toastEnabled || !soundEnabled || soundType === 'none'}
              className="control px-2 h-8 text-xs text-secondary hover:text-foreground hover:border-accent/30 disabled:opacity-50 transition-colors cursor-pointer"
            >
              试听
            </button>
          </div>
        </div>
      </section>

      <section className="panel mt-6">
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
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs bg-elevated text-secondary hover:text-foreground transition-colors disabled:opacity-50 shrink-0"
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

      <section className="panel mt-6">
        <div className="flex items-center gap-2 mb-4">
          <Info className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">关于</h3>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">版本</div>
            <div className="text-[11px] text-muted truncate">当前安装的应用版本</div>
          </div>
          <span className="font-mono text-xs text-secondary shrink-0">
            {versionData?.version ?? '—'}
          </span>
        </div>

        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">检查更新</div>
            <div className="text-[11px] text-muted truncate">前往 GitHub Releases 下载最新版本</div>
          </div>
          <a
            href="https://github.com/shy3130/tickflow-stock-panel/releases/latest"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs bg-elevated text-secondary hover:text-foreground transition-colors shrink-0"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            检查更新
          </a>
        </div>
      </section>
    </>
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
