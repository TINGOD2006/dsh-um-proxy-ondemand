import assert from 'node:assert/strict'
import test from 'node:test'
import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { cfgOf, ensureProxy, stopProxy, healthDetail, readState, writeStateThrottled, clearState, wakeRetryCount } from '../lib/proxy.js'
import { commandArgs, listenerPids, matchesProxyProcess } from '../lib/process.js'
import { detectPythonCli } from '../lib/paths.js'

const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)))
const close = server => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) }
async function freePort() { const s = http.createServer(); const p = await listen(s); await close(s); return p }
const fixture = fileURLToPath(new URL('./fixtures/proxy.py', import.meta.url))
const python = process.env.UM_TEST_PYTHON || detectPythonCli()

test('health rejects non-200, malformed, truncated and oversized responses', async () => {
  for (const mode of ['503', 'invalid', 'truncated', 'oversized']) {
    const server = http.createServer((req, res) => {
      res.writeHead(mode === '503' ? 503 : 200)
      if (mode === 'truncated') { res.write('{'); res.destroy(); return }
      res.end(mode === 'invalid' ? 'not json' : mode === 'oversized' ? ' '.repeat(65537) : '{"service":"um-opencode-proxy"}')
    })
    const port = await listen(server)
    try { assert.equal((await healthDetail(port, 500)).ok, false, mode) }
    finally { await close(server) }
  }
})

test('health has a total deadline even when the server keeps sending data', async () => {
  const server = http.createServer((req, res) => {
    res.writeHead(200); res.write('{')
    const interval = setInterval(() => res.write(' '), 10)
    res.on('close', () => clearInterval(interval))
  })
  const port = await listen(server)
  try {
    const start = performance.now()
    assert.equal((await healthDetail(port, 80)).ok, false)
    assert.ok(performance.now() - start < 350)
  } finally { await close(server) }
})

test('health honors custom service and handles invalid arguments without leaked timers', async () => {
  const server = http.createServer((req,res) => res.end('{"service":"custom"}'))
  const port = await listen(server)
  try { assert.equal((await healthDetail(port, 500, 'custom')).ok, true) }
  finally { await close(server) }
  assert.deepEqual(await healthDetail(-1, 20), { ok: false, refused: false })
})

test('configuration rejects invalid ports and timer values', () => {
  for (const port of [0, -1, NaN, 'bad', 65536, 1.5]) assert.throws(() => cfgOf({ port }))
  for (const startupTimeoutMs of [0, Infinity, NaN, -2]) assert.throws(() => cfgOf({ startupTimeoutMs }))
})

test('ownership requires the interpreter, full script path, serve command and exact port', () => {
  const cfg = { python: 'C:\\Tools\\Python312\\pythonw.exe', script: 'C:\\User Space\\proxy.py', port: 4567 }
  const good = { ExecutablePath: 'C:\\Tools\\Python312\\python.exe', CommandLine: '"C:\\Tools\\Python312\\python.exe" -B "C:\\User Space\\proxy.py" serve --port 4567' }
  assert.equal(matchesProxyProcess(good, cfg), true)
  for (const text of [good.CommandLine.replace('4567','45670'), good.CommandLine.replace('serve','login'), good.CommandLine.replace('User Space','Other Space'), good.CommandLine + ' --port 1']) {
    assert.equal(matchesProxyProcess({ ...good, CommandLine: text }, cfg), false)
  }
  assert.equal(matchesProxyProcess({ ...good, ExecutablePath: 'C:\\Other\\python.exe' }, cfg), false)
  assert.equal(matchesProxyProcess({ CommandLine: good.CommandLine }, cfg), false)
  assert.deepEqual(commandArgs('"C:\\Program Files\\python.exe" "C:\\scripts\\proxy.py" serve --port=4567'), ['C:\\Program Files\\python.exe','C:\\scripts\\proxy.py','serve','--port=4567'])
})

