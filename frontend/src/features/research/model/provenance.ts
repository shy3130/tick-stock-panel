import { asArray, asNumber, asRecord, asString, asStringArray } from './parse'

export interface ProvenanceSource {
  kind: string
  generation: string | null
  manifest_sha256: string | null
  status: string | null
  path: string | null
}

export interface ProvenanceBlock {
  generation: string | null
  manifest_sha256: string | null
  cohort_hash: string | null
  pinned: boolean | null
  sources: ProvenanceSource[]
  notes: string[]
}

export interface ArtifactFile {
  name: string
  sha256: string | null
  bytes: number | null
  rows: number | null
}

export interface ArtifactAvailability {
  summary: boolean
  events: boolean
  series: boolean
  raw_result: boolean
  manifest: boolean
  files: ArtifactFile[]
}

export function parseProvenance(value: unknown): ProvenanceBlock {
  const rec = asRecord(value)
  if (!rec) {
    return { generation: null, manifest_sha256: null, cohort_hash: null, pinned: null, sources: [], notes: [] }
  }
  const nestedManifest = asRecord(rec.manifest)
  const manifestSha = asString(rec.manifest_sha256)
    ?? (typeof rec.manifest === 'string' ? rec.manifest : null)
    ?? asString(nestedManifest?.sha256)
    ?? asString(nestedManifest?.manifest_sha256)
  return {
    generation: asString(rec.generation ?? rec.canonical_generation),
    manifest_sha256: manifestSha,
    cohort_hash: asString(rec.cohort_hash),
    pinned: typeof rec.pinned === 'boolean' ? rec.pinned : null,
    sources: asArray(rec.sources).map((item) => {
      const row = asRecord(item)
      if (!row) return null
      const sourceManifest = asRecord(row.manifest)
      return {
        kind: asString(row.kind) ?? 'unknown',
        generation: asString(row.generation),
        manifest_sha256: asString(row.manifest_sha256)
          ?? (typeof row.manifest === 'string' ? row.manifest : null)
          ?? asString(sourceManifest?.sha256),
        status: asString(row.status),
        path: asString(row.path),
      }
    }).filter((item): item is ProvenanceSource => item !== null),
    notes: asStringArray(rec.notes),
  }
}

export function parseArtifacts(value: unknown): ArtifactAvailability {
  const empty: ArtifactAvailability = {
    summary: false,
    events: false,
    series: false,
    raw_result: false,
    manifest: false,
    files: [],
  }
  const rec = asRecord(value)
  if (!rec) return empty

  const filesRec = asRecord(rec.files)
  if (filesRec) {
    const files = Object.entries(filesRec).map(([name, meta]) => {
      const row = asRecord(meta)
      return {
        name,
        sha256: asString(row?.sha256),
        bytes: asNumber(row?.bytes),
        rows: asNumber(row?.rows),
      }
    })
    const names = new Set(files.map((file) => file.name))
    return {
      summary: names.has('summary.json'),
      events: names.has('events.parquet'),
      series: names.has('series.parquet'),
      raw_result: names.has('raw-result.json') || names.has('raw_result.json'),
      manifest: true,
      files,
    }
  }

  const flag = (key: string): boolean => rec[key] === true || asRecord(rec[key])?.available === true
  return {
    summary: flag('summary') || rec.summary_json === true,
    events: flag('events') || rec.events_parquet === true,
    series: flag('series') || rec.series_parquet === true,
    raw_result: flag('raw_result') || rec.raw === true,
    manifest: flag('manifest') || rec.manifest_json === true,
    files: [],
  }
}

export function parseUnavailableReasons(value: unknown): { code: string; message: string; observed: number | null; required: number | null }[] {
  return asArray(value).map((item) => {
    if (typeof item === 'string') return { code: item, message: item, observed: null, required: null }
    const rec = asRecord(item)
    if (!rec) return null
    const code = asString(rec.code) ?? 'unavailable'
    const detail = typeof rec.detail === 'string' ? rec.detail : null
    return {
      code,
      message: asString(rec.message) ?? detail ?? code,
      observed: asNumber(rec.observed),
      required: asNumber(rec.required),
    }
  }).filter((item): item is { code: string; message: string; observed: number | null; required: number | null } => item !== null)
}
