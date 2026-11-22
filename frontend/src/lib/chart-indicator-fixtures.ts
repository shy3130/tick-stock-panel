// Legacy saved definitions for compatibility tests; never imported by production UI.
import type { ChartIndicatorDefinition, IndicatorParameter, IndicatorOutput } from './custom-chart-indicators'

const identifier = () => `ci_${globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`}`
const parameter = (name: string, value: number, min = 2, max = 512, integer = true): IndicatorParameter => ({ name, value, min, max, integer })
const output = (key: string, label: string, formula: string, color = '#3b82f6', style: IndicatorOutput['style'] = 'line'): IndicatorOutput => ({ key, label, formula, color, style })

export function legacyChartIndicator(template = 'ma', presets?: ChartIndicatorDefinition[]): ChartIndicatorDefinition {
  const preset = presets?.find(d => d.id === `preset_${template}`)
  if (preset) return { ...JSON.parse(JSON.stringify(preset)), id: identifier() }
  // Preserve v1 templates while an older host/cache has no preset catalogue.
  if (!['ma', 'boll', 'macd', 'rsi', 'bias', 'blank'].includes(template)) throw new Error('指标模板不可用，请重新加载配置')
  const common = { id: identifier(), precision: 2, reset_daily: false }
  if (template === 'boll') return { ...common, name: '布林带', pane: 'main', format: 'price',
    parameters: [parameter('N', 20), parameter('K', 2, 0.1, 10, false)], outputs: [
      output('MID', '中轨', 'MA(C,N)', '#f59e0b'), output('UPPER', '上轨', 'MID+K*STD(C,N)', '#a855f7'), output('LOWER', '下轨', 'MID-K*STD(C,N)', '#06b6d4'),
    ] }
  if (template === 'macd') return { ...common, precision: 3, name: '自定义 MACD', pane: 'sub', format: 'number',
    parameters: [parameter('SHORT', 12), parameter('LONG', 26), parameter('SIGNAL', 9)], outputs: [
      output('DIF', 'DIF', 'EMA(C,SHORT)-EMA(C,LONG)', '#f59e0b'), output('DEA', 'DEA', 'EMA(DIF,SIGNAL)', '#a855f7'),
      output('HIST', 'MACD', '(DIF-DEA)*2', '#ef4444', 'histogram'),
    ] }
  if (template === 'rsi') return { ...common, name: '自定义 RSI', pane: 'sub', format: 'number', parameters: [parameter('N', 14)],
    outputs: [output('RSI', 'RSI', 'SMA(MAX(C-REF(C,1),0),N,1)/SMA(ABS(C-REF(C,1)),N,1)*100', '#a855f7')] }
  if (template === 'bias') return { ...common, name: '乖离率', pane: 'sub', format: 'percent', parameters: [parameter('N', 20)],
    outputs: [output('BIAS', 'BIAS', '(C/MA(C,N)-1)*100', '#f59e0b')] }
  return { ...common, name: template === 'blank' ? '新指标' : '自定义均线', pane: template === 'blank' ? 'sub' : 'main',
    format: template === 'blank' ? 'number' : 'price', parameters: [parameter('N', 20)], outputs: [output('LINE1', '均线', 'MA(C,N)')] }
}
