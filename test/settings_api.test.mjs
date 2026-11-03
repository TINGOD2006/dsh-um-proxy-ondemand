/**
 * 設定頁宿主端（lib/settings-api.js）的單元測試。
 *
 * 這裡全部用假的 ctx：`webServer.register` 收下路由、`configEditor` /
 * `credentials` 由測試自己捏出來，所以不需要真的 DSH、也不會碰到 profile。
 * 唯一會碰檔案系統的是 token 解析那兩條，若沙箱不給寫就自動略過。
 *
 * 用法： node --test test/settings_api.test.mjs
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Readable } from 'node:stream'

import {
  API_PREFIX,
  FIELD_SPECS,
  findEntry,
  parseSettingsPatch,
  readSettings,
  pythonForCli,
  registerSettingsApi,
  settingDefaults,
  tokenInfo,
} from '../lib/settings-api.js'

const ENTRY_ID = 'dsh-um-proxy-ondemand'

/** 假請求：把 JSON 內文變成可讀串流。 */
function fakeRequest(method, url, body) {
  const chunks = body === undefined ? [] : [Buffer.from(JSON.stringify(body), 'utf8')]
  const req = Readable.from(chunks)
  req.method = method
  req.url = url
  return req
}

/** 假回應：只記下狀態碼與 JSON 內容。 */
function fakeResponse() {
  return {
    status: null,
    headers: null,
    body: null,
    writableEnded: false,
    writeHead(status, headers) {
      this.status = status
      this.headers = headers
    },
    end(body) {
      this.body = body ? JSON.parse(body) : null
      this.writableEnded = true
    },
  }
}

/** 假 loader 條目。 */
function fakeEntry(id = ENTRY_ID) {
  return { options: { id, name: id }, fiber: null, ctx: null }
}

/** 假 configEditor。 */
function fakeEditor(options = {}) {
  const calls = []
  const entry = options.entry ?? fakeEntry()
  const rows = options.rows ?? [{ entry, inherited: { port: 8788, alwaysOn: true }, override: {} }]
  return {
    calls,
    entry,
    entries: () => (options.noEntries ? [] : [entry]),
    configuration: () => rows,
    edit: async (target, change) => {
      calls.push({ target, next: change(options.base ?? { port: 8788, alwaysOn: true }, {}) })
    },
  }
}

/** 假 credentials 服務。 */
function fakeCredentials(describeResult = { configured: true, writable: true, source: 'store' }) {
  const sets = []
  return {
    sets,
    describe: async () => describeResult,
    set: async (ref, value) => {
      sets.push([ref, value])
    },
  }
}

/** 掛上 API，回傳可直接呼叫的路由。 */
function mount(ctx, options) {
  const registered = []
  ctx.webServer = {
    register: (route) => {
      registered.push(route)
      return () => {}
    },
  }
  const dispose = registerSettingsApi(ctx, options)
  return { route: registered[0], dispose, registered }
}

/** 呼叫一次 API。 */
async function call(route, method, subPath, body) {
  const res = fakeResponse()
  await route.handler(fakeRequest(method, API_PREFIX + subPath, body), res)
  await new Promise((resolve) => setImmediate(resolve))
  return res
}

// 刻意不用任何真實路徑：這些測試全部走假的 ctx，不該假設機器上裝了什麼。
const fakeHome = path.join(os.tmpdir(), 'um-test-home')
const baseCfg = {
  port: 8788,
  python: path.join(fakeHome, 'pythonw.exe'),
  script: path.join(fakeHome, 'um_opencode_setup.py'),
  logFile: path.join(fakeHome, 'proxy.log'),
  stateFile: path.join(fakeHome, 'ondemand-8788.state.json'),
  startupTimeoutMs: 20000,
}

const baseOptions = {
  cfg: { ...baseCfg, alwaysOn: true },
  snapshot: async () => ({ ok: true, running: true, pid: 4242, port: 8788, stats: { requests: 3 } }),
  home: path.join(os.tmpdir(), 'um-home-does-not-exist'),
}

// ── 狀態 ──────────────────────────────────────────────────────────────────

