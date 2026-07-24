import { ListColumnCustomizer } from '@/components/ListColumnCustomizer'
import { COLUMN_GROUPS, type ColumnConfig } from '@/lib/watchlist-columns'

interface ColumnCustomizerProps {
  columns: ColumnConfig[]
  onChange: (columns: ColumnConfig[]) => void
  open: boolean
  onClose: () => void
}

export function ColumnCustomizer({ columns, onChange, open, onClose }: ColumnCustomizerProps) {
  return (
    <ListColumnCustomizer
      columns={columns}
      groups={COLUMN_GROUPS}
      onChange={onChange}
      open={open}
      onClose={onClose}
      title="自定义列"
      builtinSectionLabel="内置列"
      extColumnAlign="right"
    />
  )
}
