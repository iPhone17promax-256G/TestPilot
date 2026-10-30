import { readFileSync } from 'node:fs'
import { Agent } from 'node:https'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 开发页与后端代理共用本次安装的证书信任，不关闭证书校验。
 * 输入是证书目录，默认使用仓库的 .tls，也可由 TESTPILOT_TLS_DIR 指定。
 * 返回 Vite server/preview 的 HTTPS 与代理配置；仅读取证书，不生成或安装信任。
 */
export function developmentTransport(
  directory = process.env.TESTPILOT_TLS_DIR || fileURLToPath(new URL('../.tls/', import.meta.url)),
) {
  const material = (name: string) => {
    try {
      return readFileSync(join(directory, name))
    } catch {
      throw new Error(
        `无法读取开发 HTTPS 证书 ${name}。请先在项目根目录运行 bash scripts/start.sh --local，` +
        '并按 README 信任本次安装的 rootCA.pem；如使用其他证书目录，设置 TESTPILOT_TLS_DIR。',
      )
    }
  }
  const cert = material('platform.pem')
  const key = material('platform-key.pem')
  const ca = material('rootCA.pem')
  const agent = new Agent({ ca, rejectUnauthorized: true })
  const backend = {
    target: 'https://127.0.0.1:8020',
    agent,
    secure: true,
    // 保留浏览器的 Host，与原始 Origin 对应，使后端仍能正确检查同源请求。
    changeOrigin: false,
  }
  return { https: { cert, key }, proxy: { '/api': backend, '/mcp': backend } }
}
