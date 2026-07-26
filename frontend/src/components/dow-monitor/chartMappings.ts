import type {
  ChartMarker,
  ChartPriceLine,
  HeadShouldersOverlay,
  HeadShouldersOverlayPoint,
  OHLC,
} from '@/components/EChartsCandlestick'

const SUPPORT_BLUE = '#3B82F6'
const RESISTANCE_MAGENTA = '#D946EF'
const LONG_TERM_AMBER = '#F59E0B'
const BUY_RED = '#EF4444'
const SELL_GREEN = '#22C55E'
const FALSE_BREAK_AMBER = '#F59E0B'
const HEAD_SHOULDERS_NEUTRAL = '#94A3B8'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && Number.isFinite(Date.parse(value))
}

function signalConclusion(side: string) {
  if (side === 'BUY') {
    return '\u4e70\u70b9\uff1a\u7a81\u7834\u4e0b\u964d\u8d8b\u52bf\u7ebf + \u7a81\u7834\u524d\u9ad8/\u538b\u529b\u4f4d'
  }
  return '\u5356\u70b9\uff1a\u8dcc\u7834\u4e0a\u6da8\u8d8b\u52bf\u7ebf + \u8dcc\u7834\u524d\u4f4e/\u652f\u6491\u4f4d'
}

function signalPinLabel(side: string) {
  if (side === 'BUY') return 'B'
  if (side === 'RISK') return 'R'
  return 'S'
}

