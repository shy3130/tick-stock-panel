import { fmtRet, type ReturnsAnalysis } from '../returnsAnalysis'

const MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']

// 涨跌色 — 与 index.css --bull/--bear (#F04438/#12B76A) 一致; 画布不吃 CSS 变量需硬编码
const BULL_RGB = '240,68,56'
const BEAR_RGB = '18,183,106'

/**
 * 月度收益热力图 — DOM 表格实现（DimensionHeatmap 同款思路），
 * 单元格背景按收益绝对值在 [0, maxAbsMonthly] 上归一着色：正红负绿、0 中性。
 */
export function MonthlyReturnHeatmap({ analysis }: { analysis: ReturnsAnalysis }) {
  const cellBg = (r: number | null): string | undefined => {
    if (r == null || r === 0) return undefined
    const t = Math.min(Math.abs(r) / analysis.maxAbsMonthly, 1)
    // 低收益也给一点底色保持可读，高收益趋向饱和
    const alpha = 0.10 + 0.55 * Math.sqrt(t)
    return r > 0 ? `rgba(${BULL_RGB},${alpha})` : `rgba(${BEAR_RGB},${alpha})`
  }
  const cellText = (r: number | null): string => {
    if (r == null) return 'text-muted/40'
    if (r === 0) return 'text-muted'
    return 'text-foreground'
  }

  const thCls = 'px-1.5 py-1.5 text-center font-bold text-muted whitespace-nowrap'
  const tdCls = 'px-1.5 py-1.5 text-center num whitespace-nowrap rounded-sm'
  const edgeCls = 'font-bold'

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        <span className="text-xs font-medium text-secondary">月度收益（%）</span>
        {analysis.positiveMonthRatio != null && (
          <span>
            正收益月份占比 <span className="num text-foreground">{(analysis.positiveMonthRatio * 100).toFixed(0)}%</span>
          </span>
        )}
        {analysis.worstMonth && (
          <span>
            最差月 <span className="num text-bear">{fmtRet(analysis.worstMonth.ret)}%</span>
            <span className="ml-0.5 text-muted/70">({analysis.worstMonth.year}.{analysis.worstMonth.month})</span>
          </span>
        )}
        {analysis.bestMonth && (
          <span>
            最佳月 <span className="num text-bull">{fmtRet(analysis.bestMonth.ret)}%</span>
            <span className="ml-0.5 text-muted/70">({analysis.bestMonth.year}.{analysis.bestMonth.month})</span>
          </span>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] border-separate border-spacing-1 text-[11px]">
          <thead>
            <tr>
              <th className={`${thCls} text-left`}>年份</th>
              {MONTH_LABELS.map(m => <th key={m} className={thCls}>{m}</th>)}
              <th className={`${thCls} text-secondary`}>全年</th>
            </tr>
          </thead>
          <tbody>
            {analysis.years.map(row => (
              <tr key={row.year}>
                <td className={`${tdCls} ${edgeCls} text-left text-secondary`}>{row.year}</td>
                {row.months.map((r, i) => (
                  <td
                    key={i}
                    className={`${tdCls} ${cellText(r)}`}
                    style={{ backgroundColor: cellBg(r) }}
                    title={r != null ? `${row.year}年${i + 1}月: ${fmtRet(r)}%` : undefined}
                  >
                    {r != null ? fmtRet(r) : ''}
                  </td>
                ))}
                <td
                  className={`${tdCls} ${edgeCls} ${cellText(row.annual)}`}
                  style={{ backgroundColor: cellBg(row.annual) }}
                  title={`${row.year}全年(复利): ${fmtRet(row.annual)}%`}
                >
                  {fmtRet(row.annual)}
                </td>
              </tr>
            ))}
            <tr>
              <td className={`${tdCls} ${edgeCls} text-left text-secondary border-t border-border`}>平均</td>
              {analysis.monthlyAvg.map((r, i) => (
                <td
                  key={i}
                  className={`${tdCls} ${edgeCls} border-t border-border ${cellText(r)}`}
                  style={{ backgroundColor: cellBg(r) }}
                  title={r != null ? `${i + 1}月跨年平均(算术): ${fmtRet(r)}%` : undefined}
                >
                  {r != null ? fmtRet(r) : ''}
                </td>
              ))}
              <td
                className={`${tdCls} ${edgeCls} border-t border-border ${cellText(analysis.avgAnnual)}`}
                style={{ backgroundColor: cellBg(analysis.avgAnnual) }}
                title={analysis.avgAnnual != null ? `平均年度收益(算术): ${fmtRet(analysis.avgAnnual)}%` : undefined}
              >
                {fmtRet(analysis.avgAnnual)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  )
}
