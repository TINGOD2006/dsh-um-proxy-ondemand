import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'

const run = promisify(execFile)
const options = { encoding: 'utf8', windowsHide: true, timeout: 15000, maxBuffer: 4 * 1024 * 1024 }

/** Parse Windows argv quoting, including escaped quotes and trailing backslashes. */
export function commandArgs(command) {
  const args = []
  let value = '', quoted = false, active = false
  for (let i = 0; i < command.length; i++) {
    const char = command[i]
    if (char === '\\') {
      let count = 1
      while (command[i + 1] === '\\') { count++; i++ }
      if (command[i + 1] === '"') {
        value += '\\'.repeat(Math.floor(count / 2))
        i++
        if (count % 2) value += '"'
        else quoted = !quoted
      } else value += '\\'.repeat(count)
      active = true
    } else if (char === '"') {
      quoted = !quoted
      active = true
    } else if (/\s/.test(char) && !quoted) {
      if (active) args.push(value)
      value = ''; active = false
    } else { value += char; active = true }
  }
  if (active) args.push(value)
  return args
}

const normalized = value => path.win32.resolve(value).toLowerCase()

export function matchesProxyProcess(info, cfg) {
  if (!info?.ExecutablePath || !info?.CommandLine) return false
  const expected = normalized(cfg.python)
  const executable = normalized(info.ExecutablePath)
  const pythonPair = /^pythonw?\.exe$/i.test(path.win32.basename(expected))
    && /^pythonw?\.exe$/i.test(path.win32.basename(executable))
    && path.win32.dirname(expected) === path.win32.dirname(executable)
  if (executable !== expected && !pythonPair) return false
  const args = commandArgs(info.CommandLine)
  let index = 1
  while (['-B', '-u', '-I', '-E', '-s'].includes(args[index])) index++
  if (!args[index] || normalized(args[index]) !== normalized(cfg.script) || args[index + 1] !== 'serve') return false
  const rest = args.slice(index + 2)
  const ports = []
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === '--port') ports.push(rest[++i])
    else if (rest[i].startsWith('--port=')) ports.push(rest[i].slice(7))
  }
  return ports.length === 1 && ports[0] === String(cfg.port)
}

export async function listenerPids(port) {
  const { stdout } = await run('netstat', ['-ano', '-p', 'tcp'], options)
  const pattern = new RegExp('^\\s*TCP\\s+(?:127\\.0\\.0\\.1|0\\.0\\.0\\.0|\\[::1\\]|\\[::\\]):' + port + '\\s+\\S+\\s+LISTENING\\s+(\\d+)', 'gmi')
  return [...new Set([...stdout.matchAll(pattern)].map(match => Number(match[1])))].filter(pid => pid > 0)
}

/** Query only numeric PIDs; never interpolate paths or arbitrary shell text. */
export async function proxyProcesses(pids, cfg) {
  const ids = [...new Set(pids)].filter(pid => Number.isInteger(pid) && pid > 0)
  if (!ids.length) return []
  const filter = ids.map(pid => `ProcessId=${pid}`).join(' OR ')
  const script = `[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process -Filter '${filter}' | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress`
  const { stdout } = await run('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], options)
  const data = stdout.trim() ? JSON.parse(stdout) : []
  return (Array.isArray(data) ? data : [data]).filter(info => matchesProxyProcess(info, cfg)).map(info => Number(info.ProcessId))
}

export async function terminateProxyProcesses(pids, cfg) {
  const owned = await proxyProcesses(pids, cfg)
  const failures = []
  for (const pid of owned) {
    try { await run('taskkill', ['/PID', String(pid), '/T', '/F'], options) }
    catch { failures.push(pid) }
  }
  return { owned, failures, skipped: pids.filter(pid => !owned.includes(pid)) }
}
