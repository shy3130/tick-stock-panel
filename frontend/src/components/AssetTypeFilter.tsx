import { ASSET_TYPE_LABELS, type AssetFilterValue } from '@/lib/useInstrumentSearch'

interface Props {
  /** 可选资产类型 (顺序即展示顺序) */
  types: string[]
  value: AssetFilterValue
  onChange: (next: AssetFilterValue) => void
  /** 当前结果条数, 仅 > 0 时展示 */
  count?: number
}

/**
 * 资产类型筛选条 —— 搜索下拉内的「全部 / 股票 / ETF / 指数」胶囊。
 * 公共搜索框与自选页内联搜索框共用; 需 sticky 定位时由调用方包一层容器。
 */
export function AssetTypeFilter({ types, value, onChange, count }: Props) {
  return (
    <div className="flex items-center gap-1" role="tablist" aria-label="资产类型筛选">
      {(['all', ...types] as AssetFilterValue[]).map((t) => (
        <button
          key={t}
          type="button"
          role="tab"
          aria-selected={value === t}
          onClick={() => onChange(t)}
          className={`rounded-full px-2 py-0.5 text-[11px] transition-colors ${
            value === t
              ? 'bg-accent/10 text-accent'
              : 'text-secondary hover:bg-elevated hover:text-foreground'
          }`}
        >
          {ASSET_TYPE_LABELS[t] ?? t}
        </button>
      ))}
      {count != null && count > 0 && (
        <span className="ml-auto mr-1 text-[10px] text-muted">已显示 {count} 条</span>
      )}
    </div>
  )
}
