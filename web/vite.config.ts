import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { developmentTransport } from './devTransport'

export default defineConfig(({ command }) => {
  // 构建静态文件不需要本机证书；开发和预览均使用启动脚本生成的 HTTPS 证书。
  const transport = command === 'serve' ? developmentTransport() : {}
  return {
    plugins: [vue()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, ...transport },
    preview: { host: '127.0.0.1', port: 5174, strictPort: true, ...transport },
  }
})
