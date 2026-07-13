/**
 * 语音播报 — 用浏览器原生 speechSynthesis, 无需依赖/音频文件。
 *
 * 与 notificationSound.ts 平行的独立模块:
 * - 引擎独立: speechSynthesis (OS 层) vs Web Audio
 * - 配置独立: voice_broadcast_* localStorage keys
 * - 开关独立: voice_broadcast_enabled (默认关)
 *
 * 节流策略 (复用通知声效"整批一声"理念, 语音更严):
 * - speakAlerts() 一次接收一批告警, 合并成一句话
 * - 正在播报时直接丢弃新批次 (快行情下宁可漏念, 不叠成噪音)
 */

import type { AlertEvent } from './api'

const LS = {
  enabled: 'voice_broadcast_enabled',     // '1'/'0', 默认关
  voice: 'voice_broadcast_voice',         // 语音包 voiceURI (空=系统默认)
  rate: 'voice_broadcast_rate',            // 语速 0.5-2, 默认 1
} as const

// ===== 激活态: 浏览器自动播放策略要求用户先交互一次 =====
let _activated = false

/**
 * 解锁 speechSynthesis — 浏览器禁止页面加载后自动发声。
 * 由设置页开关/试听按钮点击时调用一次, 之后 SSE 推来的告警才能自动念。
 */
export function activateVoice() {
  try {
    if (!_activated && 'speechSynthesis' in window) {
      _activated = true
      // 空语句触发激活, 不实际发声
      const u = new SpeechSynthesisUtterance('')
      u.volume = 0
      window.speechSynthesis.speak(u)
    }
  } catch { /* ignore */ }
}

/** 是否支持语音播报 */
export function isVoiceSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
}

// ===== 中文语音包检测 (供设置页下拉) =====

/**
 * 返回系统可用的中文语音包。
 * 注意: getVoices() 首次调用可能返回空, 需监听 voiceschanged 事件。
 */
export function listZhVoices(): SpeechSynthesisVoice[] {
  try {
    return window.speechSynthesis.getVoices().filter(v => v.lang.startsWith('zh'))
  } catch { return [] }
}

// ===== 语音包解析: 用户手选 > 默认偏好(Google 中国大陆) > 兜底 =====

/**
 * 解析当前应使用的语音包。
 * 优先级:
 *   1. 用户在设置页手选的 (voice_broadcast_voice)
 *   2. 默认偏好: Google 中国大陆 (Chrome 在线云语音, zh-CN, 音质接近真人)
 *   3. 兜底: 任意 zh-CN
 *   4. 都没有: undefined (交浏览器系统默认, 可能不标准但不崩)
 */
function resolveVoice(): SpeechSynthesisVoice | undefined {
  try {
    const voices = window.speechSynthesis.getVoices()
    if (voices.length === 0) return undefined

    // 1. 用户手选
    const configured = localStorage.getItem(LS.voice)
    if (configured) {
      const m = voices.find(v => v.voiceURI === configured)
      if (m) return m
    }

    // 2. 默认偏好: Google 中国大陆
    const googleCN = voices.find(v => /Google/i.test(v.name) && v.lang === 'zh-CN')
    if (googleCN) return googleCN

    // 3. 兜底: 任意 zh-CN
    return voices.find(v => v.lang === 'zh-CN')
  } catch { return undefined }
}

/** 当前实际使用的语音 voiceURI (供设置页下拉回显) */
export function getCurrentVoiceURI(): string {
  return resolveVoice()?.voiceURI ?? ''
}

// ===== 文案拼接: 按 source 分类, 只念名称不念代码 =====

const MAX_SPEAK = 3  // 单批最多逐条念 3 只, 超出汇总成数量

/** 涨跌幅用中文习惯念: 5.2% → 涨5.2%, -3.1% → 跌3.1% */
function fmtPctText(pct: number): string {
  if (pct >= 0) return `涨${pct.toFixed(1)}%`
  return `跌${Math.abs(pct).toFixed(1)}%`
}

