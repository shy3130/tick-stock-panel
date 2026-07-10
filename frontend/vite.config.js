import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';
export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    server: {
        host: '0.0.0.0', // 允许局域网访问
        port: 3011,
        proxy: {
            // dev 时 /api 转发到 FastAPI
            '/api': {
                target: 'http://localhost:3018',
                // SSE 端点需要禁用缓冲
                configure: (proxy) => {
                    proxy.on('proxyReq', (_proxyReq, req) => {
                        if (req.url?.includes('/stream')) {
                            _proxyReq.setHeader('Accept', 'text/event-stream');
                            _proxyReq.setHeader('Cache-Control', 'no-cache');
                            _proxyReq.setHeader('Connection', 'keep-alive');
                        }
                    });
                },
            },
            '/health': 'http://localhost:3018',
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
                        if (id.includes('echarts'))
                            return 'echarts';
                        if (id.includes('lightweight-charts'))
                            return 'lightweight-charts';
                    }
                },
            },
        },
    },
});
