import { describe, expect, it } from 'vitest'
import { setKlineBatchLimits, getDailyBatchChunkSize, getMinuteBatchChunkSize, resetKlineBatchLimits } from '@/lib/klineBatchLimits'

describe('klineBatchLimits', () => {
  it('默认 400/400 (与后端 fallback 一致)', () => {
    resetKlineBatchLimits()
    expect(getDailyBatchChunkSize()).toBe(400)
    expect(getMinuteBatchChunkSize()).toBe(400)
  })
  it('灌入偏好后立即生效', () => {
    setKlineBatchLimits({ daily: 1500, minute: 120 })
    expect(getDailyBatchChunkSize()).toBe(1500)
    expect(getMinuteBatchChunkSize()).toBe(120)
  })
  it('越界自动收敛, 不会发出必然被拒的请求', () => {
    setKlineBatchLimits({ daily: 99999, minute: 1 })
    expect(getDailyBatchChunkSize()).toBe(5000)
    expect(getMinuteBatchChunkSize()).toBe(50)
  })
  it('缺省字段不覆盖当前值 (偏好未返回时不污染)', () => {
    setKlineBatchLimits({ daily: 800 })
    setKlineBatchLimits({ minute: 200 })
    expect(getDailyBatchChunkSize()).toBe(800)
    expect(getMinuteBatchChunkSize()).toBe(200)
  })
})
