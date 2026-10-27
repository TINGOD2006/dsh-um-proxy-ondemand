import assert from 'node:assert/strict'
import { test, mock } from 'node:test'
import { setImmediate as tick } from 'node:timers/promises'
import * as realProxy from '../lib/proxy.js'

let healthImpl, ensureImpl, stopImpl, stateImpl
mock.module('../lib/proxy.js', { exports: {
  ...realProxy,
  health: (...args) => healthImpl(...args),
  ensureProxy: (...args) => ensureImpl(...args),
  stopProxy: (...args) => stopImpl(...args),
  readState: () => stateImpl(),
  clearState: () => {}, flushState: () => {},
} })
const { apply } = await import('../lib/index.js')
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
function setup(config = {}) {
  const events = new Map(), effects = [], routes = []
  const ctx = { on: (e, fn) => events.set(e, fn), effect: fn => effects.push(fn), webServer: { register: route => { routes.push(route); return () => {} } } }
  apply(ctx, { idleSeconds: 1, ...config })
  return {
    stream: (next, options = { provider: 'um-genai' }) => events.get('llm/stream')(options, next),
    dispose: effects[0](),
    status: async () => { let result; await routes[0].handler({}, { writeHead() {}, end(body) { result = JSON.parse(body) } }); return result },
  }
}
function reset() {
  healthImpl = async () => false
  ensureImpl = async () => ({ ok: true, started: false, pid: 1 })
  stopImpl = async () => ({ ok: true, stopped: true, pid: 1 })
  stateImpl = () => null
}

test('startup cleanup yields to a new request and cannot stop its active stream', async () => {
  reset()
  const probe = deferred(), release = deferred(), entered = deferred()
  let stops = 0
  healthImpl = () => probe.promise
  stopImpl = async () => { stops++; return { ok: true, stopped: true } }
  const plugin = setup()
  const consume = (async () => { for await (const value of plugin.stream(() => (async function* () { entered.resolve(); await release.promise; yield 'ok' })())) {} })()
  probe.resolve(true)
  await entered.promise
  assert.equal(stops, 0)
  await plugin.dispose()
  assert.equal(stops, 0, 'dispose must wait until the active stream finishes')
  release.resolve()
  await consume
  assert.equal(stops, 1)
})

test('request arriving during idle stop waits for stop before waking', async t => {
  reset()
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const stop = deferred(), entered = deferred()
  let wakes = 0
  ensureImpl = async () => { wakes++; return { ok: true, started: false } }
  const plugin = setup()
  for await (const value of plugin.stream(() => (async function* () { yield 'first' })())) {}
  stopImpl = () => { entered.resolve(); return stop.promise }
  t.mock.timers.tick(1000)
  await entered.promise
  const iterator = plugin.stream(() => (async function* () { yield 'second' })())
  const request = iterator.next()
  await tick()
  assert.equal(wakes, 1)
  stop.resolve({ ok: true, stopped: true })
  assert.deepEqual(await request, { value: 'second', done: false })
  assert.equal(wakes, 2)
  // Close the iterator so its finally block releases the active request.
  stopImpl = async () => ({ ok: true, stopped: true })
  await iterator.return()
  await plugin.dispose()
})

test('disposed lazy generator cannot wake a proxy or rearm a timer', async () => {
  reset()
  let wakes = 0
  ensureImpl = async () => { wakes++ }
  const plugin = setup()
  const lazy = plugin.stream(() => (async function* () { yield 1 })())
  await plugin.dispose()
  await assert.rejects(lazy.next(), /disposed/)
  assert.equal(wakes, 0)
  assert.equal((await plugin.status()).idleTimerArmed, false)
})

test('idle stop failures rearm cleanup and preserve diagnostic errors', async t => {
  reset()
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const plugin = setup()
  for await (const value of plugin.stream(() => (async function* () { yield 1 })())) {}
  stopImpl = async () => ({ ok: false, stopped: false, reason: 'process-still-running' })
  t.mock.timers.tick(1000)
  await tick()
  const status = await plugin.status()
  assert.equal(status.idleTimerArmed, true)
  assert.match(status.lastError, /process-still-running/)
  stopImpl = async () => ({ ok: true, stopped: true })
  await plugin.dispose()
})

test('explicit provider overrides default matching and invalid idle settings fail early', async () => {
  reset()
  const plugin = setup({ provider: 'custom' })
  const sentinel = {}
  assert.equal(plugin.stream(() => sentinel), sentinel)
  await plugin.dispose()
  for (const config of [{ idleSeconds: 'bad' }, { idleMinutes: Infinity }, { idleMinutes: 999999, idleSeconds: null }]) assert.throws(() => setup(config))
})
