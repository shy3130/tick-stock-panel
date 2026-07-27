import type { DailyBriefing } from './briefing'

function inline(value: string): string {
  return value.replace(/[\r\n]+/g, ' ').replace(/\|/g, '\\|').trim()
}

function money(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}

function percent(value: number | null, ratio = false): string {
  if (value == null || !Number.isFinite(value)) return '--'
  const normalized = ratio ? value * 100 : value
  const display = Math.abs(normalized) < 0.005 ? 0 : normalized
  return `${display > 0 ? '+' : ''}${display.toFixed(2)}%`
}

export function dailyBriefingFilename(briefing: DailyBriefing): string {
  return `sycee-${briefing.mode === 'morning' ? 'morning' : 'evening'}-${briefing.asOf}.md`
}

export function dailyBriefingMarkdown(briefing: DailyBriefing): string {
  const modeLabel = briefing.mode === 'morning' ? '晨报' : '晚报'
  const lines = [
    `# Sycee ${modeLabel} · ${briefing.asOf}`,
    '',
    `生成时间：${new Date(briefing.generatedAt).toLocaleString('zh-CN')}`,
    '',
    '## 今日关注',
    '',
    ...briefing.focus.map(item => `- **${inline(item.title)}**：${inline(item.detail)}`),
    '',
    '## 市场',
    '',
    `- 情绪：${inline(briefing.market.label)}${briefing.market.score == null ? '' : `（${briefing.market.score.toFixed(0)}）`}`,
    `- 涨 / 平 / 跌：${briefing.market.up ?? '--'} / ${briefing.market.flat ?? '--'} / ${briefing.market.down ?? '--'}，上涨率 ${percent(briefing.market.upPct)}`,
    `- 涨停 / 炸板 / 跌停：${briefing.market.limitUp ?? '--'} / ${briefing.market.broken ?? '--'} / ${briefing.market.limitDown ?? '--'}`,
  ]

  if (briefing.market.leaders.length > 0) {
    lines.push(`- 领先方向：${briefing.market.leaders.map(item => `${inline(item.name)}（${item.kind} ${percent(item.avgPct)}）`).join('、')}`)
  }
  if (briefing.market.recapSummary) lines.push(`- 市场复盘：${inline(briefing.market.recapSummary)}`)

  lines.push(
    '',
    '## 持仓',
    '',
    `组合市值 ${money(briefing.portfolio.marketValue)}，浮动盈亏 ${money(briefing.portfolio.unrealizedPnl)}（${percent(briefing.portfolio.floatingReturn, true)}）。`,
    '',
  )
  if (briefing.portfolio.positions.length === 0) {
    lines.push('暂无持仓。')
  } else {
    lines.push('| 标的 | 数量 | 现价 | 当日涨跌 | 浮动盈亏 | 持仓收益 |', '| --- | ---: | ---: | ---: | ---: | ---: |')
    for (const position of briefing.portfolio.positions) {
      lines.push(`| ${inline(position.name)} ${position.symbol} | ${position.quantity} | ${money(position.currentPrice)} | ${percent(position.dailyChangePct)} | ${money(position.unrealizedPnl)} | ${percent(position.returnPct, true)} |`)
    }
  }

  lines.push('', '## 重点事件与证据', '')
  if (briefing.eventGroups.length === 0) {
    lines.push('报告窗口内没有持仓或自选股提醒。')
  } else {
    for (const group of briefing.eventGroups) {
      const direction = group.direction === 'risk' ? '风险' : group.direction === 'opportunity' ? '机会' : '观察'
      lines.push(`### ${inline(group.name)} ${group.symbol} · ${group.scope === 'holding' ? '持仓' : '自选'}${direction} · 优先级 ${group.score}`)
      lines.push('', `权重：${group.reasons.map(reason => `${inline(reason.label)} +${reason.points}`).join('、')}`, '')
      for (const alert of group.evidence) {
        lines.push(`- ${new Date(alert.ts).toLocaleString('zh-CN')} · ${inline(alert.rule_name || alert.source)}：${inline(alert.message)}`)
      }
      lines.push('')
    }
  }

  lines.push('', '## 策略跟踪', '')
  if (briefing.tracks.length === 0) {
    lines.push('暂无策略跟踪计划。')
  } else {
    for (const track of briefing.tracks) {
      lines.push(`- ${inline(track.name)}：${track.pending ? '待更新' : track.status === 'tracking' ? '已对齐' : track.status === 'paused' ? '已暂停' : '已结束'}${track.latest ? `，最新快照 ${track.latest.end_date}，累计收益 ${percent(track.latest.total_return, true)}` : ''}`)
    }
  }

  lines.push('', '## 研究动作', '')
  if (briefing.research.length === 0) {
    lines.push('暂无待整理或跟踪中的研究记录。')
  } else {
    for (const entry of briefing.research) {
      lines.push(`- **${inline(entry.title)}**${entry.subject ? `（${inline(entry.subject)}）` : ''}：${inline(entry.plan) || '尚未填写下一步'}`)
    }
  }
  if (briefing.unavailable.length > 0) {
    lines.push('', `> 本次未能读取：${briefing.unavailable.join('、')}。相关段落可能不完整。`)
  }

  return `${lines.join('\n')}\n`
}
