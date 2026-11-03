/**
 * dsh-um-proxy-ondemand —— 設定頁的宿主端。
 *
 * 三件事：
 *   1. 狀態快照（代理活著沒、統計、token 剩多少、金鑰設了沒、帳號存了沒）；
 *   2. 一般設定寫入 —— 走 `ctx.configEditor.edit()`，把值原子寫進 profile 的
 *      `cordis.patch.yml` 該列的 config 區塊，再由 Loader 熱重載；
 *   3. 帳密 —— `UM_GENAI_API_KEY` 走 DSH 的 credentials 幕後（`ctx.credentials`），
 *      UM 帳號密碼則原封不動交給外部工具 `um_opencode_setup.py set-credentials`
 *      （它自己用 DPAPI 加密存檔），DSH 這邊不落地、不回讀、不記錄。
 *
 * 與瀏覽器只走同源 HTTP（`/um-proxy-ondemand/api/*`），不碰 RPC：
 * 失敗模式最少，也沒有 connection 服務缺失的降級分支。
 *
 * 設計約束
 * --------
 * - `configEditor` / `credentials` 都可能沒掛載（例如測試用的 mock ctx）。
 *   缺席時回 503 並附說明，插件本體照常運作 —— apply() 不因此拋錯，
 *   所以 lib/index.js 的 inject 維持只有 ['webServer']。
 * - 這一支只碰「設定」相關的路徑，代理的啟動/停止完全不經手。
 *
 * @module dsh-um-proxy-ondemand/settings-api
 */

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { DEFAULTS, cfgOf, defaults } from './proxy.js'
import { homeDir } from './paths.js'

/** 設定頁用的 API 前綴（與唯讀的 /um-proxy-ondemand/state.json 並存，不衝突）。 */
export const API_PREFIX = '/um-proxy-ondemand/api'

/** loader 條目的 id（同時也是 profile patch 的那一列）。 */
const ENTRY_ID = 'dsh-um-proxy-ondemand'

/** DSH provider 用來指向本地代理的憑證引用名稱。 */
const KEY_REF = 'UM_GENAI_API_KEY'

/** 請求內文上限：設定頁送的都是小 JSON，超過就是有人在亂送。 */
const MAX_BODY_BYTES = 64 * 1024

/** 子行程輸出上限：只要錯誤原因的最後一段，不要把整份 log 拉回來。 */
const MAX_OUTPUT_BYTES = 8 * 1024

/** set-credentials 的最長等待。 */
const ACCOUNT_TIMEOUT_MS = 60_000

/** 模型清單同步的最長等待（要連 gateway 抓 /v1/models）。 */
const MODELS_SYNC_TIMEOUT_MS = 120_000

/**
 * 可以在設定頁編輯的欄位。
 *
 * 只列「改了有意義」的：`idleSeconds` 是測試用的短週期覆寫、`service`
 * 是 /health 的識別字串，兩者都不進 UI（前者會讓 15 分鐘的設定變得難以預期，
 * 後者改了代理就認不出來）。
 */
export const FIELD_SPECS = [
  { key: 'port', kind: 'integer', min: 1, max: 65535, label: '代理埠號', hint: '必須和 provider 的 baseURL 一致（預設 8788）' },
  { key: 'alwaysOn', kind: 'boolean', label: '常駐模式', hint: '開啟後不閒置關閉、宿主啟動時主動喚醒；代理掛掉由看門狗補位' },
  { key: 'idleMinutes', kind: 'integer', min: 0, max: 1440, label: '閒置關閉（分鐘）', hint: '關掉常駐後生效；0 也視為常駐' },
  { key: 'provider', kind: 'string', maxLength: 120, label: 'provider 名稱', hint: '留空＝任何名稱含 um-genai 的 provider' },
  { key: 'startupTimeoutMs', kind: 'integer', min: 1000, max: 2147483647, label: '冷啟動逾時（毫秒）', hint: '等代理 /health 通過的上限（預設 20000）' },
  { key: 'python', kind: 'string', maxLength: 512, label: 'Python 路徑', hint: '啟動代理用的解譯器（預設 pythonw.exe，不閃窗）' },
  { key: 'script', kind: 'string', maxLength: 512, label: '代理腳本', hint: 'um_opencode_setup.py 的完整路徑' },
  { key: 'logFile', kind: 'string', maxLength: 512, label: '代理日誌', hint: '' },
  { key: 'stateFile', kind: 'string', maxLength: 512, label: '狀態檔', hint: '本插件自己的 pid/最後使用時間' },
]

