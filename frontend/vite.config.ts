import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Lightweight Charts 5.2.1 uses viewport distances as canvas coordinates.
// Normalize both distributions at the shared input handler, before hit testing.
const chartPointerCoordinates = new RegExp([
  String.raw`(?<rect>[\w$]+)\s*=\s*(?<target>this\.[\w$]+)\.getBoundingClientRect\(\)`,
  String.raw`[^;]*;\s*return\s*\{[^{}]*?`,
  String.raw`localX:\s*\(?(?<x>[\w$]+\.clientX\s*-\s*\k<rect>\.left)\)?,(?<indent>\s*)`,
  String.raw`localY:\s*\(?(?<y>[\w$]+\.clientY\s*-\s*\k<rect>\.top)\)?,`,
].join(''), 'g')

const scaleLightweightChartsPointer: Plugin = {
  name: 'lightweight-charts-page-zoom',
  enforce: 'pre',
  // Dev must pass the package through this transform before serving it.
  config: () => ({ optimizeDeps: { exclude: ['lightweight-charts'] } }),
  transform(code, id) {
    if (!/\/lightweight-charts\/dist\/lightweight-charts\.(?:development|production)\.mjs(?:\?|$)/.test(id)) return

    const matches = [...code.matchAll(chartPointerCoordinates)]
    const match = matches[0]
    if (matches.length !== 1 || !match.groups) {
      this.error('Lightweight Charts pointer handler changed. Recheck the interface zoom fix when upgrading.')
    }

    const [original] = match
    const { rect, target, x, y, indent } = match.groups
    const prefix = original.slice(0, original.indexOf('localX:'))
    const localX = `localX: (${x}) * (${rect}.width > 0 ? ${target}.offsetWidth / ${rect}.width : 1),`
    const localY = `localY: (${y}) * (${rect}.height > 0 ? ${target}.offsetHeight / ${rect}.height : 1),`
    return code.replace(original, () => `${prefix}${localX}${indent}${localY}`)
  },
}

const backendHost = process.env.BACKEND_HOST || '127.0.0.1'
const proxyHost = ['0.0.0.0', '::'].includes(backendHost) ? '127.0.0.1' : backendHost
const backendPort = process.env.BACKEND_PORT || '3018'
const backendTarget = `http://${proxyHost}:${backendPort}`

export default defineConfig({
  plugins: [react(), scaleLightweightChartsPointer],
  experimental: {
    // 后端将资源挂到带版本的路径；分块预加载和 CSS 字体也须跟随该路径。
    renderBuiltUrl(_filename, { hostType }) {
      if (hostType !== 'html') return { relative: true }
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',   // dev.sh / dev.ps1 会用 CLI --host 覆盖
    port: 3011,
    proxy: {
      // dev 时 /api 转发到与启动脚本相同的 FastAPI 地址
      '/api': {
        target: backendTarget,
        // SSE 端点需要禁用缓冲
        configure: (proxy) => {
          proxy.on('proxyReq', (_proxyReq, req) => {
            if (req.url?.includes('/stream')) {
              _proxyReq.setHeader('Accept', 'text/event-stream')
              _proxyReq.setHeader('Cache-Control', 'no-cache')
              _proxyReq.setHeader('Connection', 'keep-alive')
            }
          })
        },
      },
      '/health': backendTarget,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // 把重型图表库拆到独立 chunk, 避免打进主包 + 让页面按需加载。
        // 用函数形式按 node_modules 路径匹配, 比对象形式更可靠。
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts')) return 'echarts'
            if (id.includes('lightweight-charts')) return 'lightweight-charts'
          }
        },
      },
    },
  },
})
