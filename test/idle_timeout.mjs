/**
 * 閒置自動關閉驗證（不依賴 DSH，也不等 15 分鐘）：
 * 用 idleSeconds 覆寫把週期壓到 3 秒，驗證核心行為是
 *   喚醒 → 串流 → 靜置 → 自動關閉 → 可再次喚醒（可重複循環）。
 *
 * 用法： node test/idle_timeout.mjs
 */

import { cfgOf, health, stopProxy } from '../lib/proxy.js'
import * as plugin from '../lib/index.js'

const cfg = cfgOf()
let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`)
  if (!ok) failures += 1
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ── mock cordis ctx ────────────────────────────────────────────────────────
const handlers = new Map()
const effects = []
const routes = []
const ctx = {
  logger: { info: (m, ...r) => console.log('  [info]', m, ...r), warn: (m, ...r) => console.log('  [warn]', m, ...r) },
  on: (e, h) => handlers.set(e, h),
  effect: (fn) => effects.push(fn),
  webServer: { register: (r) => (routes.push(r), () => {}) },
}

await stopProxy(cfg)
plugin.apply(ctx, { port: cfg.port, idleMinutes: 15, idleSeconds: 3 })

const handler = handlers.get('llm/stream')
const streamOf = (...chunks) =>
  handler({ provider: 'um-genai' }, () =>
    (async function* () {
      for (const c of chunks) yield c
    })(),
  )

// ── 一次使用 ───────────────────────────────────────────────────────────────
const got = []
for await (const c of streamOf('x')) got.push(c)
check('使用時代理被喚醒', (await health(cfg.port)) === true)
check('串流正常', got.join(',') === 'x', JSON.stringify(got))

// ── 靜置到期 ───────────────────────────────────────────────────────────────
const t0 = Date.now()
let stopped = false
while (Date.now() - t0 < 25000) {
  await sleep(400)
  if (!(await health(cfg.port))) {
    stopped = true
    break
  }
}
check('閒置到期後代理自動關閉', stopped, `實測 ${Date.now() - t0} ms（設定 3000 ms）`)

let payload = null
await routes[0].handler({}, { writeHead: () => {}, end: (b) => (payload = JSON.parse(b)) })
check('統計記錄了一次閒置關閉', payload?.stats?.idleStops === 1, JSON.stringify(payload?.stats))
check('狀態端點回報 running=false', payload?.running === false)

// ── 再來一次，確認可重複循環 ───────────────────────────────────────────────
const got2 = []
for await (const c of streamOf('y')) got2.push(c)
check('關閉後可再次喚醒', got2.join(',') === 'y' && (await health(cfg.port)) === true)

// ── 清理 ───────────────────────────────────────────────────────────────────
const dispose = effects[0]()
if (typeof dispose === 'function') dispose()
await sleep(2500)
check('dispose 後代理被關閉', (await health(cfg.port)) === false)

console.log(`\n==== ${failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'} ====`)
process.exit(failures === 0 ? 0 : 1)
