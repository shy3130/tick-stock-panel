import { useCallback, useEffect, useLayoutEffect, useState, type RefObject } from 'react'

export const VIRTUAL_LIST_THRESHOLD = 100

function findVerticalScrollParent(element: HTMLElement | null): HTMLElement | null {
  let current = element?.parentElement ?? null
  while (current) {
    const overflowY = window.getComputedStyle(current).overflowY
    if (/(auto|scroll|overlay)/.test(overflowY) && current.scrollHeight > current.clientHeight) {
      return current
    }
    current = current.parentElement
  }
  return null
}

export function useParentScroll(
  containerRef: RefObject<HTMLElement>,
  enabled: boolean,
) {
  const [scrollMargin, setScrollMargin] = useState(0)

  const getScrollElement = useCallback(
    () => enabled ? findVerticalScrollParent(containerRef.current) : null,
    [containerRef, enabled],
  )

  const updateScrollMargin = useCallback(() => {
    if (!enabled || !containerRef.current) return
    const scrollElement = getScrollElement()
    if (!scrollElement) return

    const containerRect = containerRef.current.getBoundingClientRect()
    const scrollRect = scrollElement.getBoundingClientRect()
    const next = containerRect.top - scrollRect.top + scrollElement.scrollTop
    setScrollMargin(current => Math.abs(current - next) < 0.5 ? current : next)
  }, [containerRef, enabled, getScrollElement])

  useLayoutEffect(updateScrollMargin)

  useEffect(() => {
    if (!enabled) return
    const scrollElement = getScrollElement()
    if (!scrollElement || !containerRef.current) return

    const observer = new ResizeObserver(updateScrollMargin)
    observer.observe(containerRef.current)
    observer.observe(scrollElement)
    window.addEventListener('resize', updateScrollMargin)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', updateScrollMargin)
    }
  }, [containerRef, enabled, getScrollElement, updateScrollMargin])

  return { getScrollElement, scrollMargin }
}
