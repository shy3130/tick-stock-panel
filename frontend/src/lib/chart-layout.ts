// 日 K 与单日分时使用同一套垂直尺寸；额外日 K 指标向下扩展。
export const CHART_LAYOUT = {
  toolbar: 40,
  readout: 64,
  timeAxis: 28,
  subPaneSlot: 96,
  separator: 1,
} as const

export function dailyChartHeight(baseHeight: number, subPaneCount: number) {
  return baseHeight + subPaneCount * CHART_LAYOUT.subPaneSlot
}

export function plotPaneHeights(plotHeight: number, subPaneCount: number) {
  return [
    Math.max(120, plotHeight - CHART_LAYOUT.timeAxis - subPaneCount * CHART_LAYOUT.subPaneSlot),
    ...Array.from({ length: subPaneCount }, () => CHART_LAYOUT.subPaneSlot - CHART_LAYOUT.separator),
  ]
}
