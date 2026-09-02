import { DatePicker } from '@/components/DatePicker'
import { InstrumentSearchAdder } from '@/components/instruments/InstrumentSearchInput'
import { Control, ControlSelect } from '@/components/ui/Primitives'
import { cn } from '@/lib/cn'
import type { ParameterField, ParameterFormModel } from '../model/schema'

export function ParameterForm({
  form,
  values,
  onChange,
}: {
  form: ParameterFormModel
  values: Record<string, unknown>
  onChange: (name: string, value: unknown) => void
}) {
  if (form.fields.length === 0 && form.skipped.length === 0) {
    return <p className="text-xs text-muted">该因子没有额外参数；只需要选择范围。</p>
  }

  return (
    <div className="space-y-4">
      {form.groups.map((group) => {
        const fields = form.fields.filter((field) => group.fields.includes(field.name))
        if (fields.length === 0) return null
        return (
          <section key={group.id} className="space-y-3" aria-label={group.title}>
            <h3 className="text-xs font-medium text-secondary">{group.title}</h3>
            <div className="grid gap-3 md:grid-cols-2">
              {fields.map((field) => (
                <FieldControl key={field.name} field={field} value={values[field.name]} onChange={onChange} />
              ))}
            </div>
          </section>
        )
      })}
      {form.skipped.length > 0 ? (
        <ul className="space-y-1 text-[11px] text-muted">
          {form.skipped.map((item) => (
            <li key={item.name}>跳过 {item.name}：{item.reason}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function FieldControl({
  field,
  value,
  onChange,
}: {
  field: ParameterField
  value: unknown
  onChange: (name: string, value: unknown) => void
}) {
  const label = (
    <span className="text-xs text-secondary">
      {field.title}
      {field.required ? <span className="text-danger"> *</span> : null}
    </span>
  )

  if (field.widget === 'boolean') {
    return (
      <label className="flex min-h-11 items-center gap-2 text-xs text-secondary">
        <input
          type="checkbox"
          className="h-4 w-4 accent-accent"
          checked={value === true}
          onChange={(event) => onChange(field.name, event.target.checked)}
        />
        {label}
      </label>
    )
  }

  if (field.widget === 'date') {
    return (
      <label className="grid gap-1.5">
        {label}
        <DatePicker
          value={typeof value === 'string' ? value : ''}
          onChange={(next) => onChange(field.name, next)}
          className="w-full"
          buttonClassName="control min-h-11 w-full justify-start text-xs sm:min-h-[var(--control-h)]"
        />
        {field.description ? <span className="text-[11px] text-muted">{field.description}</span> : null}
      </label>
    )
  }

  if (field.widget === 'enum') {
    return (
      <label className="grid gap-1.5">
        {label}
        <ControlSelect
          className="min-h-11 text-xs sm:min-h-[var(--control-h)]"
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => onChange(field.name, event.target.value)}
        >
          <option value="">{field.required ? '请选择' : '默认'}</option>
          {field.enumValues.map((item) => (
            <option key={item} value={item}>{item}</option>
          ))}
        </ControlSelect>
      </label>
    )
  }

  if (field.widget === 'multi_enum') {
    const selected = Array.isArray(value) ? value.map(String) : []
    return (
      <fieldset className="grid gap-1.5 md:col-span-2">
        <legend className="text-xs text-secondary">{field.title}</legend>
        <div className="flex flex-wrap gap-2">
          {field.enumValues.map((item) => {
            const checked = selected.includes(item)
            return (
              <label key={item} className={cn('inline-flex min-h-11 items-center gap-2 rounded-btn border px-2.5 text-xs', checked ? 'border-accent bg-accent/10' : 'border-border')}>
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-accent"
                  checked={checked}
                  onChange={() => {
                    onChange(field.name, checked ? selected.filter((row) => row !== item) : [...selected, item])
                  }}
                />
                {item}
              </label>
            )
          })}
        </div>
      </fieldset>
    )
  }

  if (field.widget === 'symbol_list') {
    const symbols = Array.isArray(value) ? value.map(String) : typeof value === 'string' && value ? [value] : []
    const max = field.multiple ? (field.maxItems ?? 200) : 1
    return (
      <div className="grid gap-1.5 md:col-span-2">
        {label}
        <InstrumentSearchAdder
          onAdd={(result) => {
            if (symbols.includes(result.symbol) || symbols.length >= max) return
            if (!field.multiple) {
              onChange(field.name, result.symbol)
              return
            }
            onChange(field.name, [...symbols, result.symbol])
          }}
          assetTypes={['stock']}
        />
        <p className="font-mono text-[11px] text-muted">{symbols.join(', ') || '未选择'}</p>
      </div>
    )
  }

  const numeric = field.widget === 'integer' || field.widget === 'number'
  return (
    <label className="grid gap-1.5">
      {label}
      <Control
        type={numeric ? 'number' : 'text'}
        className="min-h-11 text-xs sm:min-h-[var(--control-h)]"
        value={value == null ? '' : String(value)}
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
        step={field.widget === 'integer' ? 1 : 'any'}
        onChange={(event) => {
          const raw = event.target.value
          if (raw === '') {
            onChange(field.name, undefined)
            return
          }
          onChange(field.name, field.widget === 'integer' ? Number.parseInt(raw, 10) : Number(raw))
        }}
      />
      {field.description ? <span className="text-[11px] text-muted">{field.description}</span> : null}
    </label>
  )
}
