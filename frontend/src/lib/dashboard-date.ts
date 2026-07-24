export function resolveDashboardDateState(
  latestDate: string | null,
  overviewAsOf: string | null,
  realtimeAsOf: string | null,
  selectedDate: string | undefined,
) {
  const maxDate = [latestDate, realtimeAsOf].filter(Boolean).sort().at(-1) ?? null
  return {
    currentDate: selectedDate ?? realtimeAsOf ?? overviewAsOf ?? '',
    maxDate,
  }
}

export function selectedDateForRequest(value: string, realtimeAsOf: string | null) {
  return value === realtimeAsOf ? undefined : value
}