const FIELD_KEYS = FIELD_SPECS.map((spec) => spec.key)

/** 帶 HTTP 狀態碼的錯誤。 */
function httpError(status, message) {
  const err = new Error(message)
  err.status = status
  return err
}

/**
 * 取一個 cordis 服務；沒有就回 undefined（服務缺席是常態，不是錯誤）。
 * @param {object} ctx 宿主上下文。
 * @param {string} name 服務名。
 */
function serviceOf(ctx, name) {
  try {
    if (typeof ctx?.get === 'function') return ctx.get(name) ?? undefined
  } catch {
    /* 沒掛載時 cordis 會拋；這不是錯誤 */
  }
  return ctx?.[name] ?? undefined
}

/** 把 JSON 寫回去；handler 已經送過就不再送第二次。 */
function sendJson(res, status, payload) {
  if (!res || res.writableEnded) return
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  })
  res.end(JSON.stringify(payload))
}

/** 讀請求內文（有上限）。 */
function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    if (!req || typeof req.on !== 'function') {
      resolve({})
      return
    }
    const chunks = []
    let size = 0
    let done = false
    const fail = (err) => {
      if (done) return
      done = true
      reject(err)
    }
    req.on('data', (chunk) => {
      if (done) return
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        fail(httpError(413, '請求內容過大'))
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (done) return
      done = true
      const text = Buffer.concat(chunks).toString('utf8').trim()
      if (text === '') {
        resolve({})
        return
      }
      try {
        const parsed = JSON.parse(text)
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          reject(httpError(400, '請求必須是 JSON 物件'))
          return
        }
        resolve(parsed)
      } catch {
        reject(httpError(400, '請求不是合法 JSON'))
      }
    })
    req.on('error', (err) => fail(httpError(400, String((err && err.message) || err))))
  })
}

/** UM 工具的家目錄（與 scripts/um-renew.py 的規則是同一條）。 */
export function umHome() {
  return path.resolve(homeDir())
}

/**
 * 從 `upstream-token`（JWT）讀到期時間。只解 payload，不驗簽 ——
 * 這裡只是給使用者看「還剩多久」，不是安全判斷。
 */
export function tokenInfo(file) {
  let raw
  try {
    raw = fs.readFileSync(file, 'utf8').trim()
  } catch {
    return { present: false }
  }
  if (raw === '') return { present: false }
  const parts = raw.split('.')
  if (parts.length < 2) return { present: true, expiresAt: null }
  let payload
  try {
    payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'))
  } catch {
    return { present: true, expiresAt: null }
  }
  const exp = Number(payload?.exp)
  if (!Number.isFinite(exp) || exp <= 0) return { present: true, expiresAt: null }
  const expiresAt = Math.round(exp * 1000)
  return {
    present: true,
    expiresAt,
    remainingMs: expiresAt - Date.now(),
    expired: expiresAt <= Date.now(),
  }
}

/**
 * 需要 stdout/exit code 的指令要 python.exe 而不是 pythonw.exe
 * （pythonw 沒有主控台，管線行為不可靠）。
 */
export function pythonForCli(cfg) {
  const python = typeof cfg?.python === 'string' ? cfg.python : ''
  if (python === '') return 'python'
  return python.replace(/pythonw(\.exe)?$/i, (_match, ext) => 'python' + (ext || ''))
}

/** 把密碼從任何要回給瀏覽器/日誌的字串裡抹掉。 */
function redact(text, secret) {
  const value = String(text ?? '')
  if (typeof secret !== 'string' || secret === '') return value
  return value.split(secret).join('***')
}

