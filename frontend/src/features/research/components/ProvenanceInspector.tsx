import { Panel, PanelBody, PanelHeader } from '@/components/ui/Primitives'
import { fmtHash } from '../lib/format'
import type { ArtifactAvailability, ProvenanceBlock } from '../model/provenance'

export function ProvenanceInspector({
  provenance,
  artifacts,
}: {
  provenance: ProvenanceBlock
  artifacts: ArtifactAvailability
}) {
  return (
    <Panel>
      <PanelHeader>
        <div>
          <p className="section-kicker">Lineage</p>
          <h3 className="section-title">数据谱系</h3>
        </div>
      </PanelHeader>
      <PanelBody className="space-y-3">
        <dl className="grid gap-2 text-xs sm:grid-cols-2">
          <Meta label="Generation" value={provenance.generation} mono />
          <Meta label="Manifest" value={fmtHash(provenance.manifest_sha256)} mono />
          <Meta label="Cohort hash" value={fmtHash(provenance.cohort_hash)} mono />
          <Meta label="Pinned" value={provenance.pinned == null ? '—' : provenance.pinned ? '是' : '否'} />
        </dl>
        {provenance.sources.length > 0 ? (
          <div className="data-table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>源</th>
                  <th>状态</th>
                  <th>Generation</th>
                  <th>Manifest</th>
                </tr>
              </thead>
              <tbody>
                {provenance.sources.map((source) => (
                  <tr key={`${source.kind}-${source.generation ?? ''}`}>
                    <td className="font-mono text-xs">{source.kind}</td>
                    <td>{source.status ?? '—'}</td>
                    <td className="font-mono text-[11px]">{source.generation ?? '—'}</td>
                    <td className="font-mono text-[11px]">{fmtHash(source.manifest_sha256)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="text-xs text-muted">没有谱系源明细。</p>}
        <p className="break-words text-[11px] text-muted">
          artifacts：
          summary {flag(artifacts.summary)} ·
          events {flag(artifacts.events)} ·
          series {flag(artifacts.series)} ·
          raw {flag(artifacts.raw_result)} ·
          manifest {flag(artifacts.manifest)}
        </p>
        {artifacts.files.length > 0 ? (
          <div className="data-table-scroll">
            <table className="data-table min-w-[28rem]">
              <thead>
                <tr>
                  <th>文件</th>
                  <th>SHA-256</th>
                  <th>字节</th>
                  <th>行数</th>
                </tr>
              </thead>
              <tbody>
                {artifacts.files.map((file) => (
                  <tr key={file.name}>
                    <td className="font-mono text-xs">{file.name}</td>
                    <td className="font-mono text-[11px]">{fmtHash(file.sha256)}</td>
                    <td className="num">{file.bytes ?? '—'}</td>
                    <td className="num">{file.rows ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {provenance.notes.map((note) => (
          <p key={note} className="text-xs text-secondary">{note}</p>
        ))}
      </PanelBody>
    </Panel>
  )
}

function Meta({ label, value, mono }: { label: string; value: string | null; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-input border border-border bg-base/40 px-2.5 py-2">
      <dt className="text-muted">{label}</dt>
      <dd className={mono ? 'truncate font-mono text-[11px]' : 'truncate'}>{value || '—'}</dd>
    </div>
  )
}

function flag(value: boolean): string {
  return value ? '有' : '无'
}
