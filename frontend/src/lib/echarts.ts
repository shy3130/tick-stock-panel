/**
 * ECharts 统一入口 — 业务代码一律 `import * as echarts from '@/lib/echarts'`,
 * 不要再直接引 'echarts'。
 *
 * 唯一增量: init 出来的实例, setOption 前把画布字号整体 +1 (lib/echartsFont.ts)。
 * 其余能力按需透传; echarts 是 `export =` 模块, 不能用 `export *`, 所以只转发
 * 实际用到的成员 (init / connect / getInstanceByDom) 与类型 (ECharts / EChartsOption),
 * 新增用法时在这里补一行即可。
 *
 * 为什么改实例上的 setOption 而不是包一层 hook: 图表实例在本项目里由 9 处
 * 各自 init (EChartsCandlestick / EChartsIntraday / Regime / 回测图表 hook …),
 * 包 hook 需要逐个改调用点且漏一处就不生效; 改实例方法一次覆盖全部。
 *
 * 注意: 这里不直接缓存 `echarts.init` 的引用, 而是调用时再取 — 测试里
 * `vi.mock('echarts')` 会替换模块导出, 懒取保证 mock 照旧生效。
 */
import * as echarts from 'echarts'
import { bumpChartFonts } from './echartsFont'

export type { ECharts, EChartsOption } from 'echarts'

/* 转发成员一律「用时取值」: vitest 的模块 mock 在访问未定义的导出键时直接抛错,
 * 模块加载期读 echarts.connect 会把只 mock 了 init 的测试整片打挂。 */
export const connect: typeof echarts.connect = ((...args: never[]) =>
  (echarts as unknown as { connect: (...a: never[]) => void }).connect(...args)) as typeof echarts.connect

export const getInstanceByDom: typeof echarts.getInstanceByDom = ((...args: never[]) =>
  (echarts as unknown as { getInstanceByDom: (...a: never[]) => unknown }).getInstanceByDom(...args)) as typeof echarts.getInstanceByDom

type EChartsInit = typeof echarts.init

export const init: EChartsInit = ((...args: Parameters<EChartsInit>) => {
  const instance = (echarts as unknown as { init: EChartsInit }).init(...args)
  const rawSetOption = instance.setOption
  // 测试里 vi.mock('echarts') 给的是 vi.fn 假实例: 不能替换, 否则断言拿不到
  // setOption.mock.calls (且断言的是原始 option, 不应被 +1 干扰)。
  if (typeof rawSetOption !== 'function' || (rawSetOption as { _isMockFunction?: boolean })._isMockFunction) {
    return instance
  }
  const boundSetOption = rawSetOption.bind(instance)
  instance.setOption = ((option: unknown, ...rest: unknown[]) => {
    if (!option) return boundSetOption(option as never, ...(rest as never[]))
    return boundSetOption(bumpChartFonts(option) as never, ...(rest as never[]))
  }) as typeof instance.setOption
  return instance
}) as EChartsInit