/** 已知欄位、且值不是 undefined 的部分。 */
function pickKnown(source) {
  const out = {}
  if (!source || typeof source !== 'object') return out
  for (const key of FIELD_KEYS) {
    if (source[key] !== undefined) out[key] = source[key]
  }
  return out
}

/** 預設值（含 index.js 自己處理的兩個鍵）。 */
export function settingDefaults() {
  const base = defaults()
  return {
    port: base.port,
    alwaysOn: false,
    idleMinutes: 15,
    provider: '',
    startupTimeoutMs: base.startupTimeoutMs,
    python: base.python,
    script: base.script,
    logFile: base.logFile,
    stateFile: base.stateFile,
  }
}

/**
 * 找出本插件在 loader 裡的那個 Entry。
 * `ctx.fiber.entry` 是首選（就是自己），拿不到才回頭掃 configEditor.entries()。
 */
export function findEntry(ctx, editor) {
  const own = ctx?.fiber?.entry
  if (own && (own.options?.id === ENTRY_ID || own.options?.name === ENTRY_ID)) return own
  if (!editor || typeof editor.entries !== 'function') return null
  let entries = []
  try {
    entries = editor.entries() || []
  } catch {
    return null
  }
  return (
    entries.find((entry) => entry?.options?.id === ENTRY_ID) ??
    entries.find((entry) => entry?.options?.name === ENTRY_ID) ??
    null
  )
}

/**
 * 目前這一列的設定值與「哪些鍵是 profile patch 明確寫的」。
 * 讀不到 configEditor 時至少回有效的 cfg（插件自己手上的那份）。
 */
export function readSettings(ctx, options) {
  const current = { ...settingDefaults(), ...pickKnown(options?.cfg) }
  const result = { current, overridden: [], defaults: settingDefaults(), fields: FIELD_SPECS.map(publicField) }
  const editor = serviceOf(ctx, 'configEditor')
  if (!editor || typeof editor.configuration !== 'function') return result
  let rows = []
  try {
    rows = editor.configuration() || []
  } catch {
    return result
  }
  const entry = findEntry(ctx, editor)
  const row =
    rows.find((item) => item?.entry === entry) ??
    rows.find((item) => item?.entry?.options?.id === ENTRY_ID) ??
    rows.find((item) => item?.entry?.options?.name === ENTRY_ID)
  if (!row) return result
  const inherited = pickKnown(row.inherited)
  const override = pickKnown(row.override)
  result.current = { ...settingDefaults(), ...inherited, ...override }
  result.overridden = Object.keys(override)
  return result
}

/** 給瀏覽器的欄位描述（不含伺服器端的驗證實作）。 */
function publicField(spec) {
  const out = { key: spec.key, kind: spec.kind, label: spec.label, hint: spec.hint }
  if (spec.min !== undefined) out.min = spec.min
  if (spec.max !== undefined) out.max = spec.max
  if (spec.maxLength !== undefined) out.maxLength = spec.maxLength
  return out
}

/** 單一欄位的型別轉換與驗證。 */
function coerceField(spec, raw) {
  if (spec.kind === 'boolean') {
    if (typeof raw === 'boolean') return raw
    if (raw === 'true' || raw === 1 || raw === '1') return true
    if (raw === 'false' || raw === 0 || raw === '0') return false
    throw httpError(400, `${spec.key} 只能是 true 或 false`)
  }
  if (spec.kind === 'integer') {
    const value = typeof raw === 'number' ? raw : Number(String(raw).trim())
    if (!Number.isFinite(value) || !Number.isInteger(value)) {
      throw httpError(400, `${spec.key} 必須是整數`)
    }
    if (value < spec.min || value > spec.max) {
      throw httpError(400, `${spec.key} 必須介於 ${spec.min} 與 ${spec.max} 之間`)
    }
    return value
  }
  const value = String(raw).trim()
  if (value.length > spec.maxLength) throw httpError(400, `${spec.key} 太長（上限 ${spec.maxLength}）`)
  if (/[\r\n]/.test(value)) throw httpError(400, `${spec.key} 不能包含換行`)
  return value
}

