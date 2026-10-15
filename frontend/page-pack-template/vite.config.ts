import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 页面插件构建: 单文件自包含 ES 模块 (react 等全部打入), 产物 dist/extension.js
// 直接上传到宿主「设置 → 页面插件」。不做代码分割 — 宿主按整个文件加载挂载。
export default defineConfig({
  plugins: [react()],
  // lib 模式不会自动替换依赖里的 process.env.NODE_ENV, 浏览器无 process 全局
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
  build: {
    outDir: 'dist',
    lib: {
      entry: 'src/extension.tsx',
      formats: ['es'],
    },
    rollupOptions: {
      output: {
        entryFileNames: 'extension.js',
        chunkFileNames: '[name].js',
        assetFileNames: '[name][extname]',
      },
    },
  },
})