function escapeHtml(value: unknown) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function formatPatternTime(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function formatPatternPoint(date: string, price: number) {
  return `${formatPatternTime(date)} / ${price.toFixed(3)}`
}

function headShouldersStageLabel(stage: string) {
  const labels: Record<string, string> = {
    FORMING: '形态形成中',
    BREAK_WATCH: '等待颈线突破',
    WICK_CROSS: '影线试探颈线',
    NECKLINE_BREAK_WEAK: '颈线弱突破',
    CONFIRMED: '已确认',
    RETEST_CONFIRMED: '回踩确认',
    FAILED: '形态失效',
    FALSE_BREAKOUT: '假突破',
  }
  return labels[stage] ?? '待确认'
}

function headShouldersEvidenceLabel(code: string) {
  const labels: Record<string, string> = {
    BREAK_WATCH: '颈线收盘突破，等待量能确认',
    WICK_CROSS: '影线试探颈线，尚未有效突破',
    NECKLINE_BREAK_WEAK: '颈线突破但量能不足',
    CONFIRMED: '颈线突破已确认',
    RETEST_CONFIRMED: '突破后回踩颈线确认有效',
    FALSE_BREAKOUT: '突破后快速回到颈线内',
  }
  return labels[code] ?? null
}

function patternPoint(
  value: unknown,
  role: HeadShouldersOverlayPoint['role'],
  barTimes: Set<string>,
): HeadShouldersOverlayPoint | null {
  if (
    !isRecord(value)
    || !isTimestamp(value.barTime)
    || !barTimes.has(value.barTime)
    || !isFiniteNumber(value.price)
  ) {
    return null
  }
  return { role, date: value.barTime, price: value.price }
}

function headShouldersTooltip(
  pattern: Record<string, unknown>,
  points: HeadShouldersOverlayPoint[],
  neckline: HeadShouldersOverlay['neckline'],
) {
  const pointByRole = new Map(points.map(point => [point.role, point]))
  const leftShoulder = pointByRole.get('A')!
  const head = pointByRole.get('B')!
  const rightShoulder = pointByRole.get('C')!
  const breakout = pointByRole.get('D')!
  const type = pattern.type === 'BOTTOM' ? '头肩底' : '头肩顶'
  const stage = typeof pattern.stage === 'string' ? pattern.stage : ''
  const volume = isRecord(pattern.volume) ? pattern.volume : null
  const invalidation = isRecord(pattern.invalidation) ? pattern.invalidation : null
  const evidence = Array.isArray(pattern.evidence)
    ? pattern.evidence
        .filter((item): item is string => typeof item === 'string')
        .map(headShouldersEvidenceLabel)
        .filter((item): item is string => item !== null)
    : []
  const rows = [
    ['左肩', formatPatternPoint(leftShoulder.date, leftShoulder.price)],
    ['头部', formatPatternPoint(head.date, head.price)],
    ['右肩', formatPatternPoint(rightShoulder.date, rightShoulder.price)],
    ['颈线锚点一', formatPatternPoint(neckline.start, neckline.startValue)],
    ['颈线锚点二', formatPatternPoint(neckline.anchor2, neckline.anchor2Value)],
    ['触发时颈线', formatPatternPoint(neckline.end, neckline.endValue)],
    ['突破点', formatPatternPoint(breakout.date, breakout.price)],
    ['突破量比', isFiniteNumber(volume?.ratio) ? `${volume.ratio.toFixed(2)}x` : '-'],
    ['确认阶段', headShouldersStageLabel(stage)],
    ['失效价', isFiniteNumber(invalidation?.price) ? invalidation.price.toFixed(3) : '-'],
    ['结构评分', isFiniteNumber(pattern.geometryScore) ? pattern.geometryScore.toFixed(1) : '-'],
    ['量能评分', isFiniteNumber(pattern.volumeScore) ? pattern.volumeScore.toFixed(1) : '-'],
    ['背景评分', isFiniteNumber(pattern.contextScore) ? pattern.contextScore.toFixed(1) : '-'],
    ['综合评分', isFiniteNumber(pattern.qualityScore) ? pattern.qualityScore.toFixed(1) : '-'],
    ['关键证据', evidence.length > 0 ? evidence.join('；') : '暂无补充证据'],
  ]
  const title = stage === 'FALSE_BREAKOUT'
    ? `${type}假突破警示`
    : `${type}${headShouldersStageLabel(stage)}`
  return [
    '<div style="min-width:280px;background:#111217;padding:10px 12px;color:#E5E7EB;font-size:11px;line-height:1.55">',
    `<div style="font-weight:650;margin-bottom:6px">${escapeHtml(title)}</div>`,
    ...rows.map(([label, value]) => (
      `<div><span style="color:#9CA3AF">${escapeHtml(label)}：</span>${escapeHtml(value)}</div>`
    )),
    '</div>',
  ].join('')
}

export function toHeadShouldersOverlays(
  payload: unknown,
  bars: unknown,
): HeadShouldersOverlay[] {
  if (!isRecord(payload) || !Array.isArray(payload.patterns)) return []
  const barTimes = new Set(toChartBars(bars).map(bar => bar.date))
  const signals = Array.isArray(payload.signals)
    ? payload.signals.filter(isRecord)
    : []

  return payload.patterns.flatMap(pattern => {
    if (
      !isRecord(pattern)
      || typeof pattern.id !== 'string'
      || (pattern.type !== 'BOTTOM' && pattern.type !== 'TOP')
      || typeof pattern.stage !== 'string'
      || !isRecord(pattern.points)
      || !isRecord(pattern.neckline)
    ) {
      return []
    }
    const points = [
      patternPoint(pattern.points.leftShoulder, 'A', barTimes),
      patternPoint(pattern.points.neckline1, 'N1', barTimes),
      patternPoint(pattern.points.head, 'B', barTimes),
      patternPoint(pattern.points.neckline2, 'N2', barTimes),
      patternPoint(pattern.points.rightShoulder, 'C', barTimes),
      patternPoint(pattern.points.breakout, 'D', barTimes),
    ]
    if (points.some(point => point === null)) return []
    const completePoints = points as HeadShouldersOverlayPoint[]
    const anchorTimes = pattern.neckline.anchorTimes
    const anchorPrices = pattern.neckline.anchorPrices
    if (
      !Array.isArray(anchorTimes)
      || anchorTimes.length !== 2
      || !isTimestamp(anchorTimes[0])
      || !isTimestamp(anchorTimes[1])
      || !barTimes.has(anchorTimes[0])
      || !barTimes.has(anchorTimes[1])
      || !Array.isArray(anchorPrices)
      || anchorPrices.length !== 2
      || !isFiniteNumber(anchorPrices[0])
      || !isFiniteNumber(anchorPrices[1])
      || !isTimestamp(pattern.neckline.triggerTime)
      || !barTimes.has(pattern.neckline.triggerTime)
      || !isFiniteNumber(pattern.neckline.triggerValue)
    ) {
      return []
    }
    const neckline = {
      start: anchorTimes[0],
      anchor2: anchorTimes[1],
      end: pattern.neckline.triggerTime,
      startValue: anchorPrices[0],
      anchor2Value: anchorPrices[1],
      endValue: pattern.neckline.triggerValue,
    }
    const confirmedStage = pattern.stage === 'CONFIRMED'
      || pattern.stage === 'RETEST_CONFIRMED'
    const expectedSide = pattern.type === 'BOTTOM' ? 'BUY' : 'SELL'
    const formalSignal = confirmedStage
      ? signals.find(signal => (
          signal.family === 'HEAD_SHOULDERS'
          && signal.patternId === pattern.id
          && signal.side === expectedSide
          && signal.stage === pattern.stage
          && isTimestamp(signal.barTime)
          && signal.barTime === completePoints[5].date
          && isFiniteNumber(signal.price)
        ))
      : undefined
    const warning = pattern.stage === 'FALSE_BREAKOUT'
    const color = warning
      ? FALSE_BREAK_AMBER
      : formalSignal
        ? expectedSide === 'BUY' ? BUY_RED : SELL_GREEN
        : HEAD_SHOULDERS_NEUTRAL

    return [{
      id: pattern.id,
      type: pattern.type,
      stage: pattern.stage,
      color,
      warning,
      points: completePoints,
      neckline,
      marker: formalSignal
        ? {
            kind: expectedSide === 'BUY' ? 'buy' as const : 'sell' as const,
            date: formalSignal.barTime as string,
            price: formalSignal.price as number,
            color,
            label: expectedSide === 'BUY' ? 'B' as const : 'S' as const,
          }
        : undefined,
      tooltipHtml: headShouldersTooltip(pattern, completePoints, neckline),
    }]
  })
}

function falseBreakTitle(side: string) {
  return side === 'BUY'
    ? '\u5047\u7a81\u7834\uff08\u539f\u4e70\u70b9\uff09'
    : '\u5047\u7a81\u7834\uff08\u539f\u5356\u70b9\uff09'
}

function falseBreakConclusion(side: string) {
  return side === 'BUY'
    ? '\u5047\u7a81\u7834\uff1a\u4e70\u5165\u4fe1\u53f7\u540e\u8dcc\u56de\u8d8b\u52bf\u7ebf\u6216\u524d\u9ad8/\u538b\u529b\u4f4d'
    : '\u5047\u7a81\u7834\uff1a\u5356\u51fa\u4fe1\u53f7\u540e\u6536\u56de\u8d8b\u52bf\u7ebf\u6216\u524d\u4f4e/\u652f\u6491\u4f4d'
}

function evidenceText(signal: Record<string, unknown>) {
  const evidence = signal.evidence
  if (!Array.isArray(evidence)) return null
  const parts = evidence.flatMap(item => {
    if (!isRecord(item)) return []
    const code = typeof item.code === 'string' ? item.code : ''
    const strength = typeof item.strength === 'string' ? item.strength : ''
    const detector = typeof item.detector === 'string' ? item.detector : ''
    const text = [code, strength, detector].filter(Boolean).join('/')
    return text ? [text] : []
  })
  return parts.length > 0 ? parts.slice(0, 3).join(' | ') : null
}

function volumeRatioForTime(bars: unknown, time: string) {
  if (!Array.isArray(bars)) return null
  const row = bars.find(item => isRecord(item) && item.timestamp === time)
  if (!isRecord(row)) return null
  if (isFiniteNumber(row.vol_ratio_5d)) return row.vol_ratio_5d
  if (isFiniteNumber(row.volume) && isFiniteNumber(row.vol_ma5) && row.vol_ma5 > 0) {
    return row.volume / row.vol_ma5
  }
  return null
}

function isStrictDoubleBreakSignal(signal: Record<string, unknown>) {
  if (!isFiniteNumber(signal.lineValue) || !isFiniteNumber(signal.structurePivotPrice)) return false
  const reasonCodes = Array.isArray(signal.reasonCodes)
    ? signal.reasonCodes.filter((item): item is string => typeof item === 'string')
    : []
  if (signal.triggerPath === 'PRIMARY_STRUCTURE') {
    return reasonCodes.includes('PRIMARY_LINE_AND_STRUCTURE_BROKEN')
  }
  if (signal.triggerPath === 'DIRECT_STRUCTURE') {
    return reasonCodes.includes('LINE_AND_NEAREST_LEVEL_BROKEN')
      || reasonCodes.includes('LINE_AND_KEY_STRUCTURE_BROKEN')
  }
  if (signal.triggerPath === 'TWO_BAR_RETEST') {
    if (signal.side === 'BUY') {
      return reasonCodes.includes('FIRST_ACCEPTANCE_HIGH_BROKEN')
    }
    if (signal.side === 'SELL' || signal.side === 'RISK') {
      return reasonCodes.includes('FIRST_ACCEPTANCE_LOW_BROKEN')
    }
  }
  return false
}

function toTurningMarkers(signals: unknown, bars: unknown): ChartMarker[] {
  if (!Array.isArray(signals)) return []
  return signals.flatMap(signal => {
    if (
      !isRecord(signal)
      || (signal.side !== 'BUY' && signal.side !== 'SELL' && signal.side !== 'RISK')
      || (signal.stage !== 'TRIGGER' && signal.stage !== 'CONFIRMED')
      || !isStrictDoubleBreakSignal(signal)
      || !isTimestamp(signal.actionableTime)
      || !isFiniteNumber(signal.price)
    ) {
      return []
    }
    const buy = signal.side === 'BUY'
    const quality = isRecord(signal.signalQuality) ? signal.signalQuality : null
    const falseBreak = quality?.replayOutcome === 'FAILED'
    const title = buy ? '\u4e70\u70b9' : signal.side === 'RISK' ? '\u98ce\u9669\u5356\u70b9' : '\u5356\u70b9'
    const reasonCodes = Array.isArray(signal.reasonCodes)
      ? signal.reasonCodes.filter((item): item is string => typeof item === 'string')
      : []
    const qualityReasonCodes = Array.isArray(quality?.reasonCodes)
      ? quality.reasonCodes.filter((item): item is string => typeof item === 'string')
      : []
    const lineValue = isFiniteNumber(signal.lineValue) ? signal.lineValue : null
    const structurePivotPrice = isFiniteNumber(signal.structurePivotPrice)
      ? signal.structurePivotPrice
      : null
    return [{
      date: signal.actionableTime,
      kind: falseBreak ? 'neutral' as const : buy ? 'buy' as const : 'sell' as const,
      signalSide: signal.side,
      above: true,
      price: signal.price,
      color: falseBreak ? FALSE_BREAK_AMBER : buy ? BUY_RED : SELL_GREEN,
      label: falseBreak ? 'F' : signalPinLabel(signal.side),
      title: falseBreak ? falseBreakTitle(signal.side) : title,
      conclusion: falseBreak ? falseBreakConclusion(signal.side) : signalConclusion(signal.side),
      reason: typeof signal.triggerPath === 'string' ? signal.triggerPath : undefined,
      confidence: falseBreak
        ? '\u56de\u653e\u786e\u8ba4\u5931\u8d25'
        : typeof signal.stage === 'string' ? signal.stage : undefined,
      lineId: typeof signal.lineId === 'string' ? signal.lineId : null,
      lineRole: typeof signal.lineRole === 'string' ? signal.lineRole : null,
      lineAnchorTimes: Array.isArray(signal.lineAnchorTimes)
        ? signal.lineAnchorTimes.filter((item): item is string => typeof item === 'string')
        : null,
      lineAnchorPrices: Array.isArray(signal.lineAnchorPrices)
        ? signal.lineAnchorPrices.filter((item): item is number => isFiniteNumber(item))
        : null,
      firstCrossTime: typeof signal.detectedTime === 'string' ? signal.detectedTime : null,
      lineValue,
      structurePivotId: typeof signal.structurePivotId === 'string' ? signal.structurePivotId : null,
      structurePivotPrice,
      structurePivotTime: typeof signal.structurePivotTime === 'string' ? signal.structurePivotTime : null,
      triggerPath: typeof signal.triggerPath === 'string' ? signal.triggerPath : null,
      reasonCodes: [...reasonCodes, ...qualityReasonCodes],
      pattern: [...reasonCodes, ...qualityReasonCodes].join(' / ') || null,
      volumeRatio: volumeRatioForTime(bars, signal.actionableTime),
      evidenceText: [...reasonCodes, ...qualityReasonCodes].join(' / ') || null,
    }]
  })
}

export function toChartBars(entries: unknown): OHLC[] {
  if (!Array.isArray(entries)) return []
  return entries.flatMap(entry => {
    if (
      !isRecord(entry)
      || !isTimestamp(entry.timestamp)
      || !isFiniteNumber(entry.open)
      || !isFiniteNumber(entry.high)
      || !isFiniteNumber(entry.low)
      || !isFiniteNumber(entry.close)
      || !isFiniteNumber(entry.volume)
    ) {
      return []
    }
    const mapped: OHLC = {
      date: entry.timestamp,
      open: entry.open,
      high: entry.high,
      low: entry.low,
      close: entry.close,
      volume: entry.volume,
    }
    for (const key of [
      'ma5',
      'ma10',
      'ma20',
      'ma60',
      'macd_dif',
      'macd_dea',
      'macd_hist',
      'rsi_6',
      'rsi_14',
      'rsi_24',
      'kdj_k',
      'kdj_d',
      'kdj_j',
      'boll_upper',
      'boll_lower',
      'vol_ma5',
      'vol_ma10',
      'vol_ratio_5d',
    ] as const) {
      const value = entry[key]
      if (value == null || isFiniteNumber(value)) mapped[key] = value
    }
    return [mapped]
  })
}

export function toChartMarkers(signals: unknown, bars?: unknown, fallbackSignals?: unknown): ChartMarker[] {
  const turningMarkers = toTurningMarkers(signals, bars)
  if (turningMarkers.length > 0) return turningMarkers
  if (fallbackSignals !== undefined) return toChartMarkers(fallbackSignals, bars)
  if (!Array.isArray(signals)) return []
  return signals.flatMap(signal => {
    if (
      !isRecord(signal)
      || (signal.side !== 'BUY' && signal.side !== 'SELL' && signal.side !== 'RISK')
      || !isTimestamp(signal.barTime)
      || !isFiniteNumber(signal.price)
    ) {
      return []
    }
    const buy = signal.side === 'BUY'
    const title = buy ? '\u4e70\u70b9' : signal.side === 'RISK' ? '\u98ce\u9669\u5356\u70b9' : '\u5356\u70b9'
    return [{
      date: signal.barTime,
      kind: buy ? 'buy' as const : 'sell' as const,
      signalSide: signal.side,
      above: true,
      price: signal.price,
      color: buy ? BUY_RED : SELL_GREEN,
      label: signalPinLabel(signal.side),
      title,
      conclusion: signalConclusion(signal.side),
      reason: typeof signal.reason === 'string' ? signal.reason : undefined,
      confidence: typeof signal.confidence === 'string' ? signal.confidence : undefined,
      lineId: typeof signal.lineId === 'string' ? signal.lineId : null,
      firstCrossTime: typeof signal.firstCrossTime === 'string' ? signal.firstCrossTime : null,
      pattern: typeof signal.pattern === 'string' ? signal.pattern : null,
      volumeRatio: isFiniteNumber(signal.volumeRatio) ? signal.volumeRatio : null,
      evidenceText: evidenceText(signal),
    }]
  })
}

function mapEngineLine(line: unknown, barTimes: Set<string>): ChartPriceLine | null {
  if (
    !isRecord(line)
    || typeof line.id !== 'string'
    || line.id.length === 0
    || (line.side !== 'SUPPORT' && line.side !== 'RESISTANCE')
    || (line.role !== 'MAIN' && line.role !== 'ACCELERATION')
    || !Array.isArray(line.anchorTimes)
    || line.anchorTimes.length !== 2
    || !isTimestamp(line.anchorTimes[0])
    || !isTimestamp(line.anchorTimes[1])
    || !barTimes.has(line.anchorTimes[0])
    || !barTimes.has(line.anchorTimes[1])
    || !Array.isArray(line.anchorPrices)
    || line.anchorPrices.length !== 2
    || !isFiniteNumber(line.anchorPrices[0])
    || !isFiniteNumber(line.anchorPrices[1])
  ) {
    return null
  }
  const support = line.side === 'SUPPORT'
  const acceleration = line.role === 'ACCELERATION'
  return {
    id: line.id,
    start: line.anchorTimes[0],
    end: line.anchorTimes[1],
    value: line.anchorPrices[0],
    endValue: line.anchorPrices[1],
    label: `${acceleration ? '\u52a0\u901f' : '\u4e3b'}${support ? '\u652f\u6491' : '\u538b\u529b'}`,
    color: support ? SUPPORT_BLUE : RESISTANCE_MAGENTA,
    lineType: acceleration ? 'dashed' : 'solid',
    width: acceleration ? 1.5 : 2,
  }
}

function mapLongTerm(longTerm: unknown, barTimes: Set<string>): ChartPriceLine | null {
  if (
    !isRecord(longTerm)
    || !isTimestamp(longTerm.first_anchor_time)
    || !isTimestamp(longTerm.second_anchor_time)
    || !barTimes.has(longTerm.first_anchor_time)
    || !barTimes.has(longTerm.second_anchor_time)
    || !isFiniteNumber(longTerm.first_anchor_price)
    || !isFiniteNumber(longTerm.second_anchor_price)
  ) {
    return null
  }
  return {
    id: 'long-term',
    start: longTerm.first_anchor_time,
    end: longTerm.second_anchor_time,
    value: longTerm.first_anchor_price,
    endValue: longTerm.second_anchor_price,
    label: '\u957f\u671f\u8d8b\u52bf',
    color: LONG_TERM_AMBER,
    lineType: 'solid',
    width: 2,
  }
}

export function toPriceLines(
  lines: unknown,
  bars: unknown,
  longTerm?: unknown,
): ChartPriceLine[] {
  const validBars = toChartBars(bars)
  const barTimes = new Set(validBars.map(bar => bar.date))
  const mapped = Array.isArray(lines)
    ? lines.flatMap(line => {
      const result = mapEngineLine(line, barTimes)
      return result ? [result] : []
    })
    : []
  const longTermLine = mapLongTerm(longTerm, barTimes)
  if (longTermLine) mapped.push(longTermLine)
  return mapped
}

export function toSignalPriceLines(markers: ChartMarker[]): ChartPriceLine[] {
  const seen = new Set<string>()
  return markers.flatMap((marker, index) => {
    const lines: ChartPriceLine[] = []
    const anchorTimes = marker.lineAnchorTimes
    const anchorPrices = marker.lineAnchorPrices
    const triggerTime = Date.parse(marker.date)
    if (!Number.isFinite(triggerTime)) return []

    if (
      Array.isArray(anchorTimes)
      && anchorTimes.length >= 2
      && isTimestamp(anchorTimes[0])
      && isTimestamp(anchorTimes[1])
      && Array.isArray(anchorPrices)
      && anchorPrices.length >= 2
      && isFiniteNumber(anchorPrices[0])
      && isFiniteNumber(anchorPrices[1])
      && isTimestamp(marker.date)
      && isFiniteNumber(marker.lineValue ?? null)
    ) {
      const secondAnchorTime = Date.parse(anchorTimes[1])
      if (Number.isFinite(secondAnchorTime) && secondAnchorTime <= triggerTime) {
        const key = [
          'trend',
          marker.kind,
          marker.date,
          anchorTimes[0],
          anchorTimes[1],
          anchorPrices[0],
          anchorPrices[1],
          marker.lineValue,
        ].join('|')
        if (!seen.has(key)) {
          seen.add(key)
          const buy = marker.kind === 'buy'
          const acceleration = marker.lineRole === 'ACCELERATION'
          lines.push({
            id: `signal-line-${index}-${marker.date}`,
            start: anchorTimes[0],
            end: marker.date,
            value: anchorPrices[0],
            endValue: marker.lineValue ?? undefined,
            label: buy ? '\u4e70\u70b9\u8d8b\u52bf\u7ebf' : '\u5356\u70b9\u8d8b\u52bf\u7ebf',
            color: buy ? 'rgba(239, 68, 68, 0.82)' : 'rgba(34, 197, 94, 0.82)',
            lineType: acceleration ? 'dashed' as const : 'solid' as const,
            width: acceleration ? 1.5 : 2,
          })
        }
      }
    }

    if (
      isTimestamp(marker.structurePivotTime ?? null)
      && isFiniteNumber(marker.structurePivotPrice ?? null)
      && Date.parse(marker.structurePivotTime ?? '') <= triggerTime
    ) {
      const levelKey = [
        'level',
        marker.kind,
        marker.date,
        marker.structurePivotTime,
        marker.structurePivotPrice,
      ].join('|')
      if (!seen.has(levelKey)) {
        seen.add(levelKey)
        const buy = marker.kind === 'buy'
        lines.push({
          id: `signal-level-${index}-${marker.date}`,
          start: marker.structurePivotTime ?? undefined,
          end: marker.date,
          value: marker.structurePivotPrice ?? 0,
          endValue: marker.structurePivotPrice ?? undefined,
          label: buy ? '\u524d\u9ad8/\u538b\u529b\u4f4d' : '\u524d\u4f4e/\u652f\u6491\u4f4d',
          color: buy ? 'rgba(245, 158, 11, 0.9)' : 'rgba(245, 158, 11, 0.9)',
          lineType: 'dotted',
          width: 1.5,
        })
      }
    }

    return lines
  })
}