/**
 * 把瀏覽器送來的 `{ values: {...} }` 變成 configEditor 要的 patch。
 * 空字串／null 代表「拿掉這個覆寫、回到預設」，所以值會是 undefined。
 */
export function parseSettingsPatch(body) {
  const values = body && typeof body.values === 'object' && body.values !== null ? body.values : body
  if (!values || typeof values !== 'object' || Array.isArray(values)) {
    throw httpError(400, '需要 { values: { ... } }')
  }
  const unknown = Object.keys(values).filter((key) => !FIELD_KEYS.includes(key))
  if (unknown.length > 0) throw httpError(400, `不認得的設定欄位：${unknown.join(', ')}`)
  const patch = {}
  for (const spec of FIELD_SPECS) {
    if (!Object.prototype.hasOwnProperty.call(values, spec.key)) continue
    const raw = values[spec.key]
    const blank = raw === null || raw === undefined || (typeof raw === 'string' && raw.trim() === '')
    if (blank) {
      if (spec.kind === 'boolean') throw httpError(400, `${spec.key} 只能是 true 或 false`)
      patch[spec.key] = undefined
      continue
    }
    patch[spec.key] = coerceField(spec, raw)
  }
  if (Object.keys(patch).length === 0) throw httpError(400, '沒有要儲存的欄位')
  return patch
}

/** 寫回 profile 的 cordis.patch.yml（再由 Loader 熱重載）。 */
export async function saveSettings(ctx, patch) {
  const editor = serviceOf(ctx, 'configEditor')
  if (!editor || typeof editor.edit !== 'function') {
    throw httpError(503, '宿主沒有提供 configEditor 服務，無法儲存設定')
  }
  const entry = findEntry(ctx, editor)
  if (!entry) throw httpError(503, `找不到插件條目 ${ENTRY_ID}，無法儲存設定`)
  await editor.edit(entry, (current) => {
    const next = { ...(current || {}) }
    for (const [key, value] of Object.entries(patch)) {
      if (value === undefined) delete next[key]
      else next[key] = value
    }
    return next
  })
}

/** DSH 內建的 llm provider 外掛；um-genai 這個 provider 必須掛在它底下。 */
export const LLM_ENTRY_NAME = '@deepseek-ai/dsh-llm-pi-ai'

/** 本外掛服務的 provider 名稱。 */
export const PROVIDER_ID = 'um-genai'

/** configEditor.documentPath 可能是字串或 getter。 */
function documentPathOf(editor) {
  if (!editor) return null
  let value
  try {
    value = typeof editor.documentPath === 'function' ? editor.documentPath() : editor.documentPath
  } catch {
    return null
  }
  return typeof value === 'string' && value !== '' ? value : null
}

/**
 * 找出 llm-pi-ai 的 loader 條目。
 * 它來自 DSH 內建的 bundle，所以即使 profile patch 沒寫過也找得到。
 */
export function findLlmEntry(ctx) {
  const editor = serviceOf(ctx, 'configEditor')
  if (!editor || typeof editor.entries !== 'function') return null
  let entries = []
  try {
    entries = editor.entries() || []
  } catch {
    return null
  }
  return (
    entries.find((entry) => entry?.options?.name === LLM_ENTRY_NAME) ??
    entries.find((entry) => entry?.options?.id === 'llm-pi-ai') ??
    null
  )
}

/** 建立 provider 時寫入的預設值；baseURL 跟著代理埠號走。 */
export function providerDefaults(cfg) {
  const port = Number.isInteger(cfg?.port) ? cfg.port : DEFAULTS.port
  return {
    displayName: 'UM GenAI (local proxy)',
    apiKeyEnv: KEY_REF,
    api: 'openai-completions',
    baseURL: `http://127.0.0.1:${port}/v1`,
    models: [],
  }
}

