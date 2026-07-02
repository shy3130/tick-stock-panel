import { useState } from 'react'
import { DatePicker } from 'tickflow-stock-panel-frontend'

export function Empty() {
  const [value, setValue] = useState('')
  return <DatePicker value={value} onChange={setValue} />
}

export function WithValue() {
  const [value, setValue] = useState('2024-06-15')
  return <DatePicker value={value} onChange={setValue} />
}

export function WithRange() {
  const [value, setValue] = useState('2024-03-01')
  return (
    <DatePicker value={value} onChange={setValue} min="2024-01-01" max="2024-12-31" align="left" />
  )
}
