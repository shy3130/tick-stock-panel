import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { loadLegacyInfoBarLayout, mergeInfoFields, type StockInfoBarLayout } from '@/lib/stock-info-fields'
import { usePreferences } from '@/lib/useSharedQueries'

export function useStockInfoBarLayout(): StockInfoBarLayout {
  const { data: prefs } = usePreferences()
  const [legacy] = useState(loadLegacyInfoBarLayout)
  const layout = prefs?.stock_info_bar_layout ?? legacy
  return useMemo(() => ({
    fields: mergeInfoFields(layout?.fields),
    baseline: layout?.baseline ?? null,
  }), [layout])
}

/** 应用启动时迁移一次，使旧布局也能随配置备份导出。 */
export function useMigrateStockInfoBarLayout(saved: StockInfoBarLayout | null | undefined) {
  const queryClient = useQueryClient()
  const attempted = useRef(false)
  useEffect(() => {
    // null 表示服务端明确未配置。undefined 包括偏好还没加载或旧服务端。
    if (saved !== null || attempted.current) return
    attempted.current = true
    const legacy = loadLegacyInfoBarLayout()
    if (!legacy) return
    void api.updateStockInfoBarLayout(legacy, true).then(
      () => queryClient.invalidateQueries({ queryKey: QK.preferences, exact: true }),
      () => { attempted.current = false },
    )
  }, [queryClient, saved])
}