/**
 * 目前 profile patch 裡的 um-genai provider 狀態（給設定頁顯示）。
 * 「有沒有設定」看的是 override 層，因為 provider 只能由使用者（或這個按鈕）寫進去。
 */
export function readProvider(ctx) {
  const result = { entry: false, configured: false, models: 0, baseURL: null }
  const editor = serviceOf(ctx, 'configEditor')
  if (!editor || typeof editor.configuration !== 'function') return result
  const entry = findLlmEntry(ctx)
  result.entry = Boolean(entry)
  if (!entry) return result
  let rows = []
  try {
    rows = editor.configuration() || []
  } catch {
    return result
  }
  const row = rows.find((item) => item?.entry === entry)
  // 只看 override 層：provider 只能由使用者（或這顆按鈕）寫進 profile patch，
  // 內建 bundle 不會提供 um-genai，所以 inherited 有值也不算「已建立」。
  const config = { ...(row?.override || {}) }
  const provider = config?.providers?.[PROVIDER_ID]
  if (provider && typeof provider === 'object' && !Array.isArray(provider)) {
    result.configured = true
    result.models = Array.isArray(provider.models) ? provider.models.length : 0
    result.baseURL = typeof provider.baseURL === 'string' ? provider.baseURL : null
  }
  return result
}

/**
 * 把 um-genai provider 寫進 profile patch。
 * 已經有就只補缺的鍵，不覆寫使用者自己調過的值。
 */
export async function ensureProvider(ctx, cfg) {
  const editor = serviceOf(ctx, 'configEditor')
  if (!editor || typeof editor.edit !== 'function') {
    throw httpError(503, '宿主沒有提供 configEditor 服務，無法建立 provider')
  }
  const entry = findLlmEntry(ctx)
  if (!entry) {
    throw httpError(
      503,
      `找不到 ${LLM_ENTRY_NAME} 條目（DSH 內建的 llm provider 外掛），無法建立 ${PROVIDER_ID} provider`,
    )
  }
  let created = false
  await editor.edit(entry, (current) => {
    const next = { ...(current || {}) }
    const providers = { ...(next.providers || {}) }
    const existing = providers[PROVIDER_ID]
    if (existing && typeof existing === 'object' && !Array.isArray(existing)) {
      providers[PROVIDER_ID] = { ...providerDefaults(cfg), ...existing }
    } else {
      providers[PROVIDER_ID] = providerDefaults(cfg)
      created = true
    }
    next.providers = providers
    return next
  })
  return { created }
}

/**
 * 把 um-genai provider 從 profile patch 移除（建立失敗時的回滾）。
 * 留著一個沒有 models 的 provider 會讓 DSH 載入時直接報錯，比完全沒有更糟。
 */
export async function removeProvider(ctx) {
  const editor = serviceOf(ctx, 'configEditor')
  if (!editor || typeof editor.edit !== 'function') return false
  const entry = findLlmEntry(ctx)
  if (!entry) return false
  let removed = false
  await editor.edit(entry, (current) => {
    const next = { ...(current || {}) }
    const providers = { ...(next.providers || {}) }
    if (providers[PROVIDER_ID] !== undefined) {
      delete providers[PROVIDER_ID]
      removed = true
    }
    if (Object.keys(providers).length === 0) delete next.providers
    else next.providers = providers
    return next
  })
  return removed
}

/** 外掛自帶的模型清單同步腳本。 */
export function modelsSyncScript() {
  return path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'scripts', 'um-models-sync.py')
}

/**
 * 把 `um-models-sync.py` 的結束碼翻成結果。
 * 它的結束碼不是單純的 0/1：0 = 本來就同步，20 = 有改寫並已套用，
 * 10 = 有落差但沒給 --apply，5 = 驗證失敗，4 = 環境錯誤。
 */
export function modelsSyncOutcome(code, out, err) {
  if (code === 0 || code === 20) {
    return { ok: true, changed: code === 20, output: String(out || '').trim() }
  }
  throw httpError(502, String(err || out || '').trim() || `模型同步失敗（exit ${code}）`)
}

