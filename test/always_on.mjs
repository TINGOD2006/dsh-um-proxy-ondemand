/**
 * 常駐模式（alwaysOn）驗證：用 mock ctx 跑真正的 apply()，確認
 *   A. 宿主啟動時會主動把代理叫起來（不用等第一個 um-genai 請求）。
 *   B. 閒置時間到了也不會把代理關掉（idleSeconds 故意調成 1 秒）。
 *   C. 宿主卸載（dispose）不會把代理一起帶走。
 *   D. 狀態端點會回報 alwaysOn: true，讓使用者看得出現在是常駐模式。
 *
 * 用法： node test/always_on.mjs
 */

import { cfgOf, health, stopProxy } from '../lib/proxy.js'
import * as plugin from '../lib/index.js'
import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const probe = http.createServer()
await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve))
const port = probe.address().port
await new Promise(resolve => probe.close(resolve))
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-always-on-'))
const cfg = cfgOf({
  port,
  python: process.env.UM_TEST_PYTHON || 'C:\\Python314\\python.exe',
  script: fileURLToPath(new URL('./fixtures/proxy.py', import.meta.url)),
  stateFile: path.join(temporary, 'state.json'),
  logFile: path.join(temporary, 'proxy.log'),
})
let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`)
  if (!ok) failures += 1
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

// ── mock cordis ctx（與 plugin_wiring.mjs 同一個形狀）─────────────────────
const handlers = new Map()
const effects = []
const routes = []
const logs = []
const ctx = {
  logger: {
    info: (msg, ...rest) => logs.push(['info', msg, ...rest]),
    warn: (msg, ...rest) => logs.push(['warn', msg, ...rest]),
  },
  on: (event, handler) => handlers.set(event, handler),
  effect: (fn) => effects.push(fn),
  webServer: { register: (route) => (routes.push(route), () => {}) },
}

await stopProxy(cfg) // 起點：測試埠上沒有代理
check('起點：代理是關閉的', (await health(cfg.port)) === false)

plugin.apply(ctx, { ...cfg, alwaysOn: true, idleSeconds: 1 })

// A. startup 是非阻塞的；等它把代理叫起來。
let warmed = false
for (let i = 0; i < 100 && !warmed; i += 1) {
  warmed = await health(cfg.port)
  if (!warmed) await sleep(100)
}
check('宿主啟動時主動喚醒了代理（alwaysOn）', warmed)
await sleep(400) // 等 startup 的 ensureProxy resolve、把訊息寫進 log
check('啟動訊息有寫進 log', logs.some(([level, msg]) => level === 'info' && String(msg).includes('always-on')))

// B. 閒置 1 秒後不該關（alwaysOn 之下 armTimer 是 no-op）。
await sleep(1800)
check('閒置超過 idleSeconds 後代理仍在跑（不會閒置關閉）', (await health(cfg.port)) === true)

// D. 狀態端點要標示 alwaysOn。
let payload = null
await routes[0].handler({}, { writeHead: () => {}, end: (body) => (payload = JSON.parse(body)) })
check('狀態端點回報 alwaysOn: true', payload?.alwaysOn === true, JSON.stringify(payload?.alwaysOn))
check('狀態端點回報 running: true', payload?.running === true)
check('狀態端點回報 idleTimerArmed: false（常駐模式不布署計時器）', payload?.idleTimerArmed === false)

// C. dispose 不該把代理關掉。
const dispose = effects[0]()
if (typeof dispose === 'function') await dispose()
await sleep(600)
check('宿主卸載後代理仍在跑（交給看門狗接手）', (await health(cfg.port)) === true)

// 收尾：測試代理不留痕跡。
await stopProxy(cfg)
check('收尾：測試代理已關閉', (await health(cfg.port)) === false)
if (!temporary.startsWith(path.join(os.tmpdir(), 'dsh-always-on-'))) throw new Error('invalid cleanup path')
fs.rmSync(temporary, { recursive: true, force: true })

console.log(failures === 0 ? '\n==== ALL PASS ====' : `\n==== ${failures} FAILURE(S) ====`)
process.exit(failures === 0 ? 0 : 1)
