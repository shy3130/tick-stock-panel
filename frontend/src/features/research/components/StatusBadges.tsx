import { Badge, type BadgeTone } from '@/components/ui/Primitives'
import {
  DATA_STATUS_META,
  ENGINEERING_META,
  JOB_STATUS_META,
  PROFILE_META,
  PROMOTION_META,
  VERDICT_META,
  type DataStatus,
  type EngineeringStatus,
  type JobStatus,
  type PromotionStatus,
  type ResearchVerdict,
  type ResultProfile,
} from '../model/status'

const TOUCH = 'inline-flex min-h-6 items-center'

export function JobStatusBadge({ value }: { value: JobStatus | null | undefined }) {
  if (!value) return <Badge tone="muted">工程未报</Badge>
  const meta = JOB_STATUS_META[value]
  return <Badge className={TOUCH} tone={meta.tone}>{meta.label}</Badge>
}

export function VerdictBadge({ value }: { value: ResearchVerdict | null | undefined }) {
  if (!value) return <Badge tone="muted">无裁决</Badge>
  const meta = VERDICT_META[value]
  return <Badge className={TOUCH} tone={meta.tone}>{meta.label}</Badge>
}

export function DataStatusBadge({ value }: { value: DataStatus | null | undefined }) {
  if (!value) return <Badge tone="muted">数据未知</Badge>
  const meta = DATA_STATUS_META[value]
  return <Badge className={TOUCH} tone={meta.tone}>{meta.label}</Badge>
}

export function PromotionBadge({ value }: { value: PromotionStatus | null | undefined }) {
  const status = value ?? 'not_promoted'
  const meta = PROMOTION_META[status]
  return <Badge className={TOUCH} tone={meta.tone}>{meta.label}</Badge>
}

export function EngineeringBadge({ value }: { value: EngineeringStatus | null | undefined }) {
  if (!value) return <Badge tone="muted">工程未知</Badge>
  const meta = ENGINEERING_META[value]
  return <Badge className={TOUCH} tone={meta.tone}>{meta.label}</Badge>
}

export function ProfileBadge({ value }: { value: ResultProfile | null | undefined }) {
  if (!value) return <Badge tone="muted">profile 未知</Badge>
  return <Badge className={TOUCH} tone="accent">{PROFILE_META[value].label}</Badge>
}

export function UnknownBadge({ label, tone = 'muted' }: { label: string; tone?: BadgeTone }) {
  return <Badge className={TOUCH} tone={tone}>{label}</Badge>
}