test('GET /status 在沒有 configEditor / credentials 時仍然回得出東西', async () => {
  const ctx = { logger: { warn() {} } }
  const { route } = mount(ctx, baseOptions)
  const res = await call(route, 'GET', '/status')

  assert.equal(res.status, 200)
  assert.equal(res.body.ok, true)
  assert.equal(res.body.widget, ENTRY_ID)
  assert.equal(res.body.host.api, API_PREFIX)
  assert.equal(res.body.status.running, true)
  assert.equal(res.body.status.pid, 4242)
  // 服務缺席要老實說，而不是假裝已設定
  assert.equal(res.body.key.available, false)
  assert.equal(res.body.key.configured, false)
  assert.equal(res.body.account.configured, false)
  assert.equal(res.body.token.present, false)
  assert.equal(res.body.settings.current.port, 8788)
  assert.equal(res.body.settings.current.alwaysOn, true)
  assert.equal(res.body.settings.fields.length, FIELD_SPECS.length)
})

test('GET /status 帶 credentials 時回報已設定與可寫狀態', async () => {
  const credentials = fakeCredentials({ configured: true, writable: false, source: 'environment' })
  const { route } = mount({ credentials }, baseOptions)
  const res = await call(route, 'GET', '/status')

  assert.equal(res.status, 200)
  assert.equal(res.body.key.available, true)
  assert.equal(res.body.key.configured, true)
  assert.equal(res.body.key.writable, false)
  assert.equal(res.body.key.source, 'environment')
})

test('GET /status 會把 profile patch 的覆寫值疊上去並標記哪些是自訂的', async () => {
  const editor = fakeEditor({
    rows: [{ entry: fakeEntry(), inherited: { port: 8788 }, override: { idleMinutes: 5 } }],
  })
  const { route } = mount({ configEditor: editor }, baseOptions)
  const res = await call(route, 'GET', '/status')

  assert.equal(res.body.settings.current.port, 8788)
  assert.equal(res.body.settings.current.idleMinutes, 5)
  assert.deepEqual(res.body.settings.overridden, ['idleMinutes'])
})

// ── 一般設定 ──────────────────────────────────────────────────────────────

test('GET /settings 回傳目前值、預設值與欄位表', async () => {
  const editor = fakeEditor()
  const { route } = mount({ configEditor: editor }, baseOptions)
  const res = await call(route, 'GET', '/settings')

  assert.equal(res.status, 200)
  assert.equal(res.body.current.port, 8788)
  assert.deepEqual(res.body.defaults, settingDefaults())
  assert.deepEqual(
    res.body.fields.map((field) => field.key),
    FIELD_SPECS.map((spec) => spec.key),
  )
})

test('POST /settings 只送出變更的鍵，空白字串代表刪掉覆寫', async () => {
  const editor = fakeEditor()
  const { route } = mount({ configEditor: editor }, baseOptions)
  const res = await call(route, 'POST', '/settings', { values: { idleMinutes: 5, provider: '' } })

  assert.equal(res.status, 200)
  assert.equal(res.body.ok, true)
  assert.equal(editor.calls.length, 1)
  const next = editor.calls[0].next
  assert.equal(next.idleMinutes, 5)
  assert.equal('provider' in next, false)
  assert.deepEqual(res.body.saved.sort(), ['idleMinutes', 'provider'])
})

test('POST /settings 型別或範圍不對就 400，且完全不碰 editor', async () => {
  const editor = fakeEditor()
  const { route } = mount({ configEditor: editor }, baseOptions)

  const bad = await call(route, 'POST', '/settings', { values: { port: 70000 } })
  assert.equal(bad.status, 400)
  assert.match(bad.body.error, /port/)

  const notInteger = await call(route, 'POST', '/settings', { values: { port: '8O88' } })
  assert.equal(notInteger.status, 400)

  const unknown = await call(route, 'POST', '/settings', { values: { nope: 1 } })
  assert.equal(unknown.status, 400)
  assert.match(unknown.body.error, /不認得/)

  const empty = await call(route, 'POST', '/settings', { values: {} })
  assert.equal(empty.status, 400)

  assert.equal(editor.calls.length, 0)
})

