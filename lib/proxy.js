/**
 * dsh-um-proxy-ondemand —— UM GenAI 本機代理的生命週期管理。
 *
 * 只做四件事：健康檢查、按需啟動、閒置停止、狀態紀錄。
 * 刻意不依賴 cordis，因此可以獨立測試（見 test/ondemand_check.mjs）。
 *
 * 為什麼不用 um_opencode_setup.py 自己的 PID_FILE
 * ------------------------------------------------
 * `um_windows.py ensure`（啟動資料夾的 um-genai-proxy.cmd）用的是預設埠 8787，
 * 而 `PID_FILE` 是單一檔案（`~/.um-opencode/proxy.pid`）。若本外掛也寫它，
 * 兩邊會互相覆蓋、甚至誤殺對方。所以本外掛用自己的 state 檔，
 * 且「這個埠上跑的到底是不是我們的代理」一律用 /health 的 service 欄位確認，
 * 不靠任何 pid 檔的信任。
 *
 * @module dsh-um-proxy-ondemand/proxy
 */

import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { listenerPids, proxyProcesses, terminateProxyProcesses } from './process.js'
import { DEFAULT_PORT, defaultsOf, statePath } from './paths.js'

/**
 * 預設值（可用 cordis.patch.yml 的 config 或設定頁覆寫）。
 *
 * 全部從環境推導，不含任何作者機器的路徑：換一台電腦裝起來就會指向
 * 那台電腦的 `~/.um-opencode` 與它自己的 Python。推導規則見 lib/paths.js。
 */
export function defaults() {
  return {
    ...defaultsOf(process.env, DEFAULT_PORT),
    /** 冷啟動最多等多久。 */
    startupTimeoutMs: 20000,
    /** /health 回應裡代表「這是 UM 代理」的欄位值。 */
    service: 'um-opencode-proxy',
  }
}

/** 載入當下的預設值快照（給設定頁顯示用；實際合併用 defaults()）。 */
export const DEFAULTS = defaults()

