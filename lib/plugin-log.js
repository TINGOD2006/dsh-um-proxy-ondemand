import fs from 'node:fs'
import path from 'node:path'

/**
 * 插件自己的警告落地。
 *
 * 宿主的 logger 只寫 console，DSH 關掉之後什麼都不剩；而插件最需要事後追查的
 * 正是「喚醒失敗」「關機清理失敗」這種一次性事件。所以 warn() 除了寫 console，
 * 也追加到 `~/.um-opencode/plugin.log`，由看門狗（scripts/um_logs.py）一起輪替，
 * 設定頁的「日誌」分頁就看得到。
 *
 * 輪替刻意不在這裡做：um_logs.py 是唯一擁有輪替的地方，兩個行程各自輪替同一個
 * 檔案只會互相踩。
 */
export function pluginLogPath(home) {
  return path.join(home, 'plugin.log')
}

/** 本地時間 `YYYY-MM-DD HH:MM:SS`，跟 renew-watchdog.log 的格式一致，方便對時。 */
function stamp(date) {
  const pad = value => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/**
 * 追加一行警告。任何失敗都吞掉——記錄失敗不能影響請求。
 *
 * @param {string} home - `~/.um-opencode`
 * @param {string} level - `warn` / `error`
 * @param {string} message - 已格式化的訊息
 */
export function pluginLog(home, level, message) {
  if (!home) return
  try {
    const file = pluginLogPath(home)
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.appendFileSync(file, `[${stamp(new Date())}] ${level} ${message}\n`, 'utf8')
  } catch {
    /* 記錄失敗不能影響請求 */
  }
}
