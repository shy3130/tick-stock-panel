/**
 * F9/F14: stockAnalysisStore 流式守卫测试。
 *
 * 直接替换 api 对象方法(store 持有同一引用),驱动真实 runStream:
 *  - 完整流: meta(data_as_of/source/adjustment/degraded/warnings)落入 dataMeta;
 *    connection 生命周期 connecting → open → closed
 *  - cancelled 终态: 取消后迟到 chunk/保存路径不得把 phase/content/connection 改回去
 *  - error 终态: 已断开 + connection closed
 *
 * monkey-patch 在 finally 中恢复, 不污染同进程其他测试文件。
 */
import { aiStreamStatus } from './aiStreamStatus.ts'
import { api } from './api.ts'
import * as store from './stockAnalysisStore.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

type Chunk = Record<string, unknown>

let queue: Chunk[] = []

/** 轮询直到任务进入终态(事件循环驱动,不猜测固定时长) */
async function waitForTerminal(id?: string): Promise<void> {
  for (let i = 0; i < 500; i++) {
    const t = store.getActiveTasks().find(x => x.id === id)
    if (t && (t.phase === 'done' || t.phase === 'error' || t.phase === 'cancelled')) return
    await new Promise(r => setTimeout(r, 2))
  }
  throw new Error('等待终态超时')
}

function find(id?: string) {
  return store.getActiveTasks().find(t => t.id === id)
}

// ---- 替换 api 方法(store 与本测试共享同一 api 对象引用) ----
const origStream = api.stockAnalyzeStream
const origCancel = api.cancelAgentAttempt
const origSave = api.stockAnalysisReportSave
const origList = api.stockAnalysisReportsList

api.stockAnalyzeStream = async function* (_symbol: string) {
  for (const c of queue) {
    await Promise.resolve()
    yield c as never
  }
} as typeof origStream
api.cancelAgentAttempt = async () => undefined as never
api.stockAnalysisReportSave = async () => ({ ok: true, report: null }) as never
api.stockAnalysisReportsList = async () => ({ reports: [] }) as never

try {
  // ---- 场景 1: 完整流 → done, meta 字段落 dataMeta ----
  {
    queue = [
      { type: 'attempt', attempt_id: 'att_1' },
      {
        type: 'meta', summary: '当前价 8.5', close: 8.5,
        data_as_of: '2026-08-20', source: 'local_duckdb', adjustment: '前复权',
        degraded: true, warnings: ['msg_missing: 无消息面', 'fin_stale: 财报滞后'],
      },
      { type: 'delta', content: '# 标题\n' },
      { type: 'delta', content: '正文' },
      { type: 'done' },
    ]
    const { id } = await store.startAnalysis('600000.SH', '浦发银行', '', undefined)
    await waitForTerminal(id)
    await new Promise(r => setTimeout(r, 5))
    const t = find(id)
    assert(!!t, '任务应存在')
    assert(t!.phase === 'done', `phase 应 done,实际 ${t!.phase}`)
    assert(t!.connection === 'closed', `connection 应 closed,实际 ${t!.connection}`)
    assert(t!.content === '# 标题\n正文', `content 应累积,实际 ${JSON.stringify(t!.content)}`)
    const dm = t!.dataMeta
    assert(dm?.data_as_of === '2026-08-20', `data_as_of 应落库,实际 ${dm?.data_as_of}`)
    assert(dm?.source === 'local_duckdb', `source 应落库,实际 ${dm?.source}`)
    assert(dm?.adjustment === '前复权', `adjustment 应落库,实际 ${dm?.adjustment}`)
    assert(dm?.degraded === true, `degraded 应落库,实际 ${dm?.degraded}`)
    assert(dm?.warnings?.length === 2, `warnings 应 2 条,实际 ${dm?.warnings?.length}`)
  }

  // ---- 场景 2: cancelled 终态不被迟到 chunk 覆盖 ----
  {
    queue = [
      { type: 'meta', data_as_of: '2026-08-20', source: 's', adjustment: 'a' },
      { type: 'delta', content: 'part1 ' },
    ]
    const { id } = await store.startAnalysis('000001.SZ', '平安银行', '', undefined)
    await new Promise(r => setTimeout(r, 5))
    await store.cancelAnalysis(id!)
    const afterCancel = find(id)
    assert(afterCancel!.phase === 'cancelled', `取消后 phase 应 cancelled,实际 ${afterCancel!.phase}`)
    assert(afterCancel!.connection === 'closed', '取消后 connection 应 closed')
    const contentAtCancel = afterCancel!.content
    // 迟到路径: 流收尾的保存逻辑等后台任务不得复活 cancelled(patchTask 终态守卫)
    await new Promise(r => setTimeout(r, 10))
    const late = find(id)
    assert(late!.phase === 'cancelled', `迟到处理后期 phase 应保持 cancelled,实际 ${late!.phase}`)
    assert(late!.connection === 'closed', '迟到处理后 connection 保持 closed')
    assert(late!.content === contentAtCancel, '迟到处理后 content 不变')
    assert(aiStreamStatus({ phase: late!.phase, connection: late!.connection })!.label === '已取消', '状态条应显示 已取消')
  }

  // ---- 场景 3: error 终态 → 已断开 ----
  {
    queue = [
      { type: 'meta', data_as_of: '2026-08-19' },
      { type: 'delta', content: 'x' },
      { type: 'error', message: 'AI 分析失败: 超时' },
    ]
    const { id } = await store.startAnalysis('300750.SZ', '宁德时代', '', undefined)
    await waitForTerminal(id)
    await new Promise(r => setTimeout(r, 5))
    const t = find(id)
    assert(t!.phase === 'error', `phase 应 error,实际 ${t!.phase}`)
    assert(t!.connection === 'closed', 'error 后 connection 应 closed')
    const st = aiStreamStatus({ phase: t!.phase, connection: t!.connection })
    assert(st?.label === '已断开', `状态条应显示 已断开,实际 ${st?.label ?? null}`)
    assert(t!.error.includes('超时'), `error 文案应保留原文,实际 ${t!.error}`)
  }

  console.log('stockAnalysisStore 流式守卫测试全部通过')
} finally {
  api.stockAnalyzeStream = origStream
  api.cancelAgentAttempt = origCancel
  api.stockAnalysisReportSave = origSave
  api.stockAnalysisReportsList = origList
}
