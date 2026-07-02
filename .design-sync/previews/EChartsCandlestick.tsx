import { EChartsCandlestick, type OHLC } from 'tickflow-stock-panel-frontend'

// Deterministic synthetic A-share daily bars — no live data source needed.
function genData(days: number): OHLC[] {
  let price = 38
  const out: OHLC[] = []
  for (let i = 0; i < days; i++) {
    const drift = Math.sin(i / 6) * 0.6 + (i % 5 === 0 ? 0.8 : -0.15)
    const open = price
    const close = +(open + drift).toFixed(2)
    const high = +(Math.max(open, close) + 0.4).toFixed(2)
    const low = +(Math.min(open, close) - 0.4).toFixed(2)
    price = close
    const d = new Date(2024, 0, 1 + i)
    out.push({
      date: d.toISOString().slice(0, 10),
      open, high, low, close,
      volume: 8_000_000 + Math.round(Math.sin(i / 3) * 3_000_000),
      ma5: i >= 4 ? +(price - 0.3).toFixed(2) : null,
      ma10: i >= 9 ? +(price - 0.6).toFixed(2) : null,
      ma20: i >= 19 ? +(price - 1.1).toFixed(2) : null,
    })
  }
  return out
}

const data = genData(90)

export function Default() {
  return <EChartsCandlestick data={data} height={420} />
}

export function WithMarkersAndRange() {
  return (
    <EChartsCandlestick
      data={data}
      height={420}
      markers={[
        { date: data[40].date, kind: 'buy', label: '买入' },
        { date: data[70].date, kind: 'sell', label: '卖出' },
      ]}
      ranges={[{ start: data[55].date, end: data[65].date, label: '强势区间', color: '#3B82F6' }]}
      priceLines={[{ value: data[data.length - 1].close, label: '现价' }]}
    />
  )
}
