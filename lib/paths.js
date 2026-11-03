/**
 * 可移植的預設路徑。
 *
 * 這裡只做一件事：把「作者的機器長什麼樣」換成「從環境推導」。
 * 外掛本身不含代理，代理是 `um_opencode_setup.py`；它的家目錄規則由
 * scripts/*.py 定為 `UM_OPENCODE_HOME` 或 `~/.um-opencode`，本檔照抄同一條規則，
 * 所以 JS 與 Python 兩邊永遠指到同一個資料夾。
 *
 * 每一個值都還能被 cordis.patch.yml 的 config 或設定頁逐項覆寫；
 * 這裡算出來的只是「沒人指定時最可能對的那個」。
 *
 * @module dsh-um-proxy-ondemand/paths
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** 預設埠：與 README、scripts/*.py、llm-pi-ai 的 baseURL 一致。 */
export const DEFAULT_PORT = 8788

function envValue(env, key) {
  const value = env?.[key]
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : ''
}

/**
 * UM 工具的家目錄：`UM_OPENCODE_HOME` 優先，否則 `~/.um-opencode`。
 * 與 scripts/um-renew.py、scripts/um_hidden_window.py 的 `HOME` 同一條規則。
 */
export function homeDir(env = process.env) {
  return envValue(env, 'UM_OPENCODE_HOME') || path.join(os.homedir(), '.um-opencode')
}

/** 代理腳本：`<家目錄>/um_opencode_setup.py`。 */
export function scriptPath(home = homeDir()) {
  return path.join(home, 'um_opencode_setup.py')
}

/** 外掛自帶的代理腳本副本（`vendor/`），家目錄沒有時拿它頂著。 */
export function bundledScriptPath() {
  return path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'vendor', 'um_opencode_setup.py')
}

/**
 * 決定實際要跑的代理腳本。
 *
 * 家目錄那份優先 —— 使用者可能自己更新過、或 UM 發了新版；
 * 家目錄沒有才退回外掛自帶的 `vendor/` 副本，這樣新機器裝完就能直接跑。
 * 兩邊都沒有就回原路徑，讓 lib/proxy.js 的預檢給出可操作的錯誤。
 */
export function resolveScript(script = scriptPath()) {
  try {
    if (fs.statSync(script).isFile()) return script
  } catch {
    /* 家目錄沒有，往下找自帶副本 */
  }
  const bundled = bundledScriptPath()
  try {
    if (fs.statSync(bundled).isFile()) return bundled
  } catch {
    /* 自帶副本也沒有（例如只裝了 lib/），回原路徑 */
  }
  return script
}

/** 代理日誌：`<家目錄>/proxy.log`。 */
export function logPath(home = homeDir()) {
  return path.join(home, 'proxy.log')
}

/** 本外掛專屬狀態檔：`<家目錄>/ondemand-<port>.state.json`。 */
export function statePath(port = DEFAULT_PORT, home = homeDir()) {
  return path.join(home, `ondemand-${port}.state.json`)
}

/** 把 "Python312" / "Python3.12" 之類的目錄名換成分數，認不出來回 null。 */
function versionScore(name) {
  const match = /python[-_ ]?(\d+)(?:\.(\d+))?/i.exec(name)
  if (!match) return null
  return Number(match[1]) * 1000 + Number(match[2] ?? 0)
}

/** 列出目錄下的 Python3* 子目錄，版本高的排前面。 */
function pythonDirs(root) {
  let names = []
  try {
    names = fs.readdirSync(root)
  } catch {
    return []
  }
  return names
    .filter((name) => /^python[-_]?3/i.test(name))
    .map((name) => ({ name, score: versionScore(name) ?? 0 }))
    .sort((a, b) => b.score - a.score)
    .map((item) => path.join(root, item.name))
}

/** PATH 上找得到的執行檔（Windows 需要自己補 .exe）。 */
function whichSync(command, env = process.env) {
  const hasExtension = /\.(exe|cmd|bat|com)$/i.test(command)
  const extensions = hasExtension
    ? ['']
    : (envValue(env, 'PATHEXT') || '.EXE;.CMD;.BAT').split(';').filter(Boolean)
  const dirs = (envValue(env, 'PATH') || '').split(path.delimiter).filter(Boolean)
  for (const dir of dirs) {
    for (const ext of extensions) {
      const candidate = path.join(dir, command + ext.toLowerCase())
      try {
        if (fs.statSync(candidate).isFile()) return candidate
      } catch {
        /* 找不到就換下一個 */
      }
    }
  }
  return ''
}

/**
 * 挑一個 Python 解譯器。
 *
 * 順序：`UM_PYTHON` → 使用者範圍安裝（%LOCALAPPDATA%\Programs\Python）→
 * 全機安裝（C:\Python3*）→ PATH → 最後才退回字面值讓 PATH 去解析。
 *
 * 為什麼預設是 pythonw：代理由宿主背景啟動，用 python.exe 會閃一個主控台視窗。
 * 需要 stdout 的場合（測試、CLI 子命令）傳 `{ gui: false }` 拿 python.exe。
 *
 * @param {object} [env] 環境變數來源（預設 process.env；測試會注入假的）。
 * @param {object} [options] `gui`（是否要 pythonw）、`platform`（預設 process.platform）。
 */
export function detectPython(env = process.env, { gui = true, platform = process.platform } = {}) {
  const explicit = envValue(env, 'UM_PYTHON')
  if (explicit !== '') {
    return gui ? explicit : explicit.replace(/pythonw(\.exe)?$/i, (_m, ext) => 'python' + (ext || ''))
  }
  if (platform !== 'win32') return gui ? 'python3' : 'python3'

  const exe = gui ? 'pythonw.exe' : 'python.exe'
  const localAppData = envValue(env, 'LOCALAPPDATA')
  const roots = []
  if (localAppData) roots.push(path.join(localAppData, 'Programs', 'Python'))
  roots.push('C:\\')

  for (const root of roots) {
    for (const dir of pythonDirs(root)) {
      const candidate = path.join(dir, exe)
      try {
        if (fs.statSync(candidate).isFile()) return candidate
      } catch {
        /* 這個版本沒有這個執行檔，換下一個 */
      }
    }
  }
  const onPath = whichSync(exe, env)
  if (onPath !== '') return onPath
  // 真的都沒有：留給 spawn 用 PATH 解析，錯誤訊息會是 ENOENT，
  // 至少使用者還能在設定頁填一條絕對路徑。
  return exe
}

/** 有主控台的那個 Python（要讀 stdout 的場合用）。 */
export function detectPythonCli(env = process.env, options = {}) {
  return detectPython(env, { ...options, gui: false })
}

/**
 * 一次算好全部預設值。
 * `env` 只是為了測試方便注入，正式路徑永遠用 process.env。
 */
export function defaultsOf(env = process.env, port = DEFAULT_PORT) {
  const home = homeDir(env)
  return {
    python: detectPython(env),
    script: scriptPath(home),
    port,
    logFile: logPath(home),
    stateFile: statePath(port, home),
  }
}
