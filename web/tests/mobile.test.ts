import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/views/MobileView.vue', import.meta.url), 'utf8')
const script = source.split('<script setup lang="ts">')[1]!.split('</script>')[0]!
const compiled = ts.transpileModule(script + '\nexport { saveApp, action, close, application, session, image, source, busy, observeOpen, selector, text }', {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function mount(request: (path: string, body: any) => Promise<any> = async () => ({}), fetch?: (...args: any[]) => Promise<any>) {
  const hooks: Record<string, (...args: any[]) => any> = {}
  const messages: string[] = []
  const state = { projectId: 'PROJECT-SELECTED', runs: [], tasks: [] }
  const exports: Record<string, any> = {}
  const fetches: { url: string; options: RequestInit }[] = []
  let refreshes = 0
  runInNewContext(compiled, {
    exports, URLSearchParams,
    fetch: async (url: string, options: RequestInit) => {
      fetches.push({ url, options })
      if (fetch) return fetch(url, options)
      const value = await request(url.replace(/^\/api/, ''), JSON.parse(options.body as string))
      return { ok: true, json: async () => value }
    },
    setInterval: (fn: () => void) => { hooks.heartbeat = fn; return 1 },
    clearInterval: () => {},
    document: { visibilityState: 'visible' },
    require(name: string) {
      if (name === 'vue') return {
        ref: (value: unknown) => ({ value }),
        onMounted: (fn: () => void) => { hooks.mount = fn },
        onBeforeUnmount: (fn: () => void) => { hooks.unmount = fn },
      }
      if (name === 'vue-router') return { onBeforeRouteLeave: (fn: () => void) => { hooks.leave = fn } }
      if (name === '../store') return { state, ui: {}, request, refresh: async () => { refreshes++ }, notify: (message: string) => messages.push(message) }
      if (name === '../uiHelpers') return { dateTime: () => '' }
      return {}
    },
  })
  return { view: exports, hooks, messages, state, fetches, refreshes: () => refreshes }
}

test('APK uploads belong to the selected project', async () => {
  let options: RequestInit | undefined
  const { view } = mount(undefined, async (_url, value) => {
    options = value
    return { ok: true, json: async () => ({}) }
  })
  view.application.value.file = new Blob(['apk'])
  await view.saveApp()
  assert.equal((options?.headers as Record<string, string>)['X-TestPilot-Project'], 'PROJECT-SELECTED')
})

test('an element wait timeout preserves the session so it can still be closed', async () => {
  const actions: string[] = []
  const { view } = mount(async (_path, { arguments: args }) => {
    actions.push(args.action)
    if (args.action === 'click') throw new Error('等待元素超时：missing')
    assert.equal(args.session_id, 'MOBILE-OPEN')
    return {}
  })
  view.session.value = 'MOBILE-OPEN'
  view.image.value = 'previous screenshot'
  await view.action('click')
  assert.equal(view.session.value, 'MOBILE-OPEN')
  assert.equal(view.image.value, 'previous screenshot')
  await view.close()
  assert.deepEqual(actions, ['click', 'close'])
  assert.equal(view.session.value, '')
})

test('an expired device session returns to the connection form', async () => {
  const { view } = mount(async () => { throw new Error('设备会话已超时，请重新打开应用') })
  view.session.value = 'MOBILE-EXPIRED'
  view.image.value = 'old screenshot'
  await view.action('observe')
  assert.equal(view.session.value, '')
  assert.equal(view.image.value, '')
})

test('leaving the route waits for an in-flight open and releases its returned session', async () => {
  const opened = deferred<any>()
  const actions: string[] = []
  const { view, hooks } = mount(async (_path, { arguments: args }) => {
    actions.push(args.action)
    if (args.action === 'open') return opened.promise
    assert.equal(args.session_id, 'MOBILE-NEW')
    return {}
  })
  const opening = view.action('open')
  const leaving = hooks.leave!()
  assert.deepEqual(actions, ['open'])
  opened.resolve({ session_id: 'MOBILE-NEW' })
  await opening
  assert.equal(await leaving, true)
  assert.deepEqual(actions, ['open', 'close'])
  assert.equal(view.session.value, '')
  assert.equal(view.busy.value, false)
})

test('forced component disposal also releases a session returned after unmount', async () => {
  const opened = deferred<any>()
  const actions: string[] = []
  const { view, hooks, refreshes } = mount(async (_path, { arguments: args }) => {
    actions.push(args.action)
    return args.action === 'open' ? opened.promise : {}
  })
  const opening = view.action('open')
  hooks.unmount!()
  opened.resolve({ session_id: 'MOBILE-LATE' })
  await opening
  await view.close()
  assert.deepEqual(actions, ['open', 'close'])
  assert.equal(refreshes(), 0)
})

test('an in-flight open is released under its original project after switching projects and unmounting', async () => {
  const opened = deferred<any>()
  const { view, hooks, state, fetches } = mount(async (_path, { arguments: args }) => {
    if (args.action === 'open') return opened.promise
    assert.equal(args.action, 'close')
    assert.equal(args.session_id, 'MOBILE-ORIGINAL-PROJECT')
    return {}
  })
  const opening = view.action('open')
  state.projectId = 'PROJECT-NEXT'
  hooks.unmount!()
  opened.resolve({ session_id: 'MOBILE-ORIGINAL-PROJECT' })
  await opening
  await view.close()
  assert.equal(fetches.length, 2)
  for (const { options } of fetches) {
    assert.equal((options.headers as Record<string, string>)['X-TestPilot-Project'], 'PROJECT-SELECTED')
  }
  assert.equal(view.session.value, '')
})

test('background observations keep controls available and serialize the next user action', async () => {
  const observed = deferred<any>()
  const actions: any[] = []
  const { view } = mount(async (_path, { arguments: args }) => {
    actions.push(args)
    return args.action === 'observe' ? observed.promise : {}
  })
  view.session.value = 'MOBILE-OPEN'
  const observing = view.action('observe', true)
  assert.equal(view.busy.value, false)
  view.selector.value = 'selected-element'
  const clicking = view.action('click')
  view.selector.value = 'edited-after-click'
  assert.equal(view.busy.value, true)
  assert.equal(actions.length, 1)
  observed.resolve({ image: 'png', source: '<screen/>' })
  await Promise.all([observing, clicking])
  assert.deepEqual(actions.map(args => args.action), ['observe', 'click'])
  assert.equal(actions[1].selector, 'selected-element')
  assert.equal(view.busy.value, false)
})

test('a failed close preserves the session for retry and prevents leaving it behind', async () => {
  const { view, hooks } = mount(async () => { throw new Error('执行服务暂时不可用') })
  view.session.value = 'MOBILE-OPEN'
  view.observeOpen.value = true
  assert.equal(await hooks.leave!(), false)
  assert.equal(view.session.value, 'MOBILE-OPEN')
  assert.equal(view.observeOpen.value, true)
  assert.equal(view.busy.value, false)
})
