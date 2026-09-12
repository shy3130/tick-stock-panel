import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const source = readFileSync(
  new URL('./DimensionMembersDialog.tsx', import.meta.url),
  'utf8',
)

test('股票名称在前，板块标签紧随名称且代码保持独立一行', () => {
  const identity = source.match(
    /<span className="min-w-0">\s*<span className="flex min-w-0 items-center gap-1\.5">([\s\S]*?)<\/span>\s*<span className="block font-mono text-\[10px\] text-muted">\{row\.symbol\}<\/span>/,
  )

  assert.ok(identity, '股票名称、板块标签和代码应使用统一的两行身份布局')
  assert.ok(
    identity[1].indexOf('{row.name || row.symbol}') < identity[1].indexOf('{board &&'),
    '板块标签应渲染在股票名称之后',
  )
})
