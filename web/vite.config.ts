import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const proxy = {'/api':'http://127.0.0.1:8020','/mcp':'http://127.0.0.1:8020'}
export default defineConfig({ plugins: [vue()], server:{host:'127.0.0.1',port:5173,strictPort:true,proxy}, preview:{host:'127.0.0.1',port:5174,strictPort:true,proxy} })
