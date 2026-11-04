import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 输入防抖窗口 (ms): 连续输入只在停手后发一次请求 */
export const SEARCH_DEBOUNCE_MS = 300
/** 每页条数: 与 instrumentSearch 既有调用方保持一致 */
export const SEARCH_PAGE_LIMIT = 20
/** 翻页软上限: 防结果过多时 DOM 无限膨胀 */
export const SEARCH_MAX_PAGES = 10

export type AssetFilterValue = 'all' | 'stock' | 'etf' | 'index'

export const ASSET_TYPE_LABELS: Record<string, string> = {
  all: '全部',
  stock: '股票',
  etf: 'ETF',
  index: '指数',
}

export interface InstrumentSearchRow {
  symbol: string
  name: string
  code: string
  asset_type?: string
}

/**
 * 标的搜索数据层 —— 公共搜索框 (财务 / 缠论 / 个股分析) 与自选页内联搜索框共用。
 *
 * 组成: ①输入防抖 (SEARCH_DEBOUNCE_MS) ②后端游标分页 (offset + has_more/next_offset)
 * ③下拉内资产类型筛选 (balance=true 时后端按类型 round-robin 交织)。
 * 分页靠服务端「三层分层全序」保证跨页不重不漏; instruments_version 用于检测
 * 翻页中途 instruments 缓存换代 (行序漂移) 并重置回第一页。
 *
 * 调用方把 listRef 挂到滚动容器、sentinelRef 挂到列表底部的哨兵元素
 * (紧邻底部提示上方), 即可获得「滚到底才加载下一页」。
 */
export function useInstrumentSearch(assetTypes?: string) {
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [filter, setFilter] = useState<AssetFilterValue>('all')
  const queryClient = useQueryClient()
  const [listEl, setListEl] = useState<HTMLDivElement | null>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)

  const types = useMemo(
    () => (assetTypes ?? 'stock').split(',').map((t) => t.trim()).filter(Boolean),
    [assetTypes],
  )

  // 只有一个资产类型时筛选条无意义 (如财务页只搜股票)
  const showFilter = types.length > 1

  // 页面切换 assetTypes 后旧筛选值可能已不在可选集合内, 回落到全部
  useEffect(() => {
    if (filter !== 'all' && !types.includes(filter)) setFilter('all')
  }, [types, filter])

  useEffect(() => {
    if (!query.trim()) { setDebouncedQuery(''); return }
    const timer = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  const trimmed = debouncedQuery.trim()
  const searchTypes = filter === 'all' ? types.join(',') : filter
  const queryKey = QK.instrumentSearchPage(trimmed, searchTypes, SEARCH_PAGE_LIMIT)

  const search = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) =>
      api.instrumentSearch(trimmed, SEARCH_PAGE_LIMIT, searchTypes, { offset: pageParam, balance: true }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    enabled: trimmed.length > 0,
    staleTime: 30_000,
    maxPages: SEARCH_MAX_PAGES,
  })

  const pages = search.data?.pages ?? []
  const results = useMemo<InstrumentSearchRow[]>(() => pages.flatMap((p) => p.results), [pages])
  const reachedCap = pages.length >= SEARCH_MAX_PAGES && Boolean(search.hasNextPage)
  const canLoadMore = Boolean(search.hasNextPage) && !reachedCap

  // 翻页中途 instruments 缓存换代: 各页行序可能已漂移, 重置回第一页重拉
  useEffect(() => {
    if (pages.length < 2) return
    const first = pages[0].instruments_version
    if (first == null || pages.every((p) => p.instruments_version === first)) return
    void queryClient.resetQueries({ queryKey })
  }, [pages, queryKey, queryClient])

  // 触底加载: 只有哨兵进入下拉容器视口才加载下一页。
  // 用 IntersectionObserver 而非「距底 N px」阈值 —— 后者在浏览器缩放 /
  // 小数布局下会提前触发 (距底还有两行就开始刷新) 或因 1px 误差漏触发。
  useEffect(() => {
    const sentinel = sentinelRef.current
    if (listEl == null || sentinel == null) return
    if (typeof IntersectionObserver === 'undefined') return

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return
        if (!canLoadMore || search.isFetchingNextPage) return
        void search.fetchNextPage()
      },
      { root: listEl, threshold: 0 },
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [listEl, canLoadMore, search.isFetchingNextPage, search.fetchNextPage])

  function clearQuery() {
    setQuery('')
    setDebouncedQuery('')
  }

  return {
    query, setQuery, clearQuery,
    trimmed,
    types, showFilter,
    filter, setFilter,
    results,
    listRef: setListEl, sentinelRef,
    isLoading: search.isLoading,
    isFetchingNextPage: search.isFetchingNextPage,
    // 防抖等待期也算「加载中」, 输入框 spinner 不闪
    isFetching: search.isFetching || query.trim() !== trimmed,
    hasMore: canLoadMore,
    reachedCap,
  }
}