/** 合併預設值。 */
export function cfgOf(overrides) {
  const given = overrides || {}
  const cfg = { ...defaults(), ...given }
  for (const [key, max] of [['port', 65535], ['startupTimeoutMs', 2147483647]]) {
    cfg[key] = Number(cfg[key])
    if (!Number.isInteger(cfg[key]) || cfg[key] < 1 || cfg[key] > max) {
      throw new TypeError(`invalid ${key}: ${cfg[key]}`)
    }
  }
  // 沒明講狀態檔就讓它跟著埠號走，否則兩個埠會共用同一個 pid 檔。
  if (given.stateFile === undefined) cfg.stateFile = statePath(cfg.port)
  return cfg
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 冷啟動時的輪詢排程（毫秒）：前段密、後段放大。
 *
 * 實測代理從 spawn 到 /health 可用只要 ~143ms（Python 314 已快取），
 * 但舊版固定睡 250ms 才探一次，於是「其實早就好了」還要多等一輪，
 * 首次請求的喚醒延遲變成 ~290ms。改成先立刻探一次、之後 20ms 起跳，
 * 喚醒延遲降到 ~160ms；真的很久的冷啟動（磁碟冷、token 檢查）才退到 250ms，
 * 不會比舊版多睡。
 */
export const STARTUP_POLL_DELAYS = [20, 20, 20, 25, 25, 50, 50, 50, 100, 100, 200, 250]

/** 第 i 次輪詢前要睡多久（超過排程長度就固定用最後一檔）。 */
export function pollDelay(i) {
  return STARTUP_POLL_DELAYS[Math.min(Math.max(0, i), STARTUP_POLL_DELAYS.length - 1)]
}

/**
 * 同一個 key 的並行呼叫共用同一個 promise（single-flight）。
 *
 * 冷啟動時若兩個請求同時進來（兩個分頁、一次工具展開多個子請求），
 * 舊版會各自 health、各自 spawn——實測真的會冒出兩個 python，
 * 其中一個寫完 PID_FILE 就 bind 失敗退出。single-flight 讓這種情況
 * 只做一輪 health、只 spawn 一次，所有呼叫者拿到同一個結果。
 *
 * @template T
 * @param {string} key
 * @param {() => Promise<T> | T} fn
 * @returns {Promise<T>}
 */
const inflight = new Map()

export function singleFlight(key, fn) {
  const existing = inflight.get(key)
  if (existing) return existing

  const tracked = Promise.resolve()
    .then(fn)
    .then(
      (value) => {
        if (inflight.get(key) === tracked) inflight.delete(key)
        return value
      },
      (err) => {
        if (inflight.get(key) === tracked) inflight.delete(key)
        throw err
      },
    )
  inflight.set(key, tracked)
  return tracked
}

/** 目前進行中的 single-flight 數量（狀態端點／測試觀察用）。 */
export function inflightCount() {
  return inflight.size
}

/** 冷啟動失敗後實際重試的次數（狀態端點／測試觀察用）。 */
let wakeRetries = 0

export function wakeRetryCount() {
  return wakeRetries
}

/**
 * 判斷冷啟動失敗是否值得「清掉卡死的 listener 再試一次」。
 *
 * 只認「我們自己 spawn 的代理沒在期限內變健康」這一種：實測 pythonw 偶爾
 * 會先把埠 bind 住卻不回應 /health（TCP 連得上、連線立刻被關閉），
 * 這時直接重試 spawn 只會撞 `Port already in use` 再失敗一次，
 * 必須先把那個半死掉的行程殺掉。
 *
 * 反之「埠被別人的服務佔著」（訊息含 refusing to start）重試無意義，
 * spawn ENOENT（python 路徑寫錯）也不會靠重試變好，直接往上拋。
 *
 * @param {unknown} err
 * @returns {boolean}
 */
export function isRecoverableWakeError(err) {
  const message = String(err?.message || err)
  return message.includes('did not become healthy') && !message.includes('refusing to start')
}

/** 重試前先等這麼久，讓被殺掉的 listener 真的放掉埠。 */
const WAKE_RETRY_DELAY_MS = 400

/**
 * 問 /health，並分辨「真的沒人在聽」與「有人回應但不是我們」。
 *
 * 這個區分是熱路徑省一次同步 netstat 的關鍵：ECONNREFUSED 代表埠上沒有
 * listener，直接 spawn 就好；只有「有回應卻不是我們的代理」（外來服務、
 * 或卡死的代理）才需要 netstat 去查是誰佔著埠。
 *
 * @param {number} port
 * @param {number} [timeoutMs]
 * @returns {Promise<{ ok: boolean, refused: boolean }>}
 */
export function healthDetail(port, timeoutMs = 2000, service = DEFAULTS.service) {
  return new Promise((resolve) => {
    let settled = false
    let req
    let deadline
    const done = (value) => {
      if (settled) return
      settled = true
      clearTimeout(deadline)
      resolve(value)
    }
    // Socket inactivity timeouts can be kept alive indefinitely by a trickle.
    deadline = setTimeout(() => {
      done({ ok: false, refused: false })
      req?.destroy()
    }, Math.max(1, timeoutMs))
    try {
      req = http.get(
        { host: '127.0.0.1', port, path: '/health', agent: false },
        (res) => {
          let body = ''
          let bytes = 0
          res.setEncoding('utf8')
          res.on('data', (chunk) => {
            bytes += Buffer.byteLength(chunk)
            if (bytes > 65536) {
              done({ ok: false, refused: false })
              req.destroy()
              return
            }
            body += chunk
          })
          res.on('end', () => {
            try {
              done({ ok: res.statusCode === 200 && JSON.parse(body)?.service === service, refused: false })
            } catch {
              done({ ok: false, refused: false })
            }
          })
          res.on('error', () => done({ ok: false, refused: false }))
          res.on('aborted', () => done({ ok: false, refused: false }))
        },
      )
      req.on('error', (err) => done({ ok: false, refused: err?.code === 'ECONNREFUSED' }))
    } catch {
      done({ ok: false, refused: false })
    }
  })
}

/**
 * 問 /health：成功且 service 相符才算「我們的代理活著」。
 * 埠上沒有任何服務、或有別的 HTTP 服務，都回 false。
 * @param {number} port
 * @param {number} [timeoutMs]
 * @returns {Promise<boolean>}
 */
export async function health(port, timeoutMs = 2000, service = DEFAULTS.service) {
  return (await healthDetail(port, timeoutMs, service)).ok
}

/** 非破壞性的存活檢查（Node 的 signal 0 在 Windows 只查詢，不會終止行程）。 */
export function isAlive(pid) {
  if (!pid || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    return err?.code === 'EPERM'
  }
}

/**
 * 由 netstat 找出**所有**正在監聽該埠的 pid。
 * 不需要 powershell，也不需要信任任何 pid 檔。
 *
 * 為什麼是複數：Windows 的 SO_REUSEADDR 允許兩個 socket 綁同一個 address:port，
 * 而 Python 的 HTTPServer 預設 allow_reuse_address = 1。舊的「每 2 分鐘保活」
 * 排程工作就曾經製造出兩個同時 LISTENING 的代理行程；只殺第一個會留下孤兒。
 *
 * @param {number} port
 * @returns {number[]}
 */
export function listeningPids(port) {
  try {
    const out = spawnSync('netstat', ['-ano'], { encoding: 'utf8', windowsHide: true, timeout: 15000 })
    const text = String(out.stdout || '')
    const re = new RegExp(
      '^\\s*TCP\\s+(?:127\\.0\\.0\\.1|0\\.0\\.0\\.0|\\[::1\\]|\\[::\\]):' + port + '\\s+\\S+\\s+LISTENING\\s+(\\d+)',
      'gmi',
    )
    const pids = new Set()
    for (const match of text.matchAll(re)) pids.add(Number(match[1]))
    return [...pids]
  } catch {
    return []
  }
}

/**
 * 正在監聽該埠的第一個 pid（沿用既有行為）。
 * @param {number} port
 * @returns {number|null}
 */
export function listeningPid(port) {
  return listeningPids(port)[0] ?? null
}

/** 讀狀態檔（壞掉就當沒有）。 */
export function readState(file) {
  try {
    const value = pendingState.get(file)?.data ?? JSON.parse(fs.readFileSync(file, 'utf8'))
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null
  } catch {
    return null
  }
}

/** 寫狀態檔（失敗不影響主流程）。 */
export function writeState(file, data) {
  let temporary
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    temporary = `${file}.${randomUUID()}.tmp`
    fs.writeFileSync(temporary, JSON.stringify(data, null, 2) + '\n', 'utf8')
    fs.renameSync(temporary, file)
  } catch {
    /* 狀態檔只是輔助，寫不進去不該讓請求失敗 */
  } finally {
    if (temporary) {
      try { fs.rmSync(temporary, { force: true }) } catch { /* best effort */ }
    }
  }
}