test('POST /settings 在沒有 configEditor 時回 503 而不是炸掉', async () => {
  const { route } = mount({}, baseOptions)
  const res = await call(route, 'POST', '/settings', { values: { idleMinutes: 5 } })

  assert.equal(res.status, 503)
  assert.equal(res.body.ok, false)
  assert.match(res.body.error, /configEditor/)
})

test('POST /settings 找不到條目時回 503', async () => {
  const editor = fakeEditor({ noEntries: true })
  const { route } = mount({ configEditor: editor }, baseOptions)
  const res = await call(route, 'POST', '/settings', { values: { idleMinutes: 5 } })

  assert.equal(res.status, 503)
  assert.equal(editor.calls.length, 0)
})

// ── 帳密 ──────────────────────────────────────────────────────────────────

test('POST /account 缺密碼就 400，不會去 spawn 子行程', async () => {
  const { route } = mount({}, baseOptions)
  const res = await call(route, 'POST', '/account', { username: 'someone' })

  assert.equal(res.status, 400)
  assert.match(res.body.error, /密碼/)
})

test('POST /account 帳號含定位字元會被擋下', async () => {
  const { route } = mount({}, baseOptions)
  const res = await call(route, 'POST', '/account', { username: 'a\tb', password: 'secret' })

  assert.equal(res.status, 400)
})

test('POST /account 找不到腳本時回 500 並說明是哪個路徑', async () => {
  const options = { ...baseOptions, cfg: { ...baseOptions.cfg, script: path.join(os.tmpdir(), 'no-such-setup.py') } }
  const { route } = mount({}, options)
  const res = await call(route, 'POST', '/account', { username: 'someone', password: 'secret' })

  assert.equal(res.status, 500)
  assert.match(res.body.error, /找不到腳本/)
})

// ── 金鑰 ──────────────────────────────────────────────────────────────────

test('POST /api-key 會寫進 credentials', async () => {
  const credentials = fakeCredentials({ configured: false, writable: true })
  const { route } = mount({ credentials }, baseOptions)
  const res = await call(route, 'POST', '/api-key', { value: '  abc123  ' })

  assert.equal(res.status, 200)
  assert.equal(res.body.ok, true)
  assert.deepEqual(credentials.sets, [['UM_GENAI_API_KEY', 'abc123']])
})

test('POST /api-key 空值 400、唯讀 409、無服務 503', async () => {
  const writable = fakeCredentials({ configured: false, writable: true })
  const a = mount({ credentials: writable }, baseOptions)
  const empty = await call(a.route, 'POST', '/api-key', { value: '   ' })
  assert.equal(empty.status, 400)
  assert.equal(writable.sets.length, 0)

  const readonly = fakeCredentials({ configured: true, writable: false, source: 'environment' })
  const b = mount({ credentials: readonly }, baseOptions)
  const conflict = await call(b.route, 'POST', '/api-key', { value: 'abc123' })
  assert.equal(conflict.status, 409)
  assert.match(conflict.body.error, /UM_GENAI_API_KEY/)
  assert.equal(readonly.sets.length, 0)

  const c = mount({}, baseOptions)
  const missing = await call(c.route, 'POST', '/api-key', { value: 'abc123' })
  assert.equal(missing.status, 503)
})

// ── 路由與錯誤 ────────────────────────────────────────────────────────────

test('不認得的路徑 404，認得但方法不對 405', async () => {
  const { route } = mount({}, baseOptions)

  const missing = await call(route, 'GET', '/nope')
  assert.equal(missing.status, 404)

  const wrongMethod = await call(route, 'GET', '/account')
  assert.equal(wrongMethod.status, 405)
})

test('壞掉的 JSON 回 400 而不是讓 handler 拋錯', async () => {
  const { route } = mount({}, baseOptions)
  const res = fakeResponse()
  const req = Readable.from([Buffer.from('{not json', 'utf8')])
  req.method = 'POST'
  req.url = API_PREFIX + '/settings'
  await route.handler(req, res)
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(res.status, 400)
  assert.equal(res.body.ok, false)
})

