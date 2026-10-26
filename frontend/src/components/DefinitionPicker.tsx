/**
 * 统一定义选择器 — 跨 kind (信号/因子) 的目录浏览、选取与逐条冒烟 (P4)。
 *
 * 数据: GET /api/definitions (统一目录视图; 坏定义不阻断目录, 单条携带 error)。
 * 冒烟: POST /api/definitions/smoke 按 id 复检 (校验 → 编译 → 依赖可解析),
 * 导入/编辑后的定义当场验证, 不必跑一次回测才发现拼错列名。
 *
 * onSelect 提供时行可选中 (供表单嵌入); 否则为纯浏览器。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { FlaskConical, Loader2, Search, SearchX } from 'lucide-react'
import { api } from '@/lib/api'
import type { DefinitionsCatalogItem, DefinitionSmokeResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const KIND_LABEL: Record<string, string> = { signal: '信号', factor: '因子' }
const LANG_LABEL: Record<string, string> = { conditions: '条件', dsl: 'DSL', python: 'Python' }

type Kind = 'signal' | 'factor'

/** 依赖列 chips: 超出 3 个折叠为 +N, 悬停看全量。 */
function DepChips({ deps }: { deps: string[] }) {
  if (deps.length === 0) return <span className="text-[11px] text-muted">无依赖</span>
  const visible = deps.slice(0, 3)
  const rest = deps.length - visible.length
  return (
    <span className="flex flex-wrap items-center gap-1" title={deps.join(', ')}>
      {visible.map((d) => (
        <span key={d} className="rounded-full bg-elevated px-1.5 py-px text-[10px] text-muted">{d}</span>
      ))}
      {rest > 0 && <span className="text-[10px] text-muted">+{rest}</span>}
    </span>
  )
}

/** 单条冒烟结果: ok 绿勾 / issues 逐条红字。 */
function SmokeResultView({ r }: { r: DefinitionSmokeResult }) {
  if (r.ok) {
    return (
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-accent">
        <span>冒烟通过</span>
        {r.warmup_bars != null && <span className="text-muted">预热 {r.warmup_bars} bars</span>}
        {r.dependencies.length > 0 && (
          <span className="text-muted">依赖 {r.dependencies.join(', ')}</span>
        )}
      </div>
    )
  }
  return (
    <div className="mt-1.5 space-y-0.5">
      {r.issues.map((i, idx) => (
        <div key={idx} className="text-[11px] text-danger">
          <span className="rounded bg-danger/10 px-1 py-px font-mono text-[10px]">{i.code}</span> {i.message}
        </div>
      ))}
    </div>
  )
}

