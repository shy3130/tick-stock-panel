import { Fragment, type ReactNode } from 'react'

/**
 * 轻量 Markdown 渲染器 — 零依赖,专为 AI 财务分析报告设计。
 *
 * 支持的语法(AI 财务分析提示词约束的子集,足够用):
 * - 标题 # ## ### ####
 * - 加粗 **text**
 * - 行内代码 `code`
 * - 无序列表 - / *
 * - 有序列表 1.
 * - 表格 | a | b |
 * - 引用 >
 * - 分隔线 --- / ***
 * - 段落
 *
 * 不追求完整 GFM,只覆盖 AI 报告会产出的结构。
 */

// ===== 行内格式:加粗 / 行内代码 / 星号评级 =====

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = []
  // 正则:匹配 **加粗** 或 `代码` 或 ★ 评级
  const re = /(\*\*([^*]+)\*\*)|(`([^`]+)`)/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(<Fragment key={`${keyBase}-t-${i}`}>{text.slice(last, m.index)}</Fragment>)
    if (m[1]) {
      // 加粗
      nodes.push(<strong key={`${keyBase}-b-${i}`} className="font-semibold text-foreground">{m[2]}</strong>)
    } else if (m[3]) {
      // 行内代码
      nodes.push(
        <code key={`${keyBase}-c-${i}`} className="px-1 py-0.5 rounded bg-elevated text-[0.85em] font-mono text-accent">
          {m[4]}
        </code>,
      )
    }
    last = m.index + m[0].length
    i++
  }
  if (last < text.length) nodes.push(<Fragment key={`${keyBase}-t-end`}>{text.slice(last)}</Fragment>)
  return nodes
}

// ===== 表格解析 =====

function parseTable(lines: string[], start: number): { rows: string[][]; consumed: number } | null {
  // 找到连续的表格行(以 | 开头)
  const tableLines: string[] = []
  let idx = start
  while (idx < lines.length && lines[idx].trim().startsWith('|')) {
    tableLines.push(lines[idx].trim())
    idx++
  }
  if (tableLines.length < 2) return null
  // 第二行必须是分隔行 |---|---|
  if (!/^|[\s-:|]+$/.test(tableLines[1]) && !tableLines[1].split('|').every(c => /^[\s-:]*$/.test(c))) {
    return null
  }
  const parseRow = (line: string) =>
    line.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim())
  const header = parseRow(tableLines[0])
  const body = tableLines.slice(2).map(parseRow)
  return { rows: [header, ...body], consumed: tableLines.length }
}

// ===== 主渲染 =====

export function MarkdownRenderer({ content }: { content: string }) {
  const lines = content.replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    // 空行
    if (!trimmed) {
      i++
      continue
    }

    // 分隔线
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push(<hr key={key++} className="my-6 border-border" />)
      i++
      continue
    }

    // 标题
    const hMatch = trimmed.match(/^(#{1,4})\s+(.+)$/)
    if (hMatch) {
      const level = hMatch[1].length
      const text = hMatch[2]
      const sizeCls = level === 1 ? 'text-base' : level === 2 ? 'text-sm' : 'text-xs'
      const mtCls = level <= 2 ? 'mt-6' : 'mt-5'
      blocks.push(
        <div key={key++} className={`${sizeCls} ${mtCls} mb-3 font-semibold text-foreground flex items-center gap-1.5`}>
          {renderInline(text, `h-${key}`)}
        </div>,
      )
      i++
      continue
    }

    // 引用
    if (trimmed.startsWith('>')) {
      const quoteLines: string[] = []
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''))
        i++
      }
      blocks.push(
        <blockquote key={key++} className="my-4 pl-3 border-l-2 border-amber-400/40 bg-amber-400/[0.04] py-1.5 pr-2 rounded-r text-xs text-secondary">
          {renderInline(quoteLines.join(' '), `q-${key}`)}
        </blockquote>,
      )
      continue
    }

    // 表格
    if (trimmed.startsWith('|')) {
      const table = parseTable(lines, i)
      if (table) {
        const [header, ...body] = table.rows
        const ncol = header.length
        blocks.push(
          <div key={key++} className="my-5 overflow-hidden rounded-btn border border-border/30">
            <table className="w-full text-xs border-collapse table-fixed">
              <colgroup>
                {/* 首列(维度)较窄;末列(判断/说明)最宽并允许折行 */}
                <col className="w-auto" />
                {Array.from({ length: ncol - 1 }).map((_, ci) => (
                  <col key={ci} className={ci === ncol - 2 ? 'w-1/2' : 'w-auto'} />
                ))}
              </colgroup>
              <thead>
                <tr className="bg-elevated/50">
                  {header.map((cell, ci) => (
                    <th key={ci} className="px-2.5 py-1.5 text-left font-medium text-foreground border-b border-border/40 whitespace-nowrap">
                      {renderInline(cell, `th-${key}-${ci}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {body.map((row, ri) => (
                  <tr key={ri} className="border-b border-border/20 last:border-0 hover:bg-elevated/20">
                    {row.map((cell, ci) => (
                      <td key={ci} className="px-2.5 py-1.5 text-foreground align-top break-words">
                        {renderInline(cell, `td-${key}-${ri}-${ci}`)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>,
        )
        i += table.consumed
        continue
      }
    }

    // 无序列表
    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''))
        i++
      }
      blocks.push(
        <ul key={key++} className="my-4 space-y-2">
          {items.map((item, ii) => (
            <li key={ii} className="flex items-start gap-2 text-sm text-foreground leading-relaxed">
              <span className="mt-[7px] h-1 w-1 rounded-full bg-accent/60 shrink-0" />
              <span>{renderInline(item, `li-${key}-${ii}`)}</span>
            </li>
          ))}
        </ul>,
      )
      continue
    }

    // 有序列表
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''))
        i++
      }
      blocks.push(
        <ol key={key++} className="my-4 space-y-2">
          {items.map((item, ii) => (
            <li key={ii} className="flex items-start gap-2 text-xs text-foreground/90 leading-relaxed">
              <span className="mt-0.5 h-4 w-4 rounded-full bg-accent/10 text-accent text-[10px] font-mono flex items-center justify-center shrink-0">
                {ii + 1}
              </span>
              <span className="flex-1 text-foreground">{renderInline(item, `ol-${key}-${ii}`)}</span>
            </li>
          ))}
        </ol>,
      )
      continue
    }

    // 普通段落
    blocks.push(
      <p key={key++} className="my-3 text-sm text-foreground leading-relaxed">
        {renderInline(trimmed, `p-${key}`)}
      </p>,
    )
    i++
  }

  // 注意:外层不加 space-y-*,否则会覆盖各块自己的 margin-top 导致间距失效。
  // 让各块的 my-* 自然叠加(margin collapse),间距更可控。
  return <div>{blocks}</div>
}
