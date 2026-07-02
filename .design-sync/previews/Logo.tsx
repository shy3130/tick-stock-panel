import { Logo } from 'tickflow-stock-panel-frontend'

export function Default() {
  return (
    <div style={{ color: 'hsl(217 91% 60%)' }}>
      <Logo size={64} />
    </div>
  )
}

export function Sizes() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 24, color: 'hsl(217 91% 60%)' }}>
      <Logo size={20} />
      <Logo size={32} />
      <Logo size={48} />
      <Logo size={64} />
    </div>
  )
}

export function OnDark() {
  return (
    <div style={{ background: 'hsl(240 6% 8%)', padding: 24, color: 'hsl(0 0% 96%)' }}>
      <Logo size={48} />
    </div>
  )
}
