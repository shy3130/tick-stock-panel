/**
 * 界面偏好的跨端持久化 — localStorage 即时读写 + 后端权威副本。
 *
 * 为什么需要后端副本: 浏览器按 origin (协议+主机+端口) 隔离 localStorage。
 * 桌面版启动时用 _find_free_port(3018) 取第一个空闲端口并以 http://127.0.0.1:{port}
 * 加载界面, 端口一变就是另一个 origin, 此前只存 localStorage 的设置 (通知弹窗、
 * 声效、语音播报、外链模板、界面缩放) 全部读不到, 表现为「设置保存了重开又变回去」。
 *
 * 分工:
 *   - 后端 preferences.json 的 ui_prefs 是权威副本, 启动时回填到 localStorage;
 *   - localStorage 保留为同步读取的本地缓存 — 通知/语音/缩放等模块在非 React
 *     路径 (SSE 回调、index.html 首帧脚本) 里同步取值, 不能等网络。
 *
 * 回填规则 (hydrateUiPrefs): 本地已有值优先保留并上推后端 —— 用户本次会话刚改的
 * 值不能被启动时的旧副本覆盖; 本地缺失的键才用后端值补。首次升级时(后端还没有
 * 任何 ui_prefs) 本地值全部上推, 完成从 localStorage-only 到双写的迁移。
 */

const ENDPOINT = '/api/settings/preferences/ui-prefs'

type Codec = 'bool01' | 'int' | 'float' | 'string' | 'json'

/**
 * 每项的 localStorage 键 / 后端键 / 编码方式。
 * - bool01: localStorage 存 '1'/'0', 后端存 bool
 * - int/float: localStorage 与后端都存数字文本
 * - json: 经 storage.ts 的 kv() 写入, localStorage 是 JSON 文本 (字符串带引号)
 */
const SPECS: { ls: string; remote: string; codec: Codec }[] = [
  { ls: 'alert_toast_enabled', remote: 'alert_toast_enabled', codec: 'bool01' },
  { ls: 'alert_toast_max', remote: 'alert_toast_max', codec: 'int' },
  { ls: 'alert_sound_enabled', remote: 'alert_sound_enabled', codec: 'bool01' },
  { ls: 'alert_sound', remote: 'alert_sound', codec: 'string' },
  { ls: 'voice_broadcast_enabled', remote: 'voice_broadcast_enabled', codec: 'bool01' },
  { ls: 'voice_broadcast_voice', remote: 'voice_broadcast_voice', codec: 'string' },
  { ls: 'voice_broadcast_rate', remote: 'voice_broadcast_rate', codec: 'float' },
  { ls: 'stock_external_template', remote: 'stock_external_template', codec: 'json' },
  { ls: 'tf-page-zoom', remote: 'page_zoom', codec: 'json' },
  // 局部界面状态也走跨端副本 (随配置备份迁移): 监控徽标/设置页收起/回测列表高度/导航形态
  { ls: 'monitor_badge_enabled', remote: 'monitor_badge_enabled', codec: 'bool01' },
  { ls: 'tf-settings-nav-collapsed', remote: 'settings_nav_collapsed', codec: 'bool01' },
  { ls: 'bt_strategy_list_maxh', remote: 'bt_strategy_list_maxh', codec: 'int' },
  { ls: 'tf-nav-state', remote: 'nav_state', codec: 'string' },
  // AI 助手悬浮球显隐 — 不入副本时桌面版换端口即回默认开启 ("关了过一会又开")
  { ls: 'assistant.fab.enabled', remote: 'assistant_fab_enabled', codec: 'bool01' },
]

const BY_LS = new Map(SPECS.map(s => [s.ls, s]))
const BY_REMOTE = new Map(SPECS.map(s => [s.remote, s]))

