import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'

/**
 * ECharts 实例管理 Hook — 自动初始化/resize/销毁。
 * 返回 ref 绑定到容器 div，和 setOption 方法。
 */
export function useECharts(
  option: EChartsOption | null,
  deps: any[] = [],
) {
  const chartRef = useRef<HTMLDivElement>(null)
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
