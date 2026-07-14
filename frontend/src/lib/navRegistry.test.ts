import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'navRegistry.ts'), 'utf8')
const navMatch = source.match(/export const BUILTIN_NAV: NavItem\[\] = \[([\s\S]*?)\]\n\nfunction analysisMenuToNavItem/)
assert.ok(navMatch, 'BUILTIN_NAV block should be present')

const items = [...navMatch[1].matchAll(/\{\s*id:\s*'[^']+'[\s\S]*?label:\s*'([^']+)'[\s\S]*?group:\s*'([^']+)'[\s\S]*?extension:\s*false(?<rest>[^}]*)\}/g)]
  .map(match => ({
    label: match[1],
    group: match[2],
    pinned: /pinned:\s*true/.test(match.groups?.rest ?? ''),
  }))

test('BUILTIN_NAV uses the approved module-section sidebar order', () => {
  const pinned = items.filter(item => item.pinned).map(item => item.label)
  const strategy = items.filter(item => !item.pinned && item.group === 'strategy').map(item => item.label)
  const research = items.filter(item => !item.pinned && item.group === 'research').map(item => item.label)
  const system = items.filter(item => !item.pinned && item.group === 'system').map(item => item.label)

  assert.deepEqual(pinned, ['看板', '自选', '监控中心'])
  assert.deepEqual(strategy, ['策略', '回测', '复盘'])
  assert.deepEqual(research, ['指数', '连板梯队', '概念分析', '行业分析', '个股分析', '财务分析'])
  assert.deepEqual(system, ['数据'])
})
