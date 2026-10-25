/**
 * 冷啟動計時 + 觸發（給 verify_no_console_window.ps1 用）：
 * 先把代理停掉，再用外掛自己的 ensureProxy 冷啟動一次，並回報耗時。
 *
 * 用法： node test/coldstart.mjs
 */

import { cfgOf, ensureProxy, health, listeningPids, stopProxy } from '../lib/proxy.js'

const cfg = cfgOf()

const stopped = await stopProxy(cfg)
console.log('stop :', JSON.stringify(stopped))

await new Promise((resolve) => setTimeout(resolve, 600))

const t0 = Date.now()
const started = await ensureProxy(cfg)
const elapsed = Date.now() - t0

console.log('start:', JSON.stringify(started), `in ${elapsed} ms`)
console.log('health =', await health(cfg.port), ' pids =', JSON.stringify(listeningPids(cfg.port)))