/**
 * 跑 `um-models-sync.py --apply`，把 gateway 目前提供的模型 id 寫進 profile patch。
 * 代理沒開或還沒登入時會失敗，錯誤訊息直接回給設定頁。
 */
export function runModelsSync(cfg, profilePath, timeoutMs = MODELS_SYNC_TIMEOUT_MS) {
  const script = modelsSyncScript()
  if (!fs.existsSync(script)) {
    return Promise.reject(httpError(500, `找不到模型同步腳本：${script}`))
  }
  const python = pythonForCli(cfg)
  const port = Number.isInteger(cfg?.port) ? cfg.port : DEFAULTS.port
  const args = [script, '--apply', '--target', 'dsh', '--base-url', `http://127.0.0.1:${port}/v1`]
  if (profilePath) args.push('--profile', profilePath)
  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawn(python, args, { windowsHide: true })
    } catch (err) {
      reject(httpError(500, `無法啟動 ${python}：${String((err && err.message) || err)}`))
      return
    }
    let out = ''
    let err = ''
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 已經結束 */
      }
      reject(httpError(504, `模型同步逾時（${Math.round(timeoutMs / 1000)} 秒）`))
    }, timeoutMs)
    child.stdout?.on('data', (chunk) => {
      if (out.length < MAX_OUTPUT_BYTES) out += chunk
    })
    child.stderr?.on('data', (chunk) => {
      if (err.length < MAX_OUTPUT_BYTES) err += chunk
    })
    child.on('error', (error) => {
      clearTimeout(timer)
      reject(httpError(500, `無法啟動 ${python}：${String((error && error.message) || error)}`))
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      try {
        resolve(modelsSyncOutcome(code, out, err))
      } catch (error) {
        reject(error)
      }
    })
  })
}

/**
 * 呼叫外部工具的 `set-credentials --stdin`：`username<TAB>password` 從 stdin 進去。
 * 密碼全程不進 argv（不會出現在任何 process 清單或日誌裡）。
 */
export function saveAccountCredentials(cfg, credentials, timeoutMs = ACCOUNT_TIMEOUT_MS) {
  // 先驗請求本身（使用者能改的），再驗環境（使用者可能要先去裝東西）。
  // 順序反過來的話，一台還沒裝代理腳本的機器上「密碼沒填」會回 500，
  // 使用者就看不到真正該修的那個欄位。
  const username = String(credentials?.username ?? '')
  const password = String(credentials?.password ?? '')
  if (username.trim() === '') return Promise.reject(httpError(400, '帳號不能是空的'))
  if (password === '') return Promise.reject(httpError(400, '密碼不能是空的'))
  if (/[\t\r\n]/.test(username)) return Promise.reject(httpError(400, '帳號不能包含定位或換行字元'))
  if (/[\r\n]/.test(password)) return Promise.reject(httpError(400, '密碼不能包含換行字元'))

  const script = typeof cfg?.script === 'string' ? cfg.script : ''
  if (script === '') return Promise.reject(httpError(500, '沒有設定代理腳本路徑'))
  if (!fs.existsSync(script)) return Promise.reject(httpError(500, `找不到腳本：${script}`))

  const python = pythonForCli(cfg)
  return new Promise((resolve, reject) => {
    let settled = false
    let timer = null
    const finish = (fn, arg) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      fn(arg)
    }
    let child
    try {
      child = spawn(python, ['-B', script, 'set-credentials', '--stdin'], {
        cwd: path.dirname(script),
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      })
    } catch (err) {
      finish(reject, httpError(500, `無法啟動 ${python}：${(err && err.message) || err}`))
      return
    }
    let stdout = ''
    let stderr = ''
    timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 已經結束就沒差 */
      }
      finish(reject, httpError(504, '設定帳密逾時'))
    }, timeoutMs)
    if (typeof timer.unref === 'function') timer.unref()

    child.stdout?.on('data', (chunk) => {
      if (stdout.length < MAX_OUTPUT_BYTES) stdout += chunk.toString('utf8')
    })
    child.stderr?.on('data', (chunk) => {
      if (stderr.length < MAX_OUTPUT_BYTES) stderr += chunk.toString('utf8')
    })
    child.on('error', (err) => {
      finish(reject, httpError(500, `無法執行 ${python}：${(err && err.message) || err}`))
    })
    child.on('close', (code) => {
      if (code === 0) {
        finish(resolve, { ok: true })
        return
      }
      const detail = redact(stderr || stdout, password).trim().slice(-400)
      finish(reject, httpError(500, `set-credentials 失敗（exit ${code}）${detail ? '：' + detail : ''}`))
    })
    // 子行程提早關掉 stdin 不該讓整個請求炸掉
    child.stdin?.on('error', () => {})
    child.stdin?.end(`${username}\t${password}\n`, 'utf8')
  })
}

