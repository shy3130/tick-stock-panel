/**
 * 复盘生成状态的全局单例 store —— 脱离 Review 组件生命周期。
 *
 * 解决的问题:生成中切换到其他页面,Review 组件卸载会丢失 phase/content。
 * 本 store 把流式生成的状态提到模块级,组件卸载后流仍在后台继续跑,
 * 回到页面订阅即可恢复显示。
 *
 * 设计:
 *  - 模块级 state(phase/content/meta/focus),唯一的生成实例
 *  - AbortController 存模块级 ref,组件卸载不中断流
 *  - 订阅者列表(notify 机制),Review mount 时订阅、unmount 时退订
 */
import { api } from '@/lib/api'
import type { MarketCode } from '@/lib/market-display'

export type ReviewPhase = 'idle' | 'loading' | 'streaming' | 'done' | 'error'

export interface ReviewMeta {
  as_of?: string
  emotion_score?: number
  emotion_label?: string
  summary?: string
}

export interface ReviewState {
  phase: ReviewPhase
  content: string
  error: string
  meta: ReviewMeta | null
  focus: string
}

const INITIAL: ReviewState = { phase: 'idle', content: '', error: '', meta: null, focus: '' }

// ===== 模块级单例状态(组件卸载不销毁)=====
let state: ReviewState = { ...INITIAL }
let abortCtrl: AbortController | null = null

// 当前生成来源: 'manual'(手动点生成) | 'sse'(定时任务 SSE 推送) | null(空闲)
// 用于区分两条流, 避免互相丢弃事件或重复归档。
let generatingSource: 'manual' | 'sse' | null = null

// ===== 订阅机制 =====
type Listener = () => void
const listeners = new Set<Listener>()

function notify() {
  for (const l of listeners) l()
}

export function getReviewState(): ReviewState {
  return state
}

export function subscribeReview(listener: Listener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// 暴露给组件直接读取最新 meta(用于自动归档,避免闭包取旧值)
export function getReviewMeta(): ReviewMeta | null {
  return state.meta
}

/** 是否正在生成(loading 或 streaming) */
export function isReviewGenerating(): boolean {
  return state.phase === 'loading' || state.phase === 'streaming'
}

/**
 * 启动复盘生成。返回后流在后台独立运行,组件卸载不影响。
 * @param asOf 复盘日期
 * @param focus 用户追加的复盘关注点
 * @param onDone 完成回调(供调用方做自动归档)
 */
export async function startReviewGeneration(
  asOf: string | undefined,
  focus: string,
  market: MarketCode,
  onDone?: (fullContent: string, meta: ReviewMeta | null) => void,
): Promise<void> {
  // 已在生成中,不重复启动
  if (isReviewGenerating()) return

  generatingSource = 'manual'
  state = { phase: 'loading', content: '', error: '', meta: null, focus }
  notify()

  abortCtrl = new AbortController()
  let buf = ''
  let failed = false
  let doneMeta: ReviewMeta | null = null

  try {
    for await (const evt of api.reviewStream(asOf, focus, market)) {
      if (abortCtrl.signal.aborted) break
      if (evt.type === 'meta') {
        doneMeta = evt
        state = { ...state, meta: evt }
        notify()
      } else if (evt.type === 'delta' && evt.content) {
        buf += evt.content
        state = { ...state, content: buf, phase: 'streaming' }
        notify()
      } else if (evt.type === 'error') {
        failed = true
        state = { ...state, error: evt.message ?? '复盘失败', phase: 'error' }
        notify()
        return
      } else if (evt.type === 'done') {
        state = { ...state, phase: 'done' }
        notify()
      }
    }
    // 流正常结束但无 done 事件,按 done 处理
    if (buf && !failed) {
      state = { ...state, phase: 'done' }
      notify()
      // 自动归档(仅手动流: 定时流由后端归档, SSE done 不走这里)
      if (buf && !failed) {
        onDone?.(buf, doneMeta)
      }
    }
  } catch (e: any) {
    if (!abortCtrl.signal.aborted) {
      state = { ...state, error: e?.message ?? '复盘失败', phase: 'error' }
      notify()
    }
  } finally {
    abortCtrl = null
    generatingSource = null
  }
}

/** 中断当前生成(供"查看历史"等场景主动中断流)。 */
export function abortReviewGeneration(): void {
  abortCtrl?.abort()
  abortCtrl = null
}

/** 设置当前查看的历史报告(把 store 状态切到 done + 该报告内容)。 */
export function setViewingReport(report: {
  content: string
  as_of?: string
  emotion_score?: number | null
  emotion_label?: string
  summary?: string
}): void {
  abortCtrl?.abort()
  abortCtrl = null
  state = {
    phase: 'done',
    content: report.content,
    error: '',
    meta: {
      as_of: report.as_of,
      emotion_score: report.emotion_score ?? undefined,
      emotion_label: report.emotion_label,
      summary: report.summary,
    },
    focus: state.focus,
  }
  notify()
}

/** 重置到 idle(清空当前显示)。 */
export function resetReview(): void {
  state = { ...INITIAL }
  notify()
}

/**
 * 喂入一条来自 SSE 的复盘事件(定时生成时后端推来的)。
 *
 * 用途: 定时复盘在后端流式生成, 通过 /api/intraday/stream 的 review_progress 事件
 * 把 meta/delta/done 等实时推给前端, 前端调本函数把事件写进 store ——
 * 这样开着复盘页的用户能看到「边生成边显示」, 和手动点生成完全一致。
 *
 * 事件格式与 recap_market_stream 产出一致:
 *   {type:'meta'|'delta'|'error'|'done'|'retry', ...}
 *
 * 与手动生成的并发:
 *  - 若手动正在生成(isReviewGenerating), 忽略 SSE 事件(手动流优先, 避免冲突)。
 *  - done 带 archived=true(定时场景后端已归档): 不重复调归档接口, 仅切到 done 态。
 *  - retry: 后端 LLM 断流重试, 清空已累积内容重新开始。
 */
export function feedReviewEvent(evt: any): void {
  if (!evt || typeof evt !== 'object') return
  const t = evt.type

  // 并发控制: 手动流进行中时, SSE 事件一律忽略(手动流优先, 避免两条流抢同一个 store)
  // 但若当前是 SSE 流自己在跑(generatingSource==='sse'), 则正常处理后续事件
  if (generatingSource === 'manual') return

  if (t === 'meta') {
    // 定时流的第一个事件: 标记来源为 sse, 进入 streaming 态, 重置 content
    generatingSource = 'sse'
    state = { phase: 'streaming', content: '', error: '', meta: evt, focus: '' }
    notify()
  } else if (t === 'delta' && evt.content) {
    // 只有 sse 流进行中时才累积(防止 meta 丢失时的孤立 delta)
    if (generatingSource !== 'sse') return
    state = { ...state, content: state.content + evt.content, phase: 'streaming' }
    notify()
  } else if (t === 'retry') {
    if (generatingSource !== 'sse') return
    // 后端重试: 清空已累积内容, 等待新一轮 meta/delta
    state = { ...state, content: '', phase: 'streaming' }
    notify()
  } else if (t === 'error') {
    if (generatingSource !== 'sse') return
    state = { ...state, error: evt.message ?? '复盘生成失败', phase: 'error' }
    notify()
    generatingSource = null
  } else if (t === 'done') {
    if (generatingSource !== 'sse') return
    // 定时场景 done 带 archived=true: 后端已归档, 前端只切 done 态, 不调归档接口。
    state = { ...state, phase: 'done' }
    notify()
    generatingSource = null
  }
}
