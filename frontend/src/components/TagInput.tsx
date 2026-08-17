import { useRef, useState } from 'react'
import { X } from 'lucide-react'

interface TagInputProps {
  tags: string[]
  onChange: (tags: string[]) => void
  /** 全部标签(含已选), 内部过滤出建议 */
  allTags: string[]
  emptyLabel?: string
  /** 紧凑尺寸(导入弹窗用), 默认常规(编辑弹窗用) */
  compact?: boolean
  autoFocus?: boolean
}

/** 可编辑标签 chips + 输入框 + 建议 — TagEditorDialog 与导入批量标签共用。 */
export function TagInput({
  tags,
  onChange,
  allTags,
  emptyLabel = '暂无标签',
  compact = false,
  autoFocus = false,
}: TagInputProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [input, setInput] = useState('')
  const suggestions = allTags.filter(t => !tags.includes(t))

  function addTag(raw?: string) {
    const clean = (raw ?? input).trim().replace(/[,，]/g, '')
    setInput('')
    inputRef.current?.focus()
    if (!clean || tags.includes(clean)) return
    onChange([...tags, clean])
  }

  function removeTag(t: string) {
    onChange(tags.filter(x => x !== t))
  }

  const inputCls = compact
    ? 'flex-1 h-7 px-2 rounded-btn bg-elevated border border-border text-[11px] text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50'
    : 'flex-1 h-8 px-2 rounded-btn bg-elevated border border-border text-xs text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50'
  const addBtnCls = compact
    ? 'px-2 h-7 rounded-btn bg-accent/15 text-accent hover:bg-accent/25 text-[11px] font-medium transition-colors'
    : 'px-2.5 h-8 rounded-btn bg-accent/15 text-accent hover:bg-accent/25 text-xs font-medium transition-colors'

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1 min-h-[22px]">
        {tags.map(t => (
          <span key={t} className="inline-flex items-center gap-1 px-1.5 py-px rounded text-[11px] font-medium leading-tight text-yellow-500 bg-yellow-500/10">
            {t}
            <button
              type="button"
              onClick={() => removeTag(t)}
              className="text-yellow-500/60 hover:text-yellow-500 transition-colors"
              aria-label={`删除标签 ${t}`}
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        {tags.length === 0 && <span className="text-[10px] text-muted self-center">{emptyLabel}</span>}
      </div>
      <div className="flex items-center gap-1.5">
        <input
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') addTag() }}
          placeholder="输入标签，回车添加"
          maxLength={20}
          autoFocus={autoFocus}
          className={inputCls}
        />
        <button type="button" onClick={() => addTag()} className={addBtnCls}>
          添加
        </button>
      </div>
      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {suggestions.map(s => (
            <button
              key={s}
              type="button"
              onClick={() => addTag(s)}
              className="px-1.5 py-px rounded text-[10px] text-muted bg-elevated hover:text-accent hover:bg-accent/10 transition-colors"
            >
              + {s}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
