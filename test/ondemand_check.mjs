/**
 * 生命週期驗證（不依賴 DSH）：
 *   1. 現況探測（health / netstat pids）
 *   2. 代理已在跑 → 先收斂到「關閉」狀態
 *   3. 冷啟動 → ensureProxy 應啟動且 /health 通過
 *   4. 沿用 → 第二次 ensureProxy 不該再開新行程
 *   5. 停止 → health 變 false、pid 消失、狀態檔清除
 *   6. 冪等 → 對未執行的代理再停一次不該出錯
 *   7. 復原 → 代理可再次被喚醒（留下使用者原本的可用狀態）
 *
 * 用法： node test/ondemand_check.mjs
 */

import {
  cfgOf,
  ensureProxy,
  health,
  isAlive,
  listeningPids,
  readState,
  stopProxy,
} from '../lib/proxy.js'

const cfg = cfgOf()
let failures = 0

const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`)
  if (!ok) failures += 1
}

const snap = async () => ({ health: await health(cfg.port), pids: listeningPids(cfg.port) })

console.log(`port=${cfg.port}  stateFile=${cfg.stateFile}\n`)

// ── 1) 現況 ────────────────────────────────────────────────────────────────
let s = await snap()
console.log(`[1] 現況 health=${s.health} listeningPids=[${s.pids.join(', ')}]`)

if (s.health) {
  console.log('[1] 收斂起點：先把已在跑的代理停掉')
  await stopProxy(cfg)
  s = await snap()
}
check('起點：代理未執行', s.health === false && s.pids.length === 0, `pids=[${s.pids.join(', ')}]`)

// ── 2) 冷啟動 ──────────────────────────────────────────────────────────────
const started = await ensureProxy(cfg)
s = await snap()
check('ensureProxy 冷啟動回報 started=true', started.started === true, JSON.stringify(started))
check('冷啟動後 /health 通過', s.health === true)
check('冷啟動後恰有一個監聽者', s.pids.length === 1, `pids=[${s.pids.join(', ')}]`)
check('pid 與 ensureProxy 回報一致', s.pids[0] === started.pid, `${s.pids[0]} vs ${started.pid}`)

const state = readState(cfg.stateFile)
check(
  '狀態檔已寫入正確 pid/port',
  state?.pid === started.pid && state?.port === cfg.port,
  JSON.stringify(state),
)

// ── 3) 沿用（不重複啟動） ──────────────────────────────────────────────────
const again = await ensureProxy(cfg)
check('第二次 ensureProxy 沿用既有行程（started=false）', again.started === false, JSON.stringify(again))
check('沿用時 pid 不變', again.pid === started.pid, `${again.pid} vs ${started.pid}`)
check('沿用沒有製造重複監聽', (await snap()).pids.length === 1)

// ── 4) 停止（含重複監聽的清乾淨） ──────────────────────────────────────────
const stop1 = await stopProxy(cfg)
s = await snap()
check('stopProxy 回報 stopped=true', stop1.stopped === true, JSON.stringify(stop1))
check('停止後 /health 失敗', s.health === false)
check('停止後埠已釋放', s.pids.length === 0, `pids=[${s.pids.join(', ')}]`)
check('停止後行程真的沒了', isAlive(started.pid) === false, `pid=${started.pid}`)
check('停止後狀態檔已清除', readState(cfg.stateFile) === null)

// ── 5) 冪等 ───────────────────────────────────────────────────────────────
const stop2 = await stopProxy(cfg)
check(
  '對未執行的代理 stopProxy 回 not-running 且 ok',
  stop2.ok === true && stop2.stopped === false,
  JSON.stringify(stop2),
)

// ── 6) 復原：把代理重新啟動（恢復使用者原本可用狀態） ──────────────────────
const revived = await ensureProxy(cfg)
s = await snap()
check('復原：代理可再次被喚醒', revived.ok === true && revived.started === true, JSON.stringify(revived))
check('復原後 health 通過且只有一個監聽者', s.health === true && s.pids.length === 1)

console.log(`\n==== ${failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'} ====`)
process.exit(failures === 0 ? 0 : 1)