/** 熱路徑狀態寫入的節流視窗：記憶體永遠最新，磁碟最多每 15 秒一次。 */
export const STATE_WRITE_MIN_INTERVAL_MS = 15000

/** 節流中的最新狀態（file → { at, data }）。 */
const pendingState = new Map()

/**
 * 純判斷：距離上次落盤是否已超過節流視窗。
 * @param {number|undefined} lastWriteAt
 * @param {number} [now]
 * @param {number} [minIntervalMs]
 * @returns {boolean}
 */
export function planStateWrite(lastWriteAt, now = Date.now(), minIntervalMs = STATE_WRITE_MIN_INTERVAL_MS) {
  if (!lastWriteAt) return true
  return now - lastWriteAt >= minIntervalMs
}

/**
 * 熱路徑用的寫入：每個請求都會更新 lastUsedAt，但沒必要每個請求都同步寫一次磁碟。
 * 這是純檔案 IO，不加鎖；最新的資料先留在 pendingState，逾時或 flush 時才落盤。
 * @param {string} file
 * @param {object} data
 * @param {number} [minIntervalMs]
 * @returns {boolean} 這次是否真的寫了磁碟
 */
export function writeStateThrottled(file, data, minIntervalMs = STATE_WRITE_MIN_INTERVAL_MS) {
  const now = Date.now()
  const prev = pendingState.get(file)
  if (!planStateWrite(prev?.at, now, minIntervalMs)) {
    prev.data = data
    return false
  }
  pendingState.set(file, { at: now, data })
  writeState(file, data)
  return true
}

/**
 * 立刻把節流中的最新狀態落盤（喚醒、停止、關閉時用）。
 * @param {string} file
 * @param {object} [data] 省略就用 pendingState 裡的最新資料
 * @returns {boolean}
 */
export function flushState(file, data) {
  const next = data ?? pendingState.get(file)?.data
  pendingState.delete(file)
  if (!next) return false
  writeState(file, next)
  return true
}

/** 刪狀態檔。 */
export function clearState(file) {
  pendingState.delete(file)
  try {
    fs.rmSync(file, { force: true })
  } catch {
    /* ignore */
  }
}

