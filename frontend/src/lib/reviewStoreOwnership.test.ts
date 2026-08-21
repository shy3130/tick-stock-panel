/**
 * reviewStore 旧流所有权守卫测试(独立 review P1 回归)。
 *
 * 场景: 取消复盘后立即再点生成 —— 旧流的 abort 会异步晚到(网络栈传播),
 * 此时新一轮已接管全局 state/abortCtrl。旧流 catch/finally 必须以
 * `abortCtrl === ac` 为所有权守卫, 不得把新一轮改写回 cancelled,
 * 也不得清掉新一轮的 generatingSource('manual' 优先, SSE 不得借机写入)。
 *
 * 时序全部用受控门(ParkGate)驱动, 不用真实定时器:
 * 旧流的 AbortError 何时落进 catch 由测试显式 reject 决定。
 * monkey-patch 在 finally 中恢复, 不污染同进程其他测试文件。
 */
import { api } from './api.ts'
import * as store from './reviewStore.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

/** 微任务泵: 只让事件循环转起来, 不猜测时长 */
async function pumpUntil(cond: () => boolean, what: string): Promise<void> {
  for (let i = 0; i < 5000; i++) {
    if (cond()) return
    await Promise.resolve()
  }
  throw new Error(`等待超时: ${what}`)
}

const origStream = api.reviewStream
const origCancel = api.cancelAgentAttempt

// tsconfig lib=ES2023 无 Promise.withResolvers 类型,此处保留 executor 形式
interface ParkGate {
  promise: Promise<void>
  resolve: () => void
  reject: (reason?: unknown) => void
}

function newParkGate(): ParkGate {
  let resolve!: () => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const parks = new Map<string, ParkGate>()
api.reviewStream = async function* (asOf?: string) {
  const label = asOf === 'A' ? 'A1' : 'B1'
  yield { type: 'attempt', attempt_id: `att_${label}` } as never
  yield { type: 'delta', content: label } as never
  const park = newParkGate()
  parks.set(label, park)
  await park.promise
} as typeof api.reviewStream
api.cancelAgentAttempt = async () => undefined as never

try {
  store.resetReview()

  // ---- 第一轮: 手动生成 A, 已流出 A1 ----
  const pA = store.startReviewGeneration('A', '')
  await pumpUntil(() => store.getReviewState().content === 'A1' && store.getReviewState().phase === 'streaming', 'A 流出首个 delta')
  assert(store.getReviewState().connection === 'open', 'A 首个 delta 后 connection 应 open')

  // ---- 取消 A(其 controller 已 abort), 立即重启 B(竞态窗口) ----
  await store.cancelReviewGeneration()
  assert(store.getReviewState().phase === 'cancelled', '取消后 phase 应 cancelled')

  const pB = store.startReviewGeneration('B', '重新关注')
  await pumpUntil(() => store.getReviewState().content === 'B1' && store.getReviewState().phase === 'streaming', 'B 流出首个 delta')

  // ---- 此刻才让旧流 A 的 AbortError 落进它的 catch(真实场景中异步晚到) ----
  parks.get('A1')!.reject(new DOMException('Aborted', 'AbortError'))
  await pA
  const s = store.getReviewState()
  assert(s.phase === 'streaming', `旧流 abort 不得把新一轮改成 cancelled, 实际 ${s.phase}`)
  assert(s.content === 'B1', `content 应仍是 B 的新内容, 实际 ${JSON.stringify(s.content)}`)
  assert(s.connection === 'open', `connection 应保持 open, 实际 ${s.connection}`)

  // ---- manual 优先: 旧流 finally 不得清 generatingSource, SSE 事件不得借机写入 ----
  store.feedReviewEvent({ type: 'delta', content: 'SSE_LEAK' })
  assert(store.getReviewState().content === 'B1', 'SSE 事件不得写入手动流期间的内容')

  // ---- B 正常完成: 门放行 → for-await 结束 → done ----
  parks.get('B1')!.resolve()
  await pB
  await pumpUntil(() => store.getReviewState().phase === 'done', 'B 完成')
  assert(store.getReviewState().content === 'B1', 'B done 后 content 保持')

  console.log('reviewStore 旧流所有权守卫测试全部通过')
} finally {
  api.reviewStream = origStream
  api.cancelAgentAttempt = origCancel
}
