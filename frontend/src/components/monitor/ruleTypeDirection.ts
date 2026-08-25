import type { MonitorRule } from '@/lib/api'

/**
 * 监控类型切换时归一 direction。
 * abnormal 使用 up/down/both；离开 abnormal 后必须清掉 up/down，避免 strategy 保存被后端拒绝。
 */
export function directionAfterTypeChange(
  currentType: MonitorRule['type'],
  nextType: MonitorRule['type'],
  currentDirection: MonitorRule['direction'],
): MonitorRule['direction'] {
  if (nextType === 'abnormal' || currentType === 'abnormal') return 'both'
  return currentDirection
}
