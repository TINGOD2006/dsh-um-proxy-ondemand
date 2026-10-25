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

/** 預設值（可用 cordis.patch.yml 的 config 覆寫）。 */
export const DEFAULTS = {
  /** GUI 子系統的 Python：本身不帶主控台，不會閃窗。 */
  python: 'C:\\Python314\\pythonw.exe',
  script: 'C:\\Users\\TINGOD\\.um-opencode\\um_opencode_setup.py',
  port: 8788,
  logFile: 'C:\\Users\\TINGOD\\.um-opencode\\proxy.log',
  /** 本外掛專屬的狀態檔（與 8787 的 proxy.pid 分開）。 */
  stateFile: 'C:\\Users\\TINGOD\\.um-opencode\\ondemand-8788.state.json',
  /** 冷啟動最多等多久。 */
  startupTimeoutMs: 20000,
  /** /health 回應裡代表「這是 UM 代理」的欄位值。 */
  service: 'um-opencode-proxy',
}

/** 合併預設值。 */
export function cfgOf(overrides) {
  return { ...DEFAULTS, ...(overrides || {}) }
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
export function healthDetail(port, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let settled = false
    const done = (value) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    const req = http.get(
      { host: '127.0.0.1', port, path: '/health', timeout: timeoutMs },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => {
          body += chunk
          if (body.length > 65536) req.destroy()
        })
        res.on('end', () => {
          try {
            done({ ok: JSON.parse(body)?.service === DEFAULTS.service, refused: false })
          } catch {
            done({ ok: false, refused: false })
          }
        })
        res.on('error', () => done({ ok: false, refused: false }))
      },
    )
    req.on('timeout', () => {
      req.destroy()
      done({ ok: false, refused: false })
    })
    req.on('error', (err) => done({ ok: false, refused: err?.code === 'ECONNREFUSED' }))
  })
}

/**
 * 問 /health：成功且 service 相符才算「我們的代理活著」。
 * 埠上沒有任何服務、或有別的 HTTP 服務，都回 false。
 * @param {number} port
 * @param {number} [timeoutMs]
 * @returns {Promise<boolean>}
 */
export async function health(port, timeoutMs = 2000) {
  return (await healthDetail(port, timeoutMs)).ok
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
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

/** 寫狀態檔（失敗不影響主流程）。 */
export function writeState(file, data) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, JSON.stringify(data, null, 2) + '\n', 'utf8')
  } catch {
    /* 狀態檔只是輔助，寫不進去不該讓請求失敗 */
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
  return singleFlight(`um-proxy:${cfg.port}`, () => wakeProxy(cfg))
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
    const stale = listeningPids(cfg.port)
    for (const pid of stale) killTree(pid)
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
  const probed = await healthDetail(cfg.port)
  if (probed.ok) {
    const state = readState(cfg.stateFile) || {}
    const adopted =
      isAlive(state.pid) && state.port === cfg.port ? state.pid : listeningPid(cfg.port)
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
    const squatter = listeningPid(cfg.port)
    if (squatter) {
      throw new Error(
        `port ${cfg.port} is held by pid ${squatter} but /health is not the UM proxy; refusing to start`,
      )
    }
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
  child.unref()

  const deadline = Date.now() + cfg.startupTimeoutMs
  for (let attempt = 0; Date.now() < deadline; attempt += 1) {
    if (await health(cfg.port, 1000)) {
      const pid = isAlive(child.pid) ? child.pid : listeningPid(cfg.port)
      const now = Date.now()
      flushState(cfg.stateFile, { port: cfg.port, pid: pid ?? null, startedAt: now, lastUsedAt: now })
      return { ok: true, started: true, pid: pid ?? null }
    }
    if (child.exitCode !== null) break
    await sleep(pollDelay(attempt))
  }
  throw new Error(`proxy on port ${cfg.port} did not become healthy within ${cfg.startupTimeoutMs} ms`)
}

/**
 * 停掉代理。只有在 /health 確認「這是 UM 代理」時才會動手，
 * 因此不可能誤殺佔用同一個埠的其他服務。
 * @param {object} [overrides]
 * @returns {Promise<{ok: boolean, stopped: boolean, reason?: string, pid?: number}>}
 */
export async function stopProxy(overrides) {
  const cfg = cfgOf(overrides)

  if (!(await health(cfg.port))) {
    clearState(cfg.stateFile)
    return { ok: true, stopped: false, reason: 'not-running' }
  }

  const state = readState(cfg.stateFile) || {}
  const listeners = listeningPids(cfg.port)
  const pids = []
  if (listeners.length > 0) {
    // 埠上還有人在聽：以 netstat 的實際監聽者為準。
    // 不要無條件把 state.pid 也加進來——Windows 會回收 pid，舊狀態檔裡的 pid
    // 可能已經被毫不相干的行程接手，殺掉它就是誤殺。
    for (const pid of listeners) if (!pids.includes(pid)) pids.push(pid)
  } else if (isAlive(state.pid) && state.port === cfg.port) {
    // netstat 查不到（權限/沙箱）時，state.pid 是唯一線索。
    pids.push(state.pid)
  }
  if (pids.length === 0) return { ok: false, stopped: false, reason: 'pid-unknown' }

  // 重複監聽時全部都要殺，否則會留下孤兒行程繼續佔著埠。
  for (const pid of pids) killTree(pid)

  const deadline = Date.now() + 6000
  while (Date.now() < deadline) {
    if (!(await health(cfg.port))) {
      clearState(cfg.stateFile)
      return { ok: true, stopped: true, pid: pids[0], pids }
    }
    await sleep(250)
  }
  return { ok: false, stopped: false, reason: 'still-healthy', pid: pids[0], pids }
}
