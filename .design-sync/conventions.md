## Conventions — TickFlow Stock Panel UI kit

This is a **dark-first, quant-terminal** design language (`_ds_bundle.css` /
`styles.css`, §6.0). Read those files before styling anything new — the
family table below is a summary, not the full source.

### Setup
No provider wrapper is required — none of these 7 components read from React
context. The app itself sets `<html class="dark">` for dark mode (default)
and no class for light mode; when composing a full screen, add `class="dark"`
to the root element to get the dark palette these components were designed
against. Text/monospace numerals use a `.num` utility class
(`font-variant-numeric: tabular-nums`, mono font) — apply it to any price,
percentage, or date figure you lay out yourself.

### Styling idiom — Tailwind utilities over CSS custom properties
Every color is `hsl(var(--token) / <alpha-value>)`, so opacity modifiers
(`bg-surface/60`) work everywhere. Real family names (never invent others):

| Class | Token | Use |
|---|---|---|
| `bg-base` / `text-foreground` | `--base` / `--fg-primary` | page background / primary text |
| `bg-surface` | `--surface` | cards, panels |
| `bg-elevated` | `--elevated` | raised surfaces (buttons, inputs) |
| `border-border` | `--border` | all borders/dividers |
| `text-secondary` / `text-muted` | `--fg-secondary` / `--fg-muted` | secondary text / hints |
| `text-accent` / `bg-accent` | `--accent` (电光蓝 #3B82F6) | brand accent, primary actions |
| `text-bull` / `text-bear` | `--bull` (red) / `--bear` (green) | **only** for price/candle semantics (A-share convention: red = up, green = down) — never for generic UI state |
| `text-warning` / `text-danger` | `--warning` / `--danger` | non-price warning/error state |

Radii are semantic, not arbitrary: `rounded-card` (8px, panels), `rounded-btn`
(6px, buttons), `rounded-input` (4px, inputs). Fonts: `font-sans` (Inter,
HarmonyOS Sans SC fallback for CJK), `font-mono` (JetBrains Mono, for
anything numeric — combine with `.num`).

### Where the truth lives
`_ds_bundle.css` (and its `styles.css` import) carries every CSS custom
property under `:root` and `html.dark` — read it before introducing a new
color. Per-component `.prompt.md` files carry the prop contract; read the
component's own file for anything not covered here.

### Example
```tsx
<div className="dark bg-base text-foreground p-4 rounded-card border border-border">
  <PageHeader
    title="个股分析"
    subtitle="600519.SH 贵州茅台"
    right={
      <button className="h-7 px-3 rounded-btn bg-accent text-white text-xs font-medium">
        加自选
      </button>
    }
  />
  <EmptyState title="暂无数据" hint="换个筛选条件试试。" />
</div>
```