/**
 * 单条告警 → 播报文案。
 * 设计原则: 必念个股名称 (不念代码), source 分类拼接:
 *   strategy(≤5只单条): "[名称] 进入/移出 策略「策略名」 [涨跌幅]"
 *   strategy(>5只批量): 直接念 message (后端已含名称列表)
 *   signal: "[名称] 入场/出场信号触发 [涨跌幅]"
 *   price/market/其他: "[名称] [message条件摘要] [涨跌幅]"
 */
function buildSingleText(a: AlertEvent): string {
  const name = a.name || '标的'
  const pctText = a.change_pct != null ? fmtPctText(a.change_pct) : ''

  // 策略类: message 存的是策略名(单条) 或完整批量描述(>5只)
  if (a.source === 'strategy') {
    // 批量事件 (symbol 为空/为 _batch): message 已含 "策略「X」进入 N 只：…" 直接念
    if (!a.symbol || a.symbol === '_batch') {
      return a.message || name
    }
    // 单条事件: 从 message 提取策略名 (格式 "策略「X」" 或直接是策略名)
    const sm = a.message?.match(/策略「([^」]+)」/)
    const sname = sm ? sm[1] : a.message || ''
    const action = a.type === 'new_entry' ? '进入' : a.type === 'dropped' ? '移出' : ''
    const parts = [name]
    if (action) parts.push(action)
    if (sname) parts.push(`策略「${sname}」`)
    if (pctText) parts.push(pctText)
    return parts.join(' ')
  }

  // 信号类: message 形如 "入场信号触发"/"出场信号触发"
  if (a.source === 'signal') {
    const parts = [name]
    if (a.message) parts.push(a.message)
    if (pctText) parts.push(pctText)
    return parts.join(' ')
  }

  // 价格/异动/其他: message 是条件摘要 (如 "现价 ≥ 100 · 涨幅 5%")
  const parts = [name]
  if (a.message) parts.push(a.message)
  if (pctText) parts.push(pctText)
  return parts.join(' ')
}

function buildText(alerts: AlertEvent[]): string {
  const head = alerts.slice(0, MAX_SPEAK)
  const parts = head.map(buildSingleText)
  let text = parts.join('；')
  if (alerts.length > MAX_SPEAK) {
    text += `；还有${alerts.length - MAX_SPEAK}只`
  }
  return text
}

// ===== 节流: 正在念时丢弃新批次, 避免快行情叠加噪音 =====
let _speaking = false

/**
 * 播报一批监控告警 (从 localStorage 读配置)。
 * 整批合并成一句话; 正在播报时丢弃新批次 (与"整批一声"理念一致)。
 */
export function speakAlerts(alerts: AlertEvent[]) {
  try {
    if (alerts.length === 0) return
    if (localStorage.getItem(LS.enabled) !== '1') return   // 开关关: 不播报
    if (!isVoiceSupported()) return                          // 不支持: 静默
    if (_speaking) return                                    // 正在念: 丢弃新批次

    const text = buildText(alerts)
    const u = new SpeechSynthesisUtterance(text)
    u.lang = 'zh-CN'
    u.rate = parseFloat(localStorage.getItem(LS.rate) || '1')

    const v = resolveVoice()
    if (v) u.voice = v

    _speaking = true
    u.onend = () => { _speaking = false }
    u.onerror = () => { _speaking = false }
    window.speechSynthesis.speak(u)
  } catch {
    // 语音不可用时静默
  }
}

/** 停止当前播报 (关闭开关/试听前调用) */
export function stopVoice() {
  try {
    if (isVoiceSupported()) {
      window.speechSynthesis.cancel()
      _speaking = false
    }
  } catch { /* ignore */ }
}

/** 试听 (设置页点"试听"用) */
export function previewVoice(text = '语音播报已开启, 这是试听效果') {
  try {
    if (!isVoiceSupported()) return
    activateVoice()
    const u = new SpeechSynthesisUtterance(text)
    u.lang = 'zh-CN'
    u.rate = parseFloat(localStorage.getItem(LS.rate) || '1')
    const v = resolveVoice()
    if (v) u.voice = v
    window.speechSynthesis.cancel()   // 试听前停掉正在念的
    window.speechSynthesis.speak(u)
  } catch { /* ignore */ }
}