test('超大內文回 413', async () => {
  const { route } = mount({}, baseOptions)
  const res = fakeResponse()
  const huge = Buffer.alloc(70 * 1024, 0x61)
  const req = Readable.from([huge])
  req.method = 'POST'
  req.url = API_PREFIX + '/settings'
  await route.handler(req, res)
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(res.status, 413)
})

test('registerSettingsApi 回傳可呼叫的 disposer，且只註冊一條前綴路由', async () => {
  const ctx = {}
  const { route, dispose, registered } = mount(ctx, baseOptions)

  assert.equal(registered.length, 1)
  assert.equal(route.kind, 'prefix')
  assert.equal(route.path, API_PREFIX)
  assert.equal(typeof dispose, 'function')
  assert.doesNotThrow(() => dispose())
})

// ── 純函式 ────────────────────────────────────────────────────────────────

test('parseSettingsPatch：布林值接受 true/false 與字串形式', () => {
  assert.deepEqual(parseSettingsPatch({ values: { alwaysOn: false } }), { alwaysOn: false })
  assert.deepEqual(parseSettingsPatch({ values: { alwaysOn: 'true' } }), { alwaysOn: true })
  assert.throws(() => parseSettingsPatch({ values: { alwaysOn: 'maybe' } }), /只能是 true 或 false/)
})

test('parseSettingsPatch：整數欄位拒絕小數與非數字，字串欄位拒絕換行', () => {
  assert.throws(() => parseSettingsPatch({ values: { idleMinutes: 1.5 } }), /整數/)
  assert.throws(() => parseSettingsPatch({ values: { idleMinutes: 'abc' } }), /整數/)
  assert.throws(() => parseSettingsPatch({ values: { provider: 'a\nb' } }), /換行/)
  assert.deepEqual(parseSettingsPatch({ values: { idleMinutes: 0 } }), { idleMinutes: 0 })
})

test('parseSettingsPatch：空白字串＝刪掉覆寫（值是 undefined）', () => {
  const patch = parseSettingsPatch({ values: { python: '   ' } })
  assert.equal('python' in patch, true)
  assert.equal(patch.python, undefined)
})

test('pythonForCli 把 pythonw.exe 換成 python.exe，其餘原樣', () => {
  assert.equal(pythonForCli({ python: 'C:\\Tools\\Python312\\pythonw.exe' }), 'C:\\Tools\\Python312\\python.exe')
  assert.equal(pythonForCli({ python: 'C:\\Tools\\Python312\\python.exe' }), 'C:\\Tools\\Python312\\python.exe')
  assert.equal(pythonForCli({ python: '/usr/bin/python3' }), '/usr/bin/python3')
  assert.equal(pythonForCli({}), 'python')
})

test('readSettings / findEntry 在完全沒有服務時給出預設值', () => {
  const settings = readSettings({}, { cfg: { port: 9000 } })
  assert.equal(settings.current.port, 9000)
  assert.equal(settings.current.alwaysOn, false)
  assert.deepEqual(settings.overridden, [])
  assert.equal(findEntry({}, null), null)
})

test('tokenInfo：沒有檔案就是沒有', () => {
  assert.deepEqual(tokenInfo(path.join(os.tmpdir(), 'no-such-token-file')), { present: false })
})

test('tokenInfo 會解析 JWT 的 exp', async (t) => {
  const dir = path.join(os.tmpdir(), 'umpod-token-' + process.pid)
  const file = path.join(dir, 'upstream-token')
  const payload = Buffer.from(JSON.stringify({ exp: 4102444800 }), 'utf8').toString('base64url')
  try {
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(file, `header.${payload}.sig`, 'utf8')
  } catch {
    t.skip('沙箱不允許寫入暫存目錄')
    return
  }
  try {
    const info = tokenInfo(file)
    assert.equal(info.present, true)
    assert.equal(info.expired, false)
    assert.equal(info.expiresAt, 4102444800 * 1000)
    assert.ok(info.remainingMs > 0)
  } finally {
    try {
      fs.rmSync(dir, { recursive: true, force: true })
    } catch {
      /* 清不掉就算了 */
    }
  }
})
