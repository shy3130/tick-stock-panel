/**
 * 页面插件示例 — 复制本模板改造你的页面。
 *
 * 产物契约(v1):
 *   - default 导出清单对象; apiVersion 固定 1
 *   - id 带命名空间 (如 demo.hello), 全局唯一, 与核心/其他扩展冲突会被拒
 *   - routes[].path 非根绝对路径且静态 (不支持 :param / 通配)
 *   - mount(el, ctx): 把页面渲染进容器, 返回清理函数(可选)
 *   - navigation[].icon 用白名单图标名 (Sparkles/Zap/Star/... 见 docs/page-sdk.md)
 *
 * 样式约束: 宿主 Tailwind 按需编译, 不保证任意类名存在 — 布局关键样式
 * 用内联样式; 主题色可用宿主 CSS 变量 (如 var(--accent))。
 */
import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ctx } from './tsp'

function HelloPage() {
  const [count, setCount] = useState(0)
  const [serverTime, setServerTime] = useState('')
  const alive = useRef(true)

  useEffect(() => {
    // 经能力桥调用宿主 API (同源、自带登录态); 这里取服务端时间做演示
    ctx.api?.health?.().then((h: { status?: string } | undefined) => {
      if (alive.current) setServerTime(h?.status ? `服务状态: ${h.status}` : '')
    }).catch(() => {})
    return () => { alive.current = false }
  }, [])

  const card: React.CSSProperties = {
    maxWidth: 640,
    margin: '3rem auto',
    padding: '2rem',
    borderRadius: 16,
    border: '1px solid rgba(125,125,125,0.25)',
    background: 'rgba(125,125,125,0.06)',
    fontFamily: 'inherit',
  }
  const button: React.CSSProperties = {
    height: 36,
    padding: '0 14px',
    borderRadius: 10,
    border: 'none',
    background: 'var(--accent, #3b82f6)',
    color: '#fff',
    fontSize: 13,
    cursor: 'pointer',
  }

  return (
    <div style={{ minHeight: '100%' }}>
      <div style={card}>
        <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>页面插件演示页</h1>
        <p style={{ fontSize: 13, opacity: 0.75, lineHeight: 1.7 }}>
          这是一个自包含的运行时页面: 打包成单文件 ES 模块后上传到「设置 → 页面插件」,
          刷新即出现在侧栏。{serverTime}
        </p>
        <button style={button} onClick={() => setCount(n => n + 1)}>
          点击 {count} 次
        </button>
      </div>
    </div>
  )
}

const extension = {
  id: 'demo.hello',
  apiVersion: 1 as const,
  routes: [
    {
      id: 'hello',
      path: '/demo-hello',
      mount: (el: HTMLElement) => {
        const root = createRoot(el)
        root.render(<HelloPage />)
        return () => root.unmount()
      },
    },
  ],
  navigation: [
    { id: 'hello', routeId: 'hello', label: '页面插件演示', icon: 'Sparkles' },
  ],
}

export default extension