export function DefinitionPicker({ kinds = ['signal', 'factor'], onSelect, selectedId }: {
  kinds?: ReadonlyArray<Kind>
  onSelect?: (item: DefinitionsCatalogItem) => void
  selectedId?: string
}) {
  const [search, setSearch] = useState('')
  const [kindFilter, setKindFilter] = useState<Kind | 'all'>(kinds.length === 1 ? kinds[0] : 'all')
  const [smokeOf, setSmokeOf] = useState<string | null>(null)  // 当前展开冒烟结果的 id

  const listQ = useQuery({
    queryKey: QK.definitions,
    queryFn: () => api.definitionsList(),
    staleTime: 30_000,
  })

  const smokeM = useMutation({
    mutationFn: (item: DefinitionsCatalogItem) => api.definitionsSmoke(item.kind, item.id),
    onSuccess: (_r, item) => setSmokeOf(item.id),
  })

  const items = useMemo(() => {
    const all = listQ.data?.items ?? []
    const kw = search.trim().toLowerCase()
    return all.filter((it) =>
      (kindFilter === 'all' || it.kind === kindFilter)
      && (!kw || it.id.toLowerCase().includes(kw) || it.label.toLowerCase().includes(kw)),
    )
  }, [listQ.data, kindFilter, search])

  const counts = useMemo(() => {
    const all = listQ.data?.items ?? []
    return {
      signal: all.filter((it) => it.kind === 'signal').length,
      factor: all.filter((it) => it.kind === 'factor').length,
    }
  }, [listQ.data])

  return (
    <div className="space-y-2.5">
      {/* 过滤行: kind chips + 搜索框 */}
      <div className="flex flex-wrap items-center gap-2">
        {kinds.length > 1 && (
          <div className="flex items-center gap-1">
            {(['all', ...kinds] as const).map((k) => (
              <button
                key={k}
                onClick={() => setKindFilter(k)}
                className={`rounded-full px-2.5 py-1 text-[11px] transition-colors ${
                  kindFilter === k
                    ? 'bg-accent/15 text-accent'
                    : 'text-muted hover:bg-elevated hover:text-foreground'
                }`}
              >
                {k === 'all' ? `全部 ${counts.signal + counts.factor}` : `${KIND_LABEL[k]} ${counts[k]}`}
              </button>
            ))}
          </div>
        )}
        <div className="relative ml-auto min-w-0 flex-1 sm:max-w-56">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索 id 或名称…"
            className="w-full rounded-btn border border-border bg-surface py-1.5 pl-8 pr-2.5 text-xs text-foreground placeholder:text-muted focus:border-accent/40 focus:outline-none"
          />
        </div>
      </div>

      {/* 目录列表 */}
      {listQ.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载定义目录…
        </div>
      ) : listQ.isError ? (
        <div className="py-8 text-center text-xs text-danger">定义目录加载失败, 请刷新重试</div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-1.5 py-8 text-xs text-muted">
          <SearchX className="h-4 w-4" />
          {search || kindFilter !== 'all' ? '没有匹配的定义' : '暂无自定义信号 / 因子 — 可在信号库或因子库中创建, 或从配置备份导入'}
        </div>
      ) : (
        <div className="space-y-1.5">
          {items.map((it) => {
            const selectable = !!onSelect
            const selected = selectedId === it.id
            const smoking = smokeM.isPending && smokeM.variables?.id === it.id
            return (
              <div
                key={`${it.kind}:${it.id}`}
                onClick={selectable ? () => onSelect!(it) : undefined}
                className={`rounded-btn border px-3 py-2 transition-colors ${
                  selected
                    ? 'border-accent/40 bg-accent/[0.06]'
                    : selectable
                      ? 'cursor-pointer border-border/70 hover:border-accent/25 hover:bg-elevated/40'
                      : 'border-border/70'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className={`shrink-0 rounded-full px-1.5 py-px text-[10px] ${
                    it.kind === 'signal' ? 'bg-accent/10 text-accent' : 'bg-secondary/10 text-secondary'
                  }`}>
                    {KIND_LABEL[it.kind]}
                  </span>
                  <span className="min-w-0 truncate text-xs font-medium text-foreground" title={it.id}>
                    {it.label}
                  </span>
                  <span className="shrink-0 truncate font-mono text-[10px] text-muted" title={it.id}>{it.id}</span>
                  <span className="ml-auto flex shrink-0 items-center gap-1.5">
                    {it.lang && LANG_LABEL[it.lang] && (
                      <span className="rounded-full bg-elevated px-1.5 py-px text-[10px] text-muted">{LANG_LABEL[it.lang]}</span>
                    )}
                    {it.timeframe && it.timeframe !== 'daily' && (
                      <span className="rounded-full bg-elevated px-1.5 py-px text-[10px] text-muted">{it.timeframe}</span>
                    )}
                    {it.kind === 'signal' && it.enabled === false && (
                      <span className="rounded-full bg-warning/10 px-1.5 py-px text-[10px] text-warning">已停用</span>
                    )}
                    {it.kind === 'factor' && it.status && it.status !== 'active' && (
                      <span className="rounded-full bg-elevated px-1.5 py-px text-[10px] text-muted">{it.status}</span>
                    )}
                    <button
                      onClick={(e) => { e.stopPropagation(); smokeM.mutate(it) }}
                      disabled={smoking}
                      title="冒烟: 校验 → 编译 → 依赖可解析 (不跑回测)"
                      className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent disabled:opacity-50"
                    >
                      {smoking ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
                      冒烟
                    </button>
                  </span>
                </div>

                {/* 元信息行: 依赖 / 预热 / 坏定义错误 */}
                <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
                  {it.error ? (
                    <span className="text-[11px] text-danger" title={it.error}>定义异常: {it.error}</span>
                  ) : (
                    <>
                      <DepChips deps={it.dependencies ?? []} />
                      {it.warmup_bars != null && (
                        <span className="text-[10px] text-muted">预热 {it.warmup_bars} bars</span>
                      )}
                    </>
                  )}
                </div>

                {/* 冒烟结果 (仅最近一条展开, 避免列表抖动) */}
                {smokeOf === it.id && smokeM.variables?.id === it.id && smokeM.data && (
                  <div className="mt-1.5 border-t border-border/60 pt-1.5">
                    <SmokeResultView r={smokeM.data} />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
