import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'

/**
 * ECharts 实例管理 Hook — 自动初始化/resize/销毁。
 * 返回 ref 绑定到容器 div，和 setOption 方法。
 *
 * 可传入一个外部 ref (containerRef) 让调用方共享同一 DOM 节点,
 * 用于在 setOption 前读取图表状态 (例如保留 dataZoom 缩放窗口)。
 */
export function useECharts(
  option: EChartsOption | null,
  deps: any[] = [],
  containerRef?: React.RefObject<HTMLDivElement>,
) {
  const ownRef = useRef<HTMLDivElement>(null)
  const chartRef = containerRef ?? ownRef
  const instanceRef = useRef<ECharts | null>(null)

  // 初始化 / 销毁
  useEffect(() => {
    if (!chartRef.current) return
    instanceRef.current = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
    const handleResize = () => instanceRef.current?.resize()
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      instanceRef.current?.dispose()
      instanceRef.current = null
    }
  }, [])

  // 更新 option
  useEffect(() => {
    if (!instanceRef.current || !option) return
    instanceRef.current.setOption(option, { notMerge: true })
  }, [option, ...deps])

  return chartRef
}