/** 換掉 `UM_GENAI_API_KEY`（空值一律拒絕：要清掉請走 unset，這裡不做）。 */
export async function saveApiKey(ctx, value) {
  const credentials = serviceOf(ctx, 'credentials')
  if (!credentials || typeof credentials.set !== 'function') {
    throw httpError(503, '宿主沒有提供 credentials 服務，無法儲存金鑰')
  }
  const key = typeof value === 'string' ? value.trim() : ''
  if (key === '') throw httpError(400, '金鑰不能是空的')
  if (key.length > 4096) throw httpError(400, '金鑰太長')
  if (typeof credentials.describe === 'function') {
    const info = await credentials.describe(KEY_REF)
    if (info && info.writable === false) {
      throw httpError(409, `${KEY_REF} 由啟動環境的環境變數提供，在這裡寫不進去；請先清掉那個變數`)
    }
  }
  await credentials.set(KEY_REF, key)
  return { ok: true, ref: KEY_REF }
}

/** 蒐集狀態頁要的全部資料。 */
export async function collectStatus(ctx, options) {
  const cfg = options?.cfg || {}
  const home = options?.home || umHome()
  const status = typeof options?.snapshot === 'function' ? await options.snapshot() : {}

  const credentials = serviceOf(ctx, 'credentials')
  let key = { available: Boolean(credentials), configured: false, writable: false }
  if (credentials && typeof credentials.describe === 'function') {
    try {
      const info = await credentials.describe(KEY_REF)
      key = {
        available: true,
        ref: KEY_REF,
        configured: Boolean(info?.configured),
        writable: Boolean(info?.writable),
        source: info?.source ?? null,
      }
    } catch (err) {
      key = { available: true, ref: KEY_REF, configured: false, writable: false, error: String((err && err.message) || err) }
    }
  }

  const accountFile = path.join(home, 'credentials.dat')
  let accountConfigured = false
  try {
    accountConfigured = fs.statSync(accountFile).isFile()
  } catch {
    accountConfigured = false
  }

  // 代理腳本不在外掛裡（它是 UM 校內工具），所以「找不到」是第一次安裝最常見的失敗。
  // 把解析後的路徑與存在與否一起回給設定頁，讓狀態區能直接說清楚要補什麼。
  const resolved = cfgOf(cfg)
  let scriptExists = false
  try {
    scriptExists = fs.statSync(resolved.script).isFile()
  } catch {
    scriptExists = false
  }

  let settings
  try {
    settings = readSettings(ctx, options)
  } catch (err) {
    settings = {
      current: { ...settingDefaults(), ...pickKnown(cfg) },
      overridden: [],
      defaults: settingDefaults(),
      fields: FIELD_SPECS.map(publicField),
      error: String((err && err.message) || err),
    }
  }

  return {
    ok: true,
    widget: ENTRY_ID,
    host: { api: API_PREFIX, home, script: { path: resolved.script, exists: scriptExists } },
    status,
    key,
    account: { configured: accountConfigured, path: accountFile },
    token: tokenInfo(path.join(home, 'upstream-token')),
    provider: readProvider(ctx),
    settings,
  }
}

