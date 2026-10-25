/**
 * 外掛接線驗證（不依賴 DSH）：用 mock ctx 真正跑一遍 lib/index.js 的 apply()，
 * 斷言三件事：
 *   A. 非 um-genai 的請求 → 立刻放行，而且完全沒有碰代理（代理維持關閉）。
 *   B. um-genai 的請求 → 冷啟動代理，且把 next() 的串流原封不動傳出去。
 *   C. 狀態端點回報的 running / 統計數字正確。
 *
 * 用法： node test/plugin_wiring.mjs
 */

import { cfgOf, health, stopProxy } from '../lib/proxy.js'
import * as plugin from '../lib/index.js'

const cfg = cfgOf()
let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`)
  if (!ok) failures += 1
}

// ── mock cordis ctx ────────────────────────────────────────────────────────
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

await stopProxy(cfg) // 起點：代理關閉

plugin.apply(ctx, { port: cfg.port, idleMinutes: 15 })

check('插件名正確', plugin.name === 'dsh-um-proxy-ondemand', plugin.name)
check('註冊了 llm/stream 監聽器', typeof handlers.get('llm/stream') === 'function')
check('註冊了唯讀狀態端點', routes.length === 1 && routes[0].path === '/um-proxy-ondemand/state.json')

const streamHandler = handlers.get('llm/stream')

// ── A. 非目標 provider：必須完全不啟動代理 ────────────────────────────────
const sentinel = { sentinel: true }
let passedThrough = null
const otherResult = streamHandler({ provider: 'lmstudio' }, () => sentinel)
check('非目標 provider 直接回傳 next() 的結果', otherResult === sentinel, String(otherResult))
check('非目標 provider 期間代理仍為關閉', (await health(cfg.port)) === false)

// 沒有 next 的畸形呼叫不該炸
check('缺少 next 時安全回傳 undefined', streamHandler({ provider: 'um-genai' }) === undefined)

// ── B. 目標 provider：先喚醒，再原封不動轉送串流 ──────────────────────────
const chunks = ['alpha', 'beta', 'gamma']
const fakeStream = async function* () {
  for (const c of chunks) yield c
}

let iterated = false
const wrapped = streamHandler({ provider: 'um-genai', model: 'DeepSeek-V4.1-Flash' }, () => {
  iterated = true
  return fakeStream()
})

check('回傳值是 async iterable', wrapped != null && typeof wrapped[Symbol.asyncIterator] === 'function')
check('建立回傳值時還沒開始串流（next 未被呼叫）', iterated === false)

const received = []
for await (const item of wrapped) received.push(item)
check('next() 被呼叫了', iterated === true)
check('串流內容原封不動傳出', JSON.stringify(received) === JSON.stringify(chunks), JSON.stringify(received))
check('經過外掛後代理已啟動且 /health 通過', (await health(cfg.port)) === true)

// ── C. 狀態端點 ───────────────────────────────────────────────────────────
let payload = null
await routes[0].handler({}, { writeHead: () => {}, end: (body) => (payload = JSON.parse(body)) })
check('狀態端點回報 running=true', payload?.running === true)
check('狀態端點回報 pid', typeof payload?.pid === 'number', `pid=${payload?.pid}`)
check('統計：1 次請求、1 次喚醒、1 次冷啟動', payload?.stats?.requests === 1 && payload?.stats?.wakes === 1 && payload?.stats?.coldStarts === 1, JSON.stringify(payload?.stats))
check('閒置計時器已布署', payload?.idleTimerArmed === true, `idleMinutes=${payload?.idleMinutes}`)
check('沒有喚醒失敗', payload?.stats?.wakeFailures === 0)

// ── 清理：跑一次 effect 的 dispose，應把代理關掉 ───────────────────────────
check('註冊了 cleanup effect', effects.length === 1)
const dispose = effects[0]()
if (typeof dispose === 'function') dispose()
await new Promise((r) => setTimeout(r, 2500))
check('dispose 後代理被關閉', (await health(cfg.port)) === false)

console.log(`\nlog 樣本：`)
for (const line of logs) console.log('  ', line.join(' '))
console.log(`\n==== ${failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'} ====`)
process.exit(failures === 0 ? 0 : 1)
