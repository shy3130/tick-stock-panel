import { customSeriesDefaultOptions, type CustomData, type CustomSeriesWhitespaceData, type ICustomSeriesPaneView,
  type ICustomSeriesPaneRenderer, type PaneRendererCustomData, type Time } from 'lightweight-charts'

interface VolumeData extends CustomData<Time> { value: number; color?: string }

// Matches Lightweight Charts 5.2.1's candle renderer, including device-pixel parity.
// Source: lightweight-charts (Apache-2.0); license and NOTICE live in public/third-party.
export function candleBodyWidth(spacing: number, ratio: number) {
  const coefficient = 1 - 0.2 * Math.atan(Math.max(4, spacing) - 4) / (Math.PI * 0.5)
  let width = spacing >= 2.5 && spacing <= 4 ? Math.floor(3 * ratio)
    : Math.max(Math.floor(ratio), Math.min(Math.floor(spacing * coefficient * ratio), Math.floor(spacing * ratio)))
  if (width >= 2 && width % 2 !== Math.floor(ratio) % 2) width--
  return width
}

/** 日 K 量柱共用原生蜡烛的像素宽度；分时和 MACD 继续用原生 Histogram。 */
export class DailyVolumeSeries implements ICustomSeriesPaneView<Time, VolumeData> {
  private data?: PaneRendererCustomData<Time, VolumeData>
  private view: ICustomSeriesPaneRenderer = { draw: (target, priceToCoordinate) => {
    const data = this.data
    if (!data?.visibleRange) return
    const zero = priceToCoordinate(0)
    if (zero == null) return
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hx, verticalPixelRatio: vy }) => {
      const width = candleBodyWidth(data.barSpacing, hx)
      const bottom = Math.round(zero * vy)
      for (let i = data.visibleRange!.from; i < data.visibleRange!.to; i++) {
        const bar = data.bars[i], y = priceToCoordinate(bar.originalData.value)
        if (y == null || bar.originalData.value <= 0) continue
        const left = Math.round(bar.x * hx) - Math.floor(width / 2)
        const top = Math.round(y * vy)
        ctx.fillStyle = bar.originalData.color ?? bar.barColor
        ctx.fillRect(left, top, width, Math.max(1, bottom - top))
      }
    })
  } }
  renderer() { return this.view }
  update(data: PaneRendererCustomData<Time, VolumeData>) { this.data = data }
  priceValueBuilder(row: VolumeData) { return [0, row.value] }
  isWhitespace(row: VolumeData | CustomSeriesWhitespaceData<Time>): row is CustomSeriesWhitespaceData<Time> {
    return !('value' in row) || !Number.isFinite(row.value) || row.value < 0
  }
  defaultOptions() { return customSeriesDefaultOptions }
  destroy() { this.data = undefined }
}
