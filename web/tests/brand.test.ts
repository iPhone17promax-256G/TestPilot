import { test } from 'node:test'
import assert from 'node:assert/strict'
import { migrateBrandStorage } from '../src/migrations/brand.ts'

function storage(entries: Record<string, string>): Storage {
  const values = new Map(Object.entries(entries))
  return {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: key => { values.delete(key) },
    clear: () => { values.clear() },
  }
}

test('迁移账号配对和项目选择，删除旧键，值保持原样', () => {
  const pairing = '{"token":"sensitive-value","modelId":"MODEL-1"}'
  const state = storage({ 'aiqa-local:http://localhost:8020:USER-1': pairing, 'aiqa-project:USER-1': 'PROJECT-1', 'unrelated': 'keep' })
  migrateBrandStorage(state)
  assert.equal(state.getItem('testpilot-local:http://localhost:8020:USER-1'), pairing)
  assert.equal(state.getItem('testpilot-project:USER-1'), 'PROJECT-1')
  assert.equal(state.getItem('aiqa-project:USER-1'), null)
  assert.equal(state.getItem('unrelated'), 'keep')
  assert.equal(state.length, 3)
})

test('重复迁移不会覆盖已有新配置；保存失败不删除原资料', () => {
  const state = storage({ 'aiqa-project': 'old', 'testpilot-project': 'new' })
  migrateBrandStorage(state)
  migrateBrandStorage(state)
  assert.equal(state.getItem('testpilot-project'), 'new')
  const blocked = storage({ 'aiqa-project': 'retain' })
  blocked.setItem = () => { throw new Error('quota') }
  assert.throws(() => migrateBrandStorage(blocked))
  assert.equal(blocked.getItem('aiqa-project'), 'retain')
})
