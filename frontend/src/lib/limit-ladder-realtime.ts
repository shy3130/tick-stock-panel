export function limitLadderRefetchInterval(asOf: string) {
  return asOf ? false : 5_000
}