test('throttled reads expose the newest state, malformed JSON objects are ignored', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-state-'))
  const file = path.join(dir, 'state.json')
  try {
    writeStateThrottled(file, { lastUsedAt: 1 })
    writeStateThrottled(file, { lastUsedAt: 2 })
    assert.equal(readState(file).lastUsedAt, 2)
    assert.equal(JSON.parse(fs.readFileSync(file)).lastUsedAt, 1)
    clearState(file)
    fs.writeFileSync(file, '[]')
    assert.equal(readState(file), null)
  } finally { clearState(file); fs.rmSync(dir, { recursive: true, force: true }) }
})

test('missing executable rejects instead of terminating the Node host', async () => {
  const port = await freePort()
  await assert.rejects(ensureProxy({ port, python: 'C:\\__missing_dsh_python__\\python.exe', startupTimeoutMs: 100 }), { code: 'ENOENT' })
})

test('child exiting before readiness fails promptly without a timeout retry', async () => {
  const before = wakeRetryCount()
  const port = await freePort()
  await assert.rejects(ensureProxy({ port, python, script: path.join(path.dirname(fixture), '__missing_fixture__.py'), startupTimeoutMs: 3000 }), /exited before becoming healthy/)
  assert.equal(wakeRetryCount(), before)
  assert.deepEqual(await listenerPids(port), [])
})

test('isolated lifecycle: concurrent wake, stop, and wake are ordered without orphan processes', { timeout: 30000 }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-life-'))
  const cfg = { port: await freePort(), python, script: fixture, logFile: path.join(dir,'proxy.log'), stateFile: path.join(dir,'state.json'), startupTimeoutMs: 3000 }
  try {
    const first = ensureProxy(cfg), duplicate = ensureProxy(cfg)
    assert.equal(first, duplicate)
    const stop = stopProxy(cfg)
    const next = ensureProxy(cfg)
    const [started, stopped, restarted] = await Promise.all([first, stop, next])
    assert.equal(started.started, true)
    assert.equal(stopped.stopped, true)
    assert.equal(restarted.started, true)
    assert.notEqual(started.pid, restarted.pid)
    assert.deepEqual(await listenerPids(cfg.port), [restarted.pid])
    assert.equal((await stopProxy(cfg)).stopped, true)
    assert.deepEqual(await listenerPids(cfg.port), [])
  } finally {
    await stopProxy(cfg)
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('startup timeout cleans its own stalled process before the single retry', { timeout: 30000 }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-stall-'))
  const cfg = { port: await freePort(), python, script: fixture, logFile: path.join(dir,'stall.log'), stateFile: path.join(dir,'state.json'), startupTimeoutMs: 300 }
  try {
    await assert.rejects(ensureProxy(cfg), /did not become healthy/)
    assert.deepEqual(await listenerPids(cfg.port), [])
    assert.equal(readState(cfg.stateFile), null)
  } finally { await stopProxy(cfg); fs.rmSync(dir, { recursive: true, force: true }) }
})

test('foreign listener survives stop even with a forged service label and stale state PID', { timeout: 15000 }, async () => {
  const server = http.createServer((req,res) => res.end('{"service":"um-opencode-proxy"}'))
  const port = await listen(server)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-foreign-'))
  const cfg = { port, stateFile: path.join(dir,'state.json') }
  fs.writeFileSync(cfg.stateFile, JSON.stringify({ port, pid: process.pid }))
  try {
    const result = await stopProxy(cfg)
    assert.equal(result.reason, 'ownership-unverified')
    assert.equal((await healthDetail(port)).ok, true)
    // Healthy adoption is allowed, but cannot grant authority to kill a foreign process.
    assert.equal((await ensureProxy(cfg)).started, false)
    assert.equal((await stopProxy(cfg)).stopped, false)
  } finally { clearState(cfg.stateFile); await close(server); fs.rmSync(dir, { recursive: true, force: true }) }
})
