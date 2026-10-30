import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { once } from 'node:events'
import { copyFileSync, mkdtempSync, rmSync } from 'node:fs'
import { createServer, get } from 'node:https'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { loadConfigFromFile } from 'vite'
import { developmentTransport } from '../devTransport.ts'

test('missing development certificates fail clearly instead of falling back to HTTP', () => {
  const directory = mkdtempSync(join(tmpdir(), 'testpilot-dev-missing-'))
  try {
    assert.throws(() => developmentTransport(directory), /platform\.pem.*bash scripts\/start\.sh --local/)
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})

test('a production build loads without any local certificate directory', async () => {
  const original = process.env.TESTPILOT_TLS_DIR
  const directory = mkdtempSync(join(tmpdir(), 'testpilot-build-no-tls-'))
  process.env.TESTPILOT_TLS_DIR = directory
  try {
    const loaded = await loadConfigFromFile({ command: 'build', mode: 'production' }, fileURLToPath(new URL('../vite.config.ts', import.meta.url)))
    assert.ok(loaded)
    assert.equal(loaded.config.server?.https, undefined)
    assert.equal(loaded.config.preview?.https, undefined)
  } finally {
    if (original === undefined) delete process.env.TESTPILOT_TLS_DIR
    else process.env.TESTPILOT_TLS_DIR = original
    rmSync(directory, { recursive: true, force: true })
  }
})

// OpenSSL is only used to generate disposable test certificates, never to modify system trust.
const opensslAvailable = spawnSync('openssl', ['version'], { stdio: 'ignore' }).status === 0
test('the development proxy trusts its configured CA and rejects another CA', { skip: !opensslAvailable }, async () => {
  const directory = mkdtempSync(join(tmpdir(), 'testpilot-dev-tls-'))
  const wrongDirectory = mkdtempSync(join(tmpdir(), 'testpilot-dev-other-ca-'))
  const generate = (output: string) => {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-keyout', join(output, 'platform-key.pem'), '-out', join(output, 'platform.pem'),
      '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], { stdio: 'ignore' })
    copyFileSync(join(output, 'platform.pem'), join(output, 'rootCA.pem'))
  }
  generate(directory)
  generate(wrongDirectory)
  const transport = developmentTransport(directory)
  const wrongTrust = developmentTransport(wrongDirectory)
  const server = createServer(transport.https, (_request, response) => response.end('secure backend'))
  const request = (agent: typeof transport.proxy['/api']['agent']) => new Promise<string>((resolve, reject) => {
    const address = server.address()
    assert.ok(address && typeof address === 'object')
    get({ host: '127.0.0.1', port: address.port, path: '/', agent }, response => {
      let body = ''
      response.setEncoding('utf8')
      response.on('data', value => { body += value })
      response.on('end', () => resolve(body))
    }).on('error', reject)
  })
  try {
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')
    assert.equal(await request(transport.proxy['/api'].agent), 'secure backend')
    await assert.rejects(request(wrongTrust.proxy['/api'].agent), /self-signed|certificate|unable to verify/i)
  } finally {
    transport.proxy['/api'].agent.destroy()
    wrongTrust.proxy['/api'].agent.destroy()
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
    rmSync(directory, { recursive: true, force: true })
    rmSync(wrongDirectory, { recursive: true, force: true })
  }
})