/** localStorage 原始文本 → 后端标量 */
function toRemote(spec: { codec: Codec }, raw: string): unknown {
  switch (spec.codec) {
    case 'bool01':
      return raw === '1'
    case 'int': {
      const n = parseInt(raw, 10)
      return Number.isFinite(n) ? n : raw
    }
    case 'float': {
      const n = parseFloat(raw)
      return Number.isFinite(n) ? n : raw
    }
    case 'json':
      try { return JSON.parse(raw) } catch { return raw }
    default:
      return raw
  }
}

/** 后端标量 → localStorage 原始文本 */
function toLocal(spec: { codec: Codec }, value: unknown): string {
  switch (spec.codec) {
    case 'bool01':
      return value ? '1' : '0'
    case 'json':
      return JSON.stringify(value)
    default:
      return String(value)
  }
}

/**
 * 写本地 + 上推后端。同步返回, 不阻塞调用方 (设置页控件是即时生效语义)。
 * 网络失败静默: localStorage 已写成功, 本次会话行为正确, 下次改动会再上推。
 */
export function setUiPref(lsKey: string, raw: string | null): void {
  try {
    if (raw === null) localStorage.removeItem(lsKey)
    else localStorage.setItem(lsKey, raw)
  } catch { /* localStorage 不可用时只依赖后端 */ }
  const spec = BY_LS.get(lsKey)
  if (!spec) return
  pushUiPrefs({ [spec.remote]: raw === null ? null : toRemote(spec, raw) })
}

let _pushTimer: number | undefined
let _pending: Record<string, unknown> = {}

function pushUiPrefs(updates: Record<string, unknown>): void {
  _pending = { ..._pending, ...updates }
  // 合并 300ms 内的连续改动 (语音语速拖动、缩放连点) 成一次请求
  if (_pushTimer !== undefined) window.clearTimeout(_pushTimer)
  _pushTimer = window.setTimeout(() => {
    const body = _pending
    _pending = {}
    _pushTimer = undefined
    void fetch(ENDPOINT, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => { /* 离线/未登录: 保持本地值, 不打断用户 */ })
  }, 300)
}

let _hydratePromise: Promise<void> | undefined

/**
 * 启动时从后端回填 localStorage (本地已有的键不回写, 反向把本地值上推)。
 * 失败静默 — 读不到后端时继续用本地缓存, 行为与改造前一致。
 * 幂等且共享同一 Promise: main.tsx 与后进加载的模块 (如 AI 助手 store) 无论
 * 谁先调用、调用几次, 拿到的都是同一次回填, await 到的都是"已完成"状态。
 */
export function hydrateUiPrefs(): Promise<void> {
  if (!_hydratePromise) _hydratePromise = _doHydrate()
  return _hydratePromise
}

async function _doHydrate(): Promise<void> {
  try {
    const res = await fetch('/api/settings/preferences', {
      headers: { Accept: 'application/json' },
    })
    if (!res.ok) return
    const payload = await res.json() as { ui_prefs?: Record<string, unknown> }
    const remote = payload?.ui_prefs
    if (!remote || typeof remote !== 'object') return
    const upstream: Record<string, unknown> = {}
    for (const spec of SPECS) {
      let local: string | null = null
      try { local = localStorage.getItem(spec.ls) } catch { /* ignore */ }
      if (local !== null) {
        // 本地优先: 用户本次会话改过的值不能被启动时的旧副本盖掉
        upstream[spec.remote] = toRemote(spec, local)
        continue
      }
      if (spec.remote in remote) {
        try { localStorage.setItem(spec.ls, toLocal(spec, remote[spec.remote])) } catch { /* ignore */ }
      }
    }
    if (Object.keys(upstream).length > 0) pushUiPrefs(upstream)
  } catch { /* 后端不可达: 保持纯本地行为 */ }
}

// 页面缩放 (tf-page-zoom) 的首帧应用在 index.html 内联脚本里, 早于本模块加载,
// 因此首帧仍读 localStorage 缓存; 后端回填后由调用方重新应用一次即可。
void BY_REMOTE // 保留索引供调试/未来扩展