/** 路由表：method + path → 處理函式。 */
async function dispatch(ctx, options, route, method, req, res) {
  if (route === '/status' && method === 'GET') {
    sendJson(res, 200, await collectStatus(ctx, options))
    return
  }
  if (route === '/settings' && method === 'GET') {
    sendJson(res, 200, { ok: true, ...readSettings(ctx, options) })
    return
  }
  if (route === '/settings' && method === 'POST') {
    const patch = parseSettingsPatch(await readJsonBody(req))
    await saveSettings(ctx, patch)
    sendJson(res, 200, { ok: true, restarting: true, saved: Object.keys(patch) })
    return
  }
  if (route === '/account' && method === 'POST') {
    const body = await readJsonBody(req)
    const result = await saveAccountCredentials(options?.cfg || {}, {
      username: body.username,
      password: body.password,
    })
    sendJson(res, 200, { ok: result.ok, ref: 'credentials.dat' })
    return
  }
  if (route === '/api-key' && method === 'POST') {
    const body = await readJsonBody(req)
    sendJson(res, 200, await saveApiKey(ctx, body.value))
    return
  }
  if (route === '/provider' && method === 'POST') {
    const cfg = options?.cfg || {}
    const ensured = await ensureProvider(ctx, cfg)
    // provider 只是半套：沒有 models 清單，模型選單還是不會出現東西。
    // 這一步會連本機代理（還沒開就自己醒）抓 gateway 的模型 id 再寫回同一份 patch。
    const sync = typeof options?.syncModels === 'function' ? options.syncModels : runModelsSync
    let models
    try {
      models = await sync(cfg, documentPathOf(serviceOf(ctx, 'configEditor')))
    } catch (err) {
      models = { ok: false, error: String((err && err.message) || err) }
    }
    // 剛建立卻拿不到模型清單時回滾：留著空 models 的 provider 會讓 DSH 載入失敗，
    // 使用者會卡在「外掛壞了」而不是「先去登入 UM 帳號」。
    let rolledBack = false
    if (ensured.created && !models.ok) {
      try {
        rolledBack = await removeProvider(ctx)
      } catch {
        rolledBack = false
      }
    }
    sendJson(res, 200, {
      ok: true,
      created: ensured.created,
      rolledBack,
      provider: readProvider(ctx),
      models,
      restarting: true,
    })
    return
  }
  const known = ['/status', '/settings', '/account', '/api-key', '/provider']
  if (known.includes(route)) throw httpError(405, `${route} 不支援 ${method}`)
  throw httpError(404, `沒有這個端點：${route}`)
}

/**
 * 註冊設定頁的 API。
 *
 * @param {object} ctx 宿主上下文（需要 webServer；configEditor / credentials 可有可無）。
 * @param {object} options
 * @param {object} options.cfg 目前的合併設定（lib/proxy.js 的 cfgOf 結果）。
 * @param {Function} options.snapshot 回傳插件內部狀態的 async 函式。
 * @param {string} [options.home] UM 工具家目錄（測試用覆寫）。
 * @returns {Function} disposer
 */
export function registerSettingsApi(ctx, options) {
  const handler = async (req, res) => {
    const url = typeof req?.url === 'string' ? req.url : ''
    const route = (url.split('?')[0] || '').slice(API_PREFIX.length) || '/'
    const method = String(req?.method || 'GET').toUpperCase()
    try {
      await dispatch(ctx, options, route, method, req, res)
    } catch (err) {
      const status = Number.isInteger(err?.status) ? err.status : 500
      if (status >= 500) {
        try {
          ctx.logger?.warn?.(`${ENTRY_ID}: settings api ${method} ${route} failed: %s`, String((err && err.message) || err))
        } catch {
          /* 記錄失敗不影響回應 */
        }
      }
      sendJson(res, status, { ok: false, error: String((err && err.message) || err) })
    }
  }
  const dispose = ctx.webServer.register({ kind: 'prefix', path: API_PREFIX, handler })
  return typeof dispose === 'function' ? dispose : () => {}
}
