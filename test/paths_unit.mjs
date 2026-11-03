/**
 * 可移植性驗證：預設值必須從環境推導，不能是作者機器的路徑。
 *
 * 這支測試的用意不是「跑得過就好」，而是「換一台電腦裝起來會怎樣」：
 * 每一條都在注入一個假的環境（HOME / LOCALAPPDATA / UM_OPENCODE_HOME / UM_PYTHON），
 * 確認推導出來的路徑跟著那個環境走，而不是跟著寫死的字串。
 *
 * 用法： node test/paths_unit.mjs
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import assert from 'node:assert/strict'

import { bundledScriptPath, DEFAULT_PORT, defaultsOf, detectPython, detectPythonCli, homeDir, logPath, resolveScript, scriptPath, statePath } from '../lib/paths.js'

let failures = 0
const check = (label, fn) => {
  try {
    fn()
    console.log(`PASS  ${label}`)
  } catch (err) {
    failures += 1
    console.log(`FAIL  ${label}  — ${(err && err.message) || err}`)
  }
}

const fakeHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-paths-'))

// ── 家目錄 ────────────────────────────────────────────────────────────────
check('沒有 UM_OPENCODE_HOME 就用 ~/.um-opencode', () => {
  assert.equal(homeDir({}), path.join(os.homedir(), '.um-opencode'))
})
check('UM_OPENCODE_HOME 覆寫家目錄', () => {
  assert.equal(homeDir({ UM_OPENCODE_HOME: fakeHome }), fakeHome)
})
check('空白或只有空白的 UM_OPENCODE_HOME 視同沒設', () => {
  assert.equal(homeDir({ UM_OPENCODE_HOME: '   ' }), path.join(os.homedir(), '.um-opencode'))
})
check('家目錄推導出的四個路徑都在同一個資料夾', () => {
  const home = 'D:\\someone\\.um-opencode'
  assert.equal(scriptPath(home), path.join(home, 'um_opencode_setup.py'))
  assert.equal(logPath(home), path.join(home, 'proxy.log'))
  assert.equal(statePath(8788, home), path.join(home, 'ondemand-8788.state.json'))
})
check('狀態檔名跟著埠號走，兩個埠不會共用同一個 pid 檔', () => {
  const home = 'D:\\someone\\.um-opencode'
  assert.notEqual(statePath(8788, home), statePath(9000, home))
  assert.match(statePath(9000, home), /ondemand-9000\.state\.json$/)
})

// ── Python 偵測 ───────────────────────────────────────────────────────────
check('UM_PYTHON 有值就完全照用', () => {
  assert.equal(detectPython({ UM_PYTHON: 'D:\\tools\\python.exe' }), 'D:\\tools\\python.exe')
})
check('UM_PYTHON 指向 pythonw 時，CLI 版本會換成 python', () => {
  assert.equal(detectPythonCli({ UM_PYTHON: 'D:\\tools\\pythonw.exe' }), 'D:\\tools\\python.exe')
})
check('非 Windows 一律用 python3', () => {
  assert.equal(detectPython({}, { platform: 'linux' }), 'python3')
  assert.equal(detectPython({}, { platform: 'darwin' }), 'python3')
  assert.equal(detectPythonCli({}, { platform: 'linux' }), 'python3')
})
check('什麼都找不到時退回字面值，讓 spawn 用 PATH 解析', () => {
  // 空 PATH + 空的 LOCALAPPDATA，且把 `C:\` 那一檔掃描指到不存在的目錄。
  // 不關掉它，這條測試就只在「機器上沒有 C:\Python3*」時才成立。
  const result = detectPython(
    { PATH: '', LOCALAPPDATA: path.join(fakeHome, 'nope'), PATHEXT: '.EXE' },
    { systemRoot: path.join(fakeHome, 'nope') },
  )
  assert.ok(result === 'pythonw.exe' || /pythonw\.exe$/i.test(result), result)
})
check('PATH 上的 pythonw 會被找到', () => {
  // 造一個假的 PATH 目錄，裡面放一個空的 pythonw.exe。
  const bin = path.join(fakeHome, 'bin')
  fs.mkdirSync(bin, { recursive: true })
  const exe = path.join(bin, 'pythonw.exe')
  fs.writeFileSync(exe, '')
  const found = detectPython(
    { PATH: bin, LOCALAPPDATA: path.join(fakeHome, 'nope'), PATHEXT: '.EXE' },
    { systemRoot: path.join(fakeHome, 'nope') },
  )
  assert.equal(found.toLowerCase(), exe.toLowerCase())
})

// ── 全部預設值 ───────────────────────────────────────────────────────────
check('defaultsOf 的每個路徑都落在指定的家目錄底下', () => {
  const defaults = defaultsOf({ UM_OPENCODE_HOME: fakeHome, UM_PYTHON: 'D:\\tools\\pythonw.exe' }, DEFAULT_PORT)
  assert.equal(defaults.port, DEFAULT_PORT)
  assert.equal(defaults.python, 'D:\\tools\\pythonw.exe')
  assert.equal(defaults.script, path.join(fakeHome, 'um_opencode_setup.py'))
  assert.equal(defaults.logFile, path.join(fakeHome, 'proxy.log'))
  assert.equal(defaults.stateFile, path.join(fakeHome, 'ondemand-8788.state.json'))
})
// ── 代理腳本：家目錄優先，沒有才用自帶副本 ──────────────────────────────
check('resolveScript：家目錄有就用家目錄那份', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-paths-home-'))
  const local = path.join(home, 'um_opencode_setup.py')
  fs.writeFileSync(local, '# local copy\n')
  try {
    assert.equal(resolveScript(local), local)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
check('resolveScript：家目錄沒有就退回 vendor 副本', () => {
  const missing = path.join(os.tmpdir(), 'dsh-paths-missing', 'um_opencode_setup.py')
  assert.equal(resolveScript(missing), bundledScriptPath())
})
check('vendor 副本真的存在，而且就是代理腳本', () => {
  const bundled = bundledScriptPath()
  assert.ok(fs.statSync(bundled).isFile(), `找不到 ${bundled}`)
  const head = fs.readFileSync(bundled, 'utf8').slice(0, 200)
  assert.match(head, /um_opencode_setup|UM GenAI/i)
})
check('defaultsOf 不含作者機器的路徑', () => {
  // 把 `C:\` 掃描關掉，否則在作者自己的機器上偵測到的就是 C:\Python314\pythonw.exe
  // ——那是「推導出來的」，不是「寫死的」，但字串長得一樣，測不出差別。
  const text = JSON.stringify(
    defaultsOf({ UM_OPENCODE_HOME: 'D:\\someone\\.um-opencode' }, DEFAULT_PORT, {
      systemRoot: path.join(fakeHome, 'nope'),
    }),
  )
  for (const forbidden of ['TINGOD', 'Python314', 'C:\\Users\\TINGOD']) {
    assert.ok(!text.includes(forbidden), `預設值不該含 ${forbidden}：${text}`)
  }
})
check('lib/paths.js 的原始碼沒有寫死作者機器的路徑', () => {
  // 上一條驗的是「推導結果」，這一條驗的是「原始碼」：就算推導結果剛好一樣，
  // 原始碼裡也不該出現作者的使用者名稱或 Python 版本目錄。
  const source = fs.readFileSync(new URL('../lib/paths.js', import.meta.url), 'utf8')
  for (const forbidden of ['TINGOD', 'Python314', 'C:\\Users\\']) {
    assert.ok(!source.includes(forbidden), `lib/paths.js 不該出現 ${forbidden}`)
  }
})

fs.rmSync(fakeHome, { recursive: true, force: true })

console.log(failures === 0 ? '\n==== ALL PASS ====' : `\n${failures} FAILED`)
process.exit(failures === 0 ? 0 : 1)