/** 連子行程一起砍（token 更新會開 headless Chrome，所以要 /T）。 */
export function killTree(pid) {
  try {
    const result = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: 20000,
    })
    return {
      ok: result.status === 0,
      output: String(result.stdout || '') + String(result.stderr || ''),
    }
  } catch (err) {
    return { ok: false, output: String(err?.message || err) }
  }
}

/**
 * 確保代理就緒：活著就沿用（順便記下 pid），否則冷啟動。
 *
 * 同一個埠的並行呼叫會共用同一個 promise（single-flight）：
 * 冷啟動時只 spawn 一支 python、只做一輪 health 探測。
 *
 * @param {object} [overrides]
 * @returns {Promise<{ok: boolean, started: boolean, pid: number|null}>}
 */
export function ensureProxy(overrides) {
  const cfg = cfgOf(overrides)
  const key = `um-proxy:${cfg.port}:${stopEpoch.get(cfg.port) || 0}`
  const existing = inflight.get(key)
  if (existing) return existing
  // Reserve the queue position now, before singleFlight's microtask runs.
  const operation = lifecycle(cfg.port, () => wakeProxy(cfg))
  return singleFlight(key, () => operation)
}

/**
 * 冷啟動 + 一次補救重試。
 *
 * 2026-10-22 實測到一次競態：`pythonw` 起來後埠有 LISTEN、但 `/health`
 * 立刻被關閉（proxy.log 甚至沒有 `start UM proxy` 那行），外掛等滿 20 秒
 * 就報失敗。這種情況重試前必須先殺掉卡住的 listener，否則第二次 spawn
 * 只會拿到 `Port already in use`。真正「別人的服務佔著埠」則不重試。
 *
 * @param {ReturnType<typeof cfgOf>} cfg
 * @returns {Promise<{ok: boolean, started: boolean, pid: number|null}>}
 */
async function wakeProxy(cfg) {
  try {
    return await ensureProxyOnce(cfg)
  } catch (err) {
    if (!isRecoverableWakeError(err)) throw err
    wakeRetries += 1
    await sleep(WAKE_RETRY_DELAY_MS)
    return ensureProxyOnce(cfg)
  }
}

/**
 * ensureProxy 的實際流程（已被 single-flight 包住）。
 * @param {ReturnType<typeof cfgOf>} cfg
 * @returns {Promise<{ok: boolean, started: boolean, pid: number|null}>}
 */
