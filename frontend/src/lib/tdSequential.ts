/**
 * 神奇九转 (TD Sequential Setup) 公用算法。
 *
 * 逐 bar 比较 close[i] 与 close[i-4]：连续走高 upCount++，连续走低 downCount++，
 * 打平 (close === close[i-4]) 视为重置。前 4 根数据不足比较基准，恒为 neutral。
 * 每一根处于计数中的 bar 都会产出一个 marker(count = 当前计数 1~9)——
 * 这是日线"神奇九转"展示 1~9 完整计数过程所需要的全量数据。
 * 计满 9 后重置计数，重新从 0 开始下一轮。
 *
 * 只做 TD Setup 阶段，不做 TD Countdown 阶段。
 */

export type TdState = 'neutral' | 'up' | 'down'

export interface TdMarker {
  /** 相对传入 closes 数组的下标 */
  i: number
  /** 当前计数 1~9 */
  count: number
  /** top = 连续走高(卖出预警), bottom = 连续走低(买入预警) */
  kind: 'top' | 'bottom'
}

export interface TdSequentialResult {
  states: TdState[]
  markers: TdMarker[]
}

export function computeTdSequential(closes: number[]): TdSequentialResult {
  const states: TdState[] = new Array(closes.length).fill('neutral')
  const markers: TdMarker[] = []
  let upCount = 0
  let downCount = 0

  for (let i = 0; i < closes.length; i++) {
    if (i < 4) {
      states[i] = 'neutral'
      upCount = 0
      downCount = 0
      continue
    }

    if (closes[i] > closes[i - 4]) {
      upCount += 1
      downCount = 0
    } else if (closes[i] < closes[i - 4]) {
      downCount += 1
      upCount = 0
    } else {
      upCount = 0
      downCount = 0
    }

    if (upCount > 0) {
      states[i] = 'up'
    } else if (downCount > 0) {
      states[i] = 'down'
    } else {
      states[i] = 'neutral'
    }

    if (upCount >= 1) {
      markers.push({ i, count: upCount, kind: 'top' })
    } else if (downCount >= 1) {
      markers.push({ i, count: downCount, kind: 'bottom' })
    }

    if (upCount === 9) upCount = 0
    if (downCount === 9) downCount = 0
  }

  return { states, markers }
}
