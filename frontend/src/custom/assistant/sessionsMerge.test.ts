import { describe, expect, it } from 'vitest'
import { mergeSessions, type MergeableSession } from './sessionsMerge'

function mk(id: string, createdAt: number, nMsgs = 1): MergeableSession {
  return { id, createdAt, messages: Array.from({ length: nMsgs }, (_, i) => ({ i })) }
}

describe('mergeSessions', () => {
  it('不相交集合合并并按 createdAt 倒序截断', () => {
    const local = [mk('a', 100), mk('b', 200)]
    const remote = [mk('c', 300), mk('d', 50)]
    const out = mergeSessions(local, 'a', remote, 'c', 20)
    expect(out.sessions.map(s => s.id)).toEqual(['c', 'b', 'a', 'd'])
    expect(out.activeId).toBe('a') // 本地有效值优先
  })

  it('同 id 取消息更多的一份; 平局本地胜出 (引用相等)', () => {
    const localA = mk('a', 100, 3)
    const remoteA = mk('a', 100, 5)
    const localB = mk('b', 200, 4)
    const remoteB = mk('b', 200, 4)
    const out = mergeSessions([localA, localB], 'a', [remoteA, remoteB], '', 20)
    expect(out.sessions.find(s => s.id === 'a')).toBe(remoteA) // 远端更长
    expect(out.sessions.find(s => s.id === 'b')).toBe(localB) // 平局本地
  })

  it('activeId 本地无效时取远端, 都无效取第一个', () => {
    const local = [mk('a', 100)]
    const remote = [mk('c', 300), mk('a', 100)]
    expect(mergeSessions(local, 'gone', remote, 'c', 20).activeId).toBe('c')
    expect(mergeSessions(local, 'gone', remote, 'gone', 20).activeId).toBe('c') // createdAt 最新
  })

  it('超过上限裁最旧; 空远端返回本地原引用', () => {
    const local = [mk('a', 300), mk('b', 200), mk('c', 100)]
    const out = mergeSessions(local, 'a', [], '', 2)
    expect(out.sessions.map(s => s.id)).toEqual(['a', 'b'])
    expect(out.sessions[0]).toBe(local[0]) // 未参与的引用保持稳定 (调用方据此跳过无谓刷新)
  })
})
