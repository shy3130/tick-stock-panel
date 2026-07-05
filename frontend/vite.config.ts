import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',   // 允许局域网访问
    port: 3011,
    // 允许通过局域网主机名访问 (如 m4max.wf / *.local)，否则 Vite 会 Blocked request
    allowedHosts: ['m4max.wf', '.wf', '.local'],
    proxy: {
      // dev 时 /api 转发到 FastAPI
      '/api': {
        target: 'http://localhost:3018',
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
      '/health': 'http://localhost:3018',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