async function ensureProxyOnce(cfg) {
  const probed = await healthDetail(cfg.port, 2000, cfg.service)
  if (probed.ok) {
    const state = readState(cfg.stateFile) || {}
    // A live ChildProcess handle is safe to reuse; persisted PIDs are only hints.
    const owned = ownedChildren.get(cfg.port)
    const adopted = childAlive(owned) ? owned.pid : (await listenerPids(cfg.port))[0] ?? null
    writeStateThrottled(cfg.stateFile, {
      ...state,
      port: cfg.port,
      pid: adopted ?? null,
      lastUsedAt: Date.now(),
    })
    return { ok: true, started: false, pid: adopted ?? null }
  }

  // ECONNREFUSED = 埠上真的沒有人在聽，直接 spawn，不必再跑一次同步 netstat（~31ms）。
  // 只有「有回應但不是我們的代理」（外來服務、卡死的代理）才需要查是誰佔著埠。
  if (!probed.refused) {
    const squatter = (await listenerPids(cfg.port))[0]
    if (squatter) {
      throw new Error(
        `port ${cfg.port} is held by pid ${squatter} but /health is not the UM proxy; refusing to start`,
      )
    }
  }

  // 代理腳本不在外掛裡（它是 UM 校內發放的單檔工具），所以「找不到」是第一次安裝最常見的失敗。
  // 先自己檢查，才不會讓使用者只看到 python 的 can't open file 或一句 did not become healthy。
  if (!fs.existsSync(cfg.script)) {
    throw new Error(
      `proxy script not found: ${cfg.script} — this file is not part of the plugin; ` +
        'install the UM um_opencode_setup.py there, or point UM_OPENCODE_HOME / the settings page "script" field at it',
    )
  }

  const child = spawn(
    cfg.python,
    [
      cfg.script,
      'serve',
      '--port',
      String(cfg.port),
      '--ignore-console-events',
      '--log-file',
      cfg.logFile,
    ],
    {
      cwd: path.dirname(cfg.script),
      detached: true,
      windowsHide: true,
      stdio: 'ignore',
    },
  )
  let spawnError = null
  child.on('error', err => { spawnError = err })
  child.unref()
  ownedChildren.set(cfg.port, child)
  child.once('exit', () => {
    if (ownedChildren.get(cfg.port) === child) ownedChildren.delete(cfg.port)
  })
  const deadline = performance.now() + cfg.startupTimeoutMs
  try {
    for (let attempt = 0; performance.now() < deadline; attempt++) {
      if (spawnError) throw spawnError
      if (await health(cfg.port, Math.max(1, Math.min(1000, deadline - performance.now())), cfg.service)) {
        if (spawnError) throw spawnError
        const pid = childAlive(child) ? child.pid : (await listenerPids(cfg.port))[0] ?? null
        const now = Date.now()
        flushState(cfg.stateFile, { port: cfg.port, pid, startedAt: now, lastUsedAt: now })
        return { ok: true, started: true, pid }
      }
      if (spawnError) throw spawnError
      if (!childAlive(child)) throw new Error(`proxy exited before becoming healthy (code ${child.exitCode}, signal ${child.signalCode})`)
      await sleep(Math.max(0, Math.min(pollDelay(attempt), deadline - performance.now())))
    }
    if (spawnError) throw spawnError
    throw new Error(`proxy on port ${cfg.port} did not become healthy within ${cfg.startupTimeoutMs} ms`)
  } catch (err) {
    // This handle belongs to the exact child we spawned, never a PID from disk.
    if (childAlive(child)) {
      const result = await terminateProxyProcesses([child.pid], cfg).catch(() => null)
      if (!result?.owned.length) child.kill()
    }
    if (ownedChildren.get(cfg.port) === child) ownedChildren.delete(cfg.port)
    throw err
  }
}

const ownedChildren = new Map()
const lifecycleQueues = new Map()
const stopEpoch = new Map()
const childAlive = child => child && child.pid && child.exitCode === null && child.signalCode === null

/** Serialize starts and stops on each port without blocking the event loop. */
function lifecycle(port, operation) {
  const previous = lifecycleQueues.get(port) || Promise.resolve()
  const current = previous.catch(() => {}).then(operation)
  lifecycleQueues.set(port, current)
  const cleanup = () => { if (lifecycleQueues.get(port) === current) lifecycleQueues.delete(port) }
  current.then(cleanup, cleanup)
  return current
}

/**
 * 停掉代理。每個候選行程都要核對 Python、完整腳本路徑與埠號；
 * 無法確認所有權時保留行程並回報停止未完成，不退回舊 state.pid。
 * @param {object} [overrides]
 * @returns {Promise<{ok: boolean, stopped: boolean, reason?: string, pid?: number}>}
 */
export function stopProxy(overrides) {
  const cfg = cfgOf(overrides)
  stopEpoch.set(cfg.port, (stopEpoch.get(cfg.port) || 0) + 1)
  return lifecycle(cfg.port, () => stopProxyOnce(cfg))
}

async function stopProxyOnce(cfg) {
  const listeners = await listenerPids(cfg.port)
  const child = ownedChildren.get(cfg.port)
  const candidates = [...new Set([...listeners, ...(childAlive(child) ? [child.pid] : [])])]
  if (!candidates.length) {
    clearState(cfg.stateFile)
    ownedChildren.delete(cfg.port)
    return { ok: true, stopped: false, reason: 'not-running' }
  }
  // A service label alone cannot establish ownership of every listener.
  const result = await terminateProxyProcesses(candidates, cfg)
  if (!result.owned.length) return { ok: false, stopped: false, reason: 'ownership-unverified' }
  const deadline = performance.now() + 6000
  while (performance.now() < deadline) {
    const remaining = await proxyProcesses(result.owned, cfg)
    if (!remaining.length) {
      clearState(cfg.stateFile)
      ownedChildren.delete(cfg.port)
      return { ok: result.skipped.length === 0, stopped: true, pid: result.owned[0], pids: result.owned,
        ...(result.skipped.length ? { reason: 'foreign-listeners-preserved' } : {}) }
    }
    await sleep(100)
  }
  return { ok: false, stopped: false, reason: 'process-still-running', pids: result.owned }
}
