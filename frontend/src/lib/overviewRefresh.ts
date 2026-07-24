export const OVERVIEW_POLL_INTERVAL_MS = 10_000

export function overviewRefetchInterval(selectedDate: string | undefined): number | false {
  return selectedDate ? false : OVERVIEW_POLL_INTERVAL_MS
}
